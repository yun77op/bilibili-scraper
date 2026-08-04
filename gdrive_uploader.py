"""
Google Drive upload module for bilibili-scraper.
Adapted from the google-drive project's gdrive.py for web server integration.

Provides OAuth authentication (web flow via callback) and file upload to Google Drive.
Shares credential files (~/.gdrive-credentials.json, ~/.gdrive-token.json) with the
standalone google-drive CLI tool — one authorization works for both.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------

_MISSING_DEPS: list[str] = []
try:
    from google.auth.transport.requests import Request  # noqa: F401
    from google.oauth2.credentials import Credentials    # noqa: F401
    from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: F401
except ImportError:
    _MISSING_DEPS.append("google-auth-oauthlib")

try:
    from googleapiclient.discovery import build          # noqa: F401
    from googleapiclient.http import MediaFileUpload      # noqa: F401
    from googleapiclient.errors import HttpError          # noqa: F401
except ImportError:
    _MISSING_DEPS.append("google-api-python-client")


def check_dependencies() -> list[str]:
    """Return a list of missing Google Drive dependency package names."""
    return list(_MISSING_DEPS)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCOPES = ["https://www.googleapis.com/auth/drive"]
DEFAULT_CREDENTIALS_PATH = os.path.expanduser("~/.gdrive-credentials.json")
DEFAULT_TOKEN_PATH = os.path.expanduser("~/.gdrive-token.json")

# In-memory store for pending OAuth flows, keyed by state token.
# (single-user local app, so a dict is fine)
_pending_flows: dict[str, InstalledAppFlow] = {}


# ---------------------------------------------------------------------------
# Proxy support
# ---------------------------------------------------------------------------

# httplib2 has broken proxy support with certain proxy configurations
# (e.g. Clash/V2Ray).  Google API client auto-detects google_auth_httplib2
# and prefers it over requests.  This adapter wraps a requests.Session to
# expose the httplib2.Http-compatible interface that googleapiclient expects,
# so we get requests' robust proxy support without changing the upload logic.


class _RequestsHttpAdapter:
    """An httplib2.Http-compatible adapter backed by a requests.Session.

    Implements the minimal interface that ``googleapiclient.discovery.build``
    needs: ``request(uri, method, body, headers) -> (response, content)``.
    """

    def __init__(self, session: Any | None = None) -> None:
        try:
            import requests as _requests
        except ImportError:
            raise ImportError("requests is required for Google Drive operations")
        self._session = session or _requests.Session()

    def request(self, uri: str, method: str = "GET", body: Any = None,
                headers: Any = None, **kwargs: Any) -> tuple[Any, bytes]:
        import requests as _requests
        try:
            resp = self._session.request(
                method=method,
                url=uri,
                data=body,
                headers=dict(headers) if headers else None,
                timeout=kwargs.get("timeout", 300),
            )
        except _requests.Timeout:
            raise TimeoutError("timed out")
        except _requests.ConnectionError as exc:
            raise ConnectionError(str(exc))

        # Build an httplib2.Response-compatible object
        class _FakeResponse:
            def __init__(self, r: _requests.Response) -> None:
                self.status = r.status_code
                self.reason = r.reason
                self._headers = r.headers

            def __getitem__(self, key: str) -> str:
                return self._headers[key]

            def get(self, key: str, default: Any = None) -> Any:
                return self._headers.get(key, default)

            def __contains__(self, key: str) -> bool:
                return key in self._headers

        return _FakeResponse(resp), resp.content


def _build_http() -> Any:
    """Build an HTTP client backed by requests (not httplib2)."""
    return _RequestsHttpAdapter()


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _credential_paths() -> tuple[str, str]:
    creds_path = os.environ.get("GDRIVE_CREDENTIALS_PATH", DEFAULT_CREDENTIALS_PATH)
    token_path = os.environ.get("GDRIVE_TOKEN_PATH", DEFAULT_TOKEN_PATH)
    return creds_path, token_path


def get_credentials() -> Credentials | None:
    """Get valid user credentials from storage, refreshing if needed.

    Returns a valid Credentials object, or None if (re-)authentication is required.
    """
    _, token_path = _credential_paths()

    creds: Credentials | None = None
    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        except Exception:
            pass  # Token corrupted

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            with open(token_path, "w") as f:
                f.write(creds.to_json())
            return creds
        except Exception:
            return None

    return None


def get_service() -> tuple[Any | None, dict[str, Any] | None]:
    """Return (service, error) — exactly one is non-None.

    On success: (Drive service object, None)
    On failure: (None, {"error": "...", "message": "..."})
    """
    if _MISSING_DEPS:
        return None, {
            "error": "missing_dependencies",
            "message": f"缺少 Google Drive 依赖：{', '.join(_MISSING_DEPS)}。请执行 pip install {' '.join(_MISSING_DEPS)}",
        }

    creds = get_credentials()
    creds_path, token_path = _credential_paths()

    if not creds:
        if not os.path.exists(creds_path):
            return None, {
                "error": "not_authenticated",
                "message": "未授权且未找到 OAuth 凭据文件。",
                "credentials_missing": True,
                "credentials_path": creds_path,
            }
        return None, {
            "error": "not_authenticated",
            "message": "未授权或 token 已过期，请重新授权。",
            "credentials_missing": False,
        }

    try:
        # Use our requests-backed adapter instead of httplib2, because
        # httplib2 is broken with some proxy configurations.  We must wrap
        # it with google_auth_httplib2.AuthorizedHttp for OAuth, which also
        # delegates to our adapter's request() method.
        from google_auth_httplib2 import AuthorizedHttp
        http = _build_http()
        authed_http = AuthorizedHttp(creds, http=http)
        service = build("drive", "v3", http=authed_http)
        return service, None
    except Exception as exc:
        return None, {
            "error": "service_error",
            "message": f"构建 Drive 服务失败：{exc}",
        }


def is_authenticated() -> bool:
    """Return True if a valid Google Drive token exists."""
    return get_credentials() is not None


def get_auth_url(redirect_uri: str = "http://localhost:8000/api/gdrive/callback") -> tuple[str, str]:
    """Generate Google OAuth authorization URL.

    Returns (auth_url, state_token).  The state_token must be passed to exchange_code() later.
    """
    creds_path, _ = _credential_paths()
    if not os.path.exists(creds_path):
        raise FileNotFoundError(
            f"OAuth 凭据文件不存在：{creds_path}\n"
            "请先从 Google Cloud Console 下载 OAuth 2.0 客户端 ID JSON，保存到该路径。"
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        creds_path, SCOPES,
        redirect_uri=redirect_uri,
    )
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",  # Always get a refresh token
        include_granted_scopes="true",
    )

    _pending_flows[state] = flow
    return auth_url, state


def exchange_code(code: str, state: str) -> tuple[bool, str]:
    """Exchange authorization code for credentials and save token.

    Returns (success, message).
    """
    flow = _pending_flows.pop(state, None)
    if flow is None:
        return False, "无效的 state 参数，可能已过期或重复使用。请重新发起授权。"

    _, token_path = _credential_paths()
    try:
        flow.fetch_token(code=code)
        creds = flow.credentials
        os.makedirs(os.path.dirname(token_path), exist_ok=True)
        with open(token_path, "w") as f:
            f.write(creds.to_json())
        return True, "授权成功！Google Drive 已连接。"
    except Exception as exc:
        return False, f"Token 交换失败：{exc}"


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def find_or_create_folder(name: str, parent_id: str = "") -> tuple[str | None, dict[str, Any] | None]:
    """Find an existing folder by name under parent, or create one.

    Args:
        name: Folder name to find or create.
        parent_id: Parent folder ID (empty = root).

    Returns:
        (folder_id, error).  folder_id is the Drive folder ID on success.
        error is a dict with "error" and "message" keys on failure.
    """
    service, err = get_service()
    if err:
        return None, err

    # Search for existing folder with this name under the parent
    query_parts = [
        f"name = '{name.replace(chr(39), chr(92)+chr(39))}'",
        "mimeType = 'application/vnd.google-apps.folder'",
        "trashed = false",
    ]
    if parent_id:
        query_parts.append(f"'{parent_id}' in parents")
    query = " and ".join(query_parts)

    try:
        results = service.files().list(
            q=query,
            fields="files(id, name)",
            pageSize=1,
        ).execute()
        files = results.get("files", [])
        if files:
            return files[0]["id"], None

        # Not found, create it
        folder_metadata: dict[str, Any] = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        if parent_id:
            folder_metadata["parents"] = [parent_id]

        folder = service.files().create(
            body=folder_metadata,
            fields="id",
        ).execute()
        return folder.get("id"), None

    except HttpError as exc:
        return None, {"error": "api_error", "message": str(exc)}


def upload_file(local_path: str, parent_folder_id: str = "", file_name: str = "",
                mime_type: str = "") -> dict[str, Any]:
    """Upload a single file to Google Drive.

    Args:
        local_path: Absolute path to the local file.
        parent_folder_id: Google Drive folder ID (empty = root).
        file_name: Custom name on Drive (default: basename of local_path).
        mime_type: MIME type (default: auto-detected).

    Returns:
        {"status": "success", "data": {...}} or {"status": "error", ...}
    """
    if not os.path.exists(local_path):
        return {"status": "error", "error": "file_not_found",
                "message": f"本地文件不存在：{local_path}"}

    service, err = get_service()
    if err:
        return {"status": "error", **err}

    name = file_name or os.path.basename(local_path)
    mime = mime_type or "application/octet-stream"

    file_metadata: dict[str, Any] = {"name": name}
    if parent_folder_id:
        file_metadata["parents"] = [parent_folder_id]

    try:
        media = MediaFileUpload(local_path, mimetype=mime, resumable=True)
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id, name, mimeType, size, webViewLink, createdTime",
        ).execute()

        return {
            "status": "success",
            "data": {
                "id": file.get("id"),
                "name": file.get("name"),
                "mimeType": file.get("mimeType"),
                "size": file.get("size"),
                "webViewLink": file.get("webViewLink"),
                "createdTime": file.get("createdTime"),
            },
        }
    except HttpError as exc:
        return {"status": "error", "error": "api_error", "message": str(exc)}


def _resolve_folder_id(folder_id: str) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve a folder ID or name to a Drive folder ID.

    If folder_id looks like a Drive ID (alphanumeric, 20+ chars), use it directly.
    Otherwise, treat it as a folder name and search/create it under root.

    Returns (folder_id, error).
    """
    if not folder_id:
        return "", None
    # Drive IDs are long alphanumeric strings
    if re.fullmatch(r"[a-zA-Z0-9_-]{20,}", folder_id):
        return folder_id, None
    # Treat as folder name
    return find_or_create_folder(folder_id)


def upload_article_files(md_path: str, pdf_path: str,
                         folder_id: str = "",
                         date_subdir: bool = False) -> list[dict[str, Any]]:
    """Upload article PDF to Google Drive (MD skipped by default).

    Args:
        md_path: Path to the markdown article (currently not uploaded).
        pdf_path: Path to the PDF article.
        folder_id: Google Drive folder ID or name (empty = root).
                   If it looks like a name (non-ID), it will be searched/created.
        date_subdir: If True, create/find a YYYYMMDD subfolder under folder_id.

    Returns a list of result dicts, one per uploaded file.
    """
    target_folder, err = _resolve_folder_id(folder_id)
    if err:
        return [{"status": "error", **err}]
    if date_subdir:
        import time as _time
        date_name = _time.strftime("%Y%m%d")
        found_id, err = find_or_create_folder(date_name, target_folder or "")
        if err:
            return [{"status": "error", **err}]
        target_folder = found_id or folder_id

    results: list[dict[str, Any]] = []
    # Only upload PDF to Google Drive
    for path in (pdf_path,):
        if os.path.exists(path):
            results.append(upload_file(path, parent_folder_id=target_folder,
                                       mime_type="application/pdf"))
        else:
            results.append({
                "status": "error", "error": "file_not_found",
                "message": f"文件不存在，跳过上传：{path}",
            })
    return results

"""
Google Drive upload module for bilibili-scraper.
Adapted from the google-drive project's gdrive.py for web server integration.

Provides OAuth authentication (web flow via callback) and file upload to Google Drive.

Token storage: per-user OAuth tokens are stored in the shared database
(``jobs.db``, table ``gdrive_tokens``), so the server and worker processes see
the same authorizations.  Legacy token files (``.gdrive-tokens/{user_id}.json``
in the project dir or ``~/.gdrive-tokens/{user_id}.json``) are read once and
migrated into the DB; the legacy single-user ``~/.gdrive-token.json`` path is
kept for the standalone google-drive CLI tool.

The OAuth *client* credentials file (``.gdrive-credentials.json`` — project
dir first, then ``~/.gdrive-credentials.json``) is still required: it
identifies the app to Google and is shared by all users.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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

ROOT = Path(__file__).resolve().parent

SCOPES = ["https://www.googleapis.com/auth/drive"]
DEFAULT_CREDENTIALS_PATH = str(ROOT / ".gdrive-credentials.json")
DEFAULT_TOKEN_PATH = str(ROOT / ".gdrive-token.json")
TOKENS_DIR = str(ROOT / ".gdrive-tokens")

# In-memory store for pending OAuth flows, keyed by state token.
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

def _resolve_credentials_path() -> str:
    """Resolve the OAuth client credentials file path.

    Priority: ``GDRIVE_CREDENTIALS_PATH`` env var → project-local
    ``.gdrive-credentials.json`` → legacy ``~/.gdrive-credentials.json``.
    If none exists, the project-local path is returned as the recommended
    location (used by setup guidance / error messages).
    """
    env = os.environ.get("GDRIVE_CREDENTIALS_PATH")
    if env:
        return env
    project_path = ROOT / ".gdrive-credentials.json"
    home_path = Path.home() / ".gdrive-credentials.json"
    if project_path.exists():
        return str(project_path)
    if home_path.exists():
        return str(home_path)
    return str(project_path)


def _resolve_token_path(user_id: str | None) -> str:
    """Resolve the token path for a user.

    Per-user tokens live in a tokens directory; the legacy single-user token
    is a single file.  Priority: env override → project-local → legacy home
    location.  Reads pick up pre-existing legacy tokens; writes default to the
    project-local location.
    """
    if user_id:
        env_dir = os.environ.get("GDRIVE_TOKENS_DIR")
        if env_dir:
            return os.path.join(env_dir, f"{user_id}.json")
        project = ROOT / ".gdrive-tokens" / f"{user_id}.json"
        home = Path.home() / ".gdrive-tokens" / f"{user_id}.json"
        if project.exists():
            return str(project)
        if home.exists():
            return str(home)
        return str(project)
    env_file = os.environ.get("GDRIVE_TOKEN_PATH")
    if env_file:
        return env_file
    project = ROOT / ".gdrive-token.json"
    home = Path.home() / ".gdrive-token.json"
    if project.exists():
        return str(project)
    if home.exists():
        return str(home)
    return str(project)


def _credential_paths(user_id: str | None = None) -> tuple[str, str]:
    creds_path = _resolve_credentials_path()
    token_path = _resolve_token_path(user_id)
    return creds_path, token_path


def _read_token(user_id: str | None) -> tuple[str | None, str | None]:
    """Read a stored token.  Returns ``(token_json, legacy_path)``.

    Per-user tokens come from the database first; a legacy token file is
    honored as a one-time migration source.  For ``user_id=None`` (legacy
    single-user mode) the token file is used directly.
    """
    if user_id:
        try:
            import db as _db
            token_json = _db.get_gdrive_token(user_id)
        except Exception:
            token_json = None
        if token_json is not None:
            return token_json, None
        legacy_path = _resolve_token_path(user_id)
        if os.path.exists(legacy_path):
            try:
                return Path(legacy_path).read_text(encoding="utf-8"), legacy_path
            except OSError:
                return None, None
        return None, None
    legacy_path = _resolve_token_path(None)
    if os.path.exists(legacy_path):
        try:
            return Path(legacy_path).read_text(encoding="utf-8"), legacy_path
        except OSError:
            return None, None
    return None, None


def _store_token(user_id: str | None, token_json: str,
                 remove_legacy: str | None = None) -> bool:
    """Persist a token and clean up legacy storage.

    Per-user tokens are written to the database; when ``remove_legacy`` is a
    path, the stale token file there is deleted so the DB stays the single
    source of truth.  Returns True on success.
    """
    if user_id:
        try:
            import db as _db
            _db.save_gdrive_token(user_id, token_json)
        except Exception:
            return False
        if remove_legacy:
            try:
                os.remove(remove_legacy)
            except OSError:
                pass
        return True
    # Legacy single-user mode: token file only.
    try:
        path = _resolve_token_path(None)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_text(token_json, encoding="utf-8")
        return True
    except OSError:
        return False


def get_credentials(user_id: str | None = None) -> Credentials | None:
    """Get valid user credentials from storage, refreshing if needed.

    Args:
        user_id: User account id — the token is stored per-user in the
                 database (``gdrive_tokens`` table).  A legacy token file
                 under the tokens directory is migrated into the DB on first
                 use.  When None the legacy single-user token file is used.

    Returns a valid Credentials object, or None if (re-)authentication is required.
    """
    token_json, legacy_path = _read_token(user_id)

    creds: Credentials | None = None
    if token_json:
        try:
            creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
        except Exception:
            pass  # Token corrupted

    if creds and creds.valid:
        if user_id and legacy_path:
            # One-time migration: move a valid legacy file token into the DB.
            _store_token(user_id, creds.to_json(), remove_legacy=legacy_path)
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            if user_id:
                _store_token(user_id, creds.to_json(), remove_legacy=legacy_path)
            else:
                _store_token(None, creds.to_json())
            return creds
        except Exception:
            return None

    return None


def get_service(user_id: str | None = None) -> tuple[Any | None, dict[str, Any] | None]:
    """Return (service, error) — exactly one is non-None.

    On success: (Drive service object, None)
    On failure: (None, {"error": "...", "message": "..."})
    """
    if _MISSING_DEPS:
        return None, {
            "error": "missing_dependencies",
            "message": f"缺少 Google Drive 依赖：{', '.join(_MISSING_DEPS)}。请执行 pip install {' '.join(_MISSING_DEPS)}",
        }

    creds = get_credentials(user_id)
    creds_path, token_path = _credential_paths(user_id)

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


def is_authenticated(user_id: str | None = None) -> bool:
    """Return True if a valid Google Drive token exists for the given user."""
    return get_credentials(user_id) is not None


def _validate_client_secrets(creds_path: str, redirect_uri: str) -> None:
    """Detect the OAuth client type and fail fast on misconfiguration.

    Google Cloud Console downloads two kinds of client JSON:

    - ``installed`` (桌面应用): only loopback redirect URIs are allowed.
    - ``web`` (Web 应用): the redirect URI must be registered in the console.

    Raises ValueError with actionable Chinese guidance on any mismatch.
    """
    try:
        with open(creds_path, encoding="utf-8") as f:
            secrets_json = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"凭据文件无法解析：{creds_path}（{exc}）") from exc

    if "installed" in secrets_json:
        client_type = "installed"
    elif "web" in secrets_json:
        client_type = "web"
    else:
        raise ValueError(
            "凭据文件格式不正确：缺少 'installed' 或 'web' 段。\n"
            "请重新从 Google Cloud Console 下载 OAuth 2.0 客户端 ID JSON。"
        )

    if client_type == "installed":
        host = urlparse(redirect_uri).hostname or ""
        if host not in ("localhost", "127.0.0.1"):
            raise ValueError(
                "当前凭据是『桌面应用』类型，重定向地址必须是本机回环地址\n"
                f"（http://localhost 或 http://127.0.0.1），当前为：{redirect_uri}\n"
                "如果用户需要通过局域网 IP 或域名访问本服务，请在 Google Cloud Console\n"
                "创建一个『Web 应用』类型的 OAuth 客户端，并在『授权重定向 URI』中添加：\n"
                + redirect_uri
            )
    else:
        registered = (secrets_json.get("web", {}) or {}).get("redirect_uris") or []
        if registered and redirect_uri not in registered:
            raise ValueError(
                f"重定向地址未在 Google Cloud Console 中注册：{redirect_uri}\n"
                "请在 Google Cloud Console → 凭据 → OAuth 客户端 ID（Web 应用）→\n"
                "『授权重定向 URI』中添加该地址后重试。"
            )


def get_auth_url(redirect_uri: str = "http://localhost:8085/api/gdrive/callback") -> tuple[str, str]:
    """Generate Google OAuth authorization URL.

    Returns (auth_url, state_token).  The state_token must be passed to exchange_code() later.
    """
    creds_path, _ = _credential_paths(None)
    if not os.path.exists(creds_path):
        raise FileNotFoundError(
            f"OAuth 凭据文件不存在：{creds_path}\n"
            "请先从 Google Cloud Console 下载 OAuth 2.0 客户端 ID JSON，保存到该路径。"
        )

    _validate_client_secrets(creds_path, redirect_uri)

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


def exchange_code(code: str, state: str, user_id: str | None = None) -> tuple[bool, str]:
    """Exchange authorization code for credentials and save token.

    Args:
        code: OAuth authorization code from the redirect query string.
        state: State token returned by get_auth_url().
        user_id: User account id — the token is stored per-user in the
                 database (``gdrive_tokens`` table); a stale legacy token
                 file is removed afterwards.

    Returns (success, message).
    """
    flow = _pending_flows.pop(state, None)
    if flow is None:
        return False, "无效的 state 参数，可能已过期或重复使用。请重新发起授权。"

    _, legacy_path = _credential_paths(user_id)
    try:
        flow.fetch_token(code=code)
        creds = flow.credentials
        if user_id:
            if not _store_token(user_id, creds.to_json(), remove_legacy=legacy_path):
                return False, "授权成功，但 token 写入数据库失败，请检查 jobs.db 权限后重新授权。"
        else:
            _store_token(None, creds.to_json())
        return True, "授权成功！Google Drive 已连接。"
    except Exception as exc:
        return False, f"Token 交换失败：{exc}"


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def find_or_create_folder(name: str, parent_id: str = "",
                          user_id: str | None = None) -> tuple[str | None, dict[str, Any] | None]:
    """Find an existing folder by name under parent, or create one.

    Args:
        name: Folder name to find or create.
        parent_id: Parent folder ID (empty = root).
        user_id: User account id for per-user Drive credentials.

    Returns:
        (folder_id, error).  folder_id is the Drive folder ID on success.
        error is a dict with "error" and "message" keys on failure.
    """
    service, err = get_service(user_id)
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
                mime_type: str = "", user_id: str | None = None) -> dict[str, Any]:
    """Upload a single file to Google Drive.

    Args:
        local_path: Absolute path to the local file.
        parent_folder_id: Google Drive folder ID (empty = root).
        file_name: Custom name on Drive (default: basename of local_path).
        mime_type: MIME type (default: auto-detected).
        user_id: User account id for per-user Drive credentials.

    Returns:
        {"status": "success", "data": {...}} or {"status": "error", ...}
    """
    if not os.path.exists(local_path):
        return {"status": "error", "error": "file_not_found",
                "message": f"本地文件不存在：{local_path}"}

    service, err = get_service(user_id)
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


def _resolve_folder_id(folder_id: str, user_id: str | None = None) -> tuple[str | None, dict[str, Any] | None]:
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
    return find_or_create_folder(folder_id, user_id=user_id)


def upload_article_files(md_path: str, pdf_path: str,
                         folder_id: str = "",
                         date_subdir: bool = False,
                         user_id: str | None = None) -> list[dict[str, Any]]:
    """Upload article PDF to Google Drive (MD skipped by default).

    Args:
        md_path: Path to the markdown article (currently not uploaded).
        pdf_path: Path to the PDF article.
        folder_id: Google Drive folder ID or name (empty = root).
                   If it looks like a name (non-ID), it will be searched/created.
        date_subdir: If True, create/find a YYYYMMDD subfolder under folder_id.
        user_id: User account id for per-user Drive credentials.

    Returns a list of result dicts, one per uploaded file.
    """
    target_folder, err = _resolve_folder_id(folder_id, user_id)
    if err:
        return [{"status": "error", **err}]
    if date_subdir:
        import time as _time
        date_name = _time.strftime("%Y%m%d")
        found_id, err = find_or_create_folder(date_name, target_folder or "", user_id=user_id)
        if err:
            return [{"status": "error", **err}]
        target_folder = found_id or folder_id

    results: list[dict[str, Any]] = []
    # Only upload PDF to Google Drive
    for path in (pdf_path,):
        if os.path.exists(path):
            results.append(upload_file(path, parent_folder_id=target_folder,
                                       mime_type="application/pdf", user_id=user_id))
        else:
            results.append({
                "status": "error", "error": "file_not_found",
                "message": f"文件不存在，跳过上传：{path}",
            })
    return results

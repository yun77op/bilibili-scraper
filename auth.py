"""User authentication helpers for the Flask web app.

Provides password hashing, Flask-session based login state, Google OAuth
login, login throttling (5 failed attempts → 15 min lockout) and the
``login_required`` / ``admin_required`` decorators.

The SECRET_KEY used to sign session cookies lives in ``.env.local``
(``FLASK_SECRET_KEY``); if absent it is auto-generated once and persisted so
sessions survive restarts.
"""

from __future__ import annotations

import functools
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from flask import redirect, request, session, url_for
from werkzeug.security import check_password_hash as _check_hash
from werkzeug.security import generate_password_hash as _generate_hash

import db as _db
from app import ENV_FILE

try:
    from google_auth_oauthlib.flow import Flow as _GoogleFlow
except ImportError:
    _GoogleFlow = None  # type: ignore[assignment,misc]

GOOGLE_LOGIN_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

# Session / throttling constants
MAX_FAILED_ATTEMPTS = 5
LOCK_SECONDS = 15 * 60
MIN_PASSWORD_LEN = 6


# ---------------------------------------------------------------------------
# Secret key management
# ---------------------------------------------------------------------------

def ensure_secret_key() -> str:
    """Return the Flask SECRET_KEY.  Requires ``.env.local`` to exist."""
    if not ENV_FILE.exists():
        print(
            f"ERROR: 缺少配置文件 {ENV_FILE}",
            file=sys.stderr,
        )
        print(
            f"      请先创建: cp {ENV_FILE.name}.example {ENV_FILE.name}，并填写配置后重新启动",
            file=sys.stderr,
        )
        sys.exit(1)
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip().startswith("FLASK_SECRET_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    key = secrets.token_hex(32)
    with ENV_FILE.open("a", encoding="utf-8") as f:
        f.write(f"\n# 自动生成，用于签名登录会话（勿泄露）\nFLASK_SECRET_KEY={key}\n")
    return key


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    # Werkzeug 默认的 scrypt 需要 hashlib.scrypt（LibreSSL 构建的 Python 没有），
    # 统一用始终可用的 pbkdf2:sha256
    return _generate_hash(password, method="pbkdf2:sha256")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _check_hash(password_hash, password)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def current_user() -> dict[str, Any] | None:
    """Return the logged-in user dict, or None.  Clears invalid sessions."""
    user_id = session.get("user_id")
    if not user_id:
        return None
    user = _db.get_user(user_id)
    if user is None or not user["is_active"]:
        session.clear()
        return None
    return user


def login_user(user: dict[str, Any]) -> None:
    session.clear()
    session["user_id"] = user["id"]
    session.permanent = True
    _db.update_user(user["id"], last_login_at=time.time())
    _db.reset_login_attempts(user["id"])


def logout_user() -> None:
    session.clear()


def login(username: str, password: str) -> tuple[bool, str]:
    """Attempt a login with throttling.  Returns (ok, message)."""
    user = _db.get_user_by_username(username.strip())
    if user is None:
        # Fake a hash check so the timing doesn't reveal whether a user exists
        verify_password(password, hash_password("invalid"))
        return False, "用户名或密码错误"

    now = time.time()
    if user["locked_until"] and now < user["locked_until"]:
        remain = int((user["locked_until"] - now) / 60) + 1
        return False, f"登录失败次数过多，请 {remain} 分钟后再试"

    if not user["is_active"]:
        return False, "账号已被禁用，请联系管理员"

    if not user["password_hash"]:
        return False, "该账号请使用 Google 登录"

    if not verify_password(password, user["password_hash"]):
        attempts = user["failed_attempts"] + 1
        if attempts >= MAX_FAILED_ATTEMPTS:
            _db.set_user_login_attempt(user["id"], attempts, now + LOCK_SECONDS)
            return False, f"登录失败次数过多，账号已锁定 15 分钟"
        _db.set_user_login_attempt(user["id"], attempts, 0)
        return False, f"用户名或密码错误（还可尝试 {MAX_FAILED_ATTEMPTS - attempts} 次）"

    login_user(user)
    return True, "登录成功"


def register(username: str, password: str) -> tuple[bool, str, dict[str, Any] | None]:
    """Create a new account.  First registered user becomes the admin.
    Returns (ok, message, user)."""
    username = username.strip()
    if not username:
        return False, "用户名不能为空", None
    if len(username) > 32:
        return False, "用户名过长（最多 32 个字符）", None
    if len(password) < MIN_PASSWORD_LEN:
        return False, f"密码至少需要 {MIN_PASSWORD_LEN} 个字符", None

    user = _db.create_user(
        user_id=secrets.token_hex(12),
        username=username,
        password_hash=hash_password(password),
    )
    if user is None:
        return False, "用户名已被占用", None
    if _db.count_users() == 1:
        _db.update_user(user["id"], is_admin=True)
        user = _db.get_user(user["id"])
    return True, "注册成功", user


# ---------------------------------------------------------------------------
# Google 登录
# ---------------------------------------------------------------------------

def _google_client_secrets_path() -> Path:
    env = os.environ.get("GOOGLE_CREDENTIALS_PATH") or os.environ.get("GDRIVE_CREDENTIALS_PATH")
    if env:
        return Path(env)
    root = Path(__file__).resolve().parent
    project = root / ".gdrive-credentials.json"
    home = Path.home() / ".gdrive-credentials.json"
    if project.exists():
        return project
    if home.exists():
        return home
    return project


def google_client_config() -> dict[str, Any] | None:
    """Return OAuth client JSON, or None if Google login is not configured."""
    cid = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    if cid and secret:
        return {
            "web": {
                "client_id": cid,
                "client_secret": secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        }
    path = _google_client_secrets_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if "web" in data or "installed" in data:
        return data
    return None


def google_login_enabled() -> bool:
    return _GoogleFlow is not None and google_client_config() is not None


def _allow_insecure_oauth_http(redirect_uri: str) -> None:
    if redirect_uri.startswith("http://"):
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


def _validate_google_login_redirect(config: dict[str, Any], redirect_uri: str) -> None:
    """Reject desktop-app clients when the callback is not on localhost."""
    if "installed" in config and "web" not in config:
        host = urlparse(redirect_uri).hostname or ""
        if host not in ("localhost", "127.0.0.1"):
            raise ValueError(
                "当前凭据是『桌面应用』类型，Google 登录的重定向地址必须是本机回环地址。\n"
                "若通过局域网 IP 或域名访问，请改用『Web 应用』OAuth 客户端，并在\n"
                "『授权重定向 URI』中添加：\n"
                + redirect_uri
            )


def build_google_login_flow(redirect_uri: str) -> Any:
    if _GoogleFlow is None:
        raise RuntimeError("缺少 google-auth-oauthlib，无法使用 Google 登录")
    config = google_client_config()
    if config is None:
        raise FileNotFoundError(
            "未配置 Google 登录。请设置 GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET，"
            "或放置 OAuth 客户端 JSON（.gdrive-credentials.json）。"
        )
    _validate_google_login_redirect(config, redirect_uri)
    _allow_insecure_oauth_http(redirect_uri)
    return _GoogleFlow.from_client_config(
        config, scopes=GOOGLE_LOGIN_SCOPES, redirect_uri=redirect_uri,
    )


def google_login_url(redirect_uri: str) -> tuple[str, str, str]:
    """Return (authorization_url, state, code_verifier).

    The verifier must be sent back on the token request (PKCE).
    """
    flow = build_google_login_flow(redirect_uri)
    auth_url, state = flow.authorization_url(
        access_type="online",
        prompt="select_account",
    )
    return auth_url, state, flow.code_verifier or ""


def _client_id_from_config(config: dict[str, Any]) -> str:
    block = config.get("web") or config.get("installed") or {}
    return str(block.get("client_id") or "")


def _google_profile_from_credentials(creds: Any, config: dict[str, Any]) -> dict[str, str]:
    """Verify the ID token (fallback: userinfo) and return sub / email / name."""
    client_id = _client_id_from_config(config)
    id_token_jwt = getattr(creds, "id_token", None)
    if id_token_jwt:
        try:
            from google.auth.transport.requests import Request as GoogleRequest
            from google.oauth2 import id_token

            info = id_token.verify_oauth2_token(
                id_token_jwt, GoogleRequest(), audience=client_id,
            )
            iss = info.get("iss")
            if iss not in ("accounts.google.com", "https://accounts.google.com"):
                raise ValueError(f"unexpected iss: {iss}")
            return {
                "sub": str(info.get("sub") or ""),
                "email": str(info.get("email") or ""),
                "email_verified": "1" if info.get("email_verified") else "",
                "name": str(info.get("name") or info.get("given_name") or ""),
            }
        except Exception:
            pass

    import requests as _requests

    resp = _requests.get(
        "https://openidconnect.googleapis.com/v1/userinfo",
        headers={"Authorization": f"Bearer {creds.token}"},
        timeout=20,
    )
    resp.raise_for_status()
    info = resp.json()
    return {
        "sub": str(info.get("sub") or ""),
        "email": str(info.get("email") or ""),
        "email_verified": "1" if info.get("email_verified") else "",
        "name": str(info.get("name") or info.get("given_name") or ""),
    }


def complete_google_oauth(
    code: str, redirect_uri: str, code_verifier: str = "",
) -> dict[str, str]:
    """Exchange the authorization code for a Google profile dict.

    Raises ValueError on OAuth / profile failures.
    """
    flow = build_google_login_flow(redirect_uri)
    flow.code_verifier = code_verifier or None
    flow.autogenerate_code_verifier = False
    try:
        flow.fetch_token(code=code)
    except Exception as exc:
        raise ValueError(f"Google 授权失败：{exc}") from exc
    config = google_client_config() or {}
    try:
        profile = _google_profile_from_credentials(flow.credentials, config)
    except Exception as exc:
        raise ValueError(f"无法读取 Google 账号信息：{exc}") from exc
    if not profile.get("sub"):
        raise ValueError("Google 未返回账号标识")
    if profile.get("email") and not profile.get("email_verified"):
        raise ValueError("Google 邮箱尚未验证")
    return profile


def _sanitize_username(raw: str) -> str:
    s = "".join(ch for ch in (raw or "").strip() if ch not in "/\\:@<>")
    s = " ".join(s.split())
    return s[:32]


def username_from_google(name: str, email: str, sub: str) -> str:
    for candidate in (name, email.split("@", 1)[0] if email else ""):
        s = _sanitize_username(candidate)
        if s:
            return s
    return f"g{(sub or '')[:10]}"[:32] or "google-user"


def unique_username(base: str) -> str:
    if not _db.get_user_by_username(base):
        return base
    stem = base[:28]
    for i in range(2, 1000):
        cand = f"{stem}-{i}"
        if not _db.get_user_by_username(cand):
            return cand
    return f"g{secrets.token_hex(8)}"


def find_or_create_google_user(
    *,
    sub: str,
    email: str = "",
    name: str = "",
) -> tuple[bool, str, dict[str, Any] | None]:
    """Find an existing Google user or register a new one.

    Does not touch the Flask session.  Returns (ok, message, user).
    """
    sub = (sub or "").strip()
    if not sub:
        return False, "Google 账号信息不完整", None

    existing = _db.get_user_by_google_sub(sub)
    if existing is not None:
        if not existing["is_active"]:
            return False, "账号已被禁用，请联系管理员", None
        if email and email != (existing.get("email") or ""):
            _db.update_user(existing["id"], email=email)
            existing = _db.get_user(existing["id"]) or existing
        return True, "登录成功", existing

    username = unique_username(username_from_google(name, email, sub))
    user = _db.create_user(
        user_id=secrets.token_hex(12),
        username=username,
        password_hash="",
        google_sub=sub,
        email=email,
    )
    if user is None:
        return False, "无法创建账号，请稍后重试", None
    if _db.count_users() == 1:
        _db.update_user(user["id"], is_admin=True)
        user = _db.get_user(user["id"])
    return True, "注册成功", user


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------

def _is_api() -> bool:
    return request.path.startswith("/api/")


def login_required(view: Callable) -> Callable:
    """Require an authenticated user.  API routes get 401 JSON, pages redirect to /login."""

    @functools.wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        user = current_user()
        if user is None:
            if _is_api():
                from flask import jsonify
                return jsonify({"error": "未登录或会话已过期"}), 401
            return redirect(url_for("login_page"))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view: Callable) -> Callable:
    """Require an authenticated admin user."""

    @functools.wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        user = current_user()
        if user is None:
            if _is_api():
                from flask import jsonify
                return jsonify({"error": "未登录或会话已过期"}), 401
            return redirect(url_for("login_page"))
        if not user["is_admin"]:
            from flask import jsonify
            return jsonify({"error": "无权限：仅管理员可操作"}), 403
        return view(*args, **kwargs)

    return wrapped

"""User authentication helpers for the Flask web app.

Provides password hashing (Werkzeug scrypt), Flask-session based login state,
login throttling (5 failed attempts → 15 min lockout) and the
``login_required`` / ``admin_required`` decorators.

The SECRET_KEY used to sign session cookies lives in ``.env.local``
(``FLASK_SECRET_KEY``); if absent it is auto-generated once and persisted so
sessions survive restarts.
"""

from __future__ import annotations

import functools
import secrets
import sys
import time
from typing import Any, Callable

from flask import redirect, request, session, url_for
from werkzeug.security import check_password_hash as _check_hash
from werkzeug.security import generate_password_hash as _generate_hash

import db as _db
from app import ENV_FILE

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

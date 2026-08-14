"""Flask web application — serves the UI pages and JSON API.

Replaces the old ``http.server`` Handler in ``app.py``.  The heavy lifting
(download / transcription / DeepSeek / PDF) still lives in ``app.py`` and runs
in the separate ``worker.py`` process; this module only handles HTTP.

Start it with ``python server.py`` (waitress) or any WSGI server.
"""

from __future__ import annotations

import io
import json
import os
import time
import uuid
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, redirect, request, send_file, session, url_for

import auth
import db as _db
from app import (
    CONFIG_FILE,
    ROOT,
    STATIC_DIR,
    job_download_files,
    load_config,
    save_config,
    upload_job_to_drive,
    write_article_html,
)
from gdrive_uploader import (
    exchange_code as gdrive_exchange_code,
    get_auth_url as gdrive_get_auth_url,
    is_authenticated as gdrive_is_authenticated,
)

# Login/register throttling (per IP, in-memory)
_REGISTER_WINDOW = 3600  # seconds
_REGISTER_LIMIT = 5
_register_attempts: dict[str, list[float]] = {}


def create_app() -> Flask:
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    app.secret_key = auth.ensure_secret_key()
    app.permanent_session_lifetime = timedelta(days=7)
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # JSON bodies

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------

    @app.get("/")
    @auth.login_required
    def index():
        return send_file(STATIC_DIR / "index.html", mimetype="text/html; charset=utf-8")

    @app.get("/settings")
    @auth.login_required
    def settings_page():
        return send_file(STATIC_DIR / "settings.html", mimetype="text/html; charset=utf-8")

    @app.get("/login")
    def login_page():
        if auth.current_user():
            return redirect(url_for("index"))
        return send_file(STATIC_DIR / "login.html", mimetype="text/html; charset=utf-8")

    @app.get("/register")
    def register_page():
        if auth.current_user():
            return redirect(url_for("index"))
        return send_file(STATIC_DIR / "register.html", mimetype="text/html; charset=utf-8")

    # ------------------------------------------------------------------
    # Auth API
    # ------------------------------------------------------------------

    @app.post("/api/register")
    def api_register():
        now = time.time()
        ip = request.remote_addr or "?"
        attempts = [t for t in _register_attempts.get(ip, []) if now - t < _REGISTER_WINDOW]
        if len(attempts) >= _REGISTER_LIMIT:
            return jsonify({"error": f"注册过于频繁，请 {_REGISTER_WINDOW // 60} 分钟后再试"}), 429
        attempts.append(now)
        _register_attempts[ip] = attempts

        data = request.get_json(silent=True) or {}
        ok, message, user = auth.register(
            str(data.get("username", "")), str(data.get("password", ""))
        )
        if not ok:
            return jsonify({"error": message}), 400
        auth.login_user(user)
        return jsonify({"ok": True, "username": user["username"], "is_admin": user["is_admin"]})

    @app.post("/api/login")
    def api_login():
        data = request.get_json(silent=True) or {}
        ok, message = auth.login(str(data.get("username", "")), str(data.get("password", "")))
        if not ok:
            return jsonify({"error": message}), 401
        user = auth.current_user()
        return jsonify({"ok": True, "username": user["username"], "is_admin": user["is_admin"]})

    @app.post("/api/logout")
    @auth.login_required
    def api_logout():
        auth.logout_user()
        return jsonify({"ok": True})

    # ------------------------------------------------------------------
    # Config / settings
    # ------------------------------------------------------------------

    def _worker_alive() -> bool:
        hb = _db.get_worker_heartbeat()
        return hb is not None and (time.time() - hb) < 10

    @app.get("/api/config")
    @auth.login_required
    def api_get_config():
        user = auth.current_user()
        settings = user["settings"]
        try:
            gdrive_authed = gdrive_is_authenticated(user["id"])
        except Exception:
            gdrive_authed = False
        return jsonify({
            "user": {"username": user["username"], "is_admin": user["is_admin"]},
            "settings": {
                "gdrive_enabled": bool(settings.get("gdrive_enabled")),
                "gdrive_folder_id": str(settings.get("gdrive_folder_id", "")).strip(),
                "gdrive_format": str(settings.get("gdrive_format", "html")).strip() or "html",
                "date_subdir": bool(settings.get("date_subdir")),
                "youtube_cookie_configured": bool(str(settings.get("youtube_cookie", "")).strip()),
            },
            "gdrive_authenticated": gdrive_authed,
            "deepseek_configured": bool(os.getenv("DEEPSEEK_API_KEY", "").strip()),
            "deepseek_model": os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro"),
            "whisper_device": os.getenv("WHISPER_DEVICE", "auto"),
            "worker_alive": _worker_alive(),
        })

    @app.post("/api/config")
    @auth.login_required
    def api_save_config():
        user = auth.current_user()
        data = request.get_json(silent=True) or {}
        settings = dict(user["settings"] or {})
        if "gdrive_enabled" in data:
            settings["gdrive_enabled"] = bool(data["gdrive_enabled"])
        if "gdrive_folder_id" in data:
            settings["gdrive_folder_id"] = str(data.get("gdrive_folder_id", "")).strip()
        if "gdrive_format" in data:
            fmt = str(data.get("gdrive_format", "html")).strip().lower()
            settings["gdrive_format"] = fmt if fmt in ("html", "pdf") else "html"
        if "date_subdir" in data:
            settings["date_subdir"] = bool(data["date_subdir"])
        if "youtube_cookie" in data:
            settings["youtube_cookie"] = str(data.get("youtube_cookie", "")).strip()
        _db.update_user(user["id"], settings=settings)
        return jsonify({"ok": True})

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    @app.get("/api/jobs")
    @auth.login_required
    def api_list_jobs():
        user = auth.current_user()
        try:
            page = max(1, int(request.args.get("page", 1)))
        except (TypeError, ValueError):
            page = 1
        try:
            per_page = min(100, max(1, int(request.args.get("per_page", 20))))
        except (TypeError, ValueError):
            per_page = 20
        return jsonify(_db.list_user_jobs_page(user["id"], page=page, per_page=per_page))

    @app.post("/api/jobs")
    @auth.login_required
    def api_create_job():
        user = auth.current_user()
        data = request.get_json(silent=True) or {}
        url = str(data.get("url", "")).strip()
        if url and not url.startswith(("http://", "https://")):
            url = "https://" + url
        if not url.startswith(("http://", "https://")) or not (
            "bilibili.com" in url or "youtube.com" in url or "youtu.be" in url
        ):
            return jsonify({"error": "请输入有效的 Bilibili 或 YouTube URL"}), 400

        # Per-user YouTube cookie; an explicit cookie overrides it for this job
        cookie_string = str(data.get("cookie", "")).strip()
        if not cookie_string:
            cookie_string = str((user["settings"] or {}).get("youtube_cookie", "")).strip()

        job_id = uuid.uuid4().hex[:12]
        try:
            _db.create_job(job_id=job_id, url=url, cookie_string=cookie_string, user_id=user["id"])
        except Exception as exc:
            return jsonify({"error": f"创建任务失败：{exc}"}), 500
        return jsonify({"id": job_id})

    @app.get("/api/jobs/<job_id>")
    @auth.login_required
    def api_job_snapshot(job_id: str):
        user = auth.current_user()
        snap = _db.get_user_job_snapshot(user["id"], job_id)
        if snap is None:
            return jsonify({"error": "任务不存在"}), 404
        return jsonify(snap)

    @app.post("/api/jobs/<job_id>/cancel")
    @auth.login_required
    def api_cancel_job(job_id: str):
        user = auth.current_user()
        if not _db.get_user_job(user["id"], job_id):
            return jsonify({"error": "任务不存在"}), 404
        return jsonify({"ok": _db.cancel_job(job_id), "id": job_id})

    @app.post("/api/jobs/<job_id>/delete")
    @auth.login_required
    def api_delete_job(job_id: str):
        user = auth.current_user()
        if not _db.get_user_job(user["id"], job_id):
            return jsonify({"error": "任务不存在"}), 404
        return jsonify({"ok": _db.delete_job(job_id), "id": job_id})

    @app.post("/api/jobs/<job_id>/retry")
    @auth.login_required
    def api_retry_job(job_id: str):
        user = auth.current_user()
        if not _db.get_user_job(user["id"], job_id):
            return jsonify({"error": "任务不存在"}), 404
        ok = _db.retry_job(job_id)
        if not ok:
            return jsonify({"error": "任务不存在或状态不允许重试"}), 400
        return jsonify({"ok": True, "id": job_id})

    @app.post("/api/jobs/<job_id>/save-drive")
    @auth.login_required
    def api_save_drive(job_id: str):
        user = auth.current_user()
        try:
            return jsonify(upload_job_to_drive(job_id, user["id"]))
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/jobs/<job_id>/download")
    @auth.login_required
    def api_download_job(job_id: str):
        user = auth.current_user()
        job = _db.get_user_job(user["id"], job_id)
        if job is None:
            return jsonify({"error": "任务不存在"}), 404
        fmt = request.args.get("format", "md").lower()
        if fmt not in ("md", "html", "pdf"):
            return jsonify({"error": "不支持的格式"}), 400
        try:
            payloads = job_download_files(job, fmt)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 400

        if len(payloads) == 1:
            filename, content = payloads[0]
        else:
            # Multi-page job → zip archive
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for filename, content in payloads:
                    zf.writestr(filename, content)
            filename = f"{Path(job.get('output_dir') or 'article').name}.zip"
            content = buf.getvalue()

        import urllib.parse
        disposition = f"attachment; filename*=UTF-8''{urllib.parse.quote(filename)}"
        return Response(content, mimetype="application/octet-stream",
                        headers={"Content-Disposition": disposition})

    # ------------------------------------------------------------------
    # Google Drive (per-user)
    # ------------------------------------------------------------------

    @app.get("/api/gdrive/status")
    @auth.login_required
    def api_gdrive_status():
        user = auth.current_user()
        try:
            authed = gdrive_is_authenticated(user["id"])
        except Exception:
            authed = False
        return jsonify({"authenticated": authed})

    @app.post("/api/gdrive/auth-url")
    @auth.login_required
    def api_gdrive_auth_url():
        try:
            redirect_uri = request.url_root.rstrip("/") + "/api/gdrive/callback"
            auth_url, state = gdrive_get_auth_url(redirect_uri)
            session["gdrive_state"] = state
            return jsonify({"url": auth_url, "state": state})
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"error": f"生成授权链接失败：{exc}"}), 500

    @app.get("/api/gdrive/callback")
    def api_gdrive_callback():
        user = auth.current_user()
        code = request.args.get("code", "")
        state = request.args.get("state", "")
        if not code:
            return _html_page("授权失败", "未收到 Google 的授权码，请重试。")
        if user is None or session.get("gdrive_state") != state:
            return _html_page("授权失败", "授权会话已失效，请回到设置页重新发起授权。")
        session.pop("gdrive_state", None)
        success, message = gdrive_exchange_code(code, state, user["id"])
        return _html_page("授权成功" if success else "授权失败", message, auto_close=success)

    # ------------------------------------------------------------------
    # YouTube login (server-side browser; for local deployment)
    # ------------------------------------------------------------------

    @app.post("/api/youtube-login")
    @auth.login_required
    def api_youtube_login():
        user = auth.current_user()
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
        except ImportError:
            return jsonify({"error": "请先安装 playwright：pip install playwright"}), 500

        user_data_dir = ROOT / ".browser-data" / user["id"]
        user_data_dir.mkdir(parents=True, exist_ok=True)

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch_persistent_context(
                    str(user_data_dir), headless=False, channel="chrome", locale="zh-CN",
                )
                page = browser.pages[0] if browser.pages else browser.new_page()
                page.goto("https://www.youtube.com/", wait_until="domcontentloaded")
                try:
                    page.wait_for_selector(
                        '#avatar-btn, ytd-account-button, button[aria-label*="Google"], '
                        'ytd-active-account-header-renderer, #account-button',
                        timeout=300_000,
                    )
                except PlaywrightTimeout:
                    browser.close()
                    return jsonify({"error": "登录超时（5 分钟），请重试"}), 408
                cookies = browser.cookies()
                browser.close()
        except Exception as exc:
            return jsonify({"error": f"启动浏览器失败: {exc}"}), 500

        cookie_str = "; ".join(
            f"{c['name']}={c['value']}"
            for c in cookies
            if c.get("domain", "").endswith("youtube.com")
        )
        if not cookie_str:
            return jsonify({"error": "未获取到 YouTube cookie，请确认已登录"}), 400

        settings = dict(user["settings"] or {})
        settings["youtube_cookie"] = cookie_str
        _db.update_user(user["id"], settings=settings)
        return jsonify({"ok": True})

    # ------------------------------------------------------------------
    # Admin
    # ------------------------------------------------------------------

    @app.get("/api/admin/users")
    @auth.admin_required
    def api_admin_users():
        users = []
        for u in _db.list_users():
            users.append({
                "id": u["id"],
                "username": u["username"],
                "is_admin": u["is_admin"],
                "is_active": u["is_active"],
                "created_at": u["created_at"],
                "last_login_at": u["last_login_at"],
            })
        return jsonify({"users": users})

    @app.post("/api/admin/users/<user_id>/toggle")
    @auth.admin_required
    def api_admin_toggle_user(user_id: str):
        me = auth.current_user()
        target = _db.get_user(user_id)
        if target is None:
            return jsonify({"error": "用户不存在"}), 404
        if target["id"] == me["id"]:
            return jsonify({"error": "不能禁用自己"}), 400
        _db.update_user(user_id, is_active=not target["is_active"])
        return jsonify({"ok": True, "is_active": not target["is_active"]})

    return app


def _html_page(title: str, message: str, auto_close: bool = False) -> str:
    js_close = "<script>window.close();</script>" if auto_close else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>{title}</title>
<style>
  body {{ font-family: -apple-system, "SF Pro Display", "Segoe UI", sans-serif;
         display:flex; align-items:center; justify-content:center; min-height:100vh;
         margin:0; background:#f5f3ef; color:#1e2528; }}
  .card {{ background:#fff; border-radius:12px; padding:32px 40px; box-shadow:0 4px 24px rgba(0,0,0,.08);
           text-align:center; max-width:420px; }}
  h2 {{ margin:0 0 8px; font-size:20px; }}
  p {{ color:#667175; line-height:1.6; }}
  .icon {{ font-size:48px; margin-bottom:12px; }}
</style></head>
<body><div class="card">
  <div class="icon">{'✅' if auto_close else '❌'}</div>
  <h2>{title}</h2><p>{message}</p>
</div>{js_close}</body></html>"""


app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8085, debug=False)

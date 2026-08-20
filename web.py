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
from urllib.parse import quote

from flask import Flask, Response, jsonify, redirect, request, send_file, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

import auth
import db as _db
import kb as _kb
from app import (
    CONFIG_FILE,
    ROOT,
    STATIC_DIR,
    job_download_files,
    load_config,
    save_config,
    transcribe_provider,
    upload_job_to_notion,
    write_article_html,
)
from summarize import summarize_job
from notion_uploader import (
    auth_status as notion_auth_status,
    exchange_code as notion_exchange_code,
    get_auth_url as notion_get_auth_url,
    list_accessible_pages as notion_list_pages,
    parse_page_id as notion_parse_page_id,
)

# Login/register throttling (per IP, in-memory)
_REGISTER_WINDOW = 3600  # seconds
_REGISTER_LIMIT = 5
_register_attempts: dict[str, list[float]] = {}


def _public_base_url() -> str:
    """Canonical public origin for OAuth redirect URIs.

    Prefer ``PUBLIC_BASE_URL`` (e.g. https://bilibili-scraper.shuilong.uk) so
    callbacks stay correct behind Nginx.  Falls back to the incoming request.
    """
    env = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if env:
        return env
    return request.url_root.rstrip("/")


def create_app() -> Flask:
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    app.secret_key = auth.ensure_secret_key()
    app.permanent_session_lifetime = timedelta(days=7)
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # JSON bodies
    # 反代 HTTPS 时用 X-Forwarded-Proto / Host 生成正确的 OAuth 回调地址
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    @app.after_request
    def _no_store_frontend_assets(resp):
        path = request.path or ""
        if path.startswith("/static/") and path.endswith((".js", ".css", ".mjs")):
            resp.headers["Cache-Control"] = "no-store, max-age=0"
        return resp

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------

    @app.get("/")
    def landing():
        # 公开落地页；已登录用户直接进入工作台
        if auth.current_user():
            return redirect(url_for("workspace"))
        return send_file(STATIC_DIR / "landing.html", mimetype="text/html; charset=utf-8")

    @app.get("/app")
    @auth.login_required
    def workspace():
        return send_file(STATIC_DIR / "index.html", mimetype="text/html; charset=utf-8")

    @app.get("/settings")
    @auth.login_required
    def settings_page():
        return send_file(STATIC_DIR / "settings.html", mimetype="text/html; charset=utf-8")

    @app.get("/kb")
    @auth.login_required
    def kb_page():
        return send_file(STATIC_DIR / "kb.html", mimetype="text/html; charset=utf-8")

    @app.get("/login")
    def login_page():
        if auth.current_user():
            return redirect(url_for("workspace"))
        return send_file(STATIC_DIR / "login.html", mimetype="text/html; charset=utf-8")

    @app.get("/register")
    def register_page():
        if auth.current_user():
            return redirect(url_for("workspace"))
        return send_file(STATIC_DIR / "register.html", mimetype="text/html; charset=utf-8")

    @app.get("/privacy")
    def privacy_page():
        return send_file(STATIC_DIR / "privacy.html", mimetype="text/html; charset=utf-8")

    @app.get("/terms")
    def terms_page():
        return send_file(STATIC_DIR / "terms.html", mimetype="text/html; charset=utf-8")

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

    @app.get("/api/auth/google/status")
    def api_google_login_status():
        return jsonify({"enabled": auth.google_login_enabled()})

    @app.get("/api/auth/google")
    def api_google_login_start():
        if auth.current_user():
            return redirect(url_for("workspace"))
        redirect_uri = _public_base_url() + "/api/auth/google/callback"
        try:
            auth_url, state, code_verifier = auth.google_login_url(redirect_uri)
        except FileNotFoundError as exc:
            return redirect("/login?error=" + quote(str(exc)))
        except ValueError as exc:
            return redirect("/login?error=" + quote(str(exc)))
        except Exception as exc:
            return redirect("/login?error=" + quote(f"无法开始 Google 登录：{exc}"))
        session["google_oauth_state"] = state
        session["google_oauth_verifier"] = code_verifier
        return redirect(auth_url)

    @app.get("/api/auth/google/callback")
    def api_google_login_callback():
        if auth.current_user():
            return redirect(url_for("workspace"))
        if request.args.get("error"):
            denied = request.args.get("error")
            msg = "已取消 Google 登录" if denied == "access_denied" else f"Google 登录失败：{denied}"
            return redirect("/login?error=" + quote(msg))
        state = request.args.get("state", "")
        code = request.args.get("code", "")
        expected = session.pop("google_oauth_state", None)
        code_verifier = session.pop("google_oauth_verifier", "") or ""
        if not expected or state != expected:
            return redirect("/login?error=" + quote("授权会话已失效，请重新登录"))
        redirect_uri = _public_base_url() + "/api/auth/google/callback"
        try:
            profile = auth.complete_google_oauth(code, redirect_uri, code_verifier)
        except ValueError as exc:
            return redirect("/login?error=" + quote(str(exc)))
        except Exception as exc:
            return redirect("/login?error=" + quote(f"Google 登录失败：{exc}"))

        existing = _db.get_user_by_google_sub(profile["sub"])
        if existing is None:
            now = time.time()
            ip = request.remote_addr or "?"
            attempts = [t for t in _register_attempts.get(ip, []) if now - t < _REGISTER_WINDOW]
            if len(attempts) >= _REGISTER_LIMIT:
                return redirect(
                    "/login?error=" + quote(f"注册过于频繁，请 {_REGISTER_WINDOW // 60} 分钟后再试")
                )
            attempts.append(now)
            _register_attempts[ip] = attempts

        ok, message, user = auth.find_or_create_google_user(
            sub=profile["sub"],
            email=profile.get("email", ""),
            name=profile.get("name", ""),
        )
        if not ok or user is None:
            return redirect("/login?error=" + quote(message))
        auth.login_user(user)
        return redirect(url_for("workspace"))

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
            status = notion_auth_status(user["id"])
        except Exception:
            status = {"authenticated": False, "workspace": "", "oauth_ready": False}
        return jsonify({
            "user": {"username": user["username"], "is_admin": user["is_admin"]},
            "settings": {
                "notion_enabled": bool(settings.get("notion_enabled")),
                "notion_parent_page_id": str(settings.get("notion_parent_page_id", "")).strip(),
                "date_subdir": bool(settings.get("date_subdir")),
                "youtube_cookie_configured": bool(str(settings.get("youtube_cookie", "")).strip()),
            },
            "notion_configured": status["authenticated"],
            "notion_workspace": status["workspace"],
            "notion_oauth_ready": status["oauth_ready"],
            "deepseek_configured": bool(os.getenv("DEEPSEEK_API_KEY", "").strip()),
            "deepseek_model": os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            "whisper_device": os.getenv("WHISPER_DEVICE", "auto"),
            "transcribe_provider": transcribe_provider(),
            "groq_configured": bool(os.getenv("GROQ_API_KEY", "").strip()),
            "groq_model": os.getenv("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo"),
            "worker_alive": _worker_alive(),
        })

    @app.post("/api/config")
    @auth.login_required
    def api_save_config():
        user = auth.current_user()
        data = request.get_json(silent=True) or {}
        settings = dict(user["settings"] or {})
        if "notion_enabled" in data:
            settings["notion_enabled"] = bool(data["notion_enabled"])
        if "notion_parent_page_id" in data:
            raw_parent = str(data.get("notion_parent_page_id", "")).strip()
            parsed = notion_parse_page_id(raw_parent)
            settings["notion_parent_page_id"] = parsed or raw_parent
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

    @app.post("/api/jobs/<job_id>/save-notion")
    @auth.login_required
    def api_save_notion(job_id: str):
        user = auth.current_user()
        try:
            return jsonify(upload_job_to_notion(job_id, user["id"]))
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/jobs/<job_id>/summary")
    @auth.login_required
    def api_job_summary(job_id: str):
        user = auth.current_user()
        job = _db.get_user_job(user["id"], job_id)
        if job is None:
            return jsonify({"error": "任务不存在"}), 404
        data = request.get_json(silent=True) or {}
        try:
            result = summarize_job(
                job,
                fmt=data.get("format", "paragraph"),
                length=data.get("length", "medium"),
                page_index=data.get("page", 0),
                regenerate=bool(data.get("regenerate")),
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 400
        if not result["cached"]:
            _db.merge_job_summary(
                job_id,
                result["page"],
                result["format"],
                result["length"],
                result["summary"],
            )
        return jsonify(result)

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
    # Notion OAuth (per-user)
    # ------------------------------------------------------------------

    @app.get("/api/notion/status")
    @auth.login_required
    def api_notion_status():
        user = auth.current_user()
        try:
            return jsonify(notion_auth_status(user["id"]))
        except Exception:
            return jsonify({"authenticated": False, "workspace": "", "oauth_ready": False})

    @app.post("/api/notion/auth-url")
    @auth.login_required
    def api_notion_auth_url():
        try:
            redirect_uri = _public_base_url() + "/api/notion/callback"
            auth_url, state = notion_get_auth_url(redirect_uri)
            session["notion_oauth_state"] = state
            session["notion_oauth_redirect"] = redirect_uri
            return jsonify({"url": auth_url, "state": state})
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"error": f"生成授权链接失败：{exc}"}), 500

    @app.get("/api/notion/callback")
    def api_notion_callback():
        user = auth.current_user()
        if request.args.get("error"):
            desc = request.args.get("error_description") or request.args.get("error") or "授权被取消"
            return _html_page("授权失败", desc)
        code = request.args.get("code", "")
        state = request.args.get("state", "")
        if not code:
            return _html_page("授权失败", "未收到 Notion 的授权码，请重试。")
        if user is None or session.get("notion_oauth_state") != state:
            return _html_page("授权失败", "授权会话已失效，请回到设置页重新发起授权。")
        redirect_uri = session.pop("notion_oauth_redirect", None) or (
            _public_base_url() + "/api/notion/callback"
        )
        session.pop("notion_oauth_state", None)
        success, message = notion_exchange_code(code, redirect_uri, user["id"])
        if success:
            listed = notion_list_pages(user["id"])
            roots = listed.get("roots") or []
            row = _db.get_user(user["id"]) or user
            settings = dict(row.get("settings") or {})
            current = str(settings.get("notion_parent_page_id") or "").strip()
            if not current and len(roots) == 1:
                settings["notion_parent_page_id"] = roots[0]["id"]
                _db.update_user(user["id"], settings=settings)
                title = roots[0].get("title") or "未命名"
                message = f"{message} 已自动选择页面「{title}」。"
        return _html_page("授权成功" if success else "授权失败", message, auto_close=success)

    @app.get("/api/notion/pages")
    @auth.login_required
    def api_notion_pages():
        user = auth.current_user()
        try:
            return jsonify(notion_list_pages(user["id"]))
        except Exception as exc:
            return jsonify({"pages": [], "roots": [], "error": str(exc)})

    @app.post("/api/notion/disconnect")
    @auth.login_required
    def api_notion_disconnect():
        user = auth.current_user()
        _db.delete_notion_token(user["id"])
        return jsonify({"ok": True})

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
                "google": bool(u.get("google_sub")),
                "email": u.get("email") or "",
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

    # ------------------------------------------------------------------
    # 知识库（RAG 问答）
    # ------------------------------------------------------------------

    @app.get("/api/kb/status")
    @auth.login_required
    def api_kb_status():
        try:
            status = _kb.index_status()
        except Exception as exc:
            return jsonify({"error": f"索引状态读取失败：{exc}"}), 500
        status["configured"] = bool(os.getenv("DEEPSEEK_API_KEY", "").strip())
        status["model"] = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
        try:
            status["samples"] = _kb.sample_questions()
        except Exception:
            status["samples"] = []
        return jsonify(status)

    @app.post("/api/kb/rebuild")
    @auth.login_required
    def api_kb_rebuild():
        try:
            index = _kb.rebuild_index(force=True)
        except Exception as exc:
            return jsonify({"error": f"索引重建失败：{exc}"}), 500
        return jsonify({
            "ok": True,
            "articles": len(_kb._collect_docs()),
            "chunks": index.get("num_docs", 0),
            "built_at": index.get("built_at"),
        })

    @app.post("/api/kb/chat")
    @auth.login_required
    def api_kb_chat():
        data = request.get_json(silent=True) or {}
        question = str(data.get("question", "")).strip()
        history = data.get("history") or []
        if not question:
            return jsonify({"error": "问题不能为空"}), 400
        if len(question) > 2000:
            return jsonify({"error": "问题过长，请精简到 2000 字以内"}), 400

        def generate() -> Any:
            try:
                for ev in _kb.chat_stream(question, history):
                    payload = ev.get("payload") or {}
                    yield f"event: {ev['type']}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            except Exception as exc:  # 兜底：任何异常都以 error 事件结束
                yield f"event: error\ndata: {json.dumps({'message': str(exc)}, ensure_ascii=False)}\n\n"

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # 反代（Nginx）下关闭缓冲，保证流式输出
            },
        )

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

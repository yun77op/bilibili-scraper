from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import site
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]

import requests


from notion_uploader import (
    create_article_page as notion_create_article_page,
    is_configured as notion_is_configured,
)

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
OUTPUT_DIR = ROOT / "outputs"
DOCS_DIR = ROOT / "docs"
MODEL_DIR = ROOT / "models"
HF_HOME = MODEL_DIR / "huggingface"
WHISPER_MODEL_DIR = MODEL_DIR / "whisper"
LOCAL_WHISPER_DIR = MODEL_DIR / "faster-whisper"
ENV_FILE = ROOT / ".env.local"
CONFIG_FILE = ROOT / "config.json"
BILIBILI_VIEW_API = "https://api.bilibili.com/x/web-interface/view"
BILIBILI_PLAYURL_APIS = [
    "https://api.bilibili.com/x/player/wbi/playurl",
    "https://api.bilibili.com/x/player/playurl",
]
BILIBILI_PLAYER_V2_API = "https://api.bilibili.com/x/player/v2"
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MAX_UPLOAD_BYTES = 24 * 1024 * 1024
GROQ_CHUNK_SECONDS = 600
BILIBILI_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
WBI_MIXIN_KEY_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]
_wbi_key_cache: dict[str, Any] = {"expires_at": 0.0, "img_key": "", "sub_key": ""}
CUDA_DLL_HANDLES: list[Any] = []


def load_local_env(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ[key] = value


load_local_env()


def load_config() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        cfg: dict[str, Any] = {}
        for old_file, key, transform in [
            (ROOT / ".pdf_dir", "pdf_dir", lambda v: v),
            (ROOT / ".auto_save", "auto_save", lambda v: v.lower() == "true"),
            (ROOT / ".date_subdir", "date_subdir", lambda v: v.lower() == "true"),
        ]:
            if old_file.exists():
                cfg[key] = transform(old_file.read_text(encoding="utf-8").strip())
        if cfg:
            save_config(cfg)
        return cfg
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_config(config: dict[str, Any]) -> None:
    CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


@dataclass
class Job:
    id: str
    url: str
    title: str = ""
    cookie_string: str = ""
    user_id: str = ""
    status: str = "queued"
    stage: str = "等待开始"
    logs: list[str] = field(default_factory=list)
    progress: int = 0
    transcript: str = ""
    article: str = ""
    error: str = ""
    output_dir: str = ""
    page_output_dirs: list[str] = field(default_factory=list)
    page_articles: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self):
        object.__setattr__(self, "_stage_timer", 0.0)
        object.__setattr__(self, "stage_times", [])

    def _stage_begin(self) -> None:
        object.__setattr__(self, "_stage_timer", time.time())

    def _stage_end(self, label: str) -> float:
        elapsed = time.time() - self._stage_timer
        self.stage_times.append((label, elapsed))
        return elapsed

    def log(self, message: str, progress: int | None = None) -> None:
        self.logs.append(message)
        self.stage = message
        if progress is not None:
            self.progress = progress
        self.updated_at = time.time()

    def build_summary(self) -> str:
        lines = ["═══ 任务汇总 ═══"]
        for label, elapsed in self.stage_times:
            lines.append(f"  {label}：{elapsed:.1f}s")
        total = time.time() - self.created_at
        lines.append(f"  总耗时：{total:.1f}s")
        if self.output_dir:
            lines.append(f"  输出目录：{self.output_dir}")
            lines.append(f"    transcript.txt")
            lines.append(f"    article.md")
        return "\n".join(lines)


# ── 数据库（SQLite）替代了原来的内存 dict/list ──────────────────
# 所有任务状态通过 db.py 读写，server 和 worker 共享同一个 jobs.db
import db as _db  # noqa: E402


# ── 取消机制 ────────────────────────────────────────────────────

class JobCancelledError(Exception):
    """任务已被用户取消"""
    pass


def check_cancelled(job: Job) -> None:
    """如果任务被取消则抛出 JobCancelledError"""
    if _db.is_job_cancelled(job.id):
        raise JobCancelledError(f"任务 {job.id} 已被取消")


# DeepSeek 取消机制：全局注册表，用于中断进行中的 HTTP 请求
_cancel_sessions: dict[str, tuple[threading.Event, requests.Session]] = {}
_cancel_lock = threading.Lock()


def sanitize_filename(value: str) -> str:
    value = re.sub(r"[^\w.\u4e00-\u9fff-]+", "-", value).strip("-")
    return value[:100] or "video"


# 目录内最长文件名：sanitize 上限 100 + "-" + yt_id(11) + 扩展名(4) = 116
_MAX_INNER_FILENAME_LEN = 116


def _output_dir_name(stem: str, suffix: str) -> str:
    """按 Windows MAX_PATH(260) 预算生成输出目录名，避免路径超长导致文件创建失败。

    suffix 不含前导 "-"，例如 "20260806-BV1aQMX6oEni-p6"。
    """
    budget = 259 - len(str(OUTPUT_DIR)) - 3 - _MAX_INNER_FILENAME_LEN - len(suffix)
    return f"{stem[:max(0, budget)]}-{suffix}"


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"缺少命令行工具：{name}。请先安装后再重试。")
    return path


def configure_cuda_dll_paths() -> None:
    if os.name != "nt":
        return

    candidates: list[Path] = []
    for package_root in site.getsitepackages():
        root = Path(package_root)
        candidates.extend(
            [
                root / "nvidia" / "cuda_runtime" / "bin",
                root / "nvidia" / "cublas" / "bin",
                root / "nvidia" / "cudnn" / "bin",
            ]
        )

    existing = [path for path in candidates if path.exists()]
    if not existing:
        return

    current_path = os.environ.get("PATH", "")
    prepend = [str(path) for path in existing if str(path) not in current_path]
    if prepend:
        os.environ["PATH"] = os.pathsep.join(prepend + [current_path])

    for path in existing:
        try:
            CUDA_DLL_HANDLES.append(os.add_dll_directory(str(path)))
        except (FileNotFoundError, OSError):
            continue


def redact_value(value: str) -> str:
    if not value:
        return value
    return value[:8] + "...[redacted]"


def run_command(args: list[str], cwd: Path, job: Job, redactions: list[str] | None = None) -> None:
    display_args = " ".join(args)
    for secret in redactions or []:
        if secret:
            display_args = display_args.replace(secret, redact_value(secret))
    job.logs.append("$ " + display_args)
    process = subprocess.Popen(
        args,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    for line in process.stdout:
        line = line.strip()
        if line:
            job.logs.append(line)
            job.updated_at = time.time()
    code = process.wait()
    if code:
        if code < 0:
            import signal as _signal
            sig_name = f"信号 {abs(code)}"
            try:
                sig_name = _signal.Signals(abs(code)).name
            except ValueError:
                pass
            raise RuntimeError(
                f"命令被终止（{sig_name}）：{' '.join(args)}"
            )
        raise RuntimeError(f"命令执行失败，退出码 {code}：{' '.join(args)}")


def fetch_bilibili_view(url: str, job: Job) -> tuple[dict, dict]:
    bvid = extract_bvid(url)
    cookie_string = load_cookie_string(job)
    headers = ensure_bilibili_visitor_cookie(build_bilibili_headers(url, cookie_string))
    view = bilibili_json(BILIBILI_VIEW_API, {"bvid": bvid}, headers, "视频信息接口")
    return view, headers


def download_audio(url: str, out_dir: Path, job: Job, view_data: dict | None = None, headers: dict | None = None) -> Path:
    if view_data is None or headers is None:
        job.log("正在解析 Bilibili 视频信息", 15)
        view_data, headers = fetch_bilibili_view(url, job)

    bvid = extract_bvid(url)
    pages = view_data.get("data", {}).get("pages") or []
    if not pages:
        raise RuntimeError("Bilibili 视频信息接口没有返回分 P 信息。")

    page_index = extract_page_index(url)
    if page_index >= len(pages):
        page_index = 0
    cid = pages[page_index]["cid"]
    title = view_data.get("data", {}).get("title") or bvid

    job.log("正在获取音频流地址", 25)
    params = {
        "bvid": bvid,
        "cid": str(cid),
        "fnval": "4048",
        "fourk": "1",
    }
    try:
        play_data = fetch_bilibili_playurl(params, headers)
    except RuntimeError as exc:
        raise RuntimeError(f"无法获取播放地址：{_playurl_error_hint(exc)}") from exc

    audio_stream, kind = pick_play_stream(
        play_data,
        cookie_configured=bool(headers.get("Cookie")),
        exclusive=upower_exclusive(view_data),
    )
    audio_url = stream_url(audio_stream)
    if not audio_url:
        raise RuntimeError("播放地址接口没有返回可下载的音频/视频 URL。")

    if kind == "durl":
        if upower_exclusive(view_data):
            job.log("该视频为充电专属（专属视频档），未开通包月充电时只能获取低清整段视频流，将下载后提取音频", 25)
        else:
            job.log("未登录时接口只返回整段视频流（MP4/FLV），将下载后提取音频，音质可能低于 DASH", 25)
    extension = audio_extension(audio_stream, audio_url, kind)
    audio_path = out_dir / f"{sanitize_filename(title)}-{bvid}.{extension}"
    job.log("正在下载音频文件", 35)
    download_file(audio_url, audio_path, headers, job,
                  backup_urls=audio_stream.get("backup_url") or audio_stream.get("backupUrl") or [])
    return audio_path


def fetch_bilibili_subtitle(url: str, out_dir: Path, job: Job, view_data: dict | None = None, headers: dict | None = None) -> str | None:
    job.log("正在检查 Bilibili 字幕", 12)
    if view_data is None or headers is None:
        view_data, headers = fetch_bilibili_view(url, job)

    bvid = extract_bvid(url)
    pages = view_data.get("data", {}).get("pages") or []
    if not pages:
        return None

    page_index = extract_page_index(url)
    if page_index >= len(pages):
        page_index = 0
    cid = pages[page_index]["cid"]

    player = bilibili_json(
        BILIBILI_PLAYER_V2_API,
        {"bvid": bvid, "cid": str(cid)},
        headers,
        "播放器字幕接口",
    )
    subtitles = player.get("data", {}).get("subtitle", {}).get("subtitles") or []
    if not subtitles:
        job.log("未找到可用字幕，准备下载音频转写", 14)
        return None

    subtitle = pick_subtitle(subtitles)
    subtitle_url = subtitle.get("subtitle_url") or subtitle.get("url")
    if not subtitle_url:
        job.log("字幕列表没有可下载地址，准备下载音频转写", 14)
        return None
    if subtitle_url.startswith("//"):
        subtitle_url = "https:" + subtitle_url

    job.log(f"正在下载字幕：{subtitle.get('lan_doc') or subtitle.get('lan') or 'unknown'}", 25)
    data = http_get(subtitle_url, {**headers, "Accept": "application/json, text/plain, */*"})
    raw_path = out_dir / "subtitle.json"
    raw_path.write_bytes(data)
    subtitle_payload = json.loads(data.decode("utf-8"))
    body = subtitle_payload.get("body") or []
    lines = []
    for item in body:
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        start = format_timestamp(float(item.get("from", 0)))
        end = format_timestamp(float(item.get("to", 0)))
        lines.append(f"[{start} - {end}] {content}")

    if not lines:
        job.log("字幕内容为空，准备下载音频转写", 14)
        return None
    return "\n".join(lines)


def pick_subtitle(subtitles: list[dict[str, Any]]) -> dict[str, Any]:
    preferred_languages = ("zh-CN", "zh-Hans", "zh", "ai-zh")
    for language in preferred_languages:
        for subtitle in subtitles:
            lan = str(subtitle.get("lan") or "")
            lan_doc = str(subtitle.get("lan_doc") or "")
            if language.lower() in lan.lower() or language.lower() in lan_doc.lower():
                return subtitle
    return subtitles[0]


def fetch_youtube_info(url: str, job: Job) -> dict:
    job.log("正在获取 YouTube 视频信息", 10)
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("缺少 Python 依赖 yt-dlp。请执行：pip install yt-dlp")

    ydl_opts: dict[str, Any] = {"quiet": True, "no_warnings": True, "socket_timeout": 60}
    proxy = os.getenv("BILIBILI_PROXY", "").strip()
    if proxy:
        ydl_opts["proxy"] = proxy
    youtube_browser = os.getenv("YOUTUBE_BROWSER", "").strip()
    if youtube_browser:
        job.log(f"正在从浏览器读取 YouTube Cookie：{youtube_browser}", 11)
        ydl_opts["cookiesfrombrowser"] = (youtube_browser,)
    else:
        cookie_result = _resolve_youtube_cookies(job)
        if cookie_result:
            ydl_opts["cookiefile"] = cookie_result
            job.log(f"已加载 YouTube Cookie", 11)
        else:
            job.log("未检测到 YouTube Cookie", 11)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False, process=False)


def download_youtube_audio(url: str, out_dir: Path, job: Job, info: dict) -> Path:
    job.log("正在下载 YouTube 音频", 25)
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("缺少 Python 依赖 yt-dlp。")

    yt_id = extract_youtube_id(url)
    title = info.get("title") or yt_id
    safe_title = sanitize_filename(title)
    output_template = str(out_dir / f"{safe_title}-{yt_id}.%(ext)s")

    def _progress_hook(d: dict) -> None:
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            if total:
                job.progress = min(48, 35 + int(downloaded / total * 13))
                job.updated_at = time.time()
        elif d["status"] == "finished":
            job.progress = 48

    ydl_opts: dict[str, Any] = {
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 60,
        "progress_hooks": [_progress_hook],
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}],
        "remote_components": ["ejs:github"],
    }

    proxy = os.getenv("BILIBILI_PROXY", "").strip()
    if proxy:
        ydl_opts["proxy"] = proxy
    youtube_browser = os.getenv("YOUTUBE_BROWSER", "").strip()
    if youtube_browser:
        ydl_opts["cookiesfrombrowser"] = (youtube_browser,)
    else:
        cookie_result = _resolve_youtube_cookies(job)
        if cookie_result:
            ydl_opts["cookiefile"] = cookie_result

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    audio_path = out_dir / f"{safe_title}-{yt_id}.m4a"
    if not audio_path.exists():
        candidates = sorted(out_dir.glob(f"{safe_title}*.m4a"))
        if not candidates:
            candidates = sorted(out_dir.glob("*.m4a"))
        if candidates:
            audio_path = candidates[0]
        else:
            raise RuntimeError("YouTube 音频下载后未找到文件。")
    return audio_path


def fetch_youtube_subtitle(url: str, out_dir: Path, job: Job, info: dict) -> str | None:
    job.log("正在检查 YouTube 字幕", 12)

    subtitles = info.get("subtitles") or {}
    auto_captions = info.get("automatic_captions") or {}
    preferred_langs = ["zh-Hans", "zh-Hant", "zh", "zh-CN", "en"]

    for lang in preferred_langs:
        for subtitle_dict in [subtitles, auto_captions]:
            if lang in subtitle_dict:
                sub_list = subtitle_dict[lang]
                if sub_list:
                    sub_info = sub_list[0]
                    sub_url = sub_info.get("url")
                    if sub_url:
                        job.log(f"正在下载 YouTube 字幕：{lang}", 25)
                        try:
                            raw = http_get(sub_url, {})
                        except RuntimeError as exc:
                            job.log(f"YouTube 字幕下载失败：{exc}，改为音频转写", 25)
                            continue
                        raw_path = out_dir / "subtitle.json"
                        raw_path.write_bytes(raw)
                        return parse_youtube_subtitle_json3(raw)

    job.log("未找到 YouTube 可用字幕，准备下载音频转写", 14)
    return None


def parse_youtube_subtitle_json3(raw_data: bytes) -> str | None:
    data = json.loads(raw_data.decode("utf-8"))
    events = data.get("events") or []
    lines = []
    for event in events:
        t_start_ms = event.get("tStartMs", 0)
        d_duration_ms = event.get("dDurationMs", 0)
        segs = event.get("segs") or []
        text_parts = [str(seg.get("utf8", "")).strip() for seg in segs if seg.get("utf8")]
        content = "".join(text_parts).strip()
        if not content:
            continue
        start = t_start_ms / 1000.0
        end = (t_start_ms + d_duration_ms) / 1000.0
        lines.append(f"[{format_timestamp(start)} - {format_timestamp(end)}] {content}")
    return "\n".join(lines) if lines else None


def extract_bvid(url: str) -> str:
    match = re.search(r"(BV[0-9A-Za-z]+)", url)
    if not match:
        raise RuntimeError("没有从 URL 中识别到 BV 号。")
    return match.group(1)


def find_cached_output(bvid: str) -> Path | None:
    if not OUTPUT_DIR.exists():
        return None
    for d in sorted(OUTPUT_DIR.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        if bvid not in d.name:
            continue
        transcript = d / "transcript.txt"
        article = d / "article.md"
        if transcript.exists() and article.exists():
            return d
    return None


def extract_page_index(url: str) -> int:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    try:
        return max(int(query.get("p", ["1"])[0]) - 1, 0)
    except ValueError:
        return 0


def identify_platform(url: str) -> str:
    if re.search(r"(youtube\.com|youtu\.be)", url):
        return "youtube"
    if re.search(r"bilibili\.com", url):
        return "bilibili"
    raise RuntimeError("暂不支持的平台。目前支持 Bilibili 和 YouTube。")


def extract_youtube_id(url: str) -> str:
    match = re.search(r"youtu\.be/([A-Za-z0-9_-]{11})", url)
    if match:
        return match.group(1)
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    v = query.get("v")
    if v:
        return v[0]
    match = re.search(r"youtube\.com/(?:embed|shorts|live)/([A-Za-z0-9_-]{11})", url)
    if match:
        return match.group(1)
    raise RuntimeError("没有从 URL 中识别到 YouTube 视频 ID。")


def find_cached_output_yt(yt_id: str) -> Path | None:
    if not OUTPUT_DIR.exists():
        return None
    for d in sorted(OUTPUT_DIR.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        if f"youtube-{yt_id}" not in d.name:
            continue
        transcript = d / "transcript.txt"
        article = d / "article.md"
        if transcript.exists() and article.exists():
            return d
    return None


def build_bilibili_headers(referer: str, cookie_string: str) -> dict[str, str]:
    headers = {
        "User-Agent": BILIBILI_USER_AGENT,
        "Referer": referer,
        "Origin": "https://www.bilibili.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if cookie_string:
        headers["Cookie"] = cookie_string
    return headers


def ensure_bilibili_visitor_cookie(headers: dict[str, str]) -> dict[str, str]:
    cookie = headers.get("Cookie", "")
    if "buvid3=" in cookie:
        return headers
    try:
        data = http_get(
            "https://api.bilibili.com/x/frontend/finger/spi",
            {**headers, "Accept": "application/json, text/plain, */*"},
        )
        payload = json.loads(data.decode("utf-8"))
        finger = payload.get("data") or {}
        extra = []
        for key, name in (("b_3", "buvid3"), ("b_4", "buvid4")):
            value = str(finger.get(key) or "").strip()
            if value:
                extra.append(f"{name}={value}")
        if not extra:
            return headers
        updated = dict(headers)
        updated["Cookie"] = "; ".join(part for part in [cookie.strip(), "; ".join(extra)] if part)
        return updated
    except Exception:
        return headers


def load_cookie_string(job: Job, platform: str = "bilibili") -> str:
    if job.cookie_string.strip():
        return job.cookie_string.strip()

    if platform == "youtube":
        cookie_env = "YOUTUBE_COOKIE"
        file_env = "YOUTUBE_COOKIES_FILE"
        config_key = "youtube_cookie"
        default_file = ROOT / "youtube-cookies.txt"
    else:
        cookie_env = "BILIBILI_COOKIE"
        file_env = "BILIBILI_COOKIES_FILE"
        config_key = "bilibili_cookie"
        default_file = None

    env_cookie = os.getenv(cookie_env, "").strip()
    if env_cookie:
        return env_cookie
    cookie_file = os.getenv(file_env, "").strip()
    if cookie_file:
        cookie_path = Path(cookie_file)
        if not cookie_path.is_absolute():
            cookie_path = ROOT / cookie_path
        if cookie_path.exists():
            return read_netscape_cookie_file(cookie_path)
    if default_file and default_file.exists():
        return read_netscape_cookie_file(default_file)
    config = load_config()
    config_cookie = str(config.get(config_key, "")).strip()
    return config_cookie


def _resolve_youtube_cookies(job: Job) -> str | None:
    cookie_string = load_cookie_string(job, "youtube")
    if not cookie_string:
        return None
    raw_path = ROOT / "youtube-cookies.txt"
    if raw_path.exists() and "\t" in raw_path.read_text(encoding="utf-8", errors="replace"):
        with open(raw_path, encoding="utf-8") as f:
            content = f.read()
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix="youtube-cookies-", suffix=".txt", delete=False)
        handle.write(content)
        handle.close()
        return handle.name
    cookie_file = write_temp_cookie_file(cookie_string, "youtube")
    return str(cookie_file)


def read_netscape_cookie_file(path: Path) -> str:
    if not path.exists():
        raise RuntimeError(f"Cookie 文件不存在：{path}")
    raw = path.read_text(encoding="utf-8", errors="replace")
    if "\t" in raw:
        cookies: list[str] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                cookies.append(f"{parts[5]}={parts[6]}")
        return "; ".join(cookies)
    return raw.strip()


def _wbi_mixin_key(img_key: str, sub_key: str) -> str:
    merged = img_key + sub_key
    return "".join(merged[i] for i in WBI_MIXIN_KEY_TABLE)[:32]


def _fetch_wbi_keys(headers: dict[str, str]) -> tuple[str, str]:
    now = time.time()
    if _wbi_key_cache["expires_at"] > now and _wbi_key_cache["img_key"]:
        return _wbi_key_cache["img_key"], _wbi_key_cache["sub_key"]
    nav = bilibili_json(
        "https://api.bilibili.com/x/web-interface/nav",
        {},
        headers,
        "WBI 密钥接口",
    )
    wbi_img = nav.get("data", {}).get("wbi_img") or {}
    img_url = str(wbi_img.get("img_url") or "")
    sub_url = str(wbi_img.get("sub_url") or "")
    img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
    sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
    if not img_key or not sub_key:
        raise RuntimeError("无法从 nav 接口获取 WBI 密钥。")
    _wbi_key_cache.update({"img_key": img_key, "sub_key": sub_key, "expires_at": now + 3600})
    return img_key, sub_key


def _sign_wbi_params(params: dict[str, str], img_key: str, sub_key: str) -> dict[str, str]:
    mixin_key = _wbi_mixin_key(img_key, sub_key)
    signed = dict(params)
    signed["wts"] = str(int(time.time()))
    signed = {
        key: "".join(ch for ch in str(value) if ch not in "!'()*")
        for key, value in sorted(signed.items())
    }
    query = urllib.parse.urlencode(signed)
    signed["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return signed


def fetch_bilibili_playurl(params: dict[str, str], headers: dict[str, str]) -> dict[str, Any]:
    last_error: RuntimeError | None = None
    for api_url in BILIBILI_PLAYURL_APIS:
        try:
            request_params = dict(params)
            if "/wbi/" in api_url:
                img_key, sub_key = _fetch_wbi_keys(headers)
                request_params = _sign_wbi_params(request_params, img_key, sub_key)
            return bilibili_json(api_url, request_params, headers, "播放地址接口")
        except RuntimeError as exc:
            last_error = exc
    if last_error is None:
        raise RuntimeError("播放地址接口请求失败。")
    raise last_error


def _playurl_error_hint(exc: RuntimeError) -> str:
    msg = str(exc)
    if "HTTP 403" in msg or "HTTP 412" in msg:
        proxy = os.getenv("BILIBILI_PROXY", "").strip()
        if not proxy:
            return (
                msg
                + "（服务器 IP 可能被 B 站限流，请在 .env.local 配置 BILIBILI_PROXY 后重启 worker）"
            )
    return msg


def bilibili_json(
    api_url: str,
    params: dict[str, str],
    headers: dict[str, str],
    label: str,
) -> dict[str, Any]:
    url = api_url + "?" + urllib.parse.urlencode(params)
    data = http_get(url, headers)
    try:
        payload = json.loads(data.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} 返回的不是 JSON。") from exc
    code = payload.get("code")
    if code not in (0, None):
        message = payload.get("message") or payload.get("msg") or "未知错误"
        raise RuntimeError(f"{label} 返回错误：code={code}，message={message}")
    return payload


def http_get(url: str, headers: dict[str, str], timeout: int = 30) -> bytes:
    proxy = os.getenv("BILIBILI_PROXY", "").strip()
    opener = urllib.request.build_opener()
    if proxy:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code}：{body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"网络请求失败：{exc.reason}") from exc


def upower_exclusive(view_data: dict[str, Any] | None) -> bool:
    """True when the video is a 充电专属（专属视频档）video.

    The view API marks these with ``data.is_upower_exclusive``; watching them
    requires an account that has an active monthly charging subscription to the
    uploader (e.g. a 30 元/月 tier), not merely a logged-in cookie.
    """
    return bool((view_data or {}).get("data", {}).get("is_upower_exclusive"))


def pick_play_stream(play_data: dict[str, Any], cookie_configured: bool = False,
                     exclusive: bool = False) -> tuple[dict[str, Any], str]:
    """Pick the best downloadable stream from a playurl response.

    Returns ``(stream, kind)`` where ``kind`` is ``"dash"`` (preferred, an
    audio-only DASH stream) or ``"durl"`` (a whole-video MP4/FLV stream).

    Bilibili only returns the DASH tree when the caller has sufficient
    permission.  For login-required videos requested without a cookie it
    silently omits ``data.dash`` and returns only ``data.durl`` (usually a
    low-quality MP4).  Falling back to ``durl`` keeps the job working; the
    audio is extracted afterwards via ffmpeg (or decoded directly by
    faster-whisper when ffmpeg is unavailable).
    """
    if exclusive:
        reason = (
            "该视频是充电专属（专属视频档），需要开通 UP 主的包月充电（例如 30 元/月）"
            "后才能观看；普通登录 Cookie 无法解锁。"
        )
    elif cookie_configured:
        reason = "已配置 Cookie，但仍无法获取视频流：该视频可能仅登录可见、需要大会员，或已被下架。"
    else:
        reason = "未配置 Cookie：该视频可能仅登录可见、需要大会员，或已被下架。"
    data = play_data.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(
            "播放地址接口没有返回视频流数据" + f"（{reason}）"
        )
    audios = data.get("dash", {}).get("audio") or []
    if audios:
        return max(audios, key=lambda item: item.get("bandwidth") or 0), "dash"
    durls = data.get("durl") or []
    if durls:
        return durls[0], "durl"
    raise RuntimeError(
        "播放地址接口既没有返回 DASH 音频流，也没有返回整段视频流。"
        f"{reason}"
        "如需解锁，需在服务器配置包含 SESSDATA 的 Bilibili 登录 Cookie"
        "（BILIBILI_COOKIE 环境变量或 config.json 的 bilibili_cookie）。"
    )


def stream_url(stream: dict[str, Any]) -> str:
    """Return the download URL of a DASH or durl stream entry."""
    return stream.get("baseUrl") or stream.get("base_url") or stream.get("url") or ""


def audio_extension(audio_stream: dict[str, Any], audio_url: str, kind: str = "dash") -> str:
    if kind == "durl":
        suffix = Path(urllib.parse.urlparse(audio_url).path).suffix.lower().lstrip(".")
        return suffix if suffix in {"mp4", "flv", "m4s", "mkv"} else "mp4"
    mime_type = audio_stream.get("mimeType") or audio_stream.get("mime_type") or ""
    if "mp4" in mime_type:
        return "m4a"
    if "webm" in mime_type:
        return "webm"
    suffix = Path(urllib.parse.urlparse(audio_url).path).suffix.lower().lstrip(".")
    return suffix if suffix in {"m4a", "mp3", "webm", "aac", "flac", "opus"} else "m4a"


def download_file(url: str, path: Path, headers: dict[str, str], job: Job,
                  backup_urls: list[str] | None = None) -> None:
    proxy = os.getenv("BILIBILI_PROXY", "").strip()
    max_retries = 3
    last_error = None
    urls = [url] + [u for u in (backup_urls or []) if u]

    for url_index, current_url in enumerate(urls):
        if url_index > 0 and path.exists() and path.stat().st_size > 0:
            # Partial data came from a previous URL; do not resume across URLs.
            try:
                path.unlink()
            except OSError:
                pass
        for attempt in range(max_retries):
            try:
                _download_file_attempt(current_url, path, headers, job, proxy, attempt)
                return
            except (urllib.error.URLError, urllib.error.HTTPError, ConnectionError, TimeoutError) as exc:
                last_error = exc
                if attempt < max_retries - 1:
                    wait = (attempt + 1) * 3
                    job.log(f"下载中断，{wait}秒后重试（{attempt + 1}/{max_retries}）...", job.progress)
                    time.sleep(wait)
                    check_cancelled(job)
                elif url_index < len(urls) - 1:
                    job.log("当前地址下载失败，切换到备用地址...", job.progress)
                else:
                    raise

    if last_error:
        raise last_error


def _download_file_attempt(url: str, path: Path, headers: dict[str, str],
                           job: Job, proxy: str, attempt: int) -> None:
    opener = urllib.request.build_opener()
    if proxy:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))

    req_headers = {**headers, "Accept": "*/*"}

    # Resume from partial download
    if attempt > 0 and path.exists():
        existing_size = path.stat().st_size
        if existing_size > 0:
            req_headers["Range"] = f"bytes={existing_size}-"
            job.log(f"断点续传，从 {existing_size / 1024 / 1024:.1f}MB 处继续", job.progress)

    request = urllib.request.Request(url, headers=req_headers, method="GET")
    try:
        with opener.open(request, timeout=120) as response:
            total = int(response.headers.get("Content-Length") or "0")
            # If server honored Range request, total is remaining bytes
            if "Content-Range" in response.headers:
                # e.g. "bytes 88092236-145217531/145217532"
                content_range = response.headers["Content-Range"]
                try:
                    parts = content_range.split("/")
                    total = int(parts[-1]) if len(parts) > 1 else total
                except (ValueError, IndexError):
                    pass

            downloaded = path.stat().st_size if (attempt > 0 and path.exists()) else 0
            mode = "ab" if downloaded > 0 else "wb"
            chunk_count = 0
            with path.open(mode) as output:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    output.write(chunk)
                    downloaded += len(chunk)
                    chunk_count += 1
                    if chunk_count % 5 == 0:
                        check_cancelled(job)
                    if total:
                        job.progress = min(48, 35 + int(downloaded / total * 13))
                        job.updated_at = time.time()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"音频下载失败：HTTP {exc.code}：{body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"音频下载失败：{exc.reason}") from exc

    if path.stat().st_size == 0:
        raise RuntimeError("音频下载结果为空。")


def write_temp_cookie_file(cookie_string: str, platform: str = "bilibili") -> Path:
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        prefix=f"{platform}-cookies-",
        suffix=".txt",
        delete=False,
    )
    path = Path(handle.name)
    with handle:
        handle.write("# Netscape HTTP Cookie File\n")
        for name, value in parse_cookie_header(cookie_string).items():
            if platform == "youtube":
                handle.write(f".youtube.com\tTRUE\t/\tFALSE\t0\t{name}\t{value}\n")
                handle.write(f"youtube.com\tFALSE\t/\tFALSE\t0\t{name}\t{value}\n")
            else:
                handle.write(f".bilibili.com\tTRUE\t/\tFALSE\t0\t{name}\t{value}\n")
                handle.write(f"bilibili.com\tFALSE\t/\tFALSE\t0\t{name}\t{value}\n")
    return path


def parse_cookie_header(cookie_string: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in cookie_string.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if name:
            cookies[name] = value
    return cookies


def validate_cookie_string(cookie_string: str) -> None:
    cookie_names = set(parse_cookie_header(cookie_string))
    if "SESSDATA" not in cookie_names:
        raise RuntimeError(
            "Cookie 不完整：缺少 SESSDATA。请不要用浏览器控制台 document.cookie 复制，"
            "需要用 Cookie-Editor 等扩展导出包含 HttpOnly Cookie 的 Netscape 文件，"
            "或在页面 Cookie 输入框粘贴包含 SESSDATA 的完整 Cookie。"
        )


def convert_for_transcription(audio_path: Path, out_dir: Path, job: Job) -> Path:
    check_cancelled(job)
    ffmpeg = require_tool("ffmpeg")
    wav_path = out_dir / "audio-16k-mono.wav"
    job.log("正在转换音频格式", 35)
    run_command(
        [
            ffmpeg,
            "-y",
            "-i",
            str(audio_path),
            "-ar",
            "16000",
            "-ac",
            "1",
            str(wav_path),
        ],
        out_dir,
        job,
    )
    return wav_path


def wav_duration_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as audio:
        frames = audio.getnframes()
        rate = audio.getframerate()
        return frames / float(rate)


def transcribe_provider() -> str:
    """Return ``groq`` or ``local``. Unset → groq when ``GROQ_API_KEY`` exists."""
    raw = os.getenv("TRANSCRIBE_PROVIDER", "").strip().lower()
    if raw in ("groq", "local"):
        return raw
    return "groq" if os.getenv("GROQ_API_KEY", "").strip() else "local"


def transcribe_language(default: str | None = None) -> str | None:
    raw = os.getenv("TRANSCRIBE_LANGUAGE", "").strip().lower()
    if not raw:
        return default
    if raw in ("auto", "detect", "none"):
        return None
    return raw


def format_transcript_segments(segments: list[tuple[float, float, str]]) -> str:
    lines: list[str] = []
    for start, end, text in segments:
        text = text.strip()
        if not text:
            continue
        lines.append(f"[{format_timestamp(start)} - {format_timestamp(end)}] {text}")
    return "\n".join(lines).strip()


def _audio_mime(path: Path) -> str:
    return {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".mp4": "audio/mp4",
        ".mpeg": "audio/mpeg",
        ".mpga": "audio/mpeg",
        ".ogg": "audio/ogg",
        ".webm": "audio/webm",
        ".flac": "audio/flac",
    }.get(path.suffix.lower(), "application/octet-stream")


def probe_audio_duration(path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            out = subprocess.check_output(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            return max(float(out.strip()), 0.0)
        except (subprocess.CalledProcessError, ValueError, OSError):
            pass
    if path.suffix.lower() == ".wav":
        try:
            return wav_duration_seconds(path)
        except wave.Error:
            pass
    return max(path.stat().st_size * 8 / 64000, 1.0)


def _groq_chunks(audio_path: Path, out_dir: Path, job: Job) -> list[tuple[Path, float]]:
    """Compress to 64kbps mp3 and split if over Groq's free-tier 25MB cap."""
    ffmpeg = shutil.which("ffmpeg")
    source = audio_path
    if ffmpeg:
        mp3_path = out_dir / "audio-groq.mp3"
        job.log("正在压缩音频以便云端转写", 45)
        run_command(
            [
                ffmpeg,
                "-y",
                "-i",
                str(audio_path),
                "-ar",
                "16000",
                "-ac",
                "1",
                "-b:a",
                "64k",
                str(mp3_path),
            ],
            out_dir,
            job,
        )
        source = mp3_path
    if source.stat().st_size <= GROQ_MAX_UPLOAD_BYTES:
        return [(source, 0.0)]
    if not ffmpeg:
        raise RuntimeError(
            f"音频 {source.stat().st_size} 字节超过 Groq 上传限制，且未安装 ffmpeg 无法分段"
        )
    chunk_dir = out_dir / "groq-chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    job.log("音频超过 Groq 单文件上限，按 10 分钟分段上传", 46)
    run_command(
        [
            ffmpeg,
            "-y",
            "-i",
            str(source),
            "-f",
            "segment",
            "-segment_time",
            str(GROQ_CHUNK_SECONDS),
            "-c",
            "copy",
            str(chunk_dir / "chunk-%03d.mp3"),
        ],
        out_dir,
        job,
    )
    chunks = sorted(chunk_dir.glob("chunk-*.mp3"))
    if not chunks:
        raise RuntimeError("音频分段失败，未生成分片文件")
    result: list[tuple[Path, float]] = []
    offset = 0.0
    for chunk in chunks:
        result.append((chunk, offset))
        offset += probe_audio_duration(chunk)
    return result


def _groq_payload_segments(payload: dict[str, Any], offset: float) -> list[tuple[float, float, str]]:
    triples: list[tuple[float, float, str]] = []
    for seg in payload.get("segments") or []:
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        start = float(seg.get("start") or 0.0) + offset
        end = float(seg.get("end") or start) + offset
        triples.append((start, end, text))
    if triples:
        return triples
    text = str(payload.get("text") or "").strip()
    if not text:
        return []
    duration = float(payload.get("duration") or 0.0)
    return [(offset, offset + duration, text)]


def transcribe_with_groq(audio_path: Path, out_dir: Path, job: Job, page_label: str = "") -> str:
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("未配置 GROQ_API_KEY")
    model = os.getenv("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo").strip() or "whisper-large-v3-turbo"
    language = transcribe_language()
    lang_note = language or "自动检测"
    job.log(f"{page_label}正在转写音频，Groq：{model}（{lang_note}）", 50)

    headers = {"Authorization": f"Bearer {api_key}"}
    triples: list[tuple[float, float, str]] = []
    chunks = _groq_chunks(audio_path, out_dir, job)
    detected = ""
    for index, (chunk_path, offset) in enumerate(chunks):
        check_cancelled(job)
        if len(chunks) > 1:
            job.log(f"{page_label}Groq 转写分片 {index + 1}/{len(chunks)}", 50 + int(index / len(chunks) * 15))
        data: dict[str, str] = {
            "model": model,
            "response_format": "verbose_json",
            "temperature": "0",
        }
        if language:
            data["language"] = language
        with chunk_path.open("rb") as fh:
            resp = requests.post(
                GROQ_TRANSCRIBE_URL,
                headers=headers,
                files={"file": (chunk_path.name, fh, _audio_mime(chunk_path))},
                data=data,
                timeout=180,
            )
        if resp.status_code != 200:
            detail = resp.text.strip()[:300]
            raise RuntimeError(f"Groq 转写失败 HTTP {resp.status_code}：{detail}")
        payload = resp.json()
        if not detected:
            detected = str(payload.get("language") or "")
        triples.extend(_groq_payload_segments(payload, offset))

    text = format_transcript_segments(triples)
    if not text:
        raise RuntimeError(f"Groq 转写结果为空，检测语言：{detected or '未知'}")
    job.progress = 70
    job.updated_at = time.time()
    return text


def transcribe_audio(audio_path: Path, out_dir: Path, job: Job, page_label: str = "") -> str:
    """Transcribe via Groq when configured, otherwise local faster-whisper.

    Groq failures fall back to the local model so a missing key or API outage
    does not fail the whole job.
    """
    provider = transcribe_provider()
    if provider == "groq":
        try:
            return transcribe_with_groq(audio_path, out_dir, job, page_label)
        except JobCancelledError:
            raise
        except Exception as exc:
            job.log(f"{page_label}Groq 转写失败（{exc}），回退到本地 Whisper", 50)
    ffmpeg_path = shutil.which("ffmpeg")
    source = audio_path
    if ffmpeg_path:
        source = convert_for_transcription(audio_path, out_dir, job)
    else:
        job.log(f"{page_label}未找到 ffmpeg，直接使用下载的音频进行转写", 35)
    return transcribe_with_faster_whisper(source, job, page_label)


@contextmanager
def _exclusive_file_lock(path: Path, job: Job, page_label: str) -> Iterator[None]:
    """Serialize local Whisper so two workers cannot both load the model."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a+")
    locked = False
    try:
        if fcntl is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                job.log(f"{page_label}本地 Whisper 已被占用，等待空闲…", 50)
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            locked = True
        yield
    finally:
        if locked and fcntl is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


def transcribe_with_faster_whisper(audio_path: Path, job: Job, page_label: str = "") -> str:
    lock_path = ROOT / ".pids" / "whisper.lock"
    with _exclusive_file_lock(lock_path, job, page_label):
        return _transcribe_with_faster_whisper_locked(audio_path, job, page_label)


def _transcribe_with_faster_whisper_locked(audio_path: Path, job: Job, page_label: str = "") -> str:
    configure_cuda_dll_paths()
    HF_HOME.mkdir(parents=True, exist_ok=True)
    WHISPER_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_WHISPER_DIR.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(HF_HOME))
    os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(HF_HOME / "hub"))
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            f"缺少 Python 依赖 faster-whisper。请执行：{sys.executable} -m pip install -r requirements.txt"
        ) from exc

    model_name = os.getenv("WHISPER_MODEL", "base")
    device = os.getenv("WHISPER_DEVICE", "auto")
    compute_type = os.getenv("WHISPER_COMPUTE_TYPE", "auto")
    job.log(f"{page_label}正在转写音频，本地模型：{model_name}", 50)
    model_path = ensure_whisper_model(model_name, job)
    model = WhisperModel(
        model_path,
        device=device,
        compute_type=compute_type,
        local_files_only=True,
    )
    duration = 1.0
    if audio_path.suffix.lower() == ".wav":
        duration = max(wav_duration_seconds(audio_path), 1.0)
    segments, info = model.transcribe(
        str(audio_path),
        language=transcribe_language(default="zh"),
        vad_filter=True,
        beam_size=5,
    )

    lines: list[str] = []
    seg_count = 0
    for segment in segments:
        start = format_timestamp(segment.start)
        end = format_timestamp(segment.end)
        lines.append(f"[{start} - {end}] {segment.text.strip()}")
        seg_count += 1
        if seg_count % 10 == 0:
            check_cancelled(job)
        if audio_path.suffix.lower() == ".wav":
            job.progress = min(70, 50 + int(segment.end / duration * 20))
        else:
            job.progress = 65
        job.updated_at = time.time()

    if not lines:
        raise RuntimeError(f"转写结果为空，检测语言：{info.language}")
    return "\n".join(lines).strip()


def ensure_whisper_model(model_name: str, job: Job) -> str:
    if Path(model_name).exists():
        return model_name

    repo_id = model_name if "/" in model_name else f"Systran/faster-whisper-{model_name}"
    target = LOCAL_WHISPER_DIR / sanitize_filename(repo_id.replace("/", "-"))
    required_files = ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"]
    if all((target / name).exists() and (target / name).stat().st_size > 0 for name in required_files):
        return str(target)

    job.log(f"正在下载 Whisper 模型到本地目录：{target}", 52)
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError("缺少 Python 依赖 huggingface-hub。请执行：pip install -r requirements.txt") from exc

    snapshot_download(
        repo_id=repo_id,
        local_dir=target,
        cache_dir=MODEL_DIR / "hf-download-cache",
        allow_patterns=required_files,
        max_workers=1,
    )
    missing = [name for name in required_files if not (target / name).exists()]
    if missing:
        raise RuntimeError(f"Whisper 模型下载不完整，缺少文件：{', '.join(missing)}")
    return str(target)


def format_timestamp(seconds: float) -> str:
    total = int(seconds)
    hh = total // 3600
    mm = (total % 3600) // 60
    ss = total % 60
    return f"{hh:02d}:{mm:02d}:{ss:02d}"


# --- Markdown 修复（实现见 markdown_repair.py，便于独立测试）------------

from markdown_repair import repair_article_markdown


def build_article_prompt(transcript: str) -> str:
    return (
        "请把下面的视频转写稿整理成一篇内容详实、结构清晰、图文并茂的简体中文技术文章。无论原视频或转写稿是什么语言，最终都必须输出简体中文（不得使用繁体中文）。\n\n"
        "核心原则——逐信息点覆盖，不得遗漏：\n"
        '原文中出现的每一个事实、数据、概念定义、例子、对比、警告、操作步骤、实践建议，都必须出现在最终文章中。不得因为"信息量太小""不重要""与主题关联不紧"而省略任何信息点。信息密集时用更细的子标题（####）分点组织，宁可文章长，不可遗漏信息。\n\n'
        "格式与结构要求：\n"
        "1. 文章开头生成清晰的标题和副标题，然后列出目录（Markdown 锚点链接）。\n"
        "2. 正文使用多级标题（##、###、####）组织，每个主题独立成节，节与节之间用 `---` 分隔。\n"
        "3. 关键概念、对比信息、参数说明、优缺点等，**必须使用 Markdown 表格**呈现，不要用纯文字罗列。表格必须严格遵守 Markdown 语法：表头、分隔行（`| --- | --- |`）和每一行数据都各自独占一行，表格上方留一个空行，禁止把整个表格连写在同一行；列表（`-`、`1.`）的每一项也必须各自独占一行，禁止把多个列表项连写在同一段落里。\n"
        "4. 如果原文涉及流程、架构、时序、决策分支等内容，用 ```mermaid 代码块绘制 Mermaid 图表（flowchart、sequenceDiagram 等）来可视化，不要用 ASCII 字符画图。Mermaid 语法务必严格正确：节点 id 只用英文字母和数字，节点的中文标签统一放在引号内（如 A[\"开始处理\"]），箭头上的标签用 |...| 包裹。\n"
        "5. 对原文的核心观点，逐条展开：解释背景、拆解原理、说明应用场景、指出局限性、给出实践建议。不要只做摘要。\n"
        '6. 每个重要结论后用 > 引用块提炼一句"核心要点"。\n'
        "7. 结尾写一段总结，回顾全文核心知识体系，并展望相关方向。\n\n"
        "数学公式（LaTeX）：\n"
        "8. 如果原文涉及数学公式、算法复杂度、统计学公式、物理公式等，请使用 LaTeX 语法呈现。\n"
        "9. 行内公式用 `$...$` 包裹，独立公式用 `$$...$$` 包裹。例如：时间复杂度 $O(n \\log n)$，贝叶斯公式：$$P(A|B) = \\frac{P(B|A)P(A)}{P(B)}$$\n"
        "10. 代码块内不要使用 LaTeX（代码里的 $ 符号不会被渲染为公式）。\n\n"
        "内容展开原则：\n"
        "11. 保留原视频的所有事实、数据、概念和论证顺序，**不编造**具体人名、机构名、数字或案例。\n"
        "12. 对原文讲得简略的观点，结合常识和领域知识适度展开：补充背景、解释概念、拆解因果关系、说明影响和适用场景。\n"
        '13. 凡是推断性补充，使用"可以理解为""这意味着""从这个角度看"等表达，避免伪装成原文明说。\n'
        "14. 可以加入通俗类比帮助理解，但不要写成原文中出现过的真实案例。\n"
        "15. 去掉口头禅、重复表达和无意义停顿。\n"
        "16. 如果原文是外语，先理解原意再用自然简体中文改写，专有名词保留原文并附简体中文解释。\n"
        "17. 如果转写稿里有明显不确定或疑似识别错误的内容，用括号标注（待核对）。\n"
        "18. 每个小节覆盖原文该主题下的所有信息点；信息密集时使用 #### 子标题分点展开，不允许省略任何原文事实或观点。\n\n"
        f"转写稿：\n{transcript}"
    )


def request_deepseek_article(transcript: str, job: Job, page_label: str = "") -> str:
    check_cancelled(job)

    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("缺少 DEEPSEEK_API_KEY，无法调用 DeepSeek 整理文章。")

    model = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是一名资深中文技术编辑，擅长把任何语言的视频转写稿整理成内容详实、有图表有表格、结构清晰的简体中文技术长文。你善于用表格对比信息，用 Mermaid 图表可视化流程与架构，用引用块提炼要点，用 LaTeX 数学公式（$...$ 行内、$$...$$ 独立）呈现数学内容。你必须全程使用简体中文，不得出现繁体中文。"},
            {"role": "user", "content": build_article_prompt(transcript)},
        ],
        "stream": False,
        "max_tokens": 32768,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    job.log(f"{page_label}正在调用 DeepSeek 整理文章：{model}", 80)

    # 注册可取消的 session，主线程轮询 DB 取消状态时可以关闭连接
    cancel_event = threading.Event()
    session = requests.Session()
    with _cancel_lock:
        _cancel_sessions[job.id] = (cancel_event, session)

    result: list[dict | None] = [None]
    error: list[Exception | None] = [None]

    def _do_request() -> None:
        try:
            resp = session.post(DEEPSEEK_API_URL, json=payload, headers=headers, timeout=180)
            resp.raise_for_status()
            result[0] = resp.json()
        except Exception as e:
            error[0] = e

    thread = threading.Thread(target=_do_request, daemon=True)
    thread.start()

    try:
        # 主线程每 0.5s 检查一次取消状态
        while thread.is_alive():
            thread.join(timeout=0.5)
            check_cancelled(job)
            if cancel_event.is_set():
                raise JobCancelledError()

        if error[0] is not None:
            if isinstance(error[0], requests.exceptions.ConnectionError):
                # 连接被 session.close() 关闭 → 很可能是取消触发的
                check_cancelled(job)
            if isinstance(error[0], requests.exceptions.HTTPError):
                body = ""
                try:
                    body = error[0].response.text[:500]
                except Exception:
                    pass
                raise RuntimeError(
                    f"DeepSeek 接口返回错误：HTTP {error[0].response.status_code} {body}"
                ) from error[0]
            raise RuntimeError(f"DeepSeek 接口调用失败：{error[0]}") from error[0]

        data = result[0]
        if data is None:
            raise RuntimeError("DeepSeek 返回结果为空。")
    finally:
        with _cancel_lock:
            _cancel_sessions.pop(job.id, None)
        session.close()

    choices = data.get("choices") or []
    article = ""
    if choices:
        message = choices[0].get("message") or {}
        article = str(message.get("content") or "").strip()
    if not article:
        raise RuntimeError("DeepSeek 返回结果为空。")
    article = repair_article_markdown(article)
    return article


def process_job(job: Job) -> None:
    job.status = "running"
    job.log("任务已开始", 5)

    try:
        check_cancelled(job)
        platform = identify_platform(job.url)
        if platform == "bilibili":
            _process_bilibili(job)
        elif platform == "youtube":
            _process_youtube(job)
    except JobCancelledError:
        job.status = "cancelled"
        job.log("任务已被用户取消", job.progress)
    except Exception as exc:
        job.status = "error"
        job.error = str(exc)
        tb = traceback.format_exc()
        job.log(f"任务失败：{exc}\n{tb}", job.progress)


def _process_bilibili(job: Job) -> None:
    bvid = extract_bvid(job.url)
    has_explicit_page = "p=" in urllib.parse.urlparse(job.url).query
    # Only use cache for single-page videos without explicit page param
    if not has_explicit_page:
        cached = find_cached_output(bvid)
        if cached:
            transcript = (cached / "transcript.txt").read_text(encoding="utf-8")
            article = (cached / "article.md").read_text(encoding="utf-8")
            if not article.startswith("> 原视频链接："):
                article = f"> 原视频链接：{job.url}\n\n{article}"
            article = repair_article_markdown(article)
            job.output_dir = str(cached)
            job.transcript = transcript
            job.article = article
            job.status = "done"
            job.log(f"复用缓存: {cached}", 100)
            job.log(job.build_summary())
            _auto_notion_upload(job)
            return

    job._stage_begin()
    view_data, headers = fetch_bilibili_view(job.url, job)
    job._stage_end("获取视频信息")

    pages = view_data.get("data", {}).get("pages") or []
    total_title = view_data.get("data", {}).get("title") or bvid
    job.title = total_title
    total = len(pages)

    page_index = extract_page_index(job.url)
    if total <= 1:
        # Single page video — process it directly
        _process_bilibili_page(job, bvid, view_data, headers, pages, 0, total_title, "")
        job.status = "done"
        job.log("任务完成", 100)
        job.log(job.build_summary())
        if job.output_dir:
            _auto_notion_upload(job)
    else:
        # Multiple pages — ?p=N means "start from episode N to the end"
        start_page = page_index if has_explicit_page else 0
        if start_page >= total:
            start_page = 0
        remaining = total - start_page

        if has_explicit_page:
            job.log(f"从第 {start_page + 1} 集开始，共 {remaining} 集待处理", 8)
        else:
            job.log(f"共 {total} 个分P，开始逐集处理", 8)

        all_transcripts: list[str] = []
        all_articles: list[str] = []
        all_output_dirs: list[Path] = []
        for i in range(start_page, total):
            if _db.is_job_cancelled(job.id):
                job.status = "cancelled"
                job.log("任务已被用户取消", job.progress)
                break
            if i > start_page:
                job._stage_begin()
            label = f"[{i + 1}/{total}] "
            # Clear previous page's transcript/article so the UI doesn't show stale content
            # while the current page is being processed.
            job.transcript = ""
            job.article = ""
            job.log(f"{label}处理第 {i + 1} 页", 10)
            _process_bilibili_page(job, bvid, view_data, headers, pages, i, total_title,
                                   page_label=label)
            all_transcripts.append(job.transcript)
            all_articles.append(job.article)
            all_output_dirs.append(Path(job.output_dir))

            # 处理完一集就立即上传，不等全部完成
            _auto_notion_upload(job)

        root_dir = Path(job.output_dir).parent if job.output_dir else OUTPUT_DIR
        if all_transcripts:
            combined = "\n\n---\n\n".join(all_transcripts)
            (root_dir / "transcript-all.txt").write_text(combined, encoding="utf-8")
            job.transcript = combined
            job.article = "\n\n---\n\n".join(all_articles)
        if job.status == "cancelled":
            job.log(f"已取消，已完成第 {start_page + 1}-{start_page + len(all_transcripts)} 集")
        else:
            job.status = "done"
            first, last = start_page + 1, total
            job.log(f"全部处理完成（第 {first}-{last} 集）", 100)
            job.log(job.build_summary())

        # Store per-page data for save_job_article()
        job.page_output_dirs = [str(d) for d in all_output_dirs]
        job.page_articles = all_articles
        return


def _process_bilibili_page(job: Job, bvid: str, view_data: dict, headers: dict,
                           pages: list, page_index: int, total_title: str,
                           page_label: str = "") -> None:
    """Process a single page/episode of a Bilibili video."""
    check_cancelled(job)

    page = pages[page_index]
    cid = page["cid"]
    if len(pages) > 1:
        page_title = page.get("part") or f"P{page_index + 1}"
    else:
        # 单 P 时 part 可能是上传工具内部名（如 horizontal_ok），用稿件总标题
        page_title = total_title or page.get("part") or f"P{page_index + 1}"
    job.title = page_title or total_title

    # 合集/分P视频：文件名前加上合集名（多分P用视频总标题，单集用 ugc_season 标题）
    collection = ""
    if len(pages) > 1:
        collection = total_title
    else:
        season = view_data.get("data", {}).get("ugc_season") or {}
        collection = season.get("title") or ""
    stem = sanitize_filename(page_title)
    collection_stem = sanitize_filename(collection) if collection else ""
    if collection_stem and collection_stem != stem:
        stem = f"{collection_stem}-{stem}"
    OUTPUT_DIR.mkdir(exist_ok=True)
    out_dir = OUTPUT_DIR / _output_dir_name(
        stem, f"{time.strftime('%Y%m%d')}-{bvid}-p{page_index + 1}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    job.output_dir = str(out_dir)

    job._stage_begin()
    transcript = _fetch_page_subtitle(bvid, cid, out_dir, job, headers)
    if transcript:
        job._stage_end("字幕获取")
        job.log(f"{page_label}已获取字幕，跳过音频转写", 70)
    else:
        job._stage_end("字幕获取(无)")
        job._stage_begin()
        audio_path = _download_page_audio(
            bvid, cid, f"{total_title}-{page_title}", out_dir, job, headers,
            page_label,
            exclusive=upower_exclusive(view_data),
        )
        job._stage_end("音频下载")
        job._stage_begin()
        transcript = transcribe_audio(audio_path, out_dir, job, page_label)
        job._stage_end("语音转写")

    transcript_path = out_dir / "transcript.txt"
    transcript_path.write_text(transcript, encoding="utf-8")
    job.transcript = transcript
    job.log(f"{page_label}转写完成", 75)

    job._stage_begin()
    if not os.getenv("DEEPSEEK_API_KEY", "").strip():
        raise RuntimeError("未配置 DEEPSEEK_API_KEY，无法生成文章。请在 .env.local 中设置后重试。")
    article = request_deepseek_article(transcript, job, page_label)
    job._stage_end("AI 文章生成")

    page_url = f"https://www.bilibili.com/video/{bvid}/?p={page_index + 1}"
    article_path = out_dir / "article.md"
    article_with_source = f"> 原视频链接：{page_url}\n\n{article}"
    article_path.write_text(article_with_source, encoding="utf-8")
    job.article = article_with_source
    job.log(f"{page_label}{page_title} 完成", 85)


def _fetch_page_subtitle(bvid: str, cid: int, out_dir: Path, job: Job,
                          headers: dict) -> str | None:
    """Fetch subtitle for a specific page CID."""
    try:
        player = bilibili_json(
            BILIBILI_PLAYER_V2_API, {"bvid": bvid, "cid": str(cid)},
            headers, "播放器字幕接口",
        )
        subtitles = player.get("data", {}).get("subtitle", {}).get("subtitles") or []
        if not subtitles:
            return None
        subtitle = pick_subtitle(subtitles)
        subtitle_url = subtitle.get("subtitle_url") or subtitle.get("url")
        if not subtitle_url:
            return None
        if subtitle_url.startswith("//"):
            subtitle_url = "https:" + subtitle_url
        data = http_get(subtitle_url, {**headers, "Accept": "application/json, text/plain, */*"})
        raw_path = out_dir / "subtitle.json"
        raw_path.write_bytes(data)
        payload = json.loads(data.decode("utf-8"))
        body = payload.get("body") or []
        lines = []
        for item in body:
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            start = format_timestamp(float(item.get("from", 0)))
            end = format_timestamp(float(item.get("to", 0)))
            lines.append(f"[{start} - {end}] {content}")
        return "\n".join(lines) if lines else None
    except Exception:
        return None


def _download_page_audio(bvid: str, cid: int, title: str, out_dir: Path,
                          job: Job, headers: dict, page_label: str,
                          exclusive: bool = False) -> Path:
    """Download audio for a specific page CID."""
    job.log(f"{page_label}正在获取音频流", 20)

    params: dict[str, str] = {"bvid": bvid, "cid": str(cid), "fnval": "4048", "fourk": "1"}
    try:
        play_data = fetch_bilibili_playurl(params, headers)
    except RuntimeError as exc:
        raise RuntimeError(f"无法获取播放地址：{_playurl_error_hint(exc)}") from exc

    audio_stream, kind = pick_play_stream(
        play_data,
        cookie_configured=bool(headers.get("Cookie")),
        exclusive=exclusive,
    )
    audio_url = stream_url(audio_stream)
    if not audio_url:
        raise RuntimeError("播放地址接口没有返回可下载的音频/视频 URL。")

    if kind == "durl":
        if exclusive:
            job.log(f"{page_label}该视频为充电专属（专属视频档），未开通包月充电时只能获取低清整段视频流，将下载后提取音频", 20)
        else:
            job.log(f"{page_label}未登录时接口只返回整段视频流（MP4/FLV），将下载后提取音频，音质可能低于 DASH", 20)
    extension = audio_extension(audio_stream, audio_url, kind)
    audio_path = out_dir / f"{sanitize_filename(title)}.{extension}"
    job.log(f"{page_label}正在下载音频", 25)
    download_file(audio_url, audio_path, headers, job,
                  backup_urls=audio_stream.get("backup_url") or audio_stream.get("backupUrl") or [])
    return audio_path


def _auto_notion_upload(job: Job) -> None:
    """Create a Notion page for the current article if the owner enabled it.

    Preferences (enabled / parent page / date subdir) come from the user's
    settings; the OAuth access token is stored per-user in ``notion_tokens``.
    """
    settings = _user_settings(job.user_id)
    if not settings.get("notion_enabled"):
        return
    if not str(job.article or "").strip():
        return
    try:
        parent_page = str(settings.get("notion_parent_page_id", "")).strip()
        use_date_subdir = bool(settings.get("date_subdir"))
        fallback_title = job.title or (Path(job.output_dir).name if job.output_dir else "")
        article = repair_article_markdown(job.article)
        result = notion_create_article_page(
            markdown=article,
            parent_page_id=parent_page,
            user_id=job.user_id,
            title=fallback_title,
            date_subdir=use_date_subdir,
        )
        if result.get("status") == "success":
            d = result["data"]
            job.log(f"已写入 Notion：{d['title']} ({d['url']})")
        else:
            job.log(f"⚠️ Notion 上传失败：{result.get('message', '')}")
    except Exception as exc:
        job.log(f"⚠️ Notion 上传异常：{exc}")


def _user_settings(user_id: str) -> dict[str, Any]:
    """Return the user's settings dict (Notion / YouTube preferences), or {}."""
    if not user_id:
        return {}
    user = _db.get_user(user_id)
    if not user:
        return {}
    return user.get("settings") or {}


def _process_youtube(job: Job) -> None:
    yt_id = extract_youtube_id(job.url)
    cached = find_cached_output_yt(yt_id)
    if cached:
        transcript = (cached / "transcript.txt").read_text(encoding="utf-8")
        article = (cached / "article.md").read_text(encoding="utf-8")
        if not article.startswith("> 原视频链接："):
            article = f"> 原视频链接：{job.url}\n\n{article}"
        article = repair_article_markdown(article)
        job.output_dir = str(cached)
        job.transcript = transcript
        job.article = article
        job.status = "done"
        job.log(f"复用缓存: {cached}", 100)
        job.log(job.build_summary())
        _auto_notion_upload(job)
        return

    check_cancelled(job)
    job._stage_begin()
    info = fetch_youtube_info(job.url, job)
    job._stage_end("获取视频信息")
    title = info.get("title") or yt_id
    job.title = title
    OUTPUT_DIR.mkdir(exist_ok=True)
    out_dir = OUTPUT_DIR / _output_dir_name(
        sanitize_filename(title), f"{time.strftime('%Y%m%d')}-youtube-{yt_id}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    job.output_dir = str(out_dir)

    job._stage_begin()
    transcript = fetch_youtube_subtitle(job.url, out_dir, job, info)
    if transcript:
        job._stage_end("字幕获取")
        job.log("已获取字幕，跳过音频转写", 70)
    else:
        job._stage_end("字幕获取(无)")
        job._stage_begin()
        audio_path = download_youtube_audio(job.url, out_dir, job, info)
        job._stage_end("音频下载")
        job._stage_begin()
        transcript = transcribe_audio(audio_path, out_dir, job)
        job._stage_end("语音转写")
    transcript_path = out_dir / "transcript.txt"
    transcript_path.write_text(transcript, encoding="utf-8")
    job.transcript = transcript
    job.log("转写完成", 75)

    job._stage_begin()
    if not os.getenv("DEEPSEEK_API_KEY", "").strip():
        raise RuntimeError("未配置 DEEPSEEK_API_KEY，无法生成文章。请在 .env.local 中设置后重试。")
    article = request_deepseek_article(transcript, job)
    job._stage_end("AI 文章生成")

    article_path = out_dir / "article.md"
    article_with_source = f"> 原视频链接：{job.url}\n\n{article}"
    article_path.write_text(article_with_source, encoding="utf-8")
    job.article = article_with_source
    job.status = "done"
    job.log("任务完成", 100)
    job.log(job.build_summary())

    _auto_notion_upload(job)


def _job_article_items(job: dict[str, Any]) -> list[tuple[str, str]]:
    """Return [(out_dir, article)] — one per page for multi-page jobs."""
    page_dirs = list(job.get("page_output_dirs") or [])
    page_articles_list = list(job.get("page_articles") or [])
    if page_dirs and page_articles_list:
        return list(zip(page_dirs, page_articles_list))
    return [(job["output_dir"], job["article"])]


def job_download_files(job: dict[str, Any], fmt: str = "md") -> list[tuple[str, bytes]]:
    """Build download payloads for a finished job's article(s).

    Args:
        job: Job dict (must be owned by the caller — checked in the web layer).
        fmt: "md" | "html" | "pdf".

    Returns a list of (filename, content) tuples — one per page for multi-page
    jobs, a single entry otherwise.  HTML/PDF are rendered to a temp file and
    read back, so nothing is persisted on disk.
    """
    if job["status"] != "done" or not job["article"].strip():
        raise RuntimeError("文章尚未生成，无法下载")

    fmt = fmt.lower()
    if fmt not in ("md", "html", "pdf"):
        raise RuntimeError(f"不支持的下载格式：{fmt}")

    items = _job_article_items(job)
    payloads: list[tuple[str, bytes]] = []
    with tempfile.TemporaryDirectory(prefix="dl-") as tmp:
        for out_dir_item, art_item in items:
            # 输出前统一过一遍格式修复（幂等），旧任务也能得到干净的下载内容
            art_item = repair_article_markdown(art_item)
            stem = Path(out_dir_item).name or "article"
            if fmt == "md":
                payloads.append((f"{stem}.md", art_item.encode("utf-8")))
                continue
            ext = "html" if fmt == "html" else "pdf"
            tmp_path = Path(tmp) / f"{stem}.{ext}"
            if fmt == "html":
                write_article_html(art_item, tmp_path)
            else:
                write_article_pdf(art_item, tmp_path)
            payloads.append((tmp_path.name, tmp_path.read_bytes()))
    return payloads


def upload_job_to_notion(job_id: str, user_id: str) -> dict[str, Any]:
    """Create Notion pages for a finished job's article(s).

    Multi-page jobs become one Notion page per 分P.
    """
    job = _db.get_user_job(user_id, job_id)
    if not job:
        raise RuntimeError("任务不存在")
    if job["status"] != "done" or not job["article"].strip():
        raise RuntimeError("文章尚未生成，无法上传")
    settings = _user_settings(user_id)
    if not notion_is_configured(user_id):
        raise RuntimeError("尚未授权 Notion，请先到设置页完成授权")
    parent_page = str(settings.get("notion_parent_page_id", "")).strip()
    if not parent_page:
        raise RuntimeError("尚未选择 Notion 写入页面，请先到设置页授权并选择页面")
    use_date_subdir = bool(settings.get("date_subdir"))

    links: list[str] = []
    for out_dir_item, art_item in _job_article_items(job):
        art_item = repair_article_markdown(art_item)
        fallback_title = job.get("title") or Path(out_dir_item).name or ""
        result = notion_create_article_page(
            markdown=art_item,
            parent_page_id=parent_page,
            user_id=user_id,
            title=fallback_title,
            date_subdir=use_date_subdir,
        )
        if result.get("status") != "success":
            raise RuntimeError(f"Notion 上传失败：{result.get('message', '')}")
        links.append(result["data"]["url"])

    try:
        _db.update_job_log(
            job_id,
            f"已手动写入 Notion（{len(links)} 页）：" + "、".join(links),
            100,
        )
    except Exception:
        pass
    return {"ok": True, "count": len(links), "links": links}


def write_article_html(article: str, path: Path) -> None:
    """Convert markdown article to a responsive HTML page with LaTeX math
    and Mermaid diagram support.

    KaTeX renders $...$ (inline) and $$...$$ (display) math; ```mermaid
    blocks render as SVG diagrams.  Both libraries load from CDN, so an
    internet connection is needed when opening the page.  Code blocks are
    excluded from math rendering.
    """
    try:
        import markdown as md_lib
    except ImportError:
        raise RuntimeError("缺少 markdown 依赖。请执行：pip install markdown")

    has_mermaid = "```mermaid" in article
    # 先保护数学公式（$$...$$、$...$、\[...\]、\(...\)）与围栏代码块，
    # 避免 Markdown 把公式中的下划线（\mathcal{L}_{aux} 的 _）当成强调语法
    # 转成 <em>，破坏公式并让 KaTeX 无法匹配分隔符；转换后再还原。
    protected, math_segments = _protect_math_segments(article)
    html_body = md_lib.markdown(
        protected,
        output_format="xhtml",
        extensions=["tables", "fenced_code"],
    )
    html_body = _restore_math_segments(html_body, math_segments)
    title = ""
    for line in article.splitlines():
        line = line.strip()
        if line.startswith("# ") and not line.startswith("## "):
            title = line[2:].strip()
            break

    if has_mermaid:
        mermaid_assets = (
            '<script defer src="https://cdn.staticfile.org/mermaid/10.9.3/mermaid.min.js"\n'
            "        onload=\"mermaid.initialize({startOnLoad: false, securityLevel: 'strict', suppressErrorRendering: true});"
            " mermaid.run({querySelector: 'pre code.language-mermaid'}).catch(function () {});\"></script>"
        )
    else:
        mermaid_assets = ""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title or '文章'}</title>
<link rel="stylesheet" href="https://cdn.staticfile.org/KaTeX/0.16.11/katex.min.css">
<script defer src="https://cdn.staticfile.org/KaTeX/0.16.11/katex.min.js"></script>
<script defer src="https://cdn.staticfile.org/KaTeX/0.16.11/contrib/auto-render.min.js"
        onload="renderMathInElement(document.body, {{delimiters: [
            {{left: '\$\$', right: '\$\$', display: true}},
            {{left: '\\\\[', right: '\\\\]', display: true}},
            {{left: '\$', right: '\$', display: false}},
            {{left: '\\\\\\(', right: '\\\\\\)', display: false}},
        ], ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code']}});"></script>
{mermaid_assets}
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC',
                 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;
    font-size: 17px; line-height: 1.85; color: #1a1a1a;
    background: #fafaf8; padding: 24px 16px 60px;
  }}
  article {{
    max-width: 680px; margin: 0 auto; background: #fff;
    padding: 32px 24px; border-radius: 8px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.06);
  }}
  h1 {{ font-size: 1.6em; margin: 1.2em 0 .6em; line-height: 1.4;
        border-bottom: 1px solid #eee; padding-bottom: .3em; }}
  h2 {{ font-size: 1.3em; margin: 1em 0 .5em; line-height: 1.4; }}
  h3 {{ font-size: 1.1em; margin: .8em 0 .4em; }}
  h4 {{ font-size: 1em; margin: .6em 0 .3em; }}
  blockquote {{
    margin: 1em 0; padding: .5em 1em; border-left: 4px solid #fb7299;
    color: #555; background: #fdf6f8; border-radius: 0 4px 4px 0;
  }}
  blockquote p {{ margin: .3em 0; }}
  p {{ margin: .8em 0; }}
  a {{ color: #fb7299; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  ul, ol {{ margin: .6em 0; padding-left: 1.5em; }}
  li {{ margin: .3em 0; }}
  code {{
    background: #f0f0f0; padding: 2px 6px; border-radius: 3px;
    font-size: .9em; font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
  }}
  pre {{
    background: #f5f5f5; padding: 16px; border-radius: 6px;
    overflow-x: auto; margin: 1em 0; font-size: .85em; line-height: 1.6;
  }}
  pre code {{ background: none; padding: 0; }}
  pre:has(> code.language-mermaid) {{ background: #fff; text-align: center; }}
  /* Mermaid 标签在 foreignObject 内继承了文章行高（body 1.85 / pre 1.6），
     比 Mermaid 测量出的标签框更高，多行标签第二行会被裁掉；
     重置标签行高并允许溢出兜底，保证两行文字完整显示。 */
  pre:has(> code.language-mermaid) svg foreignObject {{ overflow: visible; }}
  pre:has(> code.language-mermaid) svg foreignObject div,
  pre:has(> code.language-mermaid) svg foreignObject span,
  pre:has(> code.language-mermaid) svg foreignObject p {{ line-height: 1.3; }}
  hr {{ border: none; border-top: 1px solid #eee; margin: 2em 0; }}
  table {{ width: 100%; border-collapse: collapse; margin: 1em 0; font-size: .9em; }}
  th, td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: left; }}
  th {{ background: #f5f5f5; }}
  /* KaTeX overrides for better inline alignment */
  .katex {{ font-size: 1.05em !important; }}
  .katex-display {{ margin: 1.2em 0; overflow-x: auto; overflow-y: hidden; }}
  .katex-display > .katex {{ font-size: 1.1em !important; }}
  @media (max-width: 500px) {{
    body {{ padding: 8px 4px 40px; font-size: 16px; }}
    article {{ padding: 20px 14px; border-radius: 4px; }}
    .katex-display {{ font-size: .95em; }}
  }}
</style>
</head>
<body>
<article>
{html_body}
</article>
</body>
</html>"""
    path.write_text(html, encoding="utf-8")


def _pdf_has_valid_content(pdf_path: Path, article: str) -> bool:
    """Check if a generated PDF contains the expected Chinese text content.

    Returns False if the PDF is suspiciously small or missing Chinese
    characters, indicating a rendering failure (e.g. the macOS system
    Chrome white-text bug).
    """
    if not pdf_path.exists():
        return False
    file_size = pdf_path.stat().st_size
    # 只拦截明显损坏（几乎为空）的 PDF；短文章的正常 PDF 可能只有几十 KB，
    # 真正的渲染失败由下面的中文字符检查兜底（白字 bug 的 PDF 提取不出文字）。
    if file_size < 8 * 1024:
        return False
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(str(pdf_path))
        text = ""
        for page in reader.pages[:3]:
            text += (page.extract_text() or "")
        # At least some Chinese characters should be present
        chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        return chinese_chars >= 10
    except Exception:
        return True  # If we can't check, assume it's fine


def write_article_pdf(article: str, path: Path) -> None:
    """Convert markdown article to PDF, with LaTeX math support.

    Uses Playwright (headless Chromium) to render a complete HTML page —
    including KaTeX math — and print it to PDF.  Falls back to reportlab
    when Playwright / Chromium is unavailable or produces invalid output.
    """
    try:
        import markdown as md_lib
    except ImportError:
        raise RuntimeError("缺少 markdown 依赖。请执行：pip install markdown")

    # Attempt Playwright first — renders CSS + JS (KaTeX) correctly.
    playwright_ok = False
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout  # noqa: F401
        _write_article_pdf_playwright(article, path, md_lib)
        playwright_ok = _pdf_has_valid_content(path, article)
    except (ImportError, Exception):
        pass  # Fall through to reportlab

    if not playwright_ok:
        # reportlab fallback (no LaTeX math rendering, but tables / headings work).
        _write_article_pdf_reportlab(article, path)


_MATH_PLACEHOLDER = "KATEXMATHSEG{}Z"
_MATH_SEGMENT_RE = re.compile(
    r"```.*?```"        # fenced code block — kept intact, no math inside
    r"|\$\$.+?\$\$"     # $$...$$
    r"|\\\[.+?\\\]"     # \[ ... \]
    r"|\\\(.+?\\\)"     # \( ... \)
    r"|\$[^$\n]+?\$",   # $...$ (single line)
    re.DOTALL,
)


def _protect_math_segments(text: str) -> tuple[str, list[str]]:
    """Stash LaTeX math (and fenced code) behind plain-text placeholders.

    Python-Markdown's backslash escaping would otherwise corrupt LaTeX
    before KaTeX sees it: ``\\[`` becomes ``[`` and ``\\\\`` becomes ``\\``,
    which breaks ``\\[...\\]`` blocks and ``\\\\`` line breaks inside math.
    """
    segments: list[str] = []

    def _stash(match: re.Match) -> str:
        segments.append(match.group(0))
        return _MATH_PLACEHOLDER.format(len(segments) - 1)

    return _MATH_SEGMENT_RE.sub(_stash, text), segments


def _restore_math_segments(html_text: str, segments: list[str]) -> str:
    """Put stashed segments back into the HTML.

    Math segments are HTML-escaped so ``<``, ``>`` and ``&`` inside them
    cannot break the markup.  Fenced code blocks are converted to
    ``<pre><code>`` directly (the placeholder prevented the markdown
    converter from seeing the fences)."""
    import html as html_lib

    for i, seg in enumerate(segments):
        placeholder = _MATH_PLACEHOLDER.format(i)
        if seg.startswith("```"):
            body = seg[3:]
            first_nl = body.find("\n")
            info = body[:first_nl].strip() if first_nl != -1 else ""
            body = body[first_nl + 1:] if first_nl != -1 else ""
            if body.rstrip("\n").endswith("```"):
                body = body.rstrip("\n")[:-3]
            lang_cls = f' class="language-{info}"' if info else ""
            code_html = (
                f"<pre><code{lang_cls}>{html_lib.escape(body.strip(chr(10)), quote=False)}</code></pre>"
            )
            wrapped = f"<p>{placeholder}</p>"
            if wrapped in html_text:
                html_text = html_text.replace(wrapped, code_html)
            else:
                html_text = html_text.replace(placeholder, code_html)
        else:
            html_text = html_text.replace(
                placeholder, html_lib.escape(seg, quote=False)
            )
    return html_text


def _build_pdf_html(article: str, md_lib) -> str:
    """Build a self-contained HTML page for PDF printing (A4, print-friendly).

    KaTeX assets are loaded from the local ``vendor/katex`` directory so PDF
    generation works without network access; falls back to the jsDelivr CDN
    if the local files are missing.  Auto-render is *not* run in the HTML —
    the caller (Playwright) calls renderMathInElement programmatically after
    the page and all scripts have finished loading.
    """
    protected, math_segments = _protect_math_segments(article)
    html_body = md_lib.markdown(
        protected,
        output_format="xhtml",
        extensions=["tables", "fenced_code"],
    )
    html_body = _restore_math_segments(html_body, math_segments)
    title = ""
    for line in article.splitlines():
        line = line.strip()
        if line.startswith("# ") and not line.startswith("## "):
            title = line[2:].strip()
            break

    katex_dir = ROOT / "vendor" / "katex"
    if all((katex_dir / name).exists() for name in ("katex.min.css", "katex.min.js", "auto-render.min.js")):
        katex_css = (katex_dir / "katex.min.css").as_uri()
        katex_js = (katex_dir / "katex.min.js").as_uri()
        katex_auto = (katex_dir / "auto-render.min.js").as_uri()
    else:
        katex_css = "https://cdn.staticfile.org/KaTeX/0.16.11/katex.min.css"
        katex_js = "https://cdn.staticfile.org/KaTeX/0.16.11/katex.min.js"
        katex_auto = "https://cdn.staticfile.org/KaTeX/0.16.11/contrib/auto-render.min.js"

    mermaid_script = ""
    if "```mermaid" in article:
        mermaid_path = ROOT / "vendor" / "mermaid" / "mermaid.min.js"
        if mermaid_path.exists():
            mermaid_script = f'<script src="{mermaid_path.as_uri()}"></script>'
        else:
            mermaid_script = '<script src="https://cdn.staticfile.org/mermaid/10.9.3/mermaid.min.js"></script>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{title or '文章'}</title>
<link rel="stylesheet" href="{katex_css}">
<script src="{katex_js}"></script>
<script src="{katex_auto}"></script>
{mermaid_script}
<style>
  @page {{ size: A4; margin: 20mm 18mm 20mm 18mm; }}
  body {{
    font-family: "PingFang SC", "Microsoft YaHei", "Hiragino Sans GB", "Noto Sans SC", sans-serif;
    font-size: 11pt; line-height: 1.8; color: #1a1a1a;
  }}
  h1 {{ font-size: 1.5em; margin: 1em 0 .5em; border-bottom: 1px solid #ccc; padding-bottom: .3em; }}
  h2 {{ font-size: 1.25em; margin: .9em 0 .4em; }}
  h3 {{ font-size: 1.1em; margin: .7em 0 .3em; }}
  h4 {{ font-size: 1em; margin: .5em 0 .2em; }}
  p {{ margin: .6em 0; }}
  blockquote {{
    margin: .8em 0; padding: .4em 1em; border-left: 4px solid #fb7299;
    color: #555; background: #fdf6f8; border-radius: 0 4px 4px 0;
  }}
  ul, ol {{ margin: .5em 0; padding-left: 1.5em; }}
  li {{ margin: .2em 0; }}
  code {{
    background: #f0f0f0; padding: 1px 5px; border-radius: 3px;
    font-size: .88em; font-family: "SF Mono", "Consolas", monospace;
  }}
  pre {{
    background: #f5f5f5; padding: 12px; border-radius: 4px;
    overflow-x: auto; margin: .8em 0; font-size: .82em; line-height: 1.5;
    white-space: pre-wrap; word-break: break-all;
  }}
  pre code {{ background: none; padding: 0; }}
  pre:has(> code.language-mermaid) {{ background: #fff; text-align: center; }}
  /* Mermaid 标签在 foreignObject 内继承了文章行高（body 1.8 / pre 1.5），
     与 Mermaid 测量标签框用的行高不一致时多行标签第二行会被裁掉；
     重置标签行高并允许溢出兜底。 */
  pre:has(> code.language-mermaid) svg foreignObject {{ overflow: visible; }}
  pre:has(> code.language-mermaid) svg foreignObject div,
  pre:has(> code.language-mermaid) svg foreignObject span,
  pre:has(> code.language-mermaid) svg foreignObject p {{ line-height: 1.3; }}
  hr {{ border: none; border-top: 1px solid #ccc; margin: 1.5em 0; }}
  table {{ width: 100%; border-collapse: collapse; margin: .8em 0; font-size: .85em; }}
  th, td {{ border: 1px solid #ccc; padding: 6px 10px; text-align: left; }}
  th {{ background: #eee; font-weight: 600; }}
  a {{ color: #fb7299; text-decoration: none; }}
  .katex {{ font-size: 1.04em !important; }}
  .katex-display {{ margin: .8em 0; overflow-x: auto; }}
  .katex-display > .katex {{ font-size: 1.06em !important; }}
</style>
</head>
<body>
<div id="content">
{html_body}
</div>
</body>
</html>"""


def _write_article_pdf_playwright(article: str, path: Path, md_lib) -> None:
    """PDF via Playwright headless Chromium — full LaTeX math support.

    After the page + KaTeX scripts finish loading, we programmatically
    call renderMathInElement via page.evaluate(), wait for the KaTeX fonts
    to load, then print to PDF.

    Uses Playwright's bundled Chromium (not system Chrome) because system
    Chrome on macOS has a known bug where Chinese text is rendered as
    invisible white-on-white vector paths in PDF output.
    """
    html = _build_pdf_html(article, md_lib)

    import tempfile
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".html", delete=False
    ) as tmp:
        tmp.write(html)
        html_path = Path(tmp.name)

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            # Always use Playwright's bundled Chromium — system Chrome
            # (channel="chrome") produces PDFs with invisible Chinese text
            # on macOS (text rendered as white vector paths).
            browser = pw.chromium.launch(headless=True)

            page = browser.new_page()
            page.goto(html_path.as_uri(), wait_until="networkidle", timeout=30000)

            # Programmatically render KaTeX math now that external scripts are
            # loaded, then wait for the KaTeX web fonts to finish loading.
            # KaTeX fonts are only requested after renderMathInElement mutates
            # the DOM; printing before they arrive leaves math glyphs blank
            # (invisible) in the PDF.
            page.evaluate("""async () => {
                if (typeof renderMathInElement !== 'undefined') {
                    renderMathInElement(document.getElementById('content'), {
                        delimiters: [
                            {left: '$$', right: '$$', display: true},
                            {left: '\\\\[', right: '\\\\]', display: true},
                            {left: '$', right: '$', display: false},
                            {left: '\\\\(', right: '\\\\)', display: false},
                        ],
                        ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code'],
                    });
                }
                if (typeof mermaid !== 'undefined') {
                    try {
                        mermaid.initialize({startOnLoad: false, securityLevel: 'strict', suppressErrorRendering: true});
                        await mermaid.run({querySelector: 'pre code.language-mermaid'});
                    } catch (e) {}
                }
                document.body.offsetHeight;  // force reflow to trigger font requests
                await document.fonts.ready;
            }""")

            page.pdf(
                path=str(path),
                format="A4",
                margin={"top": "20mm", "right": "18mm", "bottom": "20mm", "left": "18mm"},
            )
            browser.close()
    finally:
        try:
            html_path.unlink(missing_ok=True)
        except Exception:
            pass


def _write_article_pdf_reportlab(article: str, path: Path) -> None:
    """PDF via reportlab — fallback when Playwright is unavailable.

    LaTeX math will NOT be rendered (reportlab has no CSS/JS engine).
    The raw $...$ / $$...$$ markup will appear as plain text in the output.
    """
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            HRFlowable,
            Paragraph,
            SimpleDocTemplate,
            Table,
            TableStyle,
            XPreformatted,
        )
        from reportlab.lib import colors
    except ImportError as exc:
        raise RuntimeError("缺少 PDF 依赖 reportlab。请执行：pip install reportlab") from exc

    try:
        import markdown
    except ImportError:
        raise RuntimeError("缺少 PDF 依赖 markdown。请执行：pip install markdown")

    font_name = register_pdf_font()
    styles = getSampleStyleSheet()
    normal = ParagraphStyle(
        "ArticleBody",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=10.5,
        leading=17,
        spaceAfter=6,
    )
    heading_levels = {
        "h1": ParagraphStyle("H1", parent=normal, fontSize=18, leading=24, spaceBefore=14, spaceAfter=8),
        "h2": ParagraphStyle("H2", parent=normal, fontSize=15, leading=21, spaceBefore=12, spaceAfter=6),
        "h3": ParagraphStyle("H3", parent=normal, fontSize=13, leading=19, spaceBefore=10, spaceAfter=6),
        "h4": ParagraphStyle("H4", parent=normal, fontSize=12, leading=18, spaceBefore=8, spaceAfter=4),
    }
    code_style = ParagraphStyle(
        "CodeBlock",
        parent=normal,
        fontName=font_name,
        fontSize=9,
        leading=14,
        leftIndent=12,
        backColor="#f0f0f0",
        spaceBefore=6,
        spaceAfter=6,
    )
    quote_style = ParagraphStyle(
        "BlockQuote",
        parent=normal,
        leftIndent=20,
        textColor="#555555",
        spaceBefore=6,
        spaceAfter=6,
    )

    html = markdown.markdown(article, output_format="xhtml", extensions=["tables", "fenced_code"])

    from html.parser import HTMLParser

    class Builder(HTMLParser):
        def __init__(self):
            super().__init__()
            self.flowables = []
            self._buf = ""
            self._tag = None
            self._list_items = []
            self._list_ordered = False
            self._table_rows = []
            self._table_header = []
            self._in_table = False
            self._in_thead = False

        def handle_starttag(self, tag, attrs):
            tag = tag.lower()
            if tag in heading_levels:
                self._flush()
                self._tag = tag
            elif tag == "p":
                self._flush()
                self._tag = "p"
            elif tag == "li":
                self._flush()
                self._tag = "li"
            elif tag == "ul":
                self._list_ordered = False
                self._list_items = []
            elif tag == "ol":
                self._list_ordered = True
                self._list_items = []
            elif tag == "hr":
                self._flush()
                self.flowables.append(HRFlowable(width="90%", thickness=0.5, spaceBefore=10, spaceAfter=10))
            elif tag == "br":
                self._buf += "<br/>"
            elif tag in ("strong", "b"):
                self._buf += "<b>"
            elif tag in ("em", "i"):
                self._buf += "<i>"
            elif tag == "code" and self._tag != "pre":
                self._buf += f'<font face="{font_name}">'
            elif tag == "pre":
                self._flush()
                self._tag = "pre"
            elif tag == "blockquote":
                self._flush()
                self._tag = "blockquote"
            elif tag == "table":
                self._in_table = True
                self._table_rows = []
                self._table_header = []
            elif tag == "thead":
                self._in_thead = True
            elif tag == "tbody":
                self._in_thead = False
            elif tag == "tr":
                self._table_current_row = []
            elif tag in ("th", "td"):
                self._buf = ""

        def handle_endtag(self, tag):
            tag = tag.lower()
            if tag in heading_levels or tag in ("p", "li", "pre", "blockquote"):
                self._flush()
                self._tag = None
            elif tag in ("ul", "ol"):
                for i, item_text in enumerate(self._list_items, 1):
                    if self._list_ordered:
                        prefix = f"{i}. "
                    else:
                        prefix = "\u2022 "
                    self.flowables.append(
                        Paragraph(prefix + item_text, normal)
                    )
                self._list_items = []
            elif tag in ("strong", "b"):
                self._buf += "</b>"
            elif tag in ("em", "i"):
                self._buf += "</i>"
            elif tag == "code" and self._tag != "pre":
                self._buf += "</font>"
            elif tag == "table":
                self._in_table = False
                self._build_table()
            elif tag == "tr":
                if hasattr(self, "_table_current_row"):
                    if self._in_thead:
                        self._table_header = self._table_current_row
                    else:
                        self._table_rows.append(self._table_current_row)
                    del self._table_current_row
            elif tag in ("th", "td"):
                if hasattr(self, "_table_current_row"):
                    self._table_current_row.append(self._buf.strip())
                    self._buf = ""

        def _build_table(self):
            if not self._table_rows and not self._table_header:
                return
            header_style = ParagraphStyle(
                "TableHeader",
                parent=normal,
                fontSize=9,
                leading=13,
                alignment=1,
            )
            cell_style = ParagraphStyle(
                "TableCell",
                parent=normal,
                fontSize=9,
                leading=13,
            )
            all_rows = []
            if self._table_header:
                all_rows.append([Paragraph(h, header_style) for h in self._table_header])
            for row in self._table_rows:
                all_rows.append([Paragraph(c, cell_style) for c in row])
            if all_rows:
                col_count = len(all_rows[0])
                available_width = 155 * mm
                col_widths = [available_width / col_count] * col_count
                tbl = Table(all_rows, colWidths=col_widths)
                tbl.setStyle(TableStyle([
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e0e0e0")),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]))
                self.flowables.append(tbl)

        def handle_data(self, data):
            self._buf += data

        def _flush(self):
            text = self._buf.strip()
            self._buf = ""
            if not text:
                return
            if self._tag in heading_levels:
                self.flowables.append(Paragraph(text, heading_levels[self._tag]))
            elif self._tag == "li":
                self._list_items.append(text)
            elif self._tag == "pre":
                # Code blocks may contain <br> (from markdown's &lt;br&gt;
                # entity, which HTMLParser converts back) — reportlab's
                # paraparser rejects them, so restore as newlines.
                text = re.sub(r"<br\s*/?>", "\n", text)
                self.flowables.append(XPreformatted(text, code_style))
            elif self._tag == "blockquote":
                self.flowables.append(Paragraph(text, quote_style))
            else:
                self.flowables.append(Paragraph(text, normal))

        def finish(self):
            self._flush()
            return self.flowables

    builder = Builder()
    builder.feed(html)
    story = builder.finish()

    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )
    doc.build(story)


def register_pdf_font() -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    font_candidates = [
        # macOS fonts
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/System/Library/Fonts/STHeiti Light.ttc"),
        Path("/System/Library/Fonts/STHeiti Medium.ttc"),
        Path("/Library/Fonts/Arial Unicode.ttf"),
        # Windows fonts
        Path("C:/Windows/Fonts/Deng.ttf"),
        Path("C:/Windows/Fonts/NotoSansSC-VF.ttf"),
        Path("C:/Windows/Fonts/simsun.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
    ]
    for font_path in font_candidates:
        if font_path.exists():
            font_name = "LocalChineseFont"
            if font_name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(font_name, str(font_path)))
            return font_name
    return "Helvetica"


def main() -> None:
    """Legacy entry point — use ``python server.py`` for the web service.
    Kept for compatibility with older scripts (start.sh etc. call server.py)."""
    print("提示：请使用 python server.py 启动 Web 服务，并在新终端运行 python worker.py 启动后台任务处理")


if __name__ == "__main__":
    main()

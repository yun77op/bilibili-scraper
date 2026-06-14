from __future__ import annotations

import argparse
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
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


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
ACCESS_LOG = ROOT / "server.log"
BILIBILI_VIEW_API = "https://api.bilibili.com/x/web-interface/view"
BILIBILI_PLAYURL_APIS = [
    "https://api.bilibili.com/x/player/wbi/playurl",
    "https://api.bilibili.com/x/player/playurl",
]
BILIBILI_PLAYER_V2_API = "https://api.bilibili.com/x/player/v2"
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
BILIBILI_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
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
            os.environ.setdefault(key, value)


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
    cookie_string: str = ""
    status: str = "queued"
    stage: str = "等待开始"
    logs: list[str] = field(default_factory=list)
    progress: int = 0
    transcript: str = ""
    article: str = ""
    error: str = ""
    output_dir: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def log(self, message: str, progress: int | None = None) -> None:
        self.logs.append(message)
        self.stage = message
        if progress is not None:
            self.progress = progress
        self.updated_at = time.time()


jobs: dict[str, Job] = {}
jobs_lock = threading.Lock()
job_queue: list[Job] = []
queue_condition = threading.Condition()


def sanitize_filename(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-")
    return value[:80] or "video"


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
        raise RuntimeError(f"命令执行失败，退出码 {code}：{' '.join(args)}")


def fetch_bilibili_view(url: str, job: Job) -> tuple[dict, dict]:
    bvid = extract_bvid(url)
    cookie_string = load_cookie_string(job)
    headers = build_bilibili_headers(url, cookie_string)
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
    play_data = None
    last_error = None
    params = {
        "bvid": bvid,
        "cid": str(cid),
        "fnval": "4048",
        "fourk": "1",
    }
    for api_url in BILIBILI_PLAYURL_APIS:
        try:
            play_data = bilibili_json(api_url, params, headers, "播放地址接口")
            break
        except RuntimeError as exc:
            last_error = exc
    if play_data is None:
        raise RuntimeError(f"无法获取播放地址：{last_error}")

    audio_stream = pick_audio_stream(play_data)
    audio_url = audio_stream.get("baseUrl") or audio_stream.get("base_url")
    if not audio_url:
        raise RuntimeError("播放地址接口没有返回音频 URL。")

    extension = audio_extension(audio_stream, audio_url)
    audio_path = out_dir / f"{sanitize_filename(title)}-{bvid}.{extension}"
    job.log("正在下载音频文件", 35)
    download_file(audio_url, audio_path, headers, job)
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


def load_cookie_string(job: Job) -> str:
    if job.cookie_string.strip():
        return job.cookie_string.strip()
    env_cookie = os.getenv("BILIBILI_COOKIE", "").strip()
    if env_cookie:
        return env_cookie
    cookie_file = os.getenv("BILIBILI_COOKIES_FILE", "").strip()
    if cookie_file:
        return read_netscape_cookie_file(Path(cookie_file))
    return ""


def read_netscape_cookie_file(path: Path) -> str:
    if not path.exists():
        raise RuntimeError(f"Cookie 文件不存在：{path}")
    cookies: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            cookies.append(f"{parts[5]}={parts[6]}")
    return "; ".join(cookies)


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


def pick_audio_stream(play_data: dict[str, Any]) -> dict[str, Any]:
    audios = play_data.get("data", {}).get("dash", {}).get("audio") or []
    if not audios:
        raise RuntimeError("播放地址接口没有返回 DASH 音频流，可能需要完整登录 Cookie。")
    return max(audios, key=lambda item: item.get("bandwidth") or 0)


def audio_extension(audio_stream: dict[str, Any], audio_url: str) -> str:
    mime_type = audio_stream.get("mimeType") or audio_stream.get("mime_type") or ""
    if "mp4" in mime_type:
        return "m4a"
    if "webm" in mime_type:
        return "webm"
    suffix = Path(urllib.parse.urlparse(audio_url).path).suffix.lower().lstrip(".")
    return suffix if suffix in {"m4a", "mp3", "webm", "aac", "flac", "opus"} else "m4a"


def download_file(url: str, path: Path, headers: dict[str, str], job: Job) -> None:
    proxy = os.getenv("BILIBILI_PROXY", "").strip()
    opener = urllib.request.build_opener()
    if proxy:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    request = urllib.request.Request(url, headers={**headers, "Accept": "*/*"}, method="GET")
    try:
        with opener.open(request, timeout=60) as response:
            total = int(response.headers.get("Content-Length") or "0")
            downloaded = 0
            with path.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    output.write(chunk)
                    downloaded += len(chunk)
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


def write_temp_cookie_file(cookie_string: str) -> Path:
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        prefix="bilibili-cookies-",
        suffix=".txt",
        delete=False,
    )
    path = Path(handle.name)
    with handle:
        handle.write("# Netscape HTTP Cookie File\n")
        for name, value in parse_cookie_header(cookie_string).items():
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


def transcribe_with_faster_whisper(audio_path: Path, job: Job) -> str:
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
    device = os.getenv("WHISPER_DEVICE", "cuda")
    compute_type = os.getenv("WHISPER_COMPUTE_TYPE", "float16" if device == "cuda" else "int8")
    job.log(f"正在转写音频，本地模型：{model_name}", 50)
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
        language=os.getenv("TRANSCRIBE_LANGUAGE", "zh"),
        vad_filter=True,
        beam_size=5,
    )

    lines: list[str] = []
    for segment in segments:
        start = format_timestamp(segment.start)
        end = format_timestamp(segment.end)
        lines.append(f"[{start} - {end}] {segment.text.strip()}")
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


def build_article_prompt(transcript: str) -> str:
    return (
        "请把下面的视频转写稿整理成一篇内容详实、结构清晰、图文并茂的中文技术文章。无论原视频或转写稿是什么语言，最终都必须输出中文。\n\n"
        "核心原则——逐信息点覆盖，不得遗漏：\n"
        '原文中出现的每一个事实、数据、概念定义、例子、对比、警告、操作步骤、实践建议，都必须出现在最终文章中。不得因为"信息量太小""不重要""与主题关联不紧"而省略任何信息点。信息密集时用更细的子标题（####）分点组织，宁可文章长，不可遗漏信息。\n\n'
        "格式与结构要求：\n"
        "1. 文章开头生成清晰的标题和副标题，然后列出目录（Markdown 锚点链接）。\n"
        "2. 正文使用多级标题（##、###、####）组织，每个主题独立成节，节与节之间用 `---` 分隔。\n"
        "3. 关键概念、对比信息、参数说明、优缺点等，**必须使用 Markdown 表格**呈现，不要用纯文字罗列。\n"
        "4. 如果原文涉及流程、架构、时序、决策分支等内容，用 ```text 代码块绘制 ASCII 图表来可视化。\n"
        "5. 对原文的核心观点，逐条展开：解释背景、拆解原理、说明应用场景、指出局限性、给出实践建议。不要只做摘要。\n"
        '6. 每个重要结论后用 > 引用块提炼一句"核心要点"。\n'
        "7. 结尾写一段总结，回顾全文核心知识体系，并展望相关方向。\n\n"
        "内容展开原则：\n"
        "8. 保留原视频的所有事实、数据、概念和论证顺序，**不编造**具体人名、机构名、数字或案例。\n"
        "9. 对原文讲得简略的观点，结合常识和领域知识适度展开：补充背景、解释概念、拆解因果关系、说明影响和适用场景。\n"
        '10. 凡是推断性补充，使用"可以理解为""这意味着""从这个角度看"等表达，避免伪装成原文明说。\n'
        "11. 可以加入通俗类比帮助理解，但不要写成原文中出现过的真实案例。\n"
        "12. 去掉口头禅、重复表达和无意义停顿。\n"
        "13. 如果原文是外语，先理解原意再用自然中文改写，专有名词保留原文并附中文解释。\n"
        "14. 如果转写稿里有明显不确定或疑似识别错误的内容，用括号标注（待核对）。\n"
        "15. 每个小节覆盖原文该主题下的所有信息点；信息密集时使用 #### 子标题分点展开，不允许省略任何原文事实或观点。\n\n"
        f"转写稿：\n{transcript}"
    )


def request_deepseek_article(transcript: str, job: Job) -> str:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("缺少 DEEPSEEK_API_KEY，无法调用 DeepSeek 整理文章。")

    model = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是一名资深中文技术编辑，擅长把任何语言的视频转写稿整理成内容详实、有图表有表格、结构清晰的中文技术长文。你善于用表格对比信息，用 ASCII 图表可视化流程，用引用块提炼要点。"},
            {"role": "user", "content": build_article_prompt(transcript)},
        ],
        "stream": False,
        "max_tokens": 32768,
    }
    request = urllib.request.Request(
        DEEPSEEK_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    job.log(f"正在调用 DeepSeek 整理文章：{model}", 80)
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"DeepSeek 接口返回错误：HTTP {exc.code} {body}") from exc

    choices = data.get("choices") or []
    article = ""
    if choices:
        message = choices[0].get("message") or {}
        article = str(message.get("content") or "").strip()
    if not article:
        raise RuntimeError("DeepSeek 返回结果为空。")
    return article


def fallback_article(transcript: str) -> str:
    clean_lines = []
    for line in transcript.splitlines():
        text = re.sub(r"^\[[^\]]+\]\s*", "", line).strip()
        if text:
            clean_lines.append(text)
    body = "\n\n".join(chunk_text("".join(clean_lines), 900))
    return (
        "# 视频内容整理\n\n"
        "## 摘要\n\n"
        "当前未配置大模型接口，以下是基于转写稿生成的基础整理稿，可在配置 DEEPSEEK_API_KEY 后重新生成更完整文章。\n\n"
        "## 正文\n\n"
        f"{body}\n\n"
        "## 备注\n\n"
        "此版本仅做基础清理和分段，没有进行深度改写。"
    )


def chunk_text(text: str, size: int) -> list[str]:
    return [text[index : index + size] for index in range(0, len(text), size)] or [""]


def job_worker() -> None:
    while True:
        with queue_condition:
            while not job_queue:
                queue_condition.wait()
            job = job_queue.pop(0)
        process_job(job)


def process_job(job: Job) -> None:
    with jobs_lock:
        job.status = "running"
        job.log("任务已开始", 5)

    try:
        bvid = extract_bvid(job.url)
        cached = find_cached_output(bvid)
        if cached:
            transcript = (cached / "transcript.txt").read_text(encoding="utf-8")
            article = (cached / "article.md").read_text(encoding="utf-8")
            job.output_dir = str(cached)
            job.transcript = transcript
            job.article = article
            job.status = "done"
            job.log(f"复用缓存: {cached}", 100)
            return

        view_data, headers = fetch_bilibili_view(job.url, job)
        title = view_data.get("data", {}).get("title") or bvid
        dir_name = f"{time.strftime('%Y%m%d')}-{bvid}-{sanitize_filename(title)}"
        OUTPUT_DIR.mkdir(exist_ok=True)
        out_dir = OUTPUT_DIR / dir_name
        out_dir.mkdir(parents=True, exist_ok=True)
        job.output_dir = str(out_dir)

        transcript = fetch_bilibili_subtitle(job.url, out_dir, job, view_data, headers)
        if transcript:
            job.log("已获取字幕，跳过音频转写", 70)
        else:
            audio_path = download_audio(job.url, out_dir, job, view_data, headers)
            ffmpeg_path = shutil.which("ffmpeg")
            transcription_source = audio_path
            if ffmpeg_path:
                transcription_source = convert_for_transcription(audio_path, out_dir, job)
            else:
                job.log("未找到 ffmpeg，直接使用下载的音频进行转写", 35)
            transcript = transcribe_with_faster_whisper(transcription_source, job)
        transcript_path = out_dir / "transcript.txt"
        transcript_path.write_text(transcript, encoding="utf-8")
        job.transcript = transcript
        job.log("转写完成", 75)

        if os.getenv("DEEPSEEK_API_KEY", "").strip():
            article = request_deepseek_article(transcript, job)
        else:
            job.log("未配置 DEEPSEEK_API_KEY，生成基础整理稿", 80)
            article = fallback_article(transcript)

        article_path = out_dir / "article.md"
        article_path.write_text(article, encoding="utf-8")
        job.article = article
        job.status = "done"
        job.log("任务完成", 100)
    except Exception as exc:
        job.status = "error"
        job.error = str(exc)
        tb = traceback.format_exc()
        job.log(f"任务失败：{exc}\n{tb}", job.progress)


class Handler(BaseHTTPRequestHandler):
    server_version = "BilibiliScraper/0.1"

    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/?"):
            self.send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return
        if self.path == "/api/config":
            cfg = load_config()
            self.send_json(
                {
                    "deepseek_configured": bool(os.getenv("DEEPSEEK_API_KEY", "").strip()),
                    "deepseek_model": os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro"),
                    "whisper_device": os.getenv("WHISPER_DEVICE", "cuda"),
                    "pdf_dir": cfg.get("pdf_dir", ""),
                    "auto_save": cfg.get("auto_save", False),
                    "date_subdir": cfg.get("date_subdir", False),
                }
            )
            return
        if self.path == "/app.js":
            self.send_file(STATIC_DIR / "app.js", "application/javascript; charset=utf-8")
            return
        if self.path == "/styles.css":
            self.send_file(STATIC_DIR / "styles.css", "text/css; charset=utf-8")
            return
        if self.path.startswith("/api/jobs/"):
            job_id = self.path.rsplit("/", 1)[-1]
            self.send_json(job_snapshot(job_id))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if self.path == "/api/config":
            length = int(self.headers.get("Content-Length", "0"))
            if length > 0:
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    cfg = load_config()
                    cfg["pdf_dir"] = str(body.get("pdf_dir", "")).strip()
                    cfg["auto_save"] = bool(body.get("auto_save"))
                    cfg["date_subdir"] = bool(body.get("date_subdir"))
                    save_config(cfg)
                    self.send_json({"ok": True})
                    return
                except json.JSONDecodeError:
                    pass
            self.send_json({"ok": False, "error": "无效请求"}, status=HTTPStatus.BAD_REQUEST)
            return

        if self.path.startswith("/api/jobs/") and self.path.endswith("/save-doc"):
            job_id = self.path.split("/")[-2]
            pdf_dir = None
            date_subdir = False
            length = int(self.headers.get("Content-Length", "0"))
            if length > 0:
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    raw = str(body.get("pdf_dir", "")).strip()
                    if raw:
                        pdf_dir = raw
                    date_subdir = bool(body.get("date_subdir"))
                except json.JSONDecodeError:
                    pass
            try:
                self.send_json(save_job_article(job_id, pdf_dir, date_subdir))
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        if self.path != "/api/jobs":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON")
            return

        url = str(payload.get("url", "")).strip()
        if not url.startswith(("http://", "https://")) or "bilibili.com" not in url:
            self.send_error(HTTPStatus.BAD_REQUEST, "请输入有效的 Bilibili URL")
            return

        cookie_string = str(payload.get("cookie", "")).strip()
        job = Job(id=uuid.uuid4().hex[:12], url=url, cookie_string=cookie_string)
        with jobs_lock:
            jobs[job.id] = job
        job.stage = "排队等待"
        job.log("已加入任务队列", 0)
        with queue_condition:
            job_queue.append(job)
            queue_condition.notify()
        self.send_json({"id": job.id})

    def send_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        msg = f"{self.address_string()} - {fmt % args}"
        with open(ACCESS_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")


def job_snapshot(job_id: str) -> dict[str, Any]:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return {"error": "任务不存在"}
        return {
            "id": job.id,
            "url": job.url,
            "status": job.status,
            "stage": job.stage,
            "logs": job.logs[-200:],
            "progress": job.progress,
            "transcript": job.transcript,
            "article": job.article,
            "error": job.error,
            "output_dir": job.output_dir,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "elapsed": int(time.time() - job.created_at) if job.status in ("running", "queued") else 0,
        }


def save_job_article(job_id: str, pdf_dir: str | None = None, date_subdir: bool = False) -> dict[str, Any]:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise RuntimeError("任务不存在")
        if job.status != "done" or not job.article.strip():
            raise RuntimeError("文章尚未生成，无法保存")
        article = job.article
        output_dir = job.output_dir

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    stem = Path(output_dir).name
    md_path = DOCS_DIR / f"{stem}.md"
    pdf_path = DOCS_DIR / f"{stem}.pdf"
    md_path.write_text(article, encoding="utf-8")
    write_article_pdf(article, pdf_path)

    extra_info = ""
    if pdf_dir:
        extra_dir = Path(pdf_dir)
        if date_subdir:
            extra_dir = extra_dir / time.strftime("%Y%m%d")
        extra_dir.mkdir(parents=True, exist_ok=True)
        extra_pdf = extra_dir / f"{stem}.pdf"
        shutil.copy2(pdf_path, extra_pdf)
        extra_info = f"；额外保存到 {extra_pdf}"

    with jobs_lock:
        job = jobs.get(job_id)
        if job:
            job.log(f"文章已保存到 docs：{md_path}；{pdf_path}{extra_info}", 100)

    return {"path": str(md_path), "pdf_path": str(pdf_path)}


def write_article_pdf(article: str, path: Path) -> None:
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
        fontName="Courier",
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

    html = markdown.markdown(article, output_format="xhtml", extensions=["tables"])

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
                self._buf += '<font face="Courier">'
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
                text = text.replace("\n", "<br/>")
                self.flowables.append(Paragraph(text, code_style))
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
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/Deng.ttf"),
        Path("C:/Windows/Fonts/simsun.ttc"),
    ]
    for font_path in font_candidates:
        if font_path.exists():
            font_name = "LocalChineseFont"
            if font_name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(font_name, str(font_path)))
            return font_name
    return "Helvetica"


def main() -> None:
    parser = argparse.ArgumentParser(description="Bilibili 视频转写和文章整理工具")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()

    STATIC_DIR.mkdir(exist_ok=True)
    worker = threading.Thread(target=job_worker, daemon=True)
    worker.start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Listening on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

# 视频转文章

本项目提供一个本地 Web UI：输入 Bilibili 或 YouTube 视频 URL，自动获取字幕或下载音频转写，再整理成文章。

| 平台 | 实现方式 |
|------|----------|
| Bilibili | 直接调用 Bilibili 视频信息和播放地址接口 |
| YouTube | yt-dlp 获取元数据、字幕和音频 |

## 准备环境

需要：

- Python 3.10+
- Python 依赖
- ffmpeg（推荐）：把音频转成 16kHz 单声道 WAV，提升转写兼容性和准确率

### 安装 ffmpeg

**macOS**

```bash
brew install ffmpeg
```

**Windows**

```powershell
winget install ffmpeg
```

安装完成后**重启终端**，验证：

```bash
ffmpeg -version
```

也可以手动下载：
- macOS: `brew install ffmpeg` 或从 https://ffmpeg.org/download.html 下载
- Windows: https://www.gyan.dev/ffmpeg/builds/ → 下载 `ffmpeg-release-essentials.zip`，解压后将 `bin` 目录加入系统 PATH

未安装 ffmpeg 时系统会自动跳过转换步骤，直接用原始音频交给 Whisper。

### Python 依赖

**macOS / Linux**

```bash
./setup.sh
```

**Windows**

```powershell
.\setup.ps1
```

脚本会安装所有依赖，并下载 KaTeX 本地资源（`vendor/katex`）和 Mermaid 本地资源（`vendor/mermaid`），PDF 的数学公式与流程图渲染不依赖外网。如果运行时提示缺少某个依赖，重新执行安装脚本即可。

### Whisper 设备（GPU 加速，可选）

Whisper 默认使用 `auto` 模式，自动选择最佳设备：

- **macOS (Apple Silicon)**：自动使用 CPU（faster-whisper 在 macOS 上通过 MPS/CPU 运行）
- **Windows (NVIDIA GPU)**：如需 CUDA 加速，先安装 CUDA 运行时库，再设置环境变量 `WHISPER_DEVICE=cuda`。安装 CUDA 库：

  ```powershell
  pip install nvidia-cublas-cu12 nvidia-cuda-runtime-cu12 nvidia-cudnn-cu12
  ```

  遇到类似 `Library cublas64_12.dll is not found` 的错误，说明 CUDA 库未正确安装。
- **CPU 模式**（所有平台）：设置 `WHISPER_DEVICE=cpu`

## 配置

复制 `.env.example` 为 `.env.local`，填入配置：

**macOS / Linux (bash)**

```bash
DEEPSEEK_API_KEY=sk-your-api-key
DEEPSEEK_MODEL=deepseek-v4-pro
BILIBILI_PROXY=http://127.0.0.1:7897
HTTP_PROXY=http://127.0.0.1:7897
HTTPS_PROXY=http://127.0.0.1:7897
HF_HUB_DISABLE_XET=1
WHISPER_DEVICE=auto
WHISPER_COMPUTE_TYPE=auto
WHISPER_MODEL=base
TRANSCRIBE_LANGUAGE=zh
```

**Windows (PowerShell)**

```powershell
DEEPSEEK_API_KEY=sk-your-api-key
DEEPSEEK_MODEL=deepseek-v4-pro
BILIBILI_PROXY=http://127.0.0.1:7897
HTTP_PROXY=http://127.0.0.1:7897
HTTPS_PROXY=http://127.0.0.1:7897
HF_HUB_DISABLE_XET=1
WHISPER_DEVICE=auto
WHISPER_COMPUTE_TYPE=auto
WHISPER_MODEL=base
TRANSCRIBE_LANGUAGE=zh
```

`BILIBILI_PROXY` 同时用于 Bilibili API 和 YouTube 访问，所有网络请求（包括模型下载）都会经过该代理。

没有配置 `DEEPSEEK_API_KEY` 时，系统会生成基础整理稿，但不会进行深度改写。

### Whisper 模型

首次运行时会自动从 HuggingFace 下载 Whisper 模型到项目内的 `models/` 目录，避免写入用户目录时权限不足。默认使用 `faster-whisper-base`，可通过 `WHISPER_MODEL` 环境变量切换（如 `small`、`medium`、`large-v3`）。

## Cookie（可选）

### Bilibili

如果视频返回 HTTP 412、需要登录才能下载高清音频，可以在页面的 Cookie 输入框粘贴 Bilibili Cookie。也可以用环境变量：

**macOS / Linux**

```bash
export BILIBILI_COOKIE="SESSDATA=...; bili_jct=...; DedeUserID=..."
export BILIBILI_COOKIES_FILE="/path/to/cookies.txt"
```

**Windows**

```powershell
$env:BILIBILI_COOKIE="SESSDATA=...; bili_jct=...; DedeUserID=..."
$env:BILIBILI_COOKIES_FILE="C:\path\to\cookies.txt"
```

推荐使用 Cookie-Editor 扩展导出 Netscape 格式的 Cookie 文件，确保包含 HttpOnly 的 `SESSDATA`。

### YouTube

YouTube 公开视频一般不需要 Cookie。如果遇到 "Sign in to confirm you're not a bot" 这类错误，需要登录 Cookie：

1. 用 Chrome 登录 youtube.com
2. 用 Cookie-Editor 扩展导出 `youtube.com` 的 Netscape 格式
3. 保存到项目目录 `youtube-cookies.txt`（已加入 `.gitignore`）

系统会自动读取该文件，无需额外配置。也可以用环境变量指定路径：

**macOS / Linux**

```bash
export YOUTUBE_COOKIES_FILE="/path/to/youtube-cookies.txt"
```

**Windows**

```powershell
$env:YOUTUBE_COOKIES_FILE="C:\path\to\youtube-cookies.txt"
```

## 启动

**macOS / Linux**

```bash
python3 app.py
```

**Windows**

```powershell
python app.py
```

打开 http://127.0.0.1:8000

### 脚本管理

**macOS / Linux**

```bash
./restart.sh    # 停止旧进程并后台启动（默认端口 8000）
./stop.sh       # 停止服务
```

也可以通过环境变量指定端口：

```bash
PORT=8080 ./restart.sh
PORT=8080 ./stop.sh
```

**Windows**

```powershell
.\restart.ps1    # 停止旧进程并启动（默认端口 8000）
.\stop.ps1       # 停止服务
```

## 输出

处理结果保存到 `outputs/` 目录，按 `YYYYMMDD-{平台}-{ID}-{标题}` 命名：

- `transcript.txt` — 转写稿（带时间戳）
- `article.md` — DeepSeek 整理后的文章

页面里的"保存到 docs"按钮会把文章另存到 `docs/` 目录，同时生成 `.md` 和 `.pdf`（可在输出设置中把保存格式改为 HTML 或 PDF + HTML；HTML 中的公式和 Mermaid 流程图通过 CDN 渲染，打开时需联网）。

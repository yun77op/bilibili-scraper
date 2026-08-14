# 视频转文章

带用户体系的 Web 服务：输入 Bilibili 或 YouTube 视频 URL，自动获取字幕或下载音频转写，再整理成文章。支持多用户注册登录、任务数据隔离、每用户独立授权 Google Drive 上传。

| 平台 | 实现方式 |
|------|----------|
| Bilibili | 直接调用 Bilibili 视频信息和播放地址接口 |
| YouTube | yt-dlp 获取元数据、字幕和音频 |

## 用户体系

- 开放注册，密码使用 scrypt 哈希存储；登录失败 5 次锁定 15 分钟
- **第一个注册的用户自动成为管理员**，并可看到旧数据（升级前已有任务自动归入管理员）
- 每个用户只能看到、操作自己的任务；文章可在线查看、复制、下载（MD / HTML / PDF），或上传到自己的 Google Drive
- 设置页（`/settings`）为独立页面：Google Drive 授权与上传偏好、YouTube Cookie 为每用户设置；DeepSeek / Whisper 等全局配置仅管理员可见

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
FLASK_SECRET_KEY=    # 会话签名密钥；不填则首次启动自动生成并写入 .env.local
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

1. 在设置页把 Cookie 粘贴到「YouTube 鉴权」输入框并保存（每用户独立），新任务会自动带上
2. 也可以在设置页点击「浏览器登录」，在服务器弹出的浏览器中登录 YouTube 后自动保存 Cookie

也可以用环境变量为整个服务指定一份默认 Cookie：

**macOS / Linux**

```bash
export YOUTUBE_COOKIES_FILE="/path/to/youtube-cookies.txt"
```

**Windows**

```powershell
$env:YOUTUBE_COOKIES_FILE="C:\path\to\youtube-cookies.txt"
```

## Google Drive（可选，每用户独立）

文章可自动或手动上传到 Google Drive：

1. 服务器上放置 OAuth 客户端凭据：从 Google Cloud Console 下载 OAuth 2.0 桌面客户端 JSON，保存为 `~/.gdrive-credentials.json`（所有用户共用这份客户端，token 按用户分开存于 `~/.gdrive-tokens/`）
2. 每个用户在设置页点击「授权 Google Drive」完成自己的 OAuth 授权
3. 设置上传开关、目标文件夹（名称或 ID）、格式（HTML / PDF）与是否按日期分目录；处理完成自动上传，也可在任务列表点 ☁️ 手动上传

## 启动

**macOS / Linux**

```bash
python3 server.py    # Web 服务（Flask + waitress，默认端口 8085）
python3 worker.py    # 后台任务处理（另开终端）
```

**Windows**

```powershell
python server.py
python worker.py
```

打开 http://127.0.0.1:8085，首次使用先注册账号（第一个注册用户为管理员）。

### 脚本管理

**macOS / Linux**

```bash
./restart.sh    # 停止旧进程并后台启动（默认端口 8085）
./stop.sh       # 停止服务
```

也可以通过环境变量指定端口：

```bash
PORT=8080 ./restart.sh
PORT=8080 ./stop.sh
```

**Windows**

```powershell
.\restart.ps1    # 停止旧进程并启动（默认端口 8085）
.\stop.ps1       # 停止服务
```

## 输出

处理过程中的中间文件（音频、字幕、转写稿）保存在 `outputs/` 目录（按 `YYYYMMDD-{平台}-{ID}-{标题}` 命名），同名视频会复用缓存避免重复转写。

最终文章不落盘归档，在任务历史中展开即可：

- 📄 **文章 / 📝 转写稿**：在线查看 + 一键复制
- **下载 MD / HTML / PDF**：浏览器直接下载（多分P任务自动打包为 zip）
- ☁️ **上传到 Google Drive**：手动上传到自己的网盘

## 对外部署

- 建议部署在公网服务器（Linux），用 Nginx / Caddy 等反代到 8085 端口并启用 HTTPS（登录 Cookie 自动带 `Secure` 标志）
- 生产环境建议多线程：`python server.py --host 0.0.0.0 --port 8085 --threads 8`
- 管理员可在设置页底部查看 DeepSeek / Whisper / Worker 状态，并禁用恶意用户
- 升级自旧版（无用户体系）时无需迁移：旧任务数据保留，第一个注册的管理员自动继承

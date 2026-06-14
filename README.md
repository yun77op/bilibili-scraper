# Bilibili 视频转文章

本项目提供一个本地 Web UI：输入 Bilibili 视频 URL，自动解析并下载音频，转写文字，再整理成文章。

下载部分不依赖 `yt-dlp`，而是直接调用 Bilibili 视频信息和播放地址接口。

## 准备环境

需要：

- Python 3.10+
- Python 依赖
- ffmpeg（推荐）：用于把音频转成 16kHz 单声道 WAV，提升转写兼容性和准确率

### 安装 ffmpeg（Windows）

```powershell
winget install ffmpeg
```

安装完成后**重启终端**，验证：

```powershell
ffmpeg -version
```

也可以手动下载：https://www.gyan.dev/ffmpeg/builds/ → 下载 `ffmpeg-release-essentials.zip`，解压后将 `bin` 目录加入系统 PATH。

未安装 ffmpeg 时系统会自动跳过转换步骤，直接用原始音频交给 Whisper。

```powershell
pip install -r requirements.txt
```

Whisper 模型默认缓存到项目内的 `models/` 目录，避免写入用户目录时权限不足。

## Cookie 和代理

如果视频返回 HTTP 412、播放地址为空、或需要登录，可以在页面的 Cookie 输入框粘贴 Bilibili Cookie。

浏览器控制台里的 `document.cookie` 通常不包含 HttpOnly 的 `SESSDATA`，这种 Cookie 不完整。更稳的方法是用 Cookie-Editor 等扩展导出包含 `SESSDATA` 的 Cookie。

也可以设置环境变量：

```powershell
$env:BILIBILI_COOKIE="SESSDATA=...; bili_jct=...; DedeUserID=..."
```

或使用 Netscape 格式 Cookie 文件：

```powershell
$env:BILIBILI_COOKIES_FILE="C:\path\to\cookies.txt"
```

如果当前网络需要代理访问 B 站和下载模型：

```powershell
$env:BILIBILI_PROXY="http://127.0.0.1:7897"
$env:HTTP_PROXY="http://127.0.0.1:7897"
$env:HTTPS_PROXY="http://127.0.0.1:7897"
```

## DeepSeek 配置

配置 `DEEPSEEK_API_KEY` 后会自动调用 DeepSeek 生成文章：

```powershell
$env:DEEPSEEK_API_KEY="你的 API Key"
```

可选配置：

```powershell
$env:DEEPSEEK_MODEL="deepseek-v4-pro"
$env:WHISPER_MODEL="base"
$env:WHISPER_DEVICE="cuda"
$env:WHISPER_COMPUTE_TYPE="float16"
$env:TRANSCRIBE_LANGUAGE="zh"
```

没有配置 `DEEPSEEK_API_KEY` 时，系统会生成基础整理稿，但不会进行深度改写。

## 启动

```powershell
python app.py
```

打开：

```text
http://127.0.0.1:8000
```

处理结果会保存到 `outputs/` 目录，每个任务包含：

- 下载后的音频
- `transcript.txt`
- `article.md`

页面里的“保存到 docs”按钮会把整理后的文章另存到 `docs/` 目录。
保存时会同时生成 Markdown 和 PDF 两个文件。

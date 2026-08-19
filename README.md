# 视频转文章

带用户体系的 Web 服务：输入 Bilibili 或 YouTube 视频 URL，自动获取字幕或下载音频转写，再整理成文章。支持多用户注册登录、任务数据隔离、每用户独立把文章写入自己的 Notion。

| 平台 | 实现方式 |
|------|----------|
| Bilibili | 直接调用 Bilibili 视频信息和播放地址接口 |
| YouTube | yt-dlp 获取元数据、字幕和音频 |

## 用户体系

- 开放注册，密码使用 scrypt 哈希存储；登录失败 5 次锁定 15 分钟
- 支持 **Google 账号一键登录/注册**（首次 Google 登录自动建号；与用户名密码并存）
- **第一个注册的用户自动成为管理员**，并可看到旧数据（升级前已有任务自动归入管理员）
- 每个用户只能看到、操作自己的任务；文章可在线查看、复制、下载（MD / HTML / PDF），或写入自己的 Notion
- 设置页（`/settings`）为独立页面：Notion Integration 与上传偏好、YouTube Cookie 为每用户设置；DeepSeek / Whisper 等全局配置仅管理员可见

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
DEEPSEEK_MODEL=deepseek-v4-flash
BILIBILI_PROXY=http://127.0.0.1:7897
HTTP_PROXY=http://127.0.0.1:7897
HTTPS_PROXY=http://127.0.0.1:7897
HF_HUB_DISABLE_XET=1
WHISPER_DEVICE=auto
WHISPER_COMPUTE_TYPE=auto
WHISPER_MODEL=base
TRANSCRIBE_LANGUAGE=auto
TRANSCRIBE_PROVIDER=
GROQ_API_KEY=
GROQ_WHISPER_MODEL=whisper-large-v3-turbo
FLASK_SECRET_KEY=    # 会话签名密钥；不填则首次启动自动生成并写入 .env.local
```

`BILIBILI_PROXY` 同时用于 Bilibili API 和 YouTube 访问，所有网络请求（包括模型下载）都会经过该代理。

没有配置 `DEEPSEEK_API_KEY` 时，系统会生成基础整理稿，但不会进行深度改写。

配置了 `GROQ_API_KEY` 后，无字幕视频会走 Groq Whisper（默认 `whisper-large-v3-turbo`），失败则回退到本地 faster-whisper。强制本地转写可设 `TRANSCRIBE_PROVIDER=local`。

### Google 登录（可选）

配置后登录/注册页会出现「使用 Google 账号登录」。凭据优先级：

1. 环境变量 `GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET`
2. `GOOGLE_CREDENTIALS_PATH` 指向的 OAuth 客户端 JSON
3. 项目内 `.gdrive-credentials.json`（或 `GDRIVE_CREDENTIALS_PATH`；文件名沿用旧路径）

在 Google Cloud Console → 凭据 → OAuth 客户端 ID（**Web 应用**，桌面应用无法用于公网域名）的 **「授权重定向 URI」** 中增加：

```
https://bilibili-scraper.shuilong.uk/api/auth/google/callback
```

本机调试可用 `http://127.0.0.1:8085/api/auth/google/callback`。Google 对非 localhost 地址要求 HTTPS。OAuth 同意屏幕需包含 `openid`、`email`、`profile`（非敏感 scope）。

生产环境在 `.env.local` 设置 `PUBLIC_BASE_URL=https://bilibili-scraper.shuilong.uk`，确保回调地址不随反代头变化。

未配置凭据时不显示 Google 按钮，用户名密码方式不受影响。用 Google 注册的账号没有登录密码，请继续用 Google 进入。

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

## Notion（可选，每用户独立）

文章可自动或手动写成 Notion 子页面。每个用户走 **OAuth**，授权时自己勾选可访问的页面，**不会**申请整个工作区权限。

服务端（管理员）先配置：

1. 打开 [notion.so/my-integrations](https://www.notion.so/my-integrations) 创建 Integration，类型选 **Public**
2. 在 Integration 的 **Redirect URIs** 中添加：

```
https://bilibili-scraper.shuilong.uk/api/notion/callback
```

本机调试可用 `http://127.0.0.1:8085/api/notion/callback`。

3. 把 OAuth Client ID / Secret 写入 `.env.local`：

```
NOTION_CLIENT_ID=
NOTION_CLIENT_SECRET=
PUBLIC_BASE_URL=https://bilibili-scraper.shuilong.uk
```

每个用户在设置页：

1. 点击「授权 Notion」，在弹窗中登录并勾选要写入的**父页面**（普通页面，不要选 Database）
2. 把该页面链接填到「父页面」并保存；可选开启自动写入和按日期分子页面
3. Access token 存入 `jobs.db` 的 `notion_tokens` 表（按 user_id 分开），接口不会回传给前端
4. 处理完成会自动建页；也可在任务列表点 📝 手动写入。多分 P 任务按集各建一页

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

打开 http://127.0.0.1:8085，首页是公开落地页（介绍产品并引导注册/登录），首次使用先注册账号（第一个注册用户为管理员）。注册登录后自动进入工作台 `/app`。

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
- 📝 **写入 Notion**：手动写成你指定父页面下的子页面

## 知识库（RAG 问答）

顶部导航进入「知识库」（`/kb`），可以用自然语言向全部已生成文章提问，回答基于文章内容生成并标注来源。

- **语料**：`jobs` 表中所有已完成任务的文章 + `outputs/*/article.md` 归档（按内容哈希去重，覆盖已删除任务的文章），自动随新任务更新
- **检索**：纯 Python BM25（中文按单字 + 二元组分词，零第三方依赖、完全离线），弱匹配会被过滤，避免把无关内容喂给模型
- **生成**：DeepSeek Chat 流式接口，回答逐字输出，可展开「深度思考过程」，底部展示参考来源；无相关内容时如实说明并提示知识库主题
- **索引**：持久化在项目根目录 `kb_index.json`；语料变化后首次查询自动重建，也可在页面右上角手动「重建索引」
- **接口**：`GET /api/kb/status`（索引状态）、`POST /api/kb/rebuild`（强制重建）、`POST /api/kb/chat`（SSE 流式问答：`status` / `sources` / `reasoning` / `delta` / `done` / `error` 事件）

## 对外部署

- 建议部署在公网服务器（Linux），用 Nginx / Caddy 等反代到 8085 端口并启用 HTTPS（登录 Cookie 自动带 `Secure` 标志）。反代请转发 `X-Forwarded-Proto` / `X-Forwarded-Host`，以便 Google 登录回调地址为 https
- 首页 `/` 为公开落地页（无需登录），适合直接对外宣传；应用工作台在 `/app`，需登录后才可访问
- 生产环境建议多线程：`python server.py --host 0.0.0.0 --port 8085 --threads 8`
- 管理员可在设置页底部查看 DeepSeek / Whisper / Worker 状态，并禁用恶意用户
- 升级自旧版（无用户体系）时无需迁移：旧任务数据保留，第一个注册的管理员自动继承

# YouTube 下载问题排查（Requested format is not available）

这份文档记录一次 YouTube 视频下载失败的排查与修复过程，方便以后再遇到同类问题时快速定位。

## 1. 现象

提交 YouTube 链接（例：`https://www.youtube.com/watch?v=5yLR_S-8Mns`）下载失败，报错：

```
ERROR: [youtube] <id>: Requested format is not available. Use --list-formats for a list of available formats
```

app 里下载音频用的格式选择器是 `bestaudio/best`（见 `app.py` 的 `download_youtube_audio`）。这条报错的含义是：**yt-dlp 没拿到任何可下载的音视频格式**，所以 `bestaudio/best` 匹配不到。

## 2. 根本原因

用 yt-dlp 直接 `--list-formats` 复测（带代理、带 cookie）后，关键日志是：

```
[debug] [youtube] Found YouTube account cookies
[debug] [youtube] Detected YouTube Premium subscription
WARNING: [youtube] n challenge solving failed: Some formats may be missing.
         Ensure you have a supported JavaScript runtime ...
WARNING: Only images are available for download.
```

结论：

- **cookie 是有效的**（甚至识别出 Premium），不是登录/cookie 失效的问题。
- 真正的原因是 **缺少 JavaScript 运行时**：YouTube 的视频流地址带一个用 JS 混淆的签名（`n` / nsig 参数），yt-dlp 必须执行这段 JS 才能算出有效地址。没有 JS 运行时时，nsig 挑战解不开，所有真正的音视频格式都被丢掉，最后只剩 storyboard 图片（`sb0`~`sb3`）。

排查时的两个误区（记录下来避免重复踩坑）：

- 一开始怀疑是 cookie 过期，但带上 `youtube-cookies.txt` 后日志显示 `Detected YouTube Premium subscription`，证明 cookie 没问题。
- `Requested format is not available` 是表象，不是格式选择器写错，改 `format` / `player_client` 都没用。

## 3. 解决办法

需要两样东西配合：**一个 JS 运行时（Deno）** + **yt-dlp 拉取 EJS 挑战求解脚本**。

### 3.1 安装 Deno

```powershell
winget install --id DenoLand.Deno -e --accept-package-agreements --accept-source-agreements
```

安装后 Deno 会写入用户 PATH（`C:\Users\<用户>\AppData\Local\Microsoft\WinGet\Links`），yt-dlp 会自动检测到。

### 3.2 让 yt-dlp 启用 EJS 求解脚本

EJS（挑战求解脚本）默认是关闭的，必须显式开启。命令行对应 `--remote-components ejs:github`；在 Python API（`yt_dlp.YoutubeDL`）里对应选项：

```python
ydl_opts["remote_components"] = ["ejs:github"]
```

本项目已在 `app.py` 的 `download_youtube_audio` 的 `ydl_opts` 中加上这一项。
（`fetch_youtube_info` 用的是 `extract_info(..., process=False)`，只取元数据、不解析格式，无需改动。）

### 3.3 重启服务

> 重要：当前正在运行的服务进程用的是旧的 PATH，**找不到刚装的 deno**。

必须**新开一个终端**（这样进程才会带上更新后的 PATH），再运行 `restart.ps1` 重启服务，改动才会生效。

## 4. 验证

装好 Deno、加上 `--remote-components ejs:github` 后，命令行复测能拿到真正的格式：

```powershell
& "C:\Users\yun77\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m yt_dlp `
  --list-formats --remote-components ejs:github `
  --proxy "http://127.0.0.1:7897" `
  --cookies "C:\Users\yun77\Documents\bilibili-scraper\youtube-cookies.txt" `
  "https://www.youtube.com/watch?v=5yLR_S-8Mns"
```

输出里出现真实音频格式即为成功（不再只有 storyboard 图片）：

```
140 m4a   audio only   129k   mp4a.40.2   m4a_dash   ← bestaudio 会选这个
251 webm  audio only   115k   opus        webm_dash
243 webm  640x360      vp9    (video)
```

## 5. 一句话总结

`Requested format is not available` ≠ 格式选择器写错，也不一定是 cookie 失效；这次是 **YouTube 的 nsig JS 挑战没人解** → 装 Deno + 开启 `remote_components: ["ejs:github"]` + 新终端重启服务，问题解决。

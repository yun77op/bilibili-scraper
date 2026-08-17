# Bilibili 播放地址接口不返回 DASH 音频流排查记录

## 现象

任务报错：`播放地址接口没有返回 DASH 音频流，可能需要完整登录 Cookie。`

实际触发点：`app.py` 的 `pick_audio_stream()`（已重构为 `pick_play_stream()`）只读取
`data.dash.audio`，为空即抛错。

## 根因（2026 实测确认）

对 `BV1WfMU6iEDa`（闪光少女斯斯和小声比比）做实测：

- **该视频是「专属视频档」（充电专属）**：view 接口返回 `data.is_upower_exclusive = true`
  （另有 `is_upower_exclusive_with_qa`、`is_upower_preview` 等字段）。UP 主开通了
  「30元档包月充电」，**必须开通包月充电才能观看，普通登录 Cookie 无效**。
- **无 Cookie 请求 playurl**（`/x/player/wbi/playurl` 与 `/x/player/playurl` 行为一致）：
  `code=0`，但 `data` 里**只有 `durl`，没有 `dash` 树**。返回的是 15 分钟 29.7MB 的
  低清整段 MP4（`mid=0` 匿名签名，`f=u_0_0`）。
- **带 WBI 签名请求结果相同**：`durl` only，说明不是签名问题，是权限问题。
- 对照组（`BV1GJ411x7h7`，普通公开视频）：无 Cookie 也能返回 3 条 DASH 音频流，
  所以大部分任务之前都正常。

另外发现：**部署环境完全没有配置任何 Bilibili Cookie**——`config.json` 没有
`bilibili_cookie` 键、`.env.local` 没有 `BILIBILI_COOKIE`、任务也全部是 `cookie=0ch`。
之前约 20 个任务成功纯属因为那些视频本身不需要登录。

### 需要什么权限的视频

| 类型 | view 接口标记 | 解锁条件 |
|---|---|---|
| 普通公开视频 | — | 匿名即可，DASH 正常 |
| 仅登录可见 | 无显式标记（playurl 匿名只回 durl） | 任意登录 Cookie（含 SESSDATA） |
| **充电专属（专属视频档）** | `is_upower_exclusive = true` | **开通 UP 主包月充电**（如 30 元/月），普通登录无效 |
| 大会员专享 | — | 大会员账号 |

## 修复内容

1. **durl 降级**：`pick_play_stream()` 在 DASH 音频缺失时回退到 `data.durl[0]`，
   下载整段视频流后用 ffmpeg 提取音频（无 ffmpeg 时 faster-whisper 可直接解码 MP4/FLV）。
   任务不再因此失败，仅日志提示音质可能低于 DASH；若识别到充电专属视频会明确提示。
2. **充电专属识别**：通过 view 接口的 `is_upower_exclusive` 标记识别充电专属视频，
   报错和日志直接说明「需开通 UP 主包月充电」。
3. **备份地址**：`download_file()` 支持 `backup_url`/`backupUrl` 列表，主地址失败自动切换。
4. **错误信息更准确**：区分「无数据」「有 durl 但无 dash」「两者皆无」，并提示是否已配置 Cookie。

> 注：曾尝试加入「设置页 Bilibili Cookie 配置」，经确认对本项目价值有限
> （转写用 durl 低清即可，充电专属视频配了也没用），已按用户要求移除，
> 只保留服务器侧既有配置渠道（`BILIBILI_COOKIE` 环境变量 / config.json `bilibili_cookie`）。

## 用户侧操作建议

- 转写用途**无需配置 Cookie**：公开视频匿名即可拿 DASH；仅登录可见/充电专属视频
  走 durl 降级也能完整转写（音质略低）。
- 若确需解锁「仅登录可见」视频的高音质，可在服务器配置 `BILIBILI_COOKIE`
  环境变量或 `config.json` 的 `bilibili_cookie`（需包含 SESSDATA，且注意 SESSDATA 会过期）。
- **充电专属（专属视频档）视频必须开通 UP 主的包月充电**（例如 30 元/月），
  普通登录 Cookie 无法解锁；未开通时只能走 durl 低清降级路径完成转写。

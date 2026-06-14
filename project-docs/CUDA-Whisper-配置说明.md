# CUDA Whisper 配置说明

这份文档记录当前项目在 Windows 环境下为 `faster-whisper` 启用 CUDA 的实际配置过程，目的是让本地转写优先走 GPU，而不是退回 CPU。

## 1. 当前项目结论

本项目已经按下面这套方式配置完成：

- Python 使用 Codex 自带运行时：
  - `C:\Users\yun77\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`
- Whisper 后端使用 `faster-whisper`
- 当前默认设备：
  - `WHISPER_DEVICE=cuda`
  - `WHISPER_COMPUTE_TYPE=float16`
- 模型下载目录改为项目本地：
  - `models\faster-whisper\`

这样做的原因有两个：

- 避免 Hugging Face 默认缓存目录的权限问题
- 避免 `cublas64_12.dll` 之类的 CUDA 动态库找不到

## 2. 为什么本机有 GPU 还会报错

这次遇到的报错是：

- `Library cublas64_12.dll is not found or cannot be loaded`

这不代表机器没有 NVIDIA GPU，而是说明当前 Python 运行环境找不到 `faster-whisper` 依赖的 CUDA 运行库。

常见原因：

- 系统里虽然装了显卡驱动，但当前 Python 环境没有对应的 CUDA 运行时包
- 动态库存在，但不在进程的 DLL 搜索路径里
- Whisper 模型缓存目录落在受限目录，下载或解包时被拒绝访问

## 3. 这次实际做了什么

### 3.1 安装 CUDA 运行时相关 Python 包

在当前项目使用的 Python 环境里安装了下面几个包：

- `nvidia-cuda-runtime-cu12`
- `nvidia-cublas-cu12`
- `nvidia-cudnn-cu12`

如果你后面需要重新安装，可以用这个命令：

```powershell
& "C:\Users\yun77\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m pip install nvidia-cuda-runtime-cu12 nvidia-cublas-cu12 nvidia-cudnn-cu12
```

这些包会把 CUDA 相关 DLL 放进当前 Python 环境的 `site-packages` 下。

### 3.2 在程序启动时补充 DLL 搜索路径

项目代码里已经加了 CUDA DLL 路径配置逻辑。启动转写前会：

- 尝试定位上述 NVIDIA 包的 DLL 目录
- 调用 `os.add_dll_directory(...)`
- 同时补进 `PATH`

这样 `ctranslate2` / `faster-whisper` 在加载时就能找到缺失的 `cublas`、`cudnn` 等库。

### 3.3 不再使用 Hugging Face 默认缓存目录

之前报过两类权限错误：

- `拒绝访问 C:\Users\yun77\.cache\huggingface`
- `Permission denied ... dummy_file_src`

所以现在模型下载目录已经切到项目本地：

```text
models\faster-whisper\Systran-faster-whisper-base
```

这样做更稳定，也方便迁移和排查。

### 3.4 默认改为 GPU 推理

本地配置文件 [`.env.local`](C:/Users/yun77/Documents/bilibili-scraper/.env.local) 里已经写入：

```env
WHISPER_DEVICE=cuda
WHISPER_COMPUTE_TYPE=float16
HF_HUB_DISABLE_XET=1
```

其中：

- `cuda` 表示优先使用 GPU
- `float16` 是常见的 CUDA 推理精度配置
- `HF_HUB_DISABLE_XET=1` 用来避免某些 Hugging Face 下载路径上的兼容问题

## 4. 当前本地配置文件

项目现在会在启动时自动读取 [`.env.local`](C:/Users/yun77/Documents/bilibili-scraper/.env.local)。

当前与 CUDA / 转写相关的配置是：

```env
WHISPER_DEVICE=cuda
WHISPER_COMPUTE_TYPE=float16
HF_HUB_DISABLE_XET=1
HTTP_PROXY=http://127.0.0.1:7897
HTTPS_PROXY=http://127.0.0.1:7897
BILIBILI_PROXY=http://127.0.0.1:7897
```

如果网络环境变化，可以按需改代理；如果想强制退回 CPU，可以改成：

```env
WHISPER_DEVICE=cpu
WHISPER_COMPUTE_TYPE=int8
```

## 5. 如何验证 CUDA 已经生效

### 方法一：看服务配置接口

启动服务后访问：

- [http://127.0.0.1:8000/api/config](http://127.0.0.1:8000/api/config)

如果返回里有：

```json
{
  "whisper_device": "cuda"
}
```

说明当前服务读取到的是 GPU 配置。

### 方法二：直接加载模型测试

可以用下面的命令做最小验证：

```powershell
& "C:\Users\yun77\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -c "from faster_whisper import WhisperModel; WhisperModel(r'models/faster-whisper/Systran-faster-whisper-base', device='cuda', compute_type='float16', local_files_only=True); print('ok')"
```

如果输出 `ok`，通常说明：

- 模型目录可读
- CUDA 相关 DLL 可加载
- `faster-whisper` 能正常初始化 GPU 后端

## 6. 如果后面又报 CUDA 相关错误，按这个顺序排查

1. 先看 `http://127.0.0.1:8000/api/config`，确认当前服务确实读到了 `whisper_device=cuda`
2. 确认不是旧的 `8000` 端口进程还在占用
3. 确认当前 Python 环境里还装着这三个包：
   - `nvidia-cuda-runtime-cu12`
   - `nvidia-cublas-cu12`
   - `nvidia-cudnn-cu12`
4. 确认模型目录在项目本地，没有又落回用户缓存目录
5. 如果仍有问题，先临时切回 CPU，保证功能可用：

```env
WHISPER_DEVICE=cpu
WHISPER_COMPUTE_TYPE=int8
```

## 7. 备注

这个项目里，“有 GPU” 不等于“Python 里的 Whisper 能直接走 GPU”。真正决定是否可用的是：

- Python 环境里有没有可用的 CUDA 运行库
- 程序有没有把 DLL 路径暴露给当前进程
- 模型目录是否有写权限

这次已经把这三个点都补上了，所以当前版本默认会优先使用 CUDA 转写。

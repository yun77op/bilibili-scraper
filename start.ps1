param(
    [int]$Port = 8085,
    [string]$HostAddr = "127.0.0.1",
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Continue"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidDir = Join-Path $scriptDir ".pids"
$logsDir = Join-Path $scriptDir "logs"
$serverLog = Join-Path $logsDir "server.log"
$serverErrLog = Join-Path $logsDir "server.err.log"
$workerLog = Join-Path $logsDir "worker.log"
$workerErrLog = Join-Path $logsDir "worker.err.log"

. (Join-Path $scriptDir "python-detect.ps1")

Write-Host "=== 视频转文章 — 启动 ===" -ForegroundColor Cyan

# 1. Stop existing processes
Write-Host "[1/3] 停止已有进程..." -ForegroundColor Gray
& "$scriptDir\stop.ps1" -Port $Port 2>$null
Start-Sleep -Seconds 1

# 2. Detect Python (shared logic: py launcher -> PATH -> common locations)
$PythonPath = Find-Python -Override $PythonPath
if (-not $PythonPath) {
    Write-Host "ERROR: Python not found. Install Python 3.10+ or pass -PythonPath C:\path\to\python.exe" -ForegroundColor Red
    exit 1
}

Write-Host "Python: $PythonPath" -ForegroundColor Gray

# 3. Create PID and log directories
New-Item -ItemType Directory -Force -Path $pidDir | Out-Null
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

# 4. Start server
Write-Host "[2/3] 启动 HTTP 服务（端口 $Port）..." -ForegroundColor Gray
$serverProcess = Start-Process -FilePath $PythonPath `
    -ArgumentList (Join-Path $scriptDir "server.py"), "--host", $HostAddr, "--port", $Port `
    -NoNewWindow -PassThru `
    -RedirectStandardOutput $serverLog `
    -RedirectStandardError $serverErrLog

$serverProcess.Id | Out-File -FilePath (Join-Path $pidDir "server.pid") -NoNewline

# Wait for server to be ready
$ready = $false
for ($i = 0; $i -lt 10; $i++) {
    try {
        $response = Invoke-WebRequest -Uri "http://${HostAddr}:${Port}/api/config" -TimeoutSec 2 -ErrorAction SilentlyContinue
        if ($response.StatusCode -eq 200) {
            $ready = $true
            break
        }
    } catch { }
    if ($serverProcess.HasExited) {
        Write-Host "ERROR: Server failed to start (check $serverLog)" -ForegroundColor Red
        exit 1
    }
    Start-Sleep -Seconds 1
}

if (-not $ready -and $serverProcess.HasExited) {
    Write-Host "ERROR: Server process died (check $serverLog)" -ForegroundColor Red
    exit 1
}

Write-Host "  √ HTTP 服务已启动 (PID: $($serverProcess.Id))" -ForegroundColor Green

# 5. Start worker
Write-Host "[3/3] 启动后台 Worker..." -ForegroundColor Gray
$workerProcess = Start-Process -FilePath $PythonPath `
    -ArgumentList (Join-Path $scriptDir "worker.py") `
    -NoNewWindow -PassThru `
    -RedirectStandardOutput $workerLog `
    -RedirectStandardError $workerErrLog

$workerProcess.Id | Out-File -FilePath (Join-Path $pidDir "worker.pid") -NoNewline

Start-Sleep -Seconds 1
if (-not $workerProcess.HasExited) {
    Write-Host "  √ Worker 已启动 (PID: $($workerProcess.Id))" -ForegroundColor Green
} else {
    Write-Host "  ! Worker 进程已退出，查看日志: $workerLog" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
Write-Host "  Web UI:  http://${HostAddr}:${Port}" -ForegroundColor White
Write-Host "  Server log: $serverLog" -ForegroundColor Gray
Write-Host "  Server err: $serverErrLog" -ForegroundColor Gray
Write-Host "  Worker log: $workerLog" -ForegroundColor Gray
Write-Host "  Worker err: $workerErrLog" -ForegroundColor Gray
Write-Host "  停止服务:   .\stop.ps1" -ForegroundColor Gray
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan

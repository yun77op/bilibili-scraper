param(
    [int]$Port = 8000,
    [string]$HostAddr = "127.0.0.1"
)

$ErrorActionPreference = "Stop"

Write-Host "=== bilibili-scraper restart ===" -ForegroundColor Cyan

# 1. 停止监听端口的进程
$connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1

if ($connection -and $connection.OwningProcess -ne 0) {
    $pidToKill = $connection.OwningProcess
    $process = Get-Process -Id $pidToKill -ErrorAction SilentlyContinue
    if ($process) {
        Write-Host "Stopping: $($process.ProcessName) (PID: $pidToKill) on port $Port" -ForegroundColor Yellow
        Stop-Process -Id $pidToKill -Force -ErrorAction SilentlyContinue
    }
}

# 2. 等待端口释放
$retries = 0
while ($retries -lt 10) {
    Start-Sleep -Seconds 1
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $conn -or $conn.OwningProcess -eq 0) {
        break
    }
    Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
    $retries++
}

if ($retries -ge 10) {
    Write-Host "Port $Port still in use, starting anyway..." -ForegroundColor Yellow
}

# 3. 启动
Write-Host "Starting: python app.py --host $HostAddr --port $Port" -ForegroundColor Green
$pythonPath = "C:\Users\yun77\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
Start-Process -NoNewWindow -FilePath $pythonPath -ArgumentList "app.py", "--host", $HostAddr, "--port", $Port
Write-Host "Server running on http://${HostAddr}:${Port}" -ForegroundColor Cyan

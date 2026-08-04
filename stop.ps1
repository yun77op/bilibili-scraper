param(
    [int]$Port = 8000,
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Continue"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidDir = Join-Path $scriptDir ".pids"

function Stop-ByPidFile {
    param([string]$PidFile, [string]$Name)
    if (Test-Path $PidFile) {
        $pid = Get-Content $PidFile -Raw 2>$null
        if ($pid) {
            $pid = $pid.Trim()
            $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
            if ($proc) {
                Write-Host "Stopping $Name (PID: $pid)" -ForegroundColor Yellow
                Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
            }
        }
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    }
}

# Stop by PID files
Stop-ByPidFile (Join-Path $pidDir "server.pid") "server"
Stop-ByPidFile (Join-Path $pidDir "worker.pid") "worker"

# Fallback: stop anything on the server port
$conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($conn -and $conn.OwningProcess -ne 0) {
    $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
    if ($proc) {
        Write-Host "Stopping process on port $Port: $($proc.ProcessName) (PID: $($proc.Id))" -ForegroundColor Yellow
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    }
}

# Fallback: stop any orphan worker.py processes
Get-Process -Name "python*" -ErrorAction SilentlyContinue | ForEach-Object {
    try {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $($_.Id)").CommandLine
        if ($cmd -match "worker\.py") {
            Write-Host "Stopping orphan worker (PID: $($_.Id))" -ForegroundColor Yellow
            Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        }
    } catch { }
}

Write-Host "All stopped." -ForegroundColor Green

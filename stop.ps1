param(
    [int]$Port = 8000
)

$connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1

if (-not $connection) {
    Write-Host "No process listening on port $Port" -ForegroundColor Gray
    exit 0
}

$pidToKill = $connection.OwningProcess
if ($pidToKill -eq 0) {
    Write-Host "Port $Port held by system idle process. Use: netstat -ano | findstr :$Port" -ForegroundColor Red
    exit 1
}

$process = Get-Process -Id $pidToKill -ErrorAction SilentlyContinue
if ($process) {
    Write-Host "Stopping $($process.ProcessName) (PID: $pidToKill) on port $Port" -ForegroundColor Yellow
    Stop-Process -Id $pidToKill -Force
    Write-Host "Stopped." -ForegroundColor Green
} else {
    Write-Host "Process not found." -ForegroundColor Red
}

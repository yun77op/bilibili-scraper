param(
    [int]$Port = 8085,
    [string]$HostAddr = "127.0.0.1"
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "=== 视频转文章 — 重启 ===" -ForegroundColor Cyan
& "$scriptDir\stop.ps1" -Port $Port
Write-Host ""
& "$scriptDir\start.ps1" -Port $Port -HostAddr $HostAddr

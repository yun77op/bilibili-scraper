param(
    [string]$PythonPath = "C:\Users\yun77\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$reqFile = Join-Path $scriptDir "requirements.txt"

Write-Host "=== setup: install deps ===" -ForegroundColor Cyan
Write-Host "Python: $PythonPath" -ForegroundColor Gray
Write-Host "Requirements: $reqFile" -ForegroundColor Gray

if (-not (Test-Path $PythonPath)) {
    Write-Host "ERROR: Python not found at $PythonPath" -ForegroundColor Red
    Write-Host "Edit `$PythonPath in this script or use the -PythonPath parameter."
    exit 1
}

Write-Host "Installing..." -ForegroundColor Yellow
& $PythonPath -m pip install -r $reqFile

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: pip install failed (exit code $LASTEXITCODE)" -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Downloading Whisper model (base)..." -ForegroundColor Yellow
$env:HF_HOME = "models/huggingface"
$env:HUGGINGFACE_HUB_CACHE = "models/huggingface/hub"
$env:HF_HUB_DISABLE_XET = "1"

$modelName = if ($env:WHISPER_MODEL) { $env:WHISPER_MODEL } else { "base" }
if ($modelName -match "/") {
    $repoId = $modelName
} else {
    $repoId = "Systran/faster-whisper-$modelName"
}
$target = "models/faster-whisper/$($repoId -replace '/', '-')"
$required = @("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")

$allExist = $true
foreach ($f in $required) {
    $p = Join-Path $target $f
    if (-not (Test-Path $p) -or (Get-Item $p).Length -eq 0) {
        $allExist = $false
        break
    }
}

if ($allExist) {
    Write-Host "Model already exists: $target" -ForegroundColor Gray
} else {
    Write-Host "Downloading $repoId -> $target ..." -ForegroundColor Gray
    & $PythonPath -c @"
import os
os.environ.setdefault('HF_HOME', 'models/huggingface')
os.environ.setdefault('HUGGINGFACE_HUB_CACHE', 'models/huggingface/hub')
os.environ.setdefault('HF_HUB_DISABLE_XET', '1')
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id='$repoId',
    local_dir='$target',
    cache_dir='models/hf-download-cache',
    allow_patterns=['config.json', 'model.bin', 'tokenizer.json', 'vocabulary.txt'],
    max_workers=1,
)
print('Model download complete.')
"@
    if ($LASTEXITCODE -ne 0) {
        Write-Host "WARNING: Model download failed (exit code $LASTEXITCODE). Will retry on first run." -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "Done." -ForegroundColor Green

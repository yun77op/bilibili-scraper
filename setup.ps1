param(
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$reqFile = Join-Path $scriptDir "requirements.txt"

. (Join-Path $scriptDir "python-detect.ps1")

# Ensure the execution policy allows running local .ps1 scripts.
# CurrentUser scope only - no admin needed. If policy is Restricted/Undefined,
# set RemoteSigned so setup/start/stop/restart.ps1 all work from now on.
Write-Host "=== setup: execution policy ===" -ForegroundColor Cyan
$curPolicy = Get-ExecutionPolicy -Scope CurrentUser -ErrorAction SilentlyContinue
if ($curPolicy -in @('Restricted', 'Undefined')) {
    Write-Host "CurrentUser policy is '$curPolicy' - setting to RemoteSigned (local scripts allowed, no admin needed)..." -ForegroundColor Yellow
    try {
        Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned -Force -ErrorAction Stop
    } catch {
        # In a session launched with -ExecutionPolicy <x> (e.g. Bypass), the
        # cmdlet throws "ExecutionPolicyOverride" even though the registry
        # value IS updated - verify the actual result below instead.
    }
    $newPolicy = Get-ExecutionPolicy -Scope CurrentUser
    if ($newPolicy -eq 'RemoteSigned') {
        Write-Host "Execution policy (CurrentUser) set to RemoteSigned." -ForegroundColor Green
    } else {
        Write-Host "WARNING: failed to set execution policy (still '$newPolicy'); run manually:" -ForegroundColor Yellow
        Write-Host "         Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned" -ForegroundColor Yellow
    }
} else {
    Write-Host "Execution policy (CurrentUser): $curPolicy (OK)" -ForegroundColor Gray
}
$effectivePolicy = Get-ExecutionPolicy
if ($effectivePolicy -eq 'Restricted') {
    Write-Host "WARNING: effective policy is still Restricted (usually set by group policy);" -ForegroundColor Yellow
    Write-Host "         other .ps1 scripts may still be blocked." -ForegroundColor Yellow
}

$PythonPath = Find-Python -Override $PythonPath
if (-not $PythonPath) {
    Write-Host ""
    Write-Host "ERROR: Python not found. Install Python 3.10+ first (e.g. 'winget install Python.Python.3.12')," -ForegroundColor Red
    Write-Host "       or pass the interpreter path explicitly: .\setup.ps1 -PythonPath C:\path\to\python.exe" -ForegroundColor Red
    exit 1
}

# Native commands (pip/curl/tar) write to stderr on failure; in PS 5.1 "Stop"
# would turn that into a terminating error. We rely on $LASTEXITCODE instead.
$ErrorActionPreference = "Continue"

Write-Host "=== setup: install deps ===" -ForegroundColor Cyan
Write-Host "Python: $PythonPath" -ForegroundColor Gray
Write-Host "Requirements: $reqFile" -ForegroundColor Gray

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
Write-Host "Downloading KaTeX assets (for PDF math rendering)..." -ForegroundColor Yellow
$katexVersion = if ($env:KATEX_VERSION) { $env:KATEX_VERSION } else { "0.16.11" }
$katexDir = Join-Path $scriptDir "vendor\katex"

$katexOk = (Test-Path (Join-Path $katexDir "katex.min.css")) -and
          (Test-Path (Join-Path $katexDir "katex.min.js")) -and
          (Test-Path (Join-Path $katexDir "auto-render.min.js")) -and
          ((Get-ChildItem (Join-Path $katexDir "fonts") -Filter "*.woff2" -ErrorAction SilentlyContinue | Measure-Object).Count -gt 0)

if ($katexOk) {
    Write-Host "KaTeX assets already exist: $katexDir" -ForegroundColor Gray
} else {
    $tmp = Join-Path $env:TEMP ("katex-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $tmp -Force | Out-Null
    $downloaded = $false
    foreach ($url in @(
        "https://registry.npmmirror.com/katex/-/katex-$katexVersion.tgz",
        "https://registry.npmjs.org/katex/-/katex-$katexVersion.tgz"
    )) {
        Write-Host "Trying $url" -ForegroundColor Gray
        & curl.exe -fsSL --connect-timeout 8 --max-time 120 $url -o (Join-Path $tmp "katex.tgz") 2>$null
        if ($LASTEXITCODE -eq 0) { $downloaded = $true; break }
    }
    if ($downloaded) {
        # --force-local: bsdtar would otherwise treat "C:\..." as a remote host
        & tar.exe --force-local -xzf (Join-Path $tmp "katex.tgz") -C $tmp 2>$null
        if ($LASTEXITCODE -eq 0) {
            New-Item -ItemType Directory -Path (Join-Path $katexDir "fonts") -Force | Out-Null
            Copy-Item (Join-Path $tmp "package\dist\katex.min.css") $katexDir
            Copy-Item (Join-Path $tmp "package\dist\katex.min.js") $katexDir
            Copy-Item (Join-Path $tmp "package\dist\contrib\auto-render.min.js") $katexDir
            Copy-Item (Join-Path $tmp "package\dist\fonts\*.woff2") (Join-Path $katexDir "fonts")
            Write-Host "KaTeX $katexVersion installed to $katexDir" -ForegroundColor Green
        } else {
            Write-Host "ERROR: failed to extract KaTeX $katexVersion." -ForegroundColor Red
            exit 1
        }
    } else {
        Write-Host "ERROR: failed to download KaTeX $katexVersion from all mirrors." -ForegroundColor Red
        exit 1
    }
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "Downloading Mermaid assets (for diagram rendering)..." -ForegroundColor Yellow
$mermaidVersion = if ($env:MERMAID_VERSION) { $env:MERMAID_VERSION } else { "10.9.3" }
$mermaidDir = Join-Path $scriptDir "vendor\mermaid"

if (Test-Path (Join-Path $mermaidDir "mermaid.min.js")) {
    Write-Host "Mermaid assets already exist: $mermaidDir" -ForegroundColor Gray
} else {
    New-Item -ItemType Directory -Path $mermaidDir -Force | Out-Null
    $downloaded = $false
    foreach ($url in @(
        "https://registry.npmmirror.com/mermaid/-/mermaid-$mermaidVersion.tgz",
        "https://registry.npmjs.org/mermaid/-/mermaid-$mermaidVersion.tgz"
    )) {
        $tmp = Join-Path $env:TEMP ("mermaid-" + [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $tmp -Force | Out-Null
        $tgz = Join-Path $tmp "mermaid.tgz"
        Write-Host "Trying $url" -ForegroundColor Gray
        & curl.exe -fsSL --connect-timeout 8 --max-time 180 $url -o $tgz 2>$null
        if ($LASTEXITCODE -eq 0) {
            & tar.exe --force-local -xzf $tgz -C $tmp 2>$null
            if ($LASTEXITCODE -eq 0 -and (Test-Path (Join-Path $tmp "package\dist\mermaid.min.js"))) {
                Copy-Item (Join-Path $tmp "package\dist\mermaid.min.js") $mermaidDir
                $downloaded = $true
            }
        }
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
        if ($downloaded) { break }
    }
    if ($downloaded) {
        Write-Host "Mermaid $mermaidVersion installed to $mermaidDir" -ForegroundColor Green
    } else {
        Write-Host "WARNING: failed to download Mermaid $mermaidVersion; diagram rendering will fall back to CDN." -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "Downloading Vue 3 (for web UI)..." -ForegroundColor Yellow
$vueVersion = if ($env:VUE_VERSION) { $env:VUE_VERSION } else { "3.5.13" }
$vueDir = Join-Path $scriptDir "static\vendor\vue"
$vueFile = Join-Path $vueDir "vue.esm-browser.prod.js"

if (Test-Path $vueFile) {
    Write-Host "Vue assets already exist: $vueDir" -ForegroundColor Gray
} else {
    New-Item -ItemType Directory -Path $vueDir -Force | Out-Null
    $downloaded = $false
    foreach ($url in @(
        "https://registry.npmmirror.com/vue/-/vue-$vueVersion.tgz",
        "https://registry.npmjs.org/vue/-/vue-$vueVersion.tgz"
    )) {
        $tmp = Join-Path $env:TEMP ("vue-" + [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $tmp -Force | Out-Null
        $tgz = Join-Path $tmp "vue.tgz"
        Write-Host "Trying $url" -ForegroundColor Gray
        & curl.exe -fsSL --connect-timeout 8 --max-time 180 $url -o $tgz 2>$null
        if ($LASTEXITCODE -eq 0) {
            & tar.exe --force-local -xzf $tgz -C $tmp 2>$null
            if ($LASTEXITCODE -eq 0 -and (Test-Path (Join-Path $tmp "package\dist\vue.esm-browser.prod.js"))) {
                Copy-Item (Join-Path $tmp "package\dist\vue.esm-browser.prod.js") $vueFile
                $downloaded = $true
            }
        }
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
        if ($downloaded) { break }
    }
    if ($downloaded) {
        Write-Host "Vue $vueVersion installed to $vueDir" -ForegroundColor Green
    } else {
        Write-Host "ERROR: failed to download Vue $vueVersion from all mirrors; web UI will not work." -ForegroundColor Red
        exit 1
    }
}

Write-Host ""
Write-Host "Done." -ForegroundColor Green

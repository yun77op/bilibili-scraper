# Shared Python detection for the PowerShell scripts.
# Dot-source this file, then call: Find-Python [-Override <path>]
# Returns the full path to a working python.exe, or $null.

function Find-Python {
    param([string]$Override)

    if ($Override) {
        if (Test-Path $Override) { return $Override }
        Write-Host "ERROR: Python not found at $Override" -ForegroundColor Red
        Write-Host "Check the -PythonPath parameter value."
        return $null
    }

    # 1) py launcher (installed with python.org / winget Python)
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $pyPath = (& py -3 -c "import sys; print(sys.executable)" 2>$null)
            if ($pyPath -and (Test-Path $pyPath.Trim())) { return $pyPath.Trim() }
        } catch {}
    }

    # 2) python on PATH (skip the Microsoft Store stub)
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python -and $python.Source -notlike "*WindowsApps*" -and (Test-Path $python.Source)) {
        return $python.Source
    }

    # 3) common install locations
    $candidates = @()
    $localPython = Join-Path $env:LOCALAPPDATA "Programs\Python"
    if (Test-Path $localPython) {
        $candidates += Get-ChildItem $localPython -Directory -Filter "Python3*" -ErrorAction SilentlyContinue |
            ForEach-Object { Join-Path $_.FullName "python.exe" }
    }
    $candidates += @(
        "C:\Python311\python.exe",
        "C:\Python312\python.exe",
        "C:\Python313\python.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }

    return $null
}

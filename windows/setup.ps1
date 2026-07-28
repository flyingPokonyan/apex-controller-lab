$ErrorActionPreference = "Stop"

$WindowsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $WindowsDir ".venv\Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($PyLauncher) {
        & py -3 -m venv (Join-Path $WindowsDir ".venv")
    }
    else {
        & python -m venv (Join-Path $WindowsDir ".venv")
    }
}

& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r (Join-Path $WindowsDir "requirements.txt")

Write-Host "Setup complete. Tutorial start: .\run.ps1 start tutorial"

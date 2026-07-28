$ErrorActionPreference = "Stop"
$WindowsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoDir = Split-Path -Parent $WindowsDir
$Python = Join-Path $WindowsDir ".venv\Scripts\python.exe"

$Command = "validate"
$ForwardArgs = @()
$PrefixArgs = @()
if ($args.Count -ge 1) {
    $Command = [string]$args[0]
}
if (@("validate", "live", "start") -notcontains $Command) {
    throw "Unknown command: $Command (supported: validate, live, start)"
}
if ($args.Count -gt 1) {
    for ($Index = 1; $Index -lt $args.Count; $Index++) {
        $ForwardArgs += [string]$args[$Index]
    }
}
if ($Command -eq "start" -and $ForwardArgs.Count -eq 0) {
    $ForwardArgs = @("--mode", "match")
}
elseif ($Command -eq "start" -and $ForwardArgs.Count -eq 1 -and $ForwardArgs[0] -eq "match") {
    $ForwardArgs = @("--mode", "match")
}
elseif ($Command -eq "start" -and $ForwardArgs.Count -eq 1 -and $ForwardArgs[0] -eq "tutorial") {
    $ForwardArgs = @("--mode", "tutorial")
}
elseif ($Command -eq "validate" -and $ForwardArgs.Count -eq 1 -and $ForwardArgs[0] -eq "tutorial") {
    $PrefixArgs = @("--config", (Join-Path $WindowsDir "config\first-tutorial-2560x1440.zh-CN.json"))
    $ForwardArgs = @()
}

if (-not (Test-Path $Python)) {
    throw "Python environment is missing. Run .\setup.ps1 from the windows directory first."
}

$env:PYTHONPATH = $WindowsDir
Push-Location $RepoDir
try {
    & $Python -m apex_automation @PrefixArgs $Command @ForwardArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}

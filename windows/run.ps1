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
if (@("validate", "observe", "live", "start", "play") -notcontains $Command) {
    throw "Unknown command: $Command (supported: validate, observe, live, start, play)"
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
elseif (@("validate", "observe") -contains $Command -and $ForwardArgs.Count -ge 1 -and $ForwardArgs[0] -eq "tutorial") {
    $PrefixArgs = @("--config", (Join-Path $WindowsDir "config\first-tutorial-2560x1440.zh-CN.json"))
    if ($ForwardArgs.Count -gt 1) {
        $ForwardArgs = @($ForwardArgs[1..($ForwardArgs.Count - 1)])
    }
    else {
        $ForwardArgs = @()
    }
}
elseif ($Command -eq "observe") {
    # Neither task profile covers every screen, and a sampling session that
    # cannot score the in-match states is missing the ones with no data at all.
    $PrefixArgs = @("--config", (Join-Path $WindowsDir "config\observe-2560x1440.zh-CN.json"))
}
elseif ($Command -eq "play") {
    # Its own profile rather than the observe one: this command sends input,
    # and it must not inherit the templates and ordered action table that the
    # capability set exists to replace.
    $PrefixArgs = @("--config", (Join-Path $WindowsDir "config\play-2560x1440.zh-CN.json"))
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

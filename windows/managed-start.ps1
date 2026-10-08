[CmdletBinding()]
param([switch]$Autostart, [switch]$UpdateOnly)

$ErrorActionPreference = "Stop"
$WindowsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoDir = Split-Path -Parent $WindowsDir
$GitExe = "git"
$BundledGit = Join-Path $env:LOCALAPPDATA "ApexController\tools\git\cmd\git.exe"
if (Test-Path -LiteralPath $BundledGit) { $GitExe = $BundledGit }

# Use the base interpreter, not site-packages from the worker environment.
# The updater remains usable after a failed pip install or game import.
$Python = $null
$Candidates = @((Join-Path $WindowsDir ".venv\Scripts\python.exe"))
$Py = Get-Command py.exe -ErrorAction SilentlyContinue
if ($Py) {
    try {
        $BasePython = & $Py.Source -3 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0) { $Candidates += [string]$BasePython }
    }
    catch { }
}
$SystemPython = Get-Command python.exe -ErrorAction SilentlyContinue
if ($SystemPython -and $SystemPython.Source -notlike "*\WindowsApps\*") { $Candidates += $SystemPython.Source }
foreach ($Candidate in $Candidates) {
    if (-not (Test-Path -LiteralPath $Candidate)) { continue }
    try { $BasePython = & $Candidate -I -c "import sys; print(getattr(sys, '_base_executable', sys.executable))" 2>$null }
    catch { continue }
    if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath ([string]$BasePython))) {
        $Python = [string]$BasePython
        break
    }
}
if (-not $Python) { throw "Python is missing. Run install.ps1 first." }

if (-not $Autostart -and -not $UpdateOnly) {
    # Interactive login, not a service: DXcam/EA require the user's desktop.
    try {
        $ShortcutPath = Join-Path ([Environment]::GetFolderPath("Startup")) "Apex Controller.lnk"
        $Shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($ShortcutPath)
        $Shortcut.TargetPath = Join-Path $PSHOME "powershell.exe"
        $Shortcut.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $MyInvocation.MyCommand.Path + '" -Autostart'
        $Shortcut.WorkingDirectory = $RepoDir
        $Shortcut.WindowStyle = 7
        $Shortcut.Save()
    }
    catch { Write-Warning "Could not register login startup. This run can continue." }
}

$LaunchArgs = @("-I", "-u", (Join-Path $WindowsDir "managed_launcher.py"), "--repo", $RepoDir, "--git", $GitExe)
if ($UpdateOnly) { $LaunchArgs += "--update-only" }
elseif (-not $Autostart) { $LaunchArgs += "--resume" }
do {
    & $Python @LaunchArgs
    $LaunchExit = $LASTEXITCODE
    # Reloading updated launcher code is not a new operator resume request.
    $LaunchArgs = @($LaunchArgs | Where-Object { $_ -ne "--resume" })
} while ($LaunchExit -eq 76)
exit $LaunchExit

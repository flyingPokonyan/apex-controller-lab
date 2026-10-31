# Trial runner: account-cycle on a trial branch that updates itself.
#
# Every few minutes a watcher fetches the branch. When it moved forward (fast
# forward only), it asks the worker to stop at the next safe point: between
# accounts, or in the lobby between matches. The worker saves the run and
# releases its lease, exits with 75, and this loop fast-forwards and starts it
# again. Exit codes follow the managed launcher: 74 = recoverable EA trouble,
# start again after a growing wait; 0 = stopped by the operator (F8, Ctrl+C);
# anything else = a failure, started again at most three times in a row.
#
# Every start first fast-forwards to the branch (so a restart by hand also
# updates), the watcher writes what it saw to trial\watcher.log, and the
# console's QuickEdit is turned off: a click into this window used to put it in
# selection mode and freeze the run at its next line of output.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File windows\trial-cycle.ps1
[CmdletBinding()]
param([string]$Branch = "claude/runner-active-trial", [int]$CheckMinutes = 5)

$ErrorActionPreference = "Stop"
$WindowsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoDir = Split-Path -Parent $WindowsDir
$GitExe = "git"
$BundledGit = Join-Path $env:LOCALAPPDATA "ApexController\tools\git\cmd\git.exe"
if (Test-Path -LiteralPath $BundledGit) { $GitExe = $BundledGit }
$Root = Join-Path $env:LOCALAPPDATA "ApexController\trial"
New-Item -ItemType Directory -Force -Path $Root | Out-Null

$Current = (& $GitExe -C $RepoDir rev-parse --abbrev-ref HEAD).Trim()
if ($Current -ne $Branch) { throw "This checkout is on $Current, not $Branch. Switch to the trial branch first." }

try {
    Add-Type -Namespace TrialConsole -Name Mode -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern System.IntPtr GetStdHandle(int handle);
[DllImport("kernel32.dll")] public static extern bool GetConsoleMode(System.IntPtr handle, out uint mode);
[DllImport("kernel32.dll")] public static extern bool SetConsoleMode(System.IntPtr handle, uint mode);
'@
    $StdIn = [TrialConsole.Mode]::GetStdHandle(-10)
    [uint32]$Mode = 0
    if ([TrialConsole.Mode]::GetConsoleMode($StdIn, [ref]$Mode)) {
        # 0x80 = extended flags (needed for the change to stick), 0x40 = QuickEdit.
        $NewMode = $Mode -bor 0x80
        if ($NewMode -band 0x40) { $NewMode = $NewMode - 0x40 }
        [void][TrialConsole.Mode]::SetConsoleMode($StdIn, [uint32]$NewMode)
        Write-Host "[trial] console QuickEdit off (a click no longer pauses the run)"
    }
} catch {
    Write-Host "[trial] could not turn QuickEdit off: $_"
}

function Update-Checkout {
    # Fast-forward to the branch before a start; when that fails, run what is checked out.
    $ErrorActionPreference = "Continue"
    $Output = & $GitExe -C $RepoDir fetch --quiet origin $Branch 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[trial] fetch failed (exit $LASTEXITCODE): $Output"
        return
    }
    $Output = & $GitExe -C $RepoDir merge --ff-only --quiet "origin/$Branch" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[trial] cannot fast-forward to origin/$Branch, starting the checked-out code: $Output"
    }
}

$Resume = $true
$Failures = 0
$Retries = 0
while ($true) {
    Update-Checkout
    $Session = [guid]::NewGuid().ToString()
    Remove-Item -LiteralPath (Join-Path $Root "request.json") -ErrorAction SilentlyContinue
    $env:APEX_MANAGED_ROOT = $Root
    $env:APEX_MANAGED_SESSION = $Session
    $env:APEX_TRIAL_MATCH_BOUNDARY = "1"
    $Watcher = Start-Job -ArgumentList $GitExe, $RepoDir, $Branch, $Root, $Session, $CheckMinutes -ScriptBlock {
        param($Git, $Repo, $Branch, $Root, $Session, $Minutes)
        $Log = Join-Path $Root "watcher.log"
        function Note($Text) { Add-Content -Path $Log -Value ("{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $Text) }
        Note "watching $Branch every $Minutes min (session $Session)"
        while ($true) {
            Start-Sleep -Seconds ($Minutes * 60)
            $Output = & $Git -C $Repo fetch --quiet origin $Branch 2>&1
            if ($LASTEXITCODE -ne 0) { Note "fetch failed (exit $LASTEXITCODE): $Output"; continue }
            $Head = (& $Git -C $Repo rev-parse HEAD).Trim()
            $Remote = (& $Git -C $Repo rev-parse "origin/$Branch").Trim()
            if ($Head -eq $Remote) { continue }
            & $Git -C $Repo merge-base --is-ancestor $Head $Remote
            if ($LASTEXITCODE -ne 0) { Note "origin/$Branch $Remote is not ahead of $Head; not updating"; continue }
            $Request = @{ session = $Session; mode = "boundary" } | ConvertTo-Json -Compress
            [System.IO.File]::WriteAllText((Join-Path $Root "request.json"), $Request)
            Note "update $Head -> $Remote found; asked the worker to stop at the next boundary"
            break
        }
    }
    Write-Host "[trial] starting $((& $GitExe -C $RepoDir log --oneline -1))"
    $Arguments = @("account-cycle", "--runner-config", (Join-Path $WindowsDir "account-cycle.private.json"))
    # Only the first start clears a local pause: later restarts must not wave
    # through a pause that is waiting for a person.
    if ($Resume) { $Arguments += "--resume" }
    $Resume = $false
    & (Join-Path $WindowsDir "run.ps1") @Arguments
    $Exit = $LASTEXITCODE
    Stop-Job -Job $Watcher -ErrorAction SilentlyContinue
    Remove-Job -Job $Watcher -Force -ErrorAction SilentlyContinue
    if ($Exit -eq 0) {
        Write-Host "[trial] stopped (F8, Ctrl+C or a normal end)."
        exit 0
    }
    if ($Exit -eq 75) {
        & $GitExe -C $RepoDir fetch --quiet origin $Branch
        & $GitExe -C $RepoDir merge --ff-only "origin/$Branch"
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[trial] cannot fast-forward to origin/$Branch; stopping for a person to look."
            exit 1
        }
        Write-Host "[trial] updated, starting again."
        $Failures = 0
        $Retries = 0
        continue
    }
    if ($Exit -eq 74) {
        $Retries += 1
        $Delay = [Math]::Min(300, 30 * [Math]::Pow(2, [Math]::Min($Retries - 1, 4)))
        Write-Host "[trial] EA not ready (74); starting again in $Delay s."
        Start-Sleep -Seconds $Delay
        continue
    }
    $Failures += 1
    if ($Failures -ge 3) {
        Write-Host "[trial] account-cycle failed $Failures times in a row (exit $Exit); stopping for a person to look."
        exit $Exit
    }
    Write-Host "[trial] account-cycle exit $Exit; starting again in 60 s (failure $Failures)."
    Start-Sleep -Seconds 60
}

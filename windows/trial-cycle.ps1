# Trial runner: account-cycle on a trial branch that updates itself.
#
# Every few minutes a watcher fetches the branch. When it moved forward (fast
# forward only), it asks the worker to stop at the next safe point: between
# accounts, or in the lobby between matches. The worker saves the run and
# releases its lease, exits with 75, and this loop fast-forwards and starts it
# again. Any other exit code ends the loop. Close the window or Ctrl+C to stop.
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
if ($Current -ne $Branch) { throw "当前分支是 $Current，不是 $Branch。先切到试验分支再运行。" }

while ($true) {
    $Session = [guid]::NewGuid().ToString()
    Remove-Item -LiteralPath (Join-Path $Root "request.json") -ErrorAction SilentlyContinue
    $env:APEX_MANAGED_ROOT = $Root
    $env:APEX_MANAGED_SESSION = $Session
    $env:APEX_TRIAL_MATCH_BOUNDARY = "1"
    $Watcher = Start-Job -ArgumentList $GitExe, $RepoDir, $Branch, $Root, $Session, $CheckMinutes -ScriptBlock {
        param($Git, $Repo, $Branch, $Root, $Session, $Minutes)
        while ($true) {
            Start-Sleep -Seconds ($Minutes * 60)
            & $Git -C $Repo fetch --quiet origin $Branch 2>$null
            if ($LASTEXITCODE -ne 0) { continue }
            $Head = (& $Git -C $Repo rev-parse HEAD).Trim()
            $Remote = (& $Git -C $Repo rev-parse "origin/$Branch").Trim()
            if ($Head -eq $Remote) { continue }
            & $Git -C $Repo merge-base --is-ancestor $Head $Remote
            if ($LASTEXITCODE -ne 0) { continue }
            $Request = @{ session = $Session; mode = "boundary" } | ConvertTo-Json -Compress
            [System.IO.File]::WriteAllText((Join-Path $Root "request.json"), $Request)
            break
        }
    }
    Write-Host "[试验] 启动 $((& $GitExe -C $RepoDir log --oneline -1))"
    & (Join-Path $WindowsDir "run.ps1") account-cycle --resume --runner-config (Join-Path $WindowsDir "account-cycle.private.json")
    $Exit = $LASTEXITCODE
    Stop-Job -Job $Watcher -ErrorAction SilentlyContinue
    Remove-Job -Job $Watcher -Force -ErrorAction SilentlyContinue
    if ($Exit -ne 75) {
        Write-Host "[试验] account-cycle 退出码 $Exit，循环结束。"
        exit $Exit
    }
    & $GitExe -C $RepoDir fetch --quiet origin $Branch
    & $GitExe -C $RepoDir merge --ff-only "origin/$Branch"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[试验] 不能快进到 origin/$Branch，停止，等人处理。"
        exit 1
    }
    Write-Host "[试验] 已更新，重新启动。"
}

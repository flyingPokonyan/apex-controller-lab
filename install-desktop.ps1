[CmdletBinding()]
param(
    [string]$RepoUrl = "https://github.com/flyingPokonyan/apex-controller-lab.git"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Desktop = [Environment]::GetFolderPath("Desktop")
$ProjectDir = Join-Path $Desktop "apex-controller-lab"
$ToolRoot = Join-Path $env:LOCALAPPDATA "ApexController\tools"
$PortableGitRoot = Join-Path $ToolRoot "git"
$PortableGitExe = Join-Path $PortableGitRoot "cmd\git.exe"
$TempRoot = Join-Path ([IO.Path]::GetTempPath()) ("apex-install-" + [Guid]::NewGuid().ToString("N"))
$GitVersion = "2.51.0"
$GitRelease = "v2.51.0.windows.1"
$GitSha256 = "a09b275d51ed3e829128e04cf4168fb54896cf6234bb30fecb8dc96a2bd321fa"
$PythonVersion = "3.12.10"
$PythonSha256 = "67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb"

function Refresh-Path {
    $MachinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$MachinePath;$UserPath"
}

function Download-FirstAvailable {
    param([string[]]$Urls, [string]$Destination)

    foreach ($Url in $Urls) {
        try {
            Write-Host "Downloading $Url"
            Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Destination
            return
        }
        catch {
            Write-Warning "Download failed, trying the next source."
            Remove-Item $Destination -Force -ErrorAction SilentlyContinue
        }
    }
    throw "All download sources failed."
}

function Assert-FileHash {
    param([string]$Path, [string]$ExpectedSha256)

    $Actual = (Get-FileHash $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Actual -ne $ExpectedSha256) {
        throw "Downloaded file checksum mismatch: $Path"
    }
}

function Find-Git {
    $Command = Get-Command git.exe -ErrorAction SilentlyContinue
    if ($Command) {
        return $Command.Source
    }
    if (Test-Path $PortableGitExe) {
        return $PortableGitExe
    }
    return $null
}

function Find-Python312 {
    $Launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($Launcher -and $Launcher.Source -notlike "*\Microsoft\WindowsApps\*") {
        & $Launcher.Source -3.12 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return @{ Path = $Launcher.Source; Args = @("-3.12") }
        }
    }

    $PythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    # Windows ships a zero-byte App Execution Alias that only opens the Store.
    # It is not an installed Python interpreter and writes an error to stderr.
    if ($PythonCommand -and $PythonCommand.Source -notlike "*\Microsoft\WindowsApps\*") {
        & $PythonCommand.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return @{ Path = $PythonCommand.Source; Args = @() }
        }
    }
    return $null
}

function Install-WithWinget {
    param([string]$PackageId, [switch]$CurrentUser)

    $Arguments = @(
        "install", "--id", $PackageId, "-e", "--source", "winget",
        "--accept-source-agreements", "--accept-package-agreements"
    )
    if ($CurrentUser) {
        $Arguments += @("--scope", "user")
    }
    $PreviousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & winget.exe @Arguments
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
    finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }
}

function Test-GitCheckout {
    param([string]$GitExe, [string]$Path)

    if (
        -not (Test-Path (Join-Path $Path ".git")) -or
        -not (Test-Path (Join-Path $Path "windows\requirements.txt")) -or
        -not (Test-Path (Join-Path $Path "windows\account-cycle-once.cmd"))
    ) {
        return $false
    }
    $PreviousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $GitExe -C $Path rev-parse --verify HEAD 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
    finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }
}

function Clone-WithRetry {
    param([string]$GitExe, [string]$RepoUrl, [string]$Destination)

    $StagingDir = "$Destination.installing"
    for ($Attempt = 1; $Attempt -le 3; $Attempt++) {
        Remove-Item $StagingDir -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "Cloning project (attempt $Attempt/3)..."
        $GitOptions = @(
            "-c", "http.version=HTTP/1.1",
            "-c", "http.maxRequests=1"
        )
        if ($Attempt -eq 2) {
            $GitOptions += @("-c", "http.sslBackend=openssl")
        }

        $PreviousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            & $GitExe @GitOptions clone --depth 1 --single-branch --no-tags `
                --filter=blob:none -- $RepoUrl $StagingDir
            $CloneExitCode = $LASTEXITCODE
        }
        catch {
            $CloneExitCode = 1
        }
        finally {
            $ErrorActionPreference = $PreviousErrorActionPreference
        }

        if ($CloneExitCode -eq 0 -and (Test-GitCheckout $GitExe $StagingDir)) {
            Move-Item $StagingDir $Destination
            return $true
        }
        Remove-Item $StagingDir -Recurse -Force -ErrorAction SilentlyContinue
        if ($Attempt -lt 3) {
            Start-Sleep -Seconds (2 * $Attempt)
        }
    }
    return $false
}

function Pull-WithRetry {
    param([string]$GitExe, [string]$Path)

    for ($Attempt = 1; $Attempt -le 3; $Attempt++) {
        Write-Host "Updating project (attempt $Attempt/3)..."
        $GitOptions = @("-c", "http.version=HTTP/1.1")
        if ($Attempt -eq 2) {
            $GitOptions += @("-c", "http.sslBackend=openssl")
        }

        $PreviousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            & $GitExe @GitOptions -C $Path pull --ff-only
            $PullExitCode = $LASTEXITCODE
        }
        catch {
            $PullExitCode = 1
        }
        finally {
            $ErrorActionPreference = $PreviousErrorActionPreference
        }
        if ($PullExitCode -eq 0) {
            return $true
        }
        if ($Attempt -lt 3) {
            Start-Sleep -Seconds (2 * $Attempt)
        }
    }
    return $false
}

New-Item -ItemType Directory -Force -Path $TempRoot, $ToolRoot | Out-Null
try {
    $Winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    $GitExe = Find-Git
    if (-not $GitExe -and $Winget) {
        Install-WithWinget "Git.Git" | Out-Null
        Refresh-Path
        $GitExe = Find-Git
    }
    if (-not $GitExe) {
        Write-Host "winget/Git unavailable; installing PortableGit directly."
        $GitArchive = Join-Path $TempRoot "PortableGit-$GitVersion-64-bit.7z.exe"
        Download-FirstAvailable -Destination $GitArchive -Urls @(
            "https://github.com/git-for-windows/git/releases/download/$GitRelease/PortableGit-$GitVersion-64-bit.7z.exe",
            "https://registry.npmmirror.com/-/binary/git-for-windows/$GitRelease/PortableGit-$GitVersion-64-bit.7z.exe"
        )
        Assert-FileHash $GitArchive $GitSha256
        Unblock-File $GitArchive
        New-Item -ItemType Directory -Force -Path $PortableGitRoot | Out-Null
        & $GitArchive -y "-o$PortableGitRoot" | Out-Null
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $PortableGitExe)) {
            throw "PortableGit extraction failed."
        }
        $GitExe = $PortableGitExe
    }

    $Python = Find-Python312
    if (-not $Python -and $Winget) {
        Install-WithWinget "Python.Python.3.12" -CurrentUser | Out-Null
        Refresh-Path
        $Python = Find-Python312
    }
    if (-not $Python) {
        Write-Host "winget/Python unavailable; installing Python directly."
        $PythonInstaller = Join-Path $TempRoot "python-$PythonVersion-amd64.exe"
        Download-FirstAvailable -Destination $PythonInstaller -Urls @(
            "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe",
            "https://registry.npmmirror.com/-/binary/python/$PythonVersion/python-$PythonVersion-amd64.exe"
        )
        Assert-FileHash $PythonInstaller $PythonSha256
        Unblock-File $PythonInstaller
        $Process = Start-Process -FilePath $PythonInstaller -ArgumentList @(
            "/quiet",
            "InstallAllUsers=0",
            "PrependPath=1",
            "Include_launcher=1",
            "Include_test=0",
            "SimpleInstall=1"
        ) -Wait -PassThru
        if ($Process.ExitCode -ne 0) {
            throw "Python installation failed with exit code $($Process.ExitCode)."
        }
        Refresh-Path
        $Python = Find-Python312
    }
    if (-not $Python) {
        throw "Python 3.12 is unavailable after installation."
    }

    if (Test-GitCheckout $GitExe $ProjectDir) {
        if (-not (Pull-WithRetry $GitExe $ProjectDir)) {
            throw "Git update failed after 3 attempts. Check the network and retry."
        }
    }
    elseif (Test-Path $ProjectDir) {
        $FailedDir = "$ProjectDir.failed-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        Move-Item $ProjectDir $FailedDir
        Write-Warning "The incomplete project was preserved at: $FailedDir"
        if (-not (Clone-WithRetry $GitExe $RepoUrl $ProjectDir)) {
            throw "Git clone failed after 3 attempts. Check the network and retry."
        }
    }
    else {
        if (-not (Clone-WithRetry $GitExe $RepoUrl $ProjectDir)) {
            throw "Git clone failed after 3 attempts. Check the network and retry."
        }
    }

    $WindowsDir = Join-Path $ProjectDir "windows"
    $VenvDir = Join-Path $WindowsDir ".venv"
    $VenvPython = Join-Path $VenvDir "Scripts\python.exe"
    if (-not (Test-Path $VenvPython)) {
        $PythonExe = $Python.Path
        $PythonPrefixArgs = @($Python.Args)
        & $PythonExe @PythonPrefixArgs -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) {
            throw "Python virtual environment creation failed."
        }
    }

    & $VenvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw "pip upgrade failed."
    }
    & $VenvPython -m pip install -r (Join-Path $WindowsDir "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        throw "Python dependency installation failed."
    }

    $ConfigSource = Join-Path $PSScriptRoot "account-cycle.private.json"
    $ConfigTarget = Join-Path $WindowsDir "account-cycle.private.json"
    if (Test-Path $ConfigSource) {
        Copy-Item $ConfigSource $ConfigTarget -Force
        Write-Host "Runner configuration installed."
    }
    else {
        Write-Host "Download account-cycle.private.json from ApexForge and place it here:"
        Write-Host $ConfigTarget -ForegroundColor Yellow
    }

    Write-Host ""
    Write-Host "Installation complete: $ProjectDir" -ForegroundColor Green
    Write-Host "First run: $WindowsDir\account-cycle-once.cmd"
}
finally {
    Remove-Item $TempRoot -Recurse -Force -ErrorAction SilentlyContinue
}

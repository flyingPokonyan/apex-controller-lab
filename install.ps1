[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "ApexController\apex-controller-lab"),
    [string]$RepoUrl = "https://github.com/flyingPokonyan/apex-controller-lab.git",
    [string]$RunnerConfigPath,
    [switch]$Start
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$ToolRoot = Join-Path $env:LOCALAPPDATA "ApexController\tools"
$GitRoot = Join-Path $ToolRoot "git"
$GitExe = Join-Path $GitRoot "cmd\git.exe"
$TempRoot = Join-Path ([IO.Path]::GetTempPath()) ("apex-install-" + [Guid]::NewGuid().ToString("N"))
$GitVersion = "2.51.0"
$GitRelease = "v2.51.0.windows.1"
$GitSha256 = "a09b275d51ed3e829128e04cf4168fb54896cf6234bb30fecb8dc96a2bd321fa"
$PythonVersion = "3.12.10"
$PythonSha256 = "67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb"

function Download-FirstAvailable {
    param(
        [string[]]$Urls,
        [string]$Destination
    )

    foreach ($Url in $Urls) {
        try {
            Write-Host "Downloading $Url"
            Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Destination
            return
        }
        catch {
            Write-Warning "Download failed, trying the next source: $Url"
            Remove-Item -LiteralPath $Destination -Force -ErrorAction SilentlyContinue
        }
    }
    throw "All download sources failed. Check the network and retry."
}

function Refresh-ProcessPath {
    $MachinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$MachinePath;$UserPath"
}

function Assert-FileHash {
    param(
        [string]$Path,
        [string]$ExpectedSha256
    )

    $Actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Actual -ne $ExpectedSha256) {
        throw "Downloaded file checksum mismatch: $Path"
    }
}

function Test-SupportedPython {
    $Launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($Launcher) {
        & $Launcher.Source -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return $true
        }
    }

    $Python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($Python) {
        & $Python.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return $true
        }
    }
    return $false
}

New-Item -ItemType Directory -Force -Path $TempRoot, $ToolRoot | Out-Null
try {
    $SystemGit = Get-Command git.exe -ErrorAction SilentlyContinue
    if ($SystemGit) {
        $GitExe = $SystemGit.Source
        Write-Host "Git already available: $GitExe"
    }
    elseif (-not (Test-Path -LiteralPath $GitExe)) {
        $GitArchive = Join-Path $TempRoot "PortableGit-$GitVersion-64-bit.7z.exe"
        Download-FirstAvailable -Destination $GitArchive -Urls @(
            "https://registry.npmmirror.com/-/binary/git-for-windows/$GitRelease/PortableGit-$GitVersion-64-bit.7z.exe",
            "https://github.com/git-for-windows/git/releases/download/$GitRelease/PortableGit-$GitVersion-64-bit.7z.exe"
        )
        Assert-FileHash -Path $GitArchive -ExpectedSha256 $GitSha256
        Unblock-File -LiteralPath $GitArchive
        New-Item -ItemType Directory -Force -Path $GitRoot | Out-Null
        & $GitArchive -y "-o$GitRoot" | Out-Null
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $GitExe)) {
            throw "PortableGit extraction failed."
        }
    }
    else {
        Write-Host "Using bundled Git: $GitExe"
    }

    if (-not (Test-SupportedPython)) {
        $PythonInstaller = Join-Path $TempRoot "python-$PythonVersion-amd64.exe"
        Download-FirstAvailable -Destination $PythonInstaller -Urls @(
            "https://registry.npmmirror.com/-/binary/python/$PythonVersion/python-$PythonVersion-amd64.exe",
            "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe"
        )
        Assert-FileHash -Path $PythonInstaller -ExpectedSha256 $PythonSha256
        Unblock-File -LiteralPath $PythonInstaller
        Write-Host "Installing Python $PythonVersion for the current user"
        $PythonProcess = Start-Process -FilePath $PythonInstaller -ArgumentList @(
            "/quiet",
            "InstallAllUsers=0",
            "PrependPath=1",
            "Include_launcher=1",
            "Include_test=0",
            "SimpleInstall=1"
        ) -Wait -PassThru
        if ($PythonProcess.ExitCode -ne 0) {
            throw "Python installation failed with exit code $($PythonProcess.ExitCode)."
        }
        Refresh-ProcessPath
    }
    if (-not (Test-SupportedPython)) {
        throw "Python 3.10 or newer is still unavailable after installation."
    }

    if (Test-Path -LiteralPath (Join-Path $InstallDir ".git")) {
        Write-Host "Updating existing project: $InstallDir"
        & $GitExe -C $InstallDir pull --ff-only
    }
    elseif (Test-Path -LiteralPath $InstallDir) {
        throw "Install directory exists but is not a Git checkout: $InstallDir"
    }
    else {
        $InstallParent = Split-Path -Parent $InstallDir
        New-Item -ItemType Directory -Force -Path $InstallParent | Out-Null
        Write-Host "Cloning project into $InstallDir"
        & $GitExe clone --depth 1 --filter=blob:none -- $RepoUrl $InstallDir
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Git clone/update failed. Retry or pass a faster mirror with -RepoUrl."
    }

    $AdjacentConfig = Join-Path $PSScriptRoot "account-cycle.private.json"
    if ($RunnerConfigPath) {
        $ConfigSource = (Resolve-Path -LiteralPath $RunnerConfigPath).Path
    }
    elseif (Test-Path -LiteralPath $AdjacentConfig) {
        $ConfigSource = $AdjacentConfig
    }
    else {
        $ConfigSource = $null
    }
    if ($ConfigSource) {
        Copy-Item -LiteralPath $ConfigSource -Destination (Join-Path $InstallDir "windows\account-cycle.private.json") -Force
        Write-Host "Runner configuration installed."
    }

    if (-not $env:PIP_INDEX_URL) {
        $env:PIP_INDEX_URL = "https://mirrors.aliyun.com/pypi/simple/"
    }
    Write-Host "Installing Python dependencies"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $InstallDir "windows\setup.ps1")
    if ($LASTEXITCODE -ne 0) {
        throw "Python dependency installation failed."
    }

    $CycleCommand = Join-Path $InstallDir "windows\account-cycle.cmd"
    Write-Host ""
    Write-Host "Ready: $CycleCommand" -ForegroundColor Green
    if ($Start) {
        if (-not (Test-Path -LiteralPath (Join-Path $InstallDir "windows\account-cycle.private.json"))) {
            throw "Cannot start because account-cycle.private.json is missing."
        }
        Start-Process -FilePath $CycleCommand -WorkingDirectory (Join-Path $InstallDir "windows")
    }
}
finally {
    Remove-Item -LiteralPath $TempRoot -Recurse -Force -ErrorAction SilentlyContinue
}

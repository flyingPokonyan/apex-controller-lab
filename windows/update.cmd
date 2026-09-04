@echo off
setlocal

set "RepoDir=%~dp0.."
set "GitExe=git"
if exist "%LOCALAPPDATA%\ApexController\tools\git\cmd\git.exe" set "GitExe=%LOCALAPPDATA%\ApexController\tools\git\cmd\git.exe"
if not defined PIP_INDEX_URL set "PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/"

"%GitExe%" -C "%RepoDir%" pull --ff-only
if errorlevel 1 (
  echo.
  echo Update failed. Check the network, repository access, and local changes.
  pause
  exit /b 1
)

if not exist "%~dp0.venv\Scripts\python.exe" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
) else (
  "%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
)

if errorlevel 1 (
  echo.
  echo Python dependency update failed.
  pause
  exit /b 1
)

echo.
echo Project and Windows dependencies are up to date.
pause

@echo off
setlocal

set "RepoDir=%~dp0.."

git -C "%RepoDir%" pull --ff-only
if errorlevel 1 (
  echo.
  echo Update failed. Check the network, GitHub SSH, and local changes.
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

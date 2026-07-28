@echo off
setlocal

set "RepoDir=%~dp0.."

git -C "%RepoDir%" pull --ff-only
if errorlevel 1 (
  echo.
  echo 更新失败。请确认网络、GitHub SSH 和本地文件没有冲突。
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
  echo Python 依赖更新失败。
  pause
  exit /b 1
)

echo.
echo 项目和 Windows 依赖已更新。
pause

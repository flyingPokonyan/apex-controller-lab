@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" display-watch
set "EXIT_CODE=%ERRORLEVEL%"
pause
exit /b %EXIT_CODE%

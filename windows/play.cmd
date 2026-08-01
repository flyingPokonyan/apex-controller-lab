@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" play
set "EXIT_CODE=%ERRORLEVEL%"
pause
exit /b %EXIT_CODE%

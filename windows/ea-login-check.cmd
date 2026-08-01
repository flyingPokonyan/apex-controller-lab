@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" ea-login-check --runner-config "%~dp0account-cycle.private.json"
set "EXIT_CODE=%ERRORLEVEL%"
pause
exit /b %EXIT_CODE%

@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" account-cycle --once --runner-config "%~dp0account-cycle.private.json"
set "EXIT_CODE=%ERRORLEVEL%"
pause
exit /b %EXIT_CODE%

@echo off
rem Compatibility alias. account-cycle now clears safe pauses and keeps looping.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" account-cycle --runner-config "%~dp0account-cycle.private.json"
set "EXIT_CODE=%ERRORLEVEL%"
pause
exit /b %EXIT_CODE%

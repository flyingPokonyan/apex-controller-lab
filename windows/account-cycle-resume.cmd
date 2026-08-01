@echo off
rem Clears the local manual pause, then runs one managed cycle.
rem Only use it after the reason for the pause has actually been resolved.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" account-cycle --once --resume --runner-config "%~dp0account-cycle.private.json"
set "EXIT_CODE=%ERRORLEVEL%"
pause
exit /b %EXIT_CODE%

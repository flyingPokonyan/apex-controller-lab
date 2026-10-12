@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run update.cmd first to install the Controller runtime.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m apex_registration.setup
set "EXIT_CODE=%ERRORLEVEL%"
pause
exit /b %EXIT_CODE%

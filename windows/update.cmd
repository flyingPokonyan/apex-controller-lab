@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0managed-start.ps1" -UpdateOnly
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%

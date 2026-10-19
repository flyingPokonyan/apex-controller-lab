@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" pack-open-test %*
pause

@echo off
rem Compatibility alias: explicit start/resume through the managed launcher.
call "%~dp0account-cycle.cmd"
exit /b %ERRORLEVEL%

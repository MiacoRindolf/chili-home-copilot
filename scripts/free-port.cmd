@echo off
REM Run from cmd.exe so output and UAC flow stay predictable (see diagnose-port-8000.cmd).
cd /d "%~dp0\.."
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0free-port.ps1" %*
exit /b %ERRORLEVEL%

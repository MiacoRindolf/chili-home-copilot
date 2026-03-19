@echo off
REM Run from cmd.exe. After UAC, watch the ELEVATED window for "Done".
cd /d "%~dp0\.."
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0reset-chili-dev.ps1" %*
exit /b %ERRORLEVEL%

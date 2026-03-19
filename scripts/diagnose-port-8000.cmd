@echo off
REM Use this from Command Prompt (cmd.exe) or Cursor's cmd terminal.
REM Running .ps1 directly from cmd often opens a separate PowerShell window — you see nothing here.
setlocal
cd /d "%~dp0\.."
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0diagnose-port-8000.ps1" %*
set "EC=%ERRORLEVEL%"
if %EC% neq 0 echo.
if %EC% neq 0 echo diagnose-port-8000.ps1 exited with code %EC% ^(0=OK, 1=unclear/nothing-listening-now, 2=action-needed^)
exit /b %EC%

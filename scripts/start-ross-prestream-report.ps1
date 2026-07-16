# Write the read-only Ross prestream readiness report before the live window.
# Safe to run repeatedly; it overwrites ross_prestream_report.json/txt with current readiness.

$ErrorActionPreference = 'SilentlyContinue'

$dir = 'D:\CHILI-Docker\chili-data\ross_stream'
if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
$log = Join-Path $dir 'ross_prestream_report.daemon.log'
$err = Join-Path $dir 'ross_prestream_report.daemon.err.log'

Start-Process -FilePath 'C:\Users\rindo\miniconda3\envs\chili-env\python.exe' `
    -ArgumentList @(
        'D:\dev\chili-home-copilot\scripts\ross_prestream_report.py',
        '--profile', 'prestream'
    ) `
    -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError $err

# Start the read-only Ross live monitor snapshot recorder.
# Appends readiness + Ross-vs-CHILI incident snapshots to a daily JSONL file.
# Safe to run repeatedly: if the live monitor is already running, this exits cleanly.

$ErrorActionPreference = 'SilentlyContinue'

$existing = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like '*ross_live_monitor_snapshot.py*' -and $_.CommandLine -like '*--profile live*' -and $_.CommandLine -like '*--watch*' }
if ($existing) { exit 0 }

$dir = 'D:\CHILI-Docker\chili-data\ross_stream'
if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
$log = Join-Path $dir 'ross_live_monitor.daemon.log'
$err = Join-Path $dir 'ross_live_monitor.daemon.err.log'
$out = Join-Path $dir 'ross_live_monitor_{date}.jsonl'

Start-Process -FilePath 'C:\Users\rindo\miniconda3\envs\chili-env\python.exe' `
    -ArgumentList @(
        'D:\dev\chili-home-copilot\scripts\ross_live_monitor_snapshot.py',
        '--profile', 'live',
        '--watch',
        '--interval-seconds', '2',
        '--seconds', '18000',
        '--out', $out
    ) `
    -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError $err

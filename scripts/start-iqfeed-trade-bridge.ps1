# Start IQConnect (if down) + the IQFeed L1 TRADE-tape bridge (if not already running).
# Scheduled at logon + daily 03:56 PT (one min after the depth bridge, to avoid an IQConnect-start
# race) via CHILI-IQFeed-Trade-Bridge* tasks, launched through scripts\run-hidden.vbs (no console
# flash — see project_scheduled_tasks_hygiene). Safe to run repeatedly: both checks are idempotent.
# Captures the equity trade tape -> iqfeed_trade_ticks -> the momentum lane's `trade_flow` feature.
# Bridge logs: D:\CHILI-Docker\chili-data\iqfeed_trades\bridge.log

$ErrorActionPreference = 'SilentlyContinue'

# 1) IQConnect (binds 127.0.0.1; serves L1 :9100 + L2 :9200; auto-logs-in with saved credentials)
if (-not (Get-Process iqconnect -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath 'E:\DTN\IQFeed\iqconnect.exe' -WorkingDirectory 'E:\DTN\IQFeed'
    Start-Sleep -Seconds 20
}

# 2) trade-bridge daemon — skip if one is already running
$existing = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like '*iqfeed_trade_bridge.py*' }
if ($existing) { exit 0 }

$dir = 'D:\CHILI-Docker\chili-data\iqfeed_trades'
if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
$log = Join-Path $dir 'bridge.log'
$err = Join-Path $dir 'bridge.err.log'
Start-Process -FilePath 'C:\Users\rindo\miniconda3\envs\chili-env\python.exe' `
    -ArgumentList 'D:\dev\chili-home-copilot\scripts\iqfeed_trade_bridge.py' `
    -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError $err

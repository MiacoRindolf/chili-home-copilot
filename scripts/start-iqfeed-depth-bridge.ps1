# Start IQConnect (if down) + the IQFeed depth bridge (if not already running).
# Scheduled at logon + daily 03:55 PT (pre-premarket) via CHILI-IQFeed-Depth-Bridge*
# tasks, launched through scripts\run-hidden.vbs (no console flash — see
# project_scheduled_tasks_hygiene). Safe to run repeatedly: both checks are
# idempotent. Bridge logs: D:\CHILI-Docker\chili-data\iqfeed_depth\bridge.log

$ErrorActionPreference = 'SilentlyContinue'

# 1) IQConnect (binds 127.0.0.1:9200; auto-logs-in with saved credentials)
if (-not (Get-Process iqconnect -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath 'E:\DTN\IQFeed\iqconnect.exe' -WorkingDirectory 'E:\DTN\IQFeed'
    Start-Sleep -Seconds 20
}

# 2) bridge daemon — skip if one is already running
$existing = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like '*iqfeed_depth_bridge.py*' }
if ($existing) { exit 0 }

$log = 'D:\CHILI-Docker\chili-data\iqfeed_depth\bridge.log'
$err = 'D:\CHILI-Docker\chili-data\iqfeed_depth\bridge.err.log'
Start-Process -FilePath 'C:\Users\rindo\miniconda3\envs\chili-env\python.exe' `
    -ArgumentList 'D:\dev\chili-home-copilot\scripts\iqfeed_depth_bridge.py' `
    -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError $err

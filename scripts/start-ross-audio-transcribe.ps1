# Start the Ross-stream AUDIO transcriber daemon (if not already running).
# Captures SYSTEM AUDIO via WASAPI loopback, transcribes rolling ~12s chunks with faster-whisper
# (CPU int8 default), and appends {ts, text} to D:\CHILI-Docker\chili-data\ross_stream\transcript.jsonl.
# Mirrors start-iqfeed-trade-bridge.ps1. Safe to run repeatedly: the running-process check is idempotent.
# Launch hidden via scripts\run-hidden.vbs (no console flash — see project_scheduled_tasks_hygiene).
# Daemon logs: D:\CHILI-Docker\chili-data\ross_stream\daemon.log (+ .err.log)
#
# Persistence (operator runs ELEVATED — Register-ScheduledTask needs admin), mirroring the
# depth/trade bridge tasks:
#   $vbs = 'D:\dev\chili-home-copilot\scripts\run-hidden.vbs'
#   $ps1 = 'D:\dev\chili-home-copilot\scripts\start-ross-audio-transcribe.ps1'
#   $arg = "`"$vbs`" powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$ps1`""
#   $a = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument $arg
#   $p = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive
#   $s = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)
#   Register-ScheduledTask -TaskName 'CHILI-Ross-Audio-Transcribe-Logon' -Action $a -Principal $p -Settings $s -Trigger (New-ScheduledTaskTrigger -AtLogon) -Force
#   Register-ScheduledTask -TaskName 'CHILI-Ross-Audio-Transcribe-Daily' -Action $a -Principal $p -Settings $s -Trigger (New-ScheduledTaskTrigger -Daily -At 5:00AM) -Force

$ErrorActionPreference = 'SilentlyContinue'

# daemon — skip if one is already running
$existing = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like '*ross_audio_transcribe.py*' }
if ($existing) { exit 0 }

$dir = 'D:\CHILI-Docker\chili-data\ross_stream'
if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
$log = Join-Path $dir 'daemon.log'
$err = Join-Path $dir 'daemon.err.log'
$marker = Join-Path $dir 'warrior_session_ok.json'
$state = Join-Path $dir 'warrior_browser_state_latest.json'
$waitLog = Join-Path $dir 'daemon.wait.log'
$deadline = (Get-Date).AddSeconds(7200)

while ((Get-Date) -lt $deadline) {
    $ok = $false
    if (Test-Path $state) {
        & 'C:\Users\rindo\miniconda3\envs\chili-env\python.exe' `
            'D:\dev\chili-home-copilot\scripts\warrior_session_marker.py' `
            --state-json-file $state `
            --state-json-file-max-age-seconds 30 `
            --out $marker | Out-Null
    }
    if (Test-Path $marker) {
        try {
            $m = Get-Content -Raw -Path $marker | ConvertFrom-Json
            $ts = [datetimeoffset]::Parse([string]$m.ts)
            $age = ([datetimeoffset]::UtcNow - $ts.ToUniversalTime()).TotalSeconds
            $hasStream = (($m.video_count -as [int]) -gt 0) -or ([bool]$m.stream_visible)
            $ok = ([bool]$m.ok) -and $hasStream -and ($age -le 30)
        } catch {
            $ok = $false
        }
    }
    if ($ok) { break }
    "waiting_for_warrior_session_marker $(Get-Date -Format o)" | Out-File -FilePath $waitLog -Append -Encoding utf8
    Start-Sleep -Seconds 5
}

if (-not $ok) {
    "warrior_session_marker_timeout $(Get-Date -Format o)" | Out-File -FilePath $waitLog -Append -Encoding utf8
    exit 0
}

$existing = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like '*ross_audio_transcribe.py*' }
if ($existing) { exit 0 }

Start-Process -FilePath 'C:\Users\rindo\miniconda3\envs\chili-env\python.exe' `
    -ArgumentList 'D:\dev\chili-home-copilot\scripts\ross_audio_transcribe.py' `
    -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError $err

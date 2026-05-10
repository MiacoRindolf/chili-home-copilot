# Outer supervisor for _claude_daemon.ps1.
#
# Run THIS instead of _claude_daemon.ps1 directly. It launches the daemon as
# a child process; if the daemon exits cleanly (heartbeat state = "restarting"
# or "exited"), the supervisor relaunches it. Hard exit (Ctrl+C, OS kill,
# script unhandled exception) is also caught and the supervisor relaunches
# with backoff.
#
# Stop the whole system: touch scripts/_claude_supervisor_stop.flag
# Stop just one daemon iteration: touch scripts/_claude_restart.flag
#
# This is the architect-grade fix for "daemon keeps hanging" -- the daemon
# itself self-restarts every 4h or 1000 commands, and the supervisor
# relaunches if something nukes the daemon entirely.

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..

$stopFlag        = "scripts/_claude_supervisor_stop.flag"
$daemonScript    = "$PSScriptRoot\_claude_daemon.ps1"
$supervisorLog   = "scripts/_claude_supervisor.log"
$heartbeatFile   = "scripts/_claude_daemon_heartbeat.json"

function SLog {
    param([string]$Line)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts [supervisor] $Line" | Add-Content $supervisorLog
    Write-Host "$ts [supervisor] $Line"
}

if (-not (Test-Path $daemonScript)) {
    SLog "FATAL: daemon script not found at $daemonScript"
    exit 1
}

SLog "supervisor started, pid=$PID; will relaunch daemon on clean exit"
$attemptCount = 0
$lastLaunchAt = $null
$consecutiveFastExits = 0

while ($true) {
    if (Test-Path $stopFlag) {
        SLog "supervisor stop flag detected, exiting"
        Remove-Item $stopFlag -Force -ErrorAction SilentlyContinue
        break
    }

    $attemptCount += 1
    $launchAt = Get-Date
    SLog "launching daemon (attempt #$attemptCount)"

    try {
        $proc = Start-Process -FilePath "powershell.exe" `
            -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $daemonScript `
            -NoNewWindow -PassThru -Wait
        $exitCode = if ($proc) { $proc.ExitCode } else { -1 }
    } catch {
        SLog "launch threw: $_"
        $exitCode = -1
    }

    $duration = ((Get-Date) - $launchAt).TotalSeconds
    SLog "daemon exited: exit_code=$exitCode duration=${duration}s"

    if (Test-Path $stopFlag) {
        SLog "supervisor stop flag set during run, exiting"
        Remove-Item $stopFlag -Force -ErrorAction SilentlyContinue
        break
    }

    # Backoff: if the daemon dies in <30s repeatedly, it's broken; back off.
    if ($duration -lt 30) {
        $consecutiveFastExits += 1
        $backoffSec = [Math]::Min(300, [Math]::Pow(2, $consecutiveFastExits) * 5)
        SLog "fast exit (#$consecutiveFastExits in row); sleeping ${backoffSec}s before relaunch"
        Start-Sleep -Seconds $backoffSec
    } else {
        $consecutiveFastExits = 0
        SLog "clean lifetime; relaunching in 2s"
        Start-Sleep -Seconds 2
    }
}

SLog "supervisor stopped"

# Outer supervisor for either _claude_daemon.ps1 (dispatch daemon, default)
# OR _claude_session_daemon.ps1 (session daemon) -- parameterized.
#
# Run THIS instead of the bare daemon. It launches the daemon as a child
# process; if the daemon exits cleanly (heartbeat state = "restarting"
# or "exited"), the supervisor relaunches it. Hard exit (Ctrl+C, OS kill,
# script unhandled exception) is also caught and the supervisor relaunches
# with backoff.
#
# Default mode (dispatch daemon):
#   .\scripts\_claude_daemon_supervisor.ps1
#
# Session-daemon mode (run in a SECOND PowerShell window):
#   .\scripts\_claude_daemon_supervisor.ps1 -Mode session
#
# Stop one supervisor: touch the stop flag named in $stopFlag below
#   (dispatch: scripts/_claude_supervisor_stop.flag
#    session:  scripts/_claude_session_supervisor_stop.flag)
#
# This is the architect-grade fix for "daemon keeps hanging" -- the daemon
# self-restarts every 4h or 1000 commands, and the supervisor relaunches
# if something nukes the daemon entirely.
#
# 2026-05-11: parameterized to also supervise the session daemon (which
# died yesterday 18:29 and was never relaunched; brief #286 / this commit).

[CmdletBinding()]
param(
    [ValidateSet("dispatch", "session")]
    [string]$Mode = "dispatch"
)

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..

if ($Mode -eq "session") {
    $daemonScript    = "$PSScriptRoot\_claude_session_daemon.ps1"
    $stopFlag        = "scripts/_claude_session_supervisor_stop.flag"
    $supervisorLog   = "scripts/_claude_session_supervisor.log"
    $pauseFlag       = "scripts/_claude_session_pause.flag"
    $tag             = "session-supervisor"
} else {
    $daemonScript    = "$PSScriptRoot\_claude_daemon.ps1"
    $stopFlag        = "scripts/_claude_supervisor_stop.flag"
    $supervisorLog   = "scripts/_claude_supervisor.log"
    $pauseFlag       = $null
    $tag             = "supervisor"
}

function SLog {
    param([string]$Line)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts [$tag] $Line" | Add-Content $supervisorLog
    Write-Host "$ts [$tag] $Line"
}

if (-not (Test-Path $daemonScript)) {
    SLog "FATAL: daemon script not found at $daemonScript"
    exit 1
}

# Session mode: clear stale pause flag on startup -- the operator starting
# the supervisor is the resolution to whatever paused it.
if ($pauseFlag -and (Test-Path $pauseFlag)) {
    Remove-Item $pauseFlag -Force -ErrorAction SilentlyContinue
    SLog "cleared stale pause flag at supervisor startup"
}

SLog "supervisor started, pid=$PID, mode=$Mode, daemon=$daemonScript"
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

    # Session mode: clear pause flag at relaunch boundary so a transient
    # timeout doesn't leave the queue jammed forever. The flag is a useful
    # tool for manual pause but should not survive a supervisor relaunch.
    if ($pauseFlag -and (Test-Path $pauseFlag)) {
        Remove-Item $pauseFlag -Force -ErrorAction SilentlyContinue
        SLog "cleared pause flag at relaunch boundary"
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

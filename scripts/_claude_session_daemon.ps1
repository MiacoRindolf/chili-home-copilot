# Claude Code session daemon -- separate from _claude_daemon.ps1.
#
# Owns LONG-RUNNING `claude` sessions (2-4h each) with a queue + lock + per-
# session log dir. _claude_daemon.ps1 keeps owning fast dev dispatches
# (docker, git, psql) so they don't block while a CC session is mid-flight.
#
# Run once in a side PowerShell window:
#   .\scripts\_claude_session_daemon.ps1
#
# Queue a session by dropping a JSON file into scripts/_claude_session_queue/.
# Schema documented in docs/STRATEGY/CLAUDE_SESSION_DAEMON.md.
#
# Stop:        touch scripts/_claude_session_stop.flag    (or Ctrl+C)
# Pause:       touch scripts/_claude_session_pause.flag   (delete to resume)
# Kill switch: delete this script from disk

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..

$queueDir   = "scripts/_claude_session_queue"
$runningDir = "scripts/_claude_session_running"
$doneDir    = "scripts/_claude_session_done"
$logDir     = "scripts/_claude_session_log"
$statusFile = "scripts/_claude_session_status.json"
$stopFlag   = "scripts/_claude_session_stop.flag"
$pauseFlag  = "scripts/_claude_session_pause.flag"
$daemonLog  = "scripts/_claude_session_daemon.log"

foreach ($d in @($queueDir, $runningDir, $doneDir, $logDir)) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}

function Log {
    param([string]$Line)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts $Line" | Add-Content $daemonLog
    Write-Host "$ts $Line"
}

function Write-Status {
    param($state)
    try {
        $state | ConvertTo-Json -Depth 6 | Out-File $statusFile -Encoding utf8 -Force
    } catch {
        Log "status write failed: $_"
    }
}

function Resolve-ClaudeExe {
    # Resolved here while the daemon's PowerShell still has $PROFILE-set PATH.
    # When Run-Session spawns powershell.exe -NoProfile, that PATH is gone, so
    # the launcher cannot reliably Get-Command 'claude' itself.
    try {
        $cmd = Get-Command claude -ErrorAction Stop
        if ($cmd -and $cmd.Source) { return $cmd.Source }
    } catch {}
    # Fallback: probe common npm-global install locations.
    $candidates = @(
        (Join-Path $env:APPDATA "npm\claude.cmd"),
        (Join-Path $env:APPDATA "npm\claude.exe"),
        (Join-Path $env:LOCALAPPDATA "npm\claude.cmd"),
        (Join-Path $env:USERPROFILE ".local\bin\claude.exe"),
        (Join-Path $env:USERPROFILE "AppData\Roaming\npm\claude.cmd")
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) { return (Resolve-Path $c).Path }
    }
    return $null
}

function Recover-Stale-Running {
    # If daemon died mid-session, .session files can be left in running/.
    # Mark them FAILED_RECOVERED and move to done/ so the queue can advance.
    Get-ChildItem $runningDir -Filter '*.session' -ErrorAction SilentlyContinue | ForEach-Object {
        $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $dest = Join-Path $doneDir "FAILED_RECOVERED_${stamp}_$($_.Name)"
        Move-Item $_.FullName $dest -Force
        Log "recovered stale running session $($_.Name) -> $(Split-Path $dest -Leaf)"
    }
}

function Get-EligibleSession {
    $now = Get-Date
    $candidates = @()
    Get-ChildItem $queueDir -Filter '*.session' -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            $s = Get-Content $_.FullName -Raw | ConvertFrom-Json
            $nb = if ($s.not_before) { [datetime]::Parse($s.not_before) } else { [datetime]::MinValue }
            if ($now -ge $nb) {
                $candidates += [pscustomobject]@{
                    File      = $_.FullName
                    Name      = $_.Name
                    Session   = $s
                    Priority  = if ($null -ne $s.priority) { [int]$s.priority } else { 1000 }
                    NotBefore = $nb
                }
            }
        } catch {
            Log "skipping malformed session file $($_.Name): $_"
        }
    }
    $candidates | Sort-Object Priority, NotBefore, Name | Select-Object -First 1
}

function Run-Session {
    param($candidate, $claudeExe)

    $s = $candidate.Session
    $id = if ($s.id) { $s.id } else { [System.IO.Path]::GetFileNameWithoutExtension($candidate.Name) }

    $sessionLogDir = Join-Path $logDir $id
    if (-not (Test-Path $sessionLogDir)) { New-Item -ItemType Directory -Path $sessionLogDir -Force | Out-Null }

    $stdoutPath = Join-Path $sessionLogDir "stdout.log"
    $stderrPath = Join-Path $sessionLogDir "stderr.log"
    $metaPath   = Join-Path $sessionLogDir "meta.json"

    # Atomic move queue -> running. The presence of a file in running/ is the lock.
    $runningPath = Join-Path $runningDir $candidate.Name
    Move-Item $candidate.File $runningPath -Force

    $startedAt = Get-Date
    $timeoutMin = if ($s.timeout_min) { [int]$s.timeout_min } else { 240 }
    $timeoutSec = $timeoutMin * 60

    # Build claude args
    $claudeArgs = @()
    if ($s.claude_args) { foreach ($a in $s.claude_args) { $claudeArgs += [string]$a } }

    $hasPrint = $false
    foreach ($a in $claudeArgs) { if ($a -eq '-p' -or $a -eq '--print') { $hasPrint = $true } }

    if (-not $hasPrint) {
        $claudeArgs += '-p'
        if ($s.prompt) {
            $claudeArgs += [string]$s.prompt
        } else {
            $claudeArgs += "Read docs/STRATEGY/PROTOCOL.md and docs/STRATEGY/NEXT_TASK.md, then execute the queued task per protocol."
        }
    }

    if (-not ($claudeArgs -contains '--dangerously-skip-permissions')) {
        $claudeArgs += '--dangerously-skip-permissions'
    }

    Log "session $id starting (timeout=${timeoutMin}m, claude=$claudeExe)"

    @{
        id          = $id
        started_at  = $startedAt.ToString('o')
        timeout_min = $timeoutMin
        args_count  = $claudeArgs.Count
        description = $s.description
        claude_exe  = $claudeExe
    } | ConvertTo-Json -Depth 5 | Out-File $metaPath -Encoding utf8 -Force

    Write-Status @{
        state = "running"
        current = @{
            id          = $id
            started_at  = $startedAt.ToString('o')
            timeout_min = $timeoutMin
            description = $s.description
        }
        queue_depth = (Get-ChildItem $queueDir -Filter '*.session' -ErrorAction SilentlyContinue | Measure-Object).Count
    }

    # Persist claude args as JSON so the launcher can read them without
    # cross-process arg-quoting headaches.
    $argsFilePath = Join-Path $sessionLogDir "args.json"
    ConvertTo-Json -InputObject $claudeArgs -Compress | Out-File $argsFilePath -Encoding utf8 -Force

    # Launch claude via _claude_session_launcher.ps1. The launcher uses
    # the absolute claude path resolved at daemon startup so PATH issues
    # in -NoProfile-spawned shells don't bite us.
    $launcherPath = Join-Path $PSScriptRoot "_claude_session_launcher.ps1"
    $psInner = "& '$launcherPath' -ArgsFile '$argsFilePath' -ClaudeExe '$claudeExe' *>&1"

    $exitCode = -1
    $timedOut = $false
    try {
        $proc = Start-Process -FilePath "powershell.exe" `
            -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $psInner `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -NoNewWindow -PassThru
        $finished = $proc.WaitForExit($timeoutSec * 1000)
        if (-not $finished) {
            try {
                $tkOut = & taskkill.exe /T /F /PID $proc.Id 2>&1
                Log "  taskkill /T /F /PID $($proc.Id): $tkOut"
            } catch {
                Log "  taskkill failed: $_"
                try { $proc.Kill() } catch {}
            }
            Start-Sleep -Milliseconds 500
            $timedOut = $true
            $exitCode = -1
        } else {
            try { $exitCode = [int]$proc.ExitCode } catch { $exitCode = 0 }
        }
    } catch {
        Add-Content $stderrPath "EXCEPTION launching claude: $_"
        $exitCode = -1
    }

    $endedAt = Get-Date
    $duration = ($endedAt - $startedAt).TotalSeconds
    Log "session $id exit=$exitCode timed_out=$timedOut duration=$([Math]::Round($duration,1))s"

    # Optional post_verify script -- second exit-code that gates pass/fail
    $verifyExit = 0
    if ($s.post_verify) {
        $verifyOutPath = Join-Path $sessionLogDir "verify.log"
        Log "running post_verify: $($s.post_verify)"
        try {
            $vproc = Start-Process -FilePath "powershell.exe" `
                -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $s.post_verify `
                -RedirectStandardOutput $verifyOutPath `
                -RedirectStandardError $verifyOutPath `
                -NoNewWindow -PassThru -Wait
            $verifyExit = [int]$vproc.ExitCode
        } catch {
            $verifyExit = -1
            "EXCEPTION in post_verify: $_" | Add-Content $verifyOutPath
        }
        Log "post_verify exit=$verifyExit"
    }

    # Defense-in-depth pass/fail check: exit 0 from the launcher is necessary
    # but not sufficient. If stderr.log has content AND duration was suspiciously
    # short (< 5s), treat as failed even if exit code says 0 -- this catches
    # the case where claude itself errored without setting LASTEXITCODE.
    $stderrSize = 0
    if (Test-Path $stderrPath) { $stderrSize = (Get-Item $stderrPath).Length }
    $suspiciouslyShort = ($duration -lt 5) -and ($stderrSize -gt 0)

    $sessionPassed = ($exitCode -eq 0 -and $verifyExit -eq 0 -and -not $timedOut -and -not $suspiciouslyShort)
    $resultMeta = @{
        id                  = $id
        started_at          = $startedAt.ToString('o')
        ended_at            = $endedAt.ToString('o')
        duration_sec        = [Math]::Round($duration, 1)
        exit_code           = $exitCode
        timed_out           = $timedOut
        verify_exit         = $verifyExit
        suspiciously_short  = $suspiciouslyShort
        stderr_bytes        = $stderrSize
        passed              = $sessionPassed
        description         = $s.description
        claude_exe          = $claudeExe
    }
    $resultMeta | ConvertTo-Json -Depth 5 | Out-File $metaPath -Encoding utf8 -Force

    # Failure handling
    $onFail = if ($s.on_fail) { $s.on_fail } else { 'pause' }
    if (-not $sessionPassed) {
        Log "session $id FAILED; on_fail=$onFail (suspicious_short=$suspiciouslyShort)"
        if ($onFail -eq 'pause') {
            "session $id failed at $(Get-Date -Format o); operator review required" | Out-File $pauseFlag -Encoding utf8 -Force
            Log "pause flag set; daemon will idle until flag is removed"
        } elseif ($onFail -eq 'abort_chain') {
            Get-ChildItem $queueDir -Filter '*.session' -ErrorAction SilentlyContinue | ForEach-Object {
                $abortName = "ABORTED_$($_.Name)"
                Move-Item $_.FullName (Join-Path $doneDir $abortName) -Force
            }
            Log "abort_chain: cleared remaining queue"
        }
        # 'continue' falls through
    }

    # Move running -> done
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $doneName = "${stamp}_$($candidate.Name)"
    if (-not $sessionPassed) { $doneName = "FAILED_$doneName" }
    Move-Item $runningPath (Join-Path $doneDir $doneName) -Force

    Write-Status @{
        state = "idle"
        last  = $resultMeta
        queue_depth = (Get-ChildItem $queueDir -Filter '*.session' -ErrorAction SilentlyContinue | Measure-Object).Count
    }
}

Log "session daemon started, pid=$PID, pwd=$(Get-Location)"

# Resolve claude exe once at startup using the operator's PATH context.
$ClaudeExe = Resolve-ClaudeExe
if (-not $ClaudeExe) {
    Log "FATAL: cannot resolve 'claude' on PATH or common npm prefixes."
    Log "       Run '(Get-Command claude).Source' in your interactive shell"
    Log "       and ensure that path is reachable from this daemon's session."
    Write-Status @{ state = "error"; error = "claude not resolved at startup"; queue_depth = 0; last = $null }
    exit 1
}
Log "claude resolved to: $ClaudeExe"

Recover-Stale-Running
Log "watching $queueDir (poll=30s); stop with $stopFlag"

Write-Status @{ state = "idle"; queue_depth = 0; last = $null; claude_exe = $ClaudeExe }

while ($true) {
    if (Test-Path $stopFlag) {
        Log "stop flag detected, exiting"
        Write-Status @{ state = "stopped"; queue_depth = 0; last = $null }
        break
    }
    if (Test-Path $pauseFlag) {
        Start-Sleep -Seconds 30
        continue
    }
    $cand = Get-EligibleSession
    if (-not $cand) {
        Start-Sleep -Seconds 30
        continue
    }
    Run-Session $cand $ClaudeExe
}

Log "session daemon stopped"

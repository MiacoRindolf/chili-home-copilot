# Claude Code bridge worker (host side).
#
# The CHILI web app runs in a container with no `claude` binary and no access to
# ~/.claude, so it drops a job in <bridge>\inbox and this worker runs the real
# CLI here on the host.  Jobs move inbox -> working -> outbox so a crash mid-run
# is visible rather than silent.
#
# The turn is FORKED (--resume <sid> --fork-session): the interactive terminal
# session owns its transcript and must never have a second writer.  The fork
# carries the full history, so context is preserved; the reply simply lands in
# its own session file.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\claude_bridge_worker.ps1
#
# Stop it with Ctrl+C, or by deleting <bridge>\worker.run

[CmdletBinding()]
param(
    [string]$BridgeDir  = $env:CHILI_CLAUDE_BRIDGE_HOST_DIR,
    [string]$RepoDir    = 'E:\dev\wt-window2',
    [int]   $PollMs     = 1500,
    [int]   $TimeoutSec = 900
)

if (-not $BridgeDir) { $BridgeDir = 'D:\CHILI-Docker\chili-data\claude_bridge' }

$ErrorActionPreference = 'Stop'

foreach ($sub in @('inbox', 'working', 'outbox', 'logs')) {
    $p = Join-Path $BridgeDir $sub
    if (-not (Test-Path $p)) { New-Item -ItemType Directory -Force -Path $p | Out-Null }
}

$claude = (Get-Command claude -ErrorAction SilentlyContinue).Source
if (-not $claude) { Write-Error 'claude CLI not on PATH.'; exit 2 }

$runFlag = Join-Path $BridgeDir 'worker.run'
Set-Content -Path $runFlag -Value (Get-Date -Format o) -Encoding utf8

Write-Host "[claude_bridge] watching $BridgeDir  (claude: $claude)"
Write-Host "[claude_bridge] repo: $RepoDir   timeout: ${TimeoutSec}s"

function Write-Heartbeat {
    $hb = @{ at = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
             pid = $PID; repo = $RepoDir } | ConvertTo-Json -Compress
    $tmp = Join-Path $BridgeDir '.hb.tmp'
    Set-Content -Path $tmp -Value $hb -Encoding utf8
    Move-Item -Path $tmp -Destination (Join-Path $BridgeDir 'worker_heartbeat.json') -Force
}

function Complete-Job {
    param([string]$Id, [hashtable]$Result)
    $tmp = Join-Path $BridgeDir ".$Id.out.tmp"
    Set-Content -Path $tmp -Value ($Result | ConvertTo-Json -Compress -Depth 6) -Encoding utf8
    Move-Item -Path $tmp -Destination (Join-Path $BridgeDir "outbox\$Id.json") -Force
    $w = Join-Path $BridgeDir "working\$Id.json"
    if (Test-Path $w) { Remove-Item $w -Force }
}

while (Test-Path $runFlag) {
    Write-Heartbeat

    $jobs = @(Get-ChildItem -Path (Join-Path $BridgeDir 'inbox') -Filter '*.json' -File -ErrorAction SilentlyContinue |
              Sort-Object CreationTime)
    if ($jobs.Count -eq 0) { Start-Sleep -Milliseconds $PollMs; continue }

    foreach ($jf in $jobs) {
        $id = $jf.BaseName
        try   { $job = Get-Content $jf.FullName -Raw -Encoding utf8 | ConvertFrom-Json }
        catch {
            Complete-Job -Id $id -Result @{ state = 'error'; error = 'Unreadable job file.' }
            Remove-Item $jf.FullName -Force -ErrorAction SilentlyContinue
            continue
        }

        $working = Join-Path $BridgeDir "working\$id.json"
        Move-Item -Path $jf.FullName -Destination $working -Force

        $sid = [string]$job.session_id
        if ($sid -notmatch '^[0-9a-fA-F-]{8,64}$') {
            Complete-Job -Id $id -Result @{ state = 'error'; error = 'Bad session id.' }
            continue
        }

        # The prompt goes in via a file and stdin redirection, never on the
        # command line: it is operator text and may contain quotes, newlines and
        # shell metacharacters.
        $promptFile = Join-Path $BridgeDir "logs\$id.prompt.txt"
        $outFile    = Join-Path $BridgeDir "logs\$id.out.txt"
        $errFile    = Join-Path $BridgeDir "logs\$id.err.txt"
        [System.IO.File]::WriteAllText($promptFile, [string]$job.message, [System.Text.UTF8Encoding]::new($false))

        Write-Host ("[claude_bridge] {0} -> session {1} ({2} chars)" -f $id, $sid.Substring(0, 8), ([string]$job.message).Length)
        $t0 = Get-Date

        # cmd handles the redirection: PowerShell's -RedirectStandard* stalls on
        # a full pipe with a chatty child (recorded incident).
        $cmd = ('""{0}"" -p --resume {1} --fork-session --output-format text < ""{2}"" > ""{3}"" 2> ""{4}""' -f `
                $claude, $sid, $promptFile, $outFile, $errFile)
        $proc = Start-Process -FilePath $env:ComSpec -ArgumentList '/c', $cmd `
                              -WorkingDirectory $RepoDir -WindowStyle Hidden -PassThru

        if (-not $proc.WaitForExit($TimeoutSec * 1000)) {
            try { $proc.Kill() } catch { }
            Complete-Job -Id $id -Result @{ state = 'error'
                                            error = "Timed out after ${TimeoutSec}s." }
            continue
        }

        $dur   = [Math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
        $reply = ''
        $err   = ''
        if (Test-Path $outFile) { $reply = (Get-Content $outFile -Raw -Encoding utf8) }
        if (Test-Path $errFile) { $err   = (Get-Content $errFile -Raw -Encoding utf8) }

        if ($proc.ExitCode -ne 0 -and -not $reply) {
            $tail = if ($err) { ($err -split "`n" | Select-Object -Last 6) -join "`n" } else { '(no stderr)' }
            Complete-Job -Id $id -Result @{ state = 'error'
                                            error = "claude exited $($proc.ExitCode).`n$tail"
                                            duration_s = $dur }
            Write-Host ("[claude_bridge] {0} FAILED rc={1} in {2}s" -f $id, $proc.ExitCode, $dur)
            continue
        }

        # The fork wrote a new session file; report the newest one so the UI can
        # follow the reply in its own transcript.
        $forked = ''
        try {
            $projRoot = Join-Path $env:USERPROFILE '.claude\projects'
            $slugDir  = Get-ChildItem $projRoot -Directory |
                        Where-Object { $_.Name -like '*chili-home-copilot*' } |
                        Sort-Object LastWriteTime -Descending | Select-Object -First 1
            if ($slugDir) {
                $newest = Get-ChildItem $slugDir.FullName -Filter '*.jsonl' -File |
                          Where-Object { $_.LastWriteTime -ge $t0 -and $_.BaseName -ne $sid } |
                          Sort-Object LastWriteTime -Descending | Select-Object -First 1
                if ($newest) { $forked = $newest.BaseName }
            }
        } catch { }

        Complete-Job -Id $id -Result @{ state = 'done'; reply = $reply.TrimEnd()
                                        forked_session_id = $forked; duration_s = $dur }
        Write-Host ("[claude_bridge] {0} done in {1}s ({2} chars)" -f $id, $dur, $reply.Length)
    }
}

Write-Host '[claude_bridge] worker.run removed — stopping.'

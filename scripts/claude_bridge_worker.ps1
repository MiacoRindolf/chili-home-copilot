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

# PowerShell resolves `claude` to the .ps1 shim first, but the job runs under
# cmd.exe, which cannot execute a .ps1 at all.  Take the .cmd/.exe form.
$claudeCandidates = @(Get-Command claude -All -ErrorAction SilentlyContinue |
                      Select-Object -ExpandProperty Source)
$claude = $claudeCandidates | Where-Object { $_ -match '\.(cmd|exe|bat)$' } | Select-Object -First 1
if (-not $claude) {
    Write-Error 'No executable claude CLI found (need claude.cmd or claude.exe on PATH).'
    exit 2
}

$runFlag = Join-Path $BridgeDir 'worker.run'
Set-Content -Path $runFlag -Value (Get-Date -Format o) -Encoding utf8

Write-Host "[claude_bridge] watching $BridgeDir  (claude: $claude)"
Write-Host "[claude_bridge] repo: $RepoDir   timeout: ${TimeoutSec}s"

# Windows PowerShell's utf8 encoding stamps a BOM, and json.loads on the reader
# side rejects it outright -- so every JSON this worker writes goes out BOM-less.
$script:NoBom = [System.Text.UTF8Encoding]::new($false)

function Write-JsonAtomic {
    param([string]$Path, [string]$Json)
    $tmp = "$Path.tmp"
    [System.IO.File]::WriteAllText($tmp, $Json, $script:NoBom)
    Move-Item -Path $tmp -Destination $Path -Force
}

function Write-Heartbeat {
    $hb = @{ at = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
             pid = $PID; repo = $RepoDir } | ConvertTo-Json -Compress
    Write-JsonAtomic -Path (Join-Path $BridgeDir 'worker_heartbeat.json') -Json $hb
}

function Resolve-SessionCwd {
    # `claude --resume` scopes sessions to the working directory, so the job has
    # to run where the session was recorded -- not where this worker lives.  The
    # transcript stamps `cwd` on its records, so read it rather than trying to
    # decode the project slug (a dash in a real directory name makes that
    # ambiguous).
    param([string]$SessionId)
    try {
        $root = Join-Path $env:USERPROFILE '.claude\projects'
        $f = Get-ChildItem $root -Directory -ErrorAction SilentlyContinue | ForEach-Object {
                 Join-Path $_.FullName "$SessionId.jsonl"
             } | Where-Object { Test-Path $_ } | Select-Object -First 1
        if (-not $f) { return $null }
        foreach ($line in (Get-Content $f -Tail 80 -Encoding utf8 -ErrorAction Stop)) {
            if (-not $line.Trim()) { continue }
            try { $rec = $line | ConvertFrom-Json } catch { continue }
            if ($rec.cwd -and (Test-Path $rec.cwd)) { return [string]$rec.cwd }
        }
    } catch { }
    return $null
}

function Complete-Job {
    param([string]$Id, [hashtable]$Result)
    Write-JsonAtomic -Path (Join-Path $BridgeDir "outbox\$Id.json") `
                     -Json ($Result | ConvertTo-Json -Compress -Depth 6)
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

        $cwd = Resolve-SessionCwd -SessionId $sid
        if (-not $cwd) {
            Complete-Job -Id $id -Result @{ state = 'error'
                                            error = "No transcript found for session $sid, so its working directory is unknown." }
            continue
        }

        Write-Host ("[claude_bridge] {0} -> session {1} in {2} ({3} chars)" -f `
                    $id, $sid.Substring(0, 8), $cwd, ([string]$job.message).Length)
        $t0 = Get-Date

        # The command goes into a runner script rather than an argument string:
        # nesting quotes through PowerShell -> Start-Process -> cmd /c is where
        # these launchers break.  cmd owns the redirection because PowerShell's
        # -RedirectStandard* stalls on a full pipe with a chatty child (recorded
        # incident).
        $runner = Join-Path $BridgeDir "logs\$id.run.cmd"
        $script = @(
            '@echo off',
            "cd /d `"$cwd`"",
            "`"$claude`" -p --resume $sid --fork-session --output-format text < `"$promptFile`" > `"$outFile`" 2> `"$errFile`""
        ) -join "`r`n"
        [System.IO.File]::WriteAllText($runner, $script + "`r`n", [System.Text.Encoding]::ASCII)

        $proc = Start-Process -FilePath $env:ComSpec -ArgumentList '/c', "`"$runner`"" `
                              -WorkingDirectory $cwd -WindowStyle Hidden -PassThru

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

        # The CLI reports an auth failure on stdout with exit 0, which would
        # otherwise be rendered as if the agent had said it.  Name it instead:
        # only the operator can re-authorise the CLI.
        if ($reply -match 'Failed to authenticate|OAuth session expired|Invalid API key|Please run .?claude login') {
            Complete-Job -Id $id -Result @{
                state = 'error'
                error = "The claude CLI is not authorised for headless runs.`n" +
                        $reply.Trim() + "`n" +
                        'Sign the CLI in on this machine (or give the worker an API key), then resend.'
                duration_s = $dur }
            Write-Host ("[claude_bridge] {0} NOT AUTHORISED after {1}s" -f $id, $dur)
            continue
        }

        Complete-Job -Id $id -Result @{ state = 'done'; reply = $reply.TrimEnd()
                                        forked_session_id = $forked; duration_s = $dur }
        Write-Host ("[claude_bridge] {0} done in {1}s ({2} chars)" -f $id, $dur, $reply.Length)
    }
}

Write-Host '[claude_bridge] worker.run removed — stopping.'

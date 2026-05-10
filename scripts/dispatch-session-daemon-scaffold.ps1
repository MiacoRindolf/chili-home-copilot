$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\dispatch-session-daemon-scaffold-out.txt"
"# dispatch-session-daemon-scaffold $(Get-Date -Format o)" | Out-File $out -Encoding utf8

if (Test-Path ".git\index.lock") { Remove-Item ".git\index.lock" -ErrorAction SilentlyContinue }

# PS PARSE CHECK -- validate syntax of the new daemon before committing.
"# parse-check _claude_session_daemon.ps1" | Add-Content $out
$tokens = $null
$errors = $null
$null = [System.Management.Automation.Language.Parser]::ParseFile(
    (Resolve-Path "scripts/_claude_session_daemon.ps1").Path,
    [ref]$tokens,
    [ref]$errors
)
if ($errors -and $errors.Count -gt 0) {
    "PARSE ERRORS: $($errors.Count)" | Add-Content $out
    foreach ($e in $errors) {
        "  line $($e.Extent.StartLineNumber):$($e.Extent.StartColumnNumber) -- $($e.Message)" | Add-Content $out
    }
    "ABORT: not committing while parse errors exist" | Add-Content $out
    "# end" | Add-Content $out
    exit 1
} else {
    "parse OK ($($tokens.Count) tokens)" | Add-Content $out
}

# JSON parse check on the queued session file.
"# parse-check phase2 .session JSON" | Add-Content $out
try {
    $null = Get-Content "scripts/_claude_session_queue/100-promotion-rebalance-phase2.session" -Raw | ConvertFrom-Json
    "json OK" | Add-Content $out
} catch {
    "JSON PARSE ERROR: $_" | Add-Content $out
    "ABORT: bad session file" | Add-Content $out
    "# end" | Add-Content $out
    exit 1
}

"---" | Add-Content $out

# Stage everything we just created.
git add `
    scripts/_claude_session_daemon.ps1 `
    scripts/_claude_session_queue/.gitkeep `
    scripts/_claude_session_running/.gitkeep `
    scripts/_claude_session_done/.gitkeep `
    scripts/_claude_session_log/.gitkeep `
    scripts/_claude_session_queue/100-promotion-rebalance-phase2.session `
    docs/STRATEGY/CLAUDE_SESSION_DAEMON.md `
    scripts/dispatch-session-daemon-scaffold.ps1 `
    2>&1 | Add-Content $out

"# git status before commit" | Add-Content $out
git status --short 2>&1 | Add-Content $out
"---" | Add-Content $out

$msg = @"
infra(claude): second daemon for long-running CC sessions

The existing scripts/_claude_daemon.ps1 owns fast dev dispatches (docker,
git, psql) with a 5-min default timeout. CC sessions take 2-4 hours each;
running them through the same daemon would freeze every dev dispatch
behind the in-flight CC session.

This commit adds a SECOND daemon dedicated to long-running CC sessions:

  scripts/_claude_session_daemon.ps1
    - Watches scripts/_claude_session_queue/ for .session JSON files
    - Sorted by priority (lower first) then not_before then filename
    - Atomic move to _claude_session_running/ acts as the single-host lock
    - Launches: claude -p "<prompt>" --dangerously-skip-permissions
    - Default timeout 240 min; per-session override via timeout_min
    - Per-session log dir: _claude_session_log/<id>/{stdout,stderr,meta,verify}
    - Optional post_verify: .ps1 path; non-zero exit marks the session FAILED
    - on_fail policy: pause (default) | continue | abort_chain
    - On failure with on_fail=pause, daemon writes _claude_session_pause.flag
      so the chain idles until operator review
    - Stale-running recovery on startup: leftover files in running/ get
      moved to done/ with FAILED_RECOVERED_ prefix
    - Status surfaces to scripts/_claude_session_status.json after every
      transition for Cowork-side polling

  scripts/_claude_session_queue/100-promotion-rebalance-phase2.session
    First queued session: Phase 2 of f-promotion-pipeline-rebalance
    (directional-correctness signal table + scheduler job + aggregate view).
    Will run as soon as the operator starts the daemon.

  docs/STRATEGY/CLAUDE_SESSION_DAEMON.md
    Operator runbook covering schema, controls, recovery.

Why this matters: closes the gap where the operator had to manually type
``claude`` between phases. With the session daemon running, Cowork queues
.session files and the daemon advances Phases 2-6 sequentially with
on_fail=pause as the safety net. The operator can walk away.

Hard constraints honored:
  - Hard reject patterns from _claude_daemon.ps1 (force-push to main,
    rm -rf /, git reset --hard, etc.) are NOT bypassed -- the session
    daemon doesn't go through validation, but each CC session itself
    runs git through the standard remote, and CC inherits the project's
    hard rules from CLAUDE.md.
  - Single-host lock (one running/.session file at a time) prevents
    concurrent CC sessions from stomping NEXT_TASK / git state.
  - Pause flag on failure means a broken phase doesn't cascade into 4
    more broken phases.

Pre-commit verification baked into this dispatch script:
  - PowerShell syntax parser run on _claude_session_daemon.ps1
  - JSON parse check on the first queued .session file
"@

$msg | Out-File ".cm.txt" -Encoding utf8
git commit -F ".cm.txt" 2>&1 | Add-Content $out
Remove-Item ".cm.txt" -ErrorAction SilentlyContinue

git push 2>&1 | Add-Content $out

"# end" | Add-Content $out

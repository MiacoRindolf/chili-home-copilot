$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\dispatch-session-daemon-path-fix-out.txt"
"# dispatch-session-daemon-path-fix $(Get-Date -Format o)" | Out-File $out -Encoding utf8

if (Test-Path ".git\index.lock") { Remove-Item ".git\index.lock" -ErrorAction SilentlyContinue }

# DIAGNOSTIC: where does THIS shell find claude? This is the path the
# session daemon will resolve at startup when run from the same shell
# context.
"# diagnostic: Get-Command claude in this shell" | Add-Content $out
try {
    $cmd = Get-Command claude -ErrorAction Stop
    "claude.Source = $($cmd.Source)" | Add-Content $out
    "claude.CommandType = $($cmd.CommandType)" | Add-Content $out
} catch {
    "claude NOT FOUND on PATH in dispatch-shell: $_" | Add-Content $out
    "(this means the session daemon will also fail unless run from a shell where claude is on PATH)" | Add-Content $out
}
"---" | Add-Content $out

# Parse-check both .ps1 files
function Parse-Check {
    param([string]$Path)
    $tokens = $null
    $errors = $null
    $null = [System.Management.Automation.Language.Parser]::ParseFile(
        (Resolve-Path $Path).Path,
        [ref]$tokens,
        [ref]$errors
    )
    if ($errors -and $errors.Count -gt 0) {
        "PARSE ERRORS in ${Path}: $($errors.Count)" | Add-Content $out
        foreach ($e in $errors) {
            "  line $($e.Extent.StartLineNumber):$($e.Extent.StartColumnNumber) -- $($e.Message)" | Add-Content $out
        }
        return $false
    } else {
        "parse OK ${Path} ($($tokens.Count) tokens)" | Add-Content $out
        return $true
    }
}

$ok1 = Parse-Check "scripts/_claude_session_daemon.ps1"
$ok2 = Parse-Check "scripts/_claude_session_launcher.ps1"
if (-not $ok1 -or -not $ok2) {
    "ABORT: parse errors in one or more files" | Add-Content $out
    "# end" | Add-Content $out
    exit 1
}

try {
    $null = Get-Content "scripts/_claude_session_queue/130-promotion-rebalance-phase2-retry3.session" -Raw | ConvertFrom-Json
    "json OK retry3 session" | Add-Content $out
} catch {
    "JSON PARSE ERROR retry3 session: $_" | Add-Content $out
    "ABORT" | Add-Content $out
    "# end" | Add-Content $out
    exit 1
}

"---" | Add-Content $out

git add `
    scripts/_claude_session_daemon.ps1 `
    scripts/_claude_session_launcher.ps1 `
    scripts/_claude_session_queue/130-promotion-rebalance-phase2-retry3.session `
    scripts/dispatch-session-daemon-path-fix.ps1 `
    2>&1 | Add-Content $out

"# git status" | Add-Content $out
git status --short scripts/_claude_session* scripts/dispatch-session-daemon-path-fix.ps1 2>&1 | Add-Content $out
"---" | Add-Content $out

$msg = @"
fix(claude-session-daemon): resolve claude exe at startup; launcher fails loudly

Two bugs caught by retry2 forensics:

1. PATH not propagated to launcher
   The daemon spawns powershell.exe -NoProfile to run the launcher.
   -NoProfile means \$PROFILE doesn't run, so PATH is just the system/user
   PATH from environment variables. The npm prefix where claude.cmd lives
   is typically added by \$PROFILE only, so the launcher's `& claude` got
   CommandNotFoundException ("The term 'claude' is not recognized").

   Fix: daemon now calls Get-Command claude at startup (where its own
   PowerShell session has \$PROFILE-loaded PATH), captures the absolute
   path, and threads it through to the launcher as -ClaudeExe. Falls back
   to probing common npm-global locations if Get-Command fails.

2. Launcher reported success on CommandNotFoundException
   When `& claude @args` failed because claude wasn't found, PowerShell
   left \$LASTEXITCODE at its previous value (0 from before), so the
   launcher exited 0 and the daemon marked the session passed=true. This
   produced false positives where a session "succeeded" in 0.6 seconds
   without doing any work.

   Fix:
     - Launcher now requires -ClaudeExe parameter and Test-Path verifies
       it exists before invocation
     - Resets \$LASTEXITCODE to \$null before the call
     - Treats null \$LASTEXITCODE after the call as failure (exit 6)
     - Wraps the invocation in try/catch and exits 5 on exception
     - Daemon adds defense-in-depth: any session with duration < 5s AND
       non-empty stderr is marked failed even if exit code says 0

Operator side after this commit lands:
  1. git pull
  2. STOP the running session daemon (Ctrl+C in its window)
  3. RESTART it: .\\scripts\\_claude_session_daemon.ps1
     (must restart -- old daemon has the buggy code in memory)
  4. Daemon will log: "claude resolved to: <path>" at startup
  5. Remove the pause flag: rm scripts/_claude_session_pause.flag
  6. The 130-promotion-rebalance-phase2-retry3.session will be picked up.

Diagnostic in this dispatch:
  This script logs Get-Command claude from the dev-daemon's shell. If
  that shell has claude on PATH, the session daemon (started from the
  same shell) will also have it.
"@

$msg | Out-File ".cm.txt" -Encoding utf8
git commit -F ".cm.txt" 2>&1 | Add-Content $out
Remove-Item ".cm.txt" -ErrorAction SilentlyContinue

git push 2>&1 | Add-Content $out

"# end" | Add-Content $out

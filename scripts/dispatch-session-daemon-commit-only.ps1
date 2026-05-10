$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\dispatch-session-daemon-commit-only-out.txt"
"# dispatch-session-daemon-commit-only $(Get-Date -Format o)" | Out-File $out -Encoding utf8

if (Test-Path ".git\index.lock") { Remove-Item ".git\index.lock" -ErrorAction SilentlyContinue }

# Parse-check both .ps1 files (daemon now in-tree at modified state; we
# want to commit it).
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
    "ABORT: parse errors in daemon or launcher" | Add-Content $out
    "# end" | Add-Content $out
    exit 1
}

"---" | Add-Content $out

# Commit ONLY the persistent infra changes. Do NOT touch queue/running/
# done/ -- those are transient state, daemon may move files between
# `git add` and `git commit`.
git add `
    scripts/_claude_session_daemon.ps1 `
    scripts/_claude_session_launcher.ps1 `
    scripts/dispatch-session-daemon-path-fix.ps1 `
    scripts/dispatch-session-daemon-commit-only.ps1 `
    2>&1 | Add-Content $out

"# git status pre-commit" | Add-Content $out
git status --short scripts/_claude_session_daemon.ps1 scripts/_claude_session_launcher.ps1 scripts/dispatch-session-daemon-path-fix.ps1 scripts/dispatch-session-daemon-commit-only.ps1 2>&1 | Add-Content $out
"---" | Add-Content $out

$msg = @"
fix(claude-session-daemon): resolve claude exe at startup; launcher fails loudly

Two bugs caught by retry2 + retry3 forensics:

1. PATH not propagated to launcher
   The daemon spawns powershell.exe -NoProfile to run the launcher.
   -NoProfile means \$PROFILE doesn't run, so PATH is just the system/user
   PATH from environment variables. The npm prefix where claude.cmd lives
   is typically added by \$PROFILE only, so the launcher's `& claude` got
   CommandNotFoundException ("The term 'claude' is not recognized").

   Fix: daemon now resolves claude at startup with Get-Command (in its
   \$PROFILE-loaded session), captures the absolute path, and threads it
   through to the launcher as -ClaudeExe. Falls back to probing common
   npm-global locations if Get-Command fails. Operator's claude lives at
   ~/.local/bin/claude.exe (verified by retry4 daemon startup log).

2. Launcher reported success on CommandNotFoundException
   When `& claude @args` failed because claude wasn't found, PowerShell
   left \$LASTEXITCODE at its previous value (0 from before), so the
   launcher exited 0 and the daemon marked the session passed=true. This
   produced retry2's false positive (passed=true, duration=0.6s, no work
   actually done).

   Fix:
     - Launcher now requires -ClaudeExe parameter and Test-Path verifies
       it exists before invocation
     - Resets \$LASTEXITCODE to \$null before the call
     - Treats null \$LASTEXITCODE after the call as failure (exit 6)
     - Wraps the invocation in try/catch and exits 5 on exception
     - Daemon adds defense-in-depth: any session with duration < 5s AND
       non-empty stderr is marked failed even if exit code says 0

Verified: retry4 daemon at 22:35:35 logged "claude resolved to:
C:\\Users\\rindo\\.local\\bin\\claude.exe" before entering its main loop.

This commit captures the persistent infra changes only -- queue/running/
done/ contents are transient and aren't tracked in commits.
"@

$msg | Out-File ".cm.txt" -Encoding utf8
git commit -F ".cm.txt" 2>&1 | Add-Content $out
Remove-Item ".cm.txt" -ErrorAction SilentlyContinue

git push 2>&1 | Add-Content $out

"# end" | Add-Content $out

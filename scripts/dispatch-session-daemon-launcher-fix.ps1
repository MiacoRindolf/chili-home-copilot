$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\dispatch-session-daemon-launcher-fix-out.txt"
"# dispatch-session-daemon-launcher-fix $(Get-Date -Format o)" | Out-File $out -Encoding utf8

if (Test-Path ".git\index.lock") { Remove-Item ".git\index.lock" -ErrorAction SilentlyContinue }

# PARSE-CHECK both .ps1 files. If either has parse errors, abort BEFORE
# committing -- a broken daemon would loop-fail every session.
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

# JSON parse check on the retry .session file
try {
    $null = Get-Content "scripts/_claude_session_queue/110-promotion-rebalance-phase2-retry.session" -Raw | ConvertFrom-Json
    "json OK retry session" | Add-Content $out
} catch {
    "JSON PARSE ERROR retry session: $_" | Add-Content $out
    "ABORT" | Add-Content $out
    "# end" | Add-Content $out
    exit 1
}

"---" | Add-Content $out

git add `
    scripts/_claude_session_daemon.ps1 `
    scripts/_claude_session_launcher.ps1 `
    scripts/_claude_session_queue/110-promotion-rebalance-phase2-retry.session `
    scripts/dispatch-session-daemon-launcher-fix.ps1 `
    2>&1 | Add-Content $out

"# git status before commit" | Add-Content $out
git status --short scripts/_claude_session* scripts/dispatch-session-daemon-launcher-fix.ps1 2>&1 | Add-Content $out
"---" | Add-Content $out

$msg = @"
fix(claude-session-daemon): launch via .ps1 bridge so claude.cmd resolves

Symptom: first Phase 2 session failed in 0.1s with exit -1 and stderr
"EXCEPTION launching claude: This command cannot be run due to the
error: The system cannot find the file specified."

Root cause: claude on Windows is installed as a .cmd shim. PowerShell's
interactive command resolver finds claude.cmd via PATH, but
Start-Process -FilePath 'claude' does NOT auto-resolve .cmd extensions.
It tries to launch a literal file named 'claude' which doesn't exist.

Fix: spawn powershell.exe with -Command "& 'scripts/_claude_session_launcher.ps1'
-ArgsFile <path>". The launcher uses PS's & operator, which natively
resolves .cmd shims. This mirrors the pattern _claude_daemon.ps1 already
uses for .ps1 commands.

Args are passed via a JSON file written into the session log dir, so
multi-line prompts with quotes / newlines survive intact (avoiding
cross-process arg-quoting hell).

Operator side after this commit lands:
  1. git pull
  2. Stop the running session daemon (Ctrl+C in its PS window OR touch
     scripts/_claude_session_stop.flag)
  3. Restart it: .\scripts\_claude_session_daemon.ps1
  4. Remove the pause flag: rm scripts/_claude_session_pause.flag
  5. The 110-promotion-rebalance-phase2-retry.session file in the queue
     will be picked up at the next 30s poll.

The original 100-*.session was already moved to done/ as FAILED on the
first run; no cleanup needed there.
"@

$msg | Out-File ".cm.txt" -Encoding utf8
git commit -F ".cm.txt" 2>&1 | Add-Content $out
Remove-Item ".cm.txt" -ErrorAction SilentlyContinue

git push 2>&1 | Add-Content $out

"# end" | Add-Content $out

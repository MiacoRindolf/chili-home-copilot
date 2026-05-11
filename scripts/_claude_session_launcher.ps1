# Bridge invoked by _claude_session_daemon.ps1 to launch `claude`.
#
# Two reasons for the bridge:
# 1. Start-Process -FilePath 'claude' on Windows fails because claude is
#    installed as a .cmd shim. PowerShell's `&` operator handles .cmd
#    shims natively when given an absolute path.
# 2. Args are passed via a JSON file rather than as PS arguments to avoid
#    cross-process arg-quoting hell when prompts contain spaces, quotes,
#    or newlines.
#
# The daemon resolves `claude` to an absolute path at daemon startup (in
# its interactive-shell context, with PATH from $PROFILE) and passes it
# here as -ClaudeExe. Without that, this script cannot rely on its own
# Get-Command lookup because the daemon spawns powershell.exe with
# -NoProfile, so the spawned shell does not have $PROFILE-set PATH.

param(
    [Parameter(Mandatory=$true)]
    [string]$ArgsFile,

    [Parameter(Mandatory=$true)]
    [string]$ClaudeExe
)

$ErrorActionPreference = "Continue"

if (-not (Test-Path $ClaudeExe)) {
    Write-Error "[_claude_session_launcher] claude executable not found at: $ClaudeExe"
    exit 4
}

if (-not (Test-Path $ArgsFile)) {
    Write-Error "[_claude_session_launcher] args file not found: $ArgsFile"
    exit 2
}

try {
    $argList = Get-Content $ArgsFile -Raw -Encoding utf8 | ConvertFrom-Json
} catch {
    Write-Error "[_claude_session_launcher] could not parse args file as JSON: $_"
    exit 3
}

if ($argList -isnot [array] -and $argList -isnot [System.Collections.IList]) {
    $argList = @($argList)
}

$strArgs = @()
foreach ($a in $argList) { $strArgs += [string]$a }

Write-Host "[_claude_session_launcher] resolved $($strArgs.Count) args; invoking $ClaudeExe"

# Reset $LASTEXITCODE before invocation so a stale value doesn't mask a
# failure where `&` cannot launch the target at all (CommandNotFound,
# missing file, etc.).
$global:LASTEXITCODE = $null

try {
    & $ClaudeExe @strArgs
} catch {
    Write-Error "[_claude_session_launcher] FAILED to invoke $($ClaudeExe): $_"
    exit 5
}

$claudeExit = $LASTEXITCODE
if ($null -eq $claudeExit) {
    # Native exit code never set means the binary didn't actually run.
    Write-Error "[_claude_session_launcher] claude did not produce an exit code (likely never invoked)"
    exit 6
}

Write-Host "[_claude_session_launcher] claude exited with code $claudeExit"

# --- Codex fallback (2026-05-11) ---------------------------------------
# When claude exits non-zero AND its stdout contains a usage-limit
# signal, optionally re-run the same prompt via codex CLI. Gated by the
# .session JSON's fallback_policy + task_complexity fields. Default is
# "pause" — sessions opt in per-file by setting fallback_policy="auto"
# or "codex". High-complexity (task_complexity="high") still pauses.
#
# Env contract (set by _claude_session_daemon.ps1):
#   CHILI_SESSION_FILE    -> .session JSON path (in _running/)
#   CHILI_SESSION_STDOUT  -> path to the redirected stdout this launcher
#                            was writing through; we grep it for the
#                            limit signal after claude exits.
# -----------------------------------------------------------------------

if ($claudeExit -ne 0 -and $env:CHILI_SESSION_STDOUT -and (Test-Path $env:CHILI_SESSION_STDOUT)) {
    $stdoutContent = ""
    try { $stdoutContent = Get-Content $env:CHILI_SESSION_STDOUT -Raw -ErrorAction Stop } catch {}
    $isLimit = $stdoutContent -match "(?i)monthly usage limit|hit your org's.*limit|rate.{0,5}limit.{0,30}exceeded"

    if ($isLimit) {
        Write-Host "[_claude_session_launcher] claude usage-limit signal detected in stdout"
        $session = $null
        if ($env:CHILI_SESSION_FILE -and (Test-Path $env:CHILI_SESSION_FILE)) {
            try { $session = Get-Content $env:CHILI_SESSION_FILE -Raw | ConvertFrom-Json } catch {}
        }
        if ($null -eq $session) {
            Write-Host "[_claude_session_launcher] no readable session file; cannot evaluate fallback"
            exit $claudeExit
        }

        $policy     = if ($session.PSObject.Properties['fallback_policy'])  { [string]$session.fallback_policy }  else { "pause" }
        $complexity = if ($session.PSObject.Properties['task_complexity']) { [string]$session.task_complexity } else { "high" }
        $codexModel = if ($session.PSObject.Properties['codex_model'])     { [string]$session.codex_model }     else { "gpt-5-codex" }
        if ([string]::IsNullOrWhiteSpace($policy))     { $policy = "pause" }
        if ([string]::IsNullOrWhiteSpace($complexity)) { $complexity = "high" }
        if ([string]::IsNullOrWhiteSpace($codexModel)) { $codexModel = "gpt-5-codex" }

        Write-Host "[_claude_session_launcher] fallback gate: policy=$policy complexity=$complexity model=$codexModel"

        $canAutoFallback = ($policy -eq "auto" -or $policy -eq "codex") -and ($complexity -ne "high")

        if (-not $canAutoFallback) {
            Write-Host "[_claude_session_launcher] NOT eligible for auto-fallback (policy=$policy, complexity=$complexity). Session will pause for operator review."
            exit $claudeExit
        }

        $codexCmd = Get-Command codex -ErrorAction SilentlyContinue
        if (-not $codexCmd) {
            Write-Host "[_claude_session_launcher] codex CLI not on PATH; cannot fall back"
            exit $claudeExit
        }

        $basePrompt = if ($session.PSObject.Properties['prompt']) { [string]$session.prompt } else { "" }
        if ([string]::IsNullOrWhiteSpace($basePrompt)) {
            Write-Host "[_claude_session_launcher] session has no .prompt to relay; cannot fall back"
            exit $claudeExit
        }

        $fallbackNotice = @"

[FALLBACK NOTICE 2026-05-11]
This session is being executed by codex ($codexModel) because Claude hit a
usage limit. Operator pre-authorized fallback for this session
(fallback_policy=$policy, task_complexity=$complexity).

Conventions to follow:
1. Tag any CC_REPORT filename with .codex.md (e.g.
   docs/STRATEGY/CC_REPORTS/<date>_<task>.codex.md). Cowork-watcher will
   surface this as OPERATOR_REVIEW_REQUIRED rather than auto-promote
   the next phase.
2. If you need plan-gate consult, write your plan to
   scripts/_claude_session_consult/$env:CHILI_SESSION_ID/plan.request.md
   and exit. The operator approval flow is the same.
3. All other hard constraints from the brief still apply unchanged.
"@

        $codexPrompt = $basePrompt + $fallbackNotice

        Write-Host "[_claude_session_launcher] invoking codex --model $codexModel (prompt length $($codexPrompt.Length))"
        $global:LASTEXITCODE = $null
        try {
            & codex --model $codexModel exec $codexPrompt
        } catch {
            Write-Error "[_claude_session_launcher] codex invocation threw: $_"
            exit $claudeExit
        }
        $codexExit = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } else { -1 }
        Write-Host "[_claude_session_launcher] codex exited with code $codexExit"
        exit $codexExit
    }
}

exit $claudeExit

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
exit $claudeExit

# Bridge invoked by _claude_session_daemon.ps1 to launch `claude`.
#
# Why a bridge: Start-Process -FilePath 'claude' on Windows fails because
# claude is installed as a .cmd shim (npm-style), and Start-Process does
# not auto-resolve .cmd extensions the way PowerShell's interactive
# command resolver does. PowerShell's `&` operator DOES resolve .cmd
# shims via PATH, so this script just splats the args and invokes.
#
# The args are passed via a JSON file path (rather than as PS arguments)
# to avoid double-quoting hell when prompts contain spaces, quotes, or
# newlines.
#
# Exit code: forwarded from claude's exit code so the daemon can detect
# failure.

param(
    [Parameter(Mandatory=$true)]
    [string]$ArgsFile
)

$ErrorActionPreference = "Continue"

if (-not (Test-Path $ArgsFile)) {
    Write-Error "args file not found: $ArgsFile"
    exit 2
}

try {
    $argList = Get-Content $ArgsFile -Raw -Encoding utf8 | ConvertFrom-Json
} catch {
    Write-Error "could not parse args file as JSON: $_"
    exit 3
}

if ($argList -isnot [array] -and $argList -isnot [System.Collections.IList]) {
    # PowerShell's ConvertFrom-Json will return a single object for a
    # one-element array in some cases; coerce to array.
    $argList = @($argList)
}

# Cast each element to string explicitly. JSON deserialization can
# produce non-string types if a value happens to look numeric.
$strArgs = @()
foreach ($a in $argList) { $strArgs += [string]$a }

Write-Host "[_claude_session_launcher] resolved $($strArgs.Count) args; invoking claude"

& claude @strArgs

$claudeExit = $LASTEXITCODE
Write-Host "[_claude_session_launcher] claude exited with code $claudeExit"
exit $claudeExit

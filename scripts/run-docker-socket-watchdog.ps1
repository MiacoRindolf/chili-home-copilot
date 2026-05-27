param(
    [int]$WarnBoundSockets = 2000,
    [int]$CriticalDockerBoundSockets = 8000,
    [string]$LogPath = ""
)

$ErrorActionPreference = "Stop"

$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $PSCommandPath }
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $scriptDir "..")).Path
$repairScript = Join-Path $scriptDir "repair-docker-socket-exhaustion.ps1"

if ([string]::IsNullOrWhiteSpace($LogPath)) {
    $LogPath = Join-Path $scriptDir "watcher-out\docker-socket-watchdog.log"
}

& $repairScript `
    -Repair `
    -WarnBoundSockets $WarnBoundSockets `
    -CriticalDockerBoundSockets $CriticalDockerBoundSockets `
    -ComposeDir $repoRoot `
    -LogPath $LogPath

exit $LASTEXITCODE

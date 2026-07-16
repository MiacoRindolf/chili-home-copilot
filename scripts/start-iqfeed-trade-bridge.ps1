[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExecutable,

    [Parameter(Mandatory = $true)]
    [string]$ManifestPath,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{64}$')]
    [string]$ManifestSha256,

    [Parameter(Mandatory = $true)]
    [string[]]$AllowedReadRoot,

    [Parameter(Mandatory = $true)]
    [string[]]$AllowedWriteRoot,

    [Parameter(Mandatory = $true)]
    [switch]$ValidateOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Compatibility entry point only.  It no longer starts IQConnect, searches by
# a Python basename, or launches a hard-coded legacy worktree.  The four
# currently installed legacy tasks remain untouched until an approved cutover;
# any future task targeting this candidate source must pass the exact immutable
# validation inputs to the unified host launcher.
$launcher = Join-Path $PSScriptRoot 'start-iqfeed-capture-host.ps1'
& $launcher `
    -PythonExecutable $PythonExecutable `
    -ManifestPath $ManifestPath `
    -ManifestSha256 $ManifestSha256 `
    -AllowedReadRoot $AllowedReadRoot `
    -AllowedWriteRoot $AllowedWriteRoot `
    -ValidateOnly:$ValidateOnly
if ($LASTEXITCODE -ne 0) {
    throw "Unified IQFeed capture-host validation failed with exit code $LASTEXITCODE"
}

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

if (-not $ValidateOnly) {
    throw 'IQFeed capture-host activation is not recertified; ValidateOnly is required.'
}

function Resolve-StrictLocalPath {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [Parameter(Mandatory = $true)][bool]$RequireFile
    )

    if ($LiteralPath.StartsWith('\\') -or $LiteralPath.StartsWith('//')) {
        throw "UNC paths are prohibited: $LiteralPath"
    }
    $item = Get-Item -LiteralPath $LiteralPath -Force -ErrorAction Stop
    $cursor = $item
    while ($null -ne $cursor) {
        if (($cursor.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Reparse-point paths are prohibited: $LiteralPath"
        }
        $cursor = $cursor.Parent
    }
    if ($RequireFile -and $item.PSIsContainer) {
        throw "Expected a file: $LiteralPath"
    }
    if ((-not $RequireFile) -and (-not $item.PSIsContainer)) {
        throw "Expected a directory: $LiteralPath"
    }
    return $item.FullName
}

$launcherPath = Resolve-StrictLocalPath -LiteralPath $PSCommandPath -RequireFile $true
$scriptDirectory = Split-Path -Parent $launcherPath
$hostScript = Resolve-StrictLocalPath `
    -LiteralPath (Join-Path $scriptDirectory 'iqfeed_capture_host.py') `
    -RequireFile $true
$pythonPath = Resolve-StrictLocalPath -LiteralPath $PythonExecutable -RequireFile $true
$manifest = Resolve-StrictLocalPath -LiteralPath $ManifestPath -RequireFile $true
$readRoots = @($AllowedReadRoot | ForEach-Object {
    Resolve-StrictLocalPath -LiteralPath $_ -RequireFile $false
})
$writeRoots = @($AllowedWriteRoot | ForEach-Object {
    Resolve-StrictLocalPath -LiteralPath $_ -RequireFile $false
})
$launcherSha256 = (Get-FileHash -LiteralPath $launcherPath -Algorithm SHA256).Hash.ToLowerInvariant()

$arguments = @(
    '-B',
    $hostScript,
    '--validate-only',
    '--launcher-path', $launcherPath,
    '--launcher-sha256', $launcherSha256,
    '--python-executable', $pythonPath,
    '--manifest', $manifest,
    '--manifest-sha256', $ManifestSha256.ToLowerInvariant()
)
foreach ($root in $readRoots) {
    $arguments += @('--allow-read-root', $root)
}
foreach ($root in $writeRoots) {
    $arguments += @('--allow-write-root', $root)
}

& $pythonPath @arguments
if ($LASTEXITCODE -ne 0) {
    throw "IQFeed capture-host validation rejected with exit code $LASTEXITCODE"
}

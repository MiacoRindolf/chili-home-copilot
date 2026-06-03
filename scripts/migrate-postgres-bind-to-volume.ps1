<#
.SYNOPSIS
Safely copy the Compose Postgres data directory from the Windows bind mount to a Docker named volume.

.DESCRIPTION
This helper is intentionally conservative:
  - default mode is a dry run;
  - it refuses to copy unless the source contains PG_VERSION;
  - it refuses to copy into a non-empty target volume;
  - it stops only the postgres Compose service when -Execute is supplied;
  - it never deletes the original D:\CHILI-Docker\postgres directory.

After a successful copy, set CHILI_POSTGRES_DATA_SOURCE=chili-postgres-data in
the environment or .env, then recreate only the postgres service.
#>

[CmdletBinding()]
param(
    [string]$ProjectDir = "",
    [string]$SourceDir = "D:\CHILI-Docker\postgres",
    [string]$VolumeName = "chili-postgres-data",
    [switch]$Execute,
    [switch]$UpdateEnvFile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "[postgres-volume] $Message"
}

function Require-AbsoluteSafePath {
    param([string]$PathValue)

    $resolved = Resolve-Path -LiteralPath $PathValue -ErrorAction Stop
    $full = [System.IO.Path]::GetFullPath($resolved.Path)
    $root = [System.IO.Path]::GetPathRoot($full)
    if (-not $root -or $full.TrimEnd("\") -eq $root.TrimEnd("\")) {
        throw "Refusing unsafe source path: $full"
    }
    return $full
}

function Invoke-Logged {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )

    Write-Step ("$FilePath " + ($Arguments -join " "))
    if (-not $Execute) {
        return
    }
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath"
    }
}

if ([string]::IsNullOrWhiteSpace($ProjectDir)) {
    $ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

$project = Require-AbsoluteSafePath -PathValue $ProjectDir
$source = Require-AbsoluteSafePath -PathValue $SourceDir
$pgVersion = Join-Path $source "PG_VERSION"
if (-not (Test-Path -LiteralPath $pgVersion)) {
    throw "Source does not look like a Postgres data directory; missing $pgVersion"
}

if ($VolumeName -notmatch "^[A-Za-z0-9][A-Za-z0-9_.-]*$") {
    throw "Unsafe Docker volume name: $VolumeName"
}

Write-Step "project: $project"
Write-Step "source: $source"
Write-Step "target volume: $VolumeName"
Write-Step ("mode: " + ($(if ($Execute) { "execute" } else { "dry-run" })))

Push-Location $project
try {
    Invoke-Logged docker @("compose", "stop", "postgres")
    Invoke-Logged docker @("volume", "create", $VolumeName)

    $emptyCheck = @(
        "run", "--rm",
        "-v", "${VolumeName}:/to",
        "alpine:3.20",
        "sh", "-lc",
        'test -z "$(ls -A /to 2>/dev/null)"'
    )
    Invoke-Logged docker $emptyCheck

    $copyArgs = @(
        "run", "--rm",
        "-v", "${source}:/from:ro",
        "-v", "${VolumeName}:/to",
        "alpine:3.20",
        "sh", "-lc",
        'test -f /from/PG_VERSION && cp -a /from/. /to/ && test -f /to/PG_VERSION'
    )
    Invoke-Logged docker $copyArgs

    if ($UpdateEnvFile) {
        $envFile = Join-Path $project ".env"
        $line = "CHILI_POSTGRES_DATA_SOURCE=$VolumeName"
        if (-not $Execute) {
            Write-Step "would update $envFile with $line"
        } else {
            if (Test-Path -LiteralPath $envFile) {
                $backup = "$envFile.postgres-volume.bak"
                Copy-Item -LiteralPath $envFile -Destination $backup -Force
                $content = Get-Content -LiteralPath $envFile -Raw
                if ($content -match "(?m)^CHILI_POSTGRES_DATA_SOURCE=") {
                    $content = $content -replace "(?m)^CHILI_POSTGRES_DATA_SOURCE=.*$", $line
                } else {
                    $content = $content.TrimEnd() + [Environment]::NewLine + $line + [Environment]::NewLine
                }
                Set-Content -LiteralPath $envFile -Value $content -Encoding UTF8
                Write-Step "updated .env and wrote backup $backup"
            } else {
                Set-Content -LiteralPath $envFile -Value ($line + [Environment]::NewLine) -Encoding UTF8
                Write-Step "created .env with $line"
            }
        }
    }

    Write-Step "next: set CHILI_POSTGRES_DATA_SOURCE=$VolumeName and run docker compose up -d --force-recreate postgres"
    Write-Step "the original source directory was left untouched: $source"
}
finally {
    Pop-Location
}

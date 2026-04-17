<#
.SYNOPSIS
  Fails if any log line is a Phase L.19 cross-asset release blocker.

.DESCRIPTION
  A line is a BLOCKER if it contains ANY of:
    - [cross_asset_ops] event=cross_asset_persisted mode=authoritative
    - [cross_asset_ops] event=cross_asset_refused_authoritative

  Phase L.19 ships the cross-asset lead/lag panel in shadow mode only.
  Phase L.19.2 will open the authoritative path with explicit
  governance approval; until then an authoritative cross_asset event
  means a config drift got into a deploy.

  Optionally, a JSON dump of the
  /api/trading/brain/cross-asset/diagnostics endpoint can be read from
  disk via -DiagnosticsJson. The gate fails when:
    * ``mean_coverage_score < MinCoverageScore``
    * ``snapshots_total < MinSnapshots`` (only if MinSnapshots > 0)

  Exit 0 = no blocker lines found (and gates pass, if provided).
  Exit 1 = one or more blocker lines / failed diagnostics gate.
  Exit 2 = file not found (when using -Path or -DiagnosticsJson).
  Exit 3 = malformed JSON passed via -DiagnosticsJson.

.EXAMPLE
  docker compose logs chili --since 30m 2>&1 | .\scripts\check_cross_asset_release_blocker.ps1

.EXAMPLE
  curl -sk https://localhost:8000/api/trading/brain/cross-asset/diagnostics -o ca.json
  .\scripts\check_cross_asset_release_blocker.ps1 -DiagnosticsJson .\ca.json -MinCoverageScore 0.5
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromPipeline = $true)]
    [psobject] $InputObject,
    [Parameter()]
    [string] $Path,
    [Parameter()]
    [string] $DiagnosticsJson,
    [Parameter()]
    [double] $MinCoverageScore = 0.0,
    [Parameter()]
    [int] $MinSnapshots = 0
)

begin {
    $blockers = [System.Collections.Generic.List[string]]::new()

    function Test-ReleaseBlockerLine {
        param([string] $Line)
        if ([string]::IsNullOrEmpty($Line)) { return $false }
        if (-not $Line.Contains("[cross_asset_ops]")) { return $false }
        if ($Line.Contains("event=cross_asset_persisted") -and $Line.Contains("mode=authoritative")) {
            return $true
        }
        if ($Line.Contains("event=cross_asset_refused_authoritative")) {
            return $true
        }
        return $false
    }

    function Add-LineIfBlocker {
        param([string] $Line)
        if (Test-ReleaseBlockerLine -Line $Line) {
            [void]$blockers.Add($Line)
        }
    }
}

process {
    if ($Path) { return }
    if ($null -ne $InputObject) {
        Add-LineIfBlocker -Line $InputObject.ToString()
    }
}

end {
    if ($Path) {
        if (-not (Test-Path -LiteralPath $Path)) {
            Write-Error "File not found: $Path"
            exit 2
        }
        Get-Content -LiteralPath $Path -ErrorAction Stop | ForEach-Object {
            Add-LineIfBlocker -Line $_
        }
    }

    if ($blockers.Count -gt 0) {
        Write-Error "Release blocker: $($blockers.Count) line(s) match [cross_asset_ops] authoritative/refused patterns"
        foreach ($b in $blockers) {
            [Console]::Error.WriteLine($b)
        }
        exit 1
    }

    if ($DiagnosticsJson) {
        if (-not (Test-Path -LiteralPath $DiagnosticsJson)) {
            Write-Error "File not found: $DiagnosticsJson"
            exit 2
        }
        try {
            $payload = Get-Content -Raw -LiteralPath $DiagnosticsJson | ConvertFrom-Json
        } catch {
            Write-Error "Malformed JSON in $DiagnosticsJson : $($_.Exception.Message)"
            exit 3
        }
        $ca = $payload.cross_asset
        if ($null -eq $ca) { $ca = $payload }

        $total = [int]($ca.snapshots_total | ForEach-Object { $_ })
        $coverage = [double]($ca.mean_coverage_score | ForEach-Object { $_ })

        if ($MinSnapshots -gt 0 -and $total -lt $MinSnapshots) {
            Write-Error "Release blocker: snapshots_total=$total < MinSnapshots=$MinSnapshots"
            exit 1
        }
        if ($MinCoverageScore -gt 0.0 -and $coverage -lt $MinCoverageScore) {
            Write-Error "Release blocker: mean_coverage_score=$coverage < MinCoverageScore=$MinCoverageScore"
            exit 1
        }
    }

    exit 0
}

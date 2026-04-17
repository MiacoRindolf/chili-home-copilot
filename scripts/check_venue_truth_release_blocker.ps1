<#
.SYNOPSIS
  Fails if any log line is a Phase-F venue-truth release blocker.

.DESCRIPTION
  A line is a BLOCKER if it contains BOTH:
    - [venue_truth_ops]
    - mode=authoritative

  Phase F rolls out venue-truth telemetry in shadow mode only; an
  ``authoritative`` log line means the cutover leaked into a non-
  authoritative deploy.

  Separately (optional via -DiagnosticsJson) a JSON dump of the
  /api/trading/brain/venue-truth/diagnostics endpoint can be piped in
  or read from disk. The gate fails when
  ``observations_total < MinObservations`` (enforce the hook actually
  ran) or when ``mean_gap_bps`` exceeds ``MaxMeanGapBps`` — a blow-up
  in that metric means our cost model is badly mis-calibrated against
  the venue.

  Exit 0 = no blocker lines found (and gates pass, if provided).
  Exit 1 = one or more blocker lines / failed diagnostics gate.
  Exit 2 = file not found (when using -Path or -DiagnosticsJson).
  Exit 3 = malformed JSON passed via -DiagnosticsJson.

.EXAMPLE
  docker compose logs chili --since 30m 2>&1 | .\scripts\check_venue_truth_release_blocker.ps1

.EXAMPLE
  curl -sk https://localhost:8000/api/trading/brain/venue-truth/diagnostics -o vt.json
  .\scripts\check_venue_truth_release_blocker.ps1 -DiagnosticsJson .\vt.json `
      -MinObservations 5 -MaxMeanGapBps 15
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
    [int] $MinObservations = 0,
    [Parameter()]
    [double] $MaxMeanGapBps = 0.0
)

begin {
    $blockers = [System.Collections.Generic.List[string]]::new()

    function Test-ReleaseBlockerLine {
        param([string] $Line)
        if ([string]::IsNullOrEmpty($Line)) { return $false }
        return $Line.Contains("[venue_truth_ops]") -and
               $Line.Contains("mode=authoritative")
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
        Write-Error "Release blocker: $($blockers.Count) line(s) match [venue_truth_ops] mode=authoritative"
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
        $vt = $payload.venue_truth
        if ($null -eq $vt) { $vt = $payload }

        $total = [int]($vt.observations_total | ForEach-Object { $_ })

        if ($MinObservations -gt 0 -and $total -lt $MinObservations) {
            Write-Error "Release blocker: observations_total=$total < MinObservations=$MinObservations"
            exit 1
        }

        if ($MaxMeanGapBps -gt 0 -and $total -gt 0) {
            $gap = $vt.mean_gap_bps
            if ($null -ne $gap -and [double]$gap -gt $MaxMeanGapBps) {
                Write-Error "Release blocker: mean_gap_bps=$gap > MaxMeanGapBps=$MaxMeanGapBps"
                exit 1
            }
        }
    }

    exit 0
}

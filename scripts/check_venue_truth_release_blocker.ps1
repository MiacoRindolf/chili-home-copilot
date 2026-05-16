<#
.SYNOPSIS
  Fails if venue-truth logs regress out of authoritative mode.

.DESCRIPTION
  As of 2026-05-15, venue-truth telemetry is AUTHORITATIVE by default
  (post Phase B of evidence-fidelity-architecture). The release-blocker
  is inverted from its phase-F shadow-lockdown role: a line is a
  BLOCKER if it contains BOTH:
    - [venue_truth_ops]
    - mode=shadow   (or mode=off - any non-authoritative leak)

  An authoritative deploy that emits ``mode=shadow`` or ``mode=off``
  lines means the venue_truth_mode setting regressed somewhere in the
  rollout (config drift, env override, partial-update).

  Legacy phase-F semantics (fire on ``mode=authoritative``) are
  available via ``-LegacyShadowLockdown`` for rollback windows.

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
    [double] $MaxMeanGapBps = 0.0,
    [Parameter()]
    [switch] $LegacyShadowLockdown
)

begin {
    $blockers = [System.Collections.Generic.List[string]]::new()

    function Test-ReleaseBlockerLine {
        param([string] $Line)
        if ([string]::IsNullOrEmpty($Line)) { return $false }
        if (-not $Line.Contains("[venue_truth_ops]")) { return $false }
        if ($LegacyShadowLockdown) {
            # Legacy phase-F semantics - fire if anyone emits authoritative.
            return $Line.Contains("mode=authoritative")
        }
        # Default (2026-05-15+) - authoritative is expected, fire on regression.
        return $Line.Contains("mode=shadow") -or $Line.Contains("mode=off")
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
        $expected = if ($LegacyShadowLockdown) { "mode=authoritative" } else { "mode=shadow or mode=off (regression from authoritative)" }
        Write-Error "Release blocker: $($blockers.Count) line(s) match [venue_truth_ops] $expected"
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

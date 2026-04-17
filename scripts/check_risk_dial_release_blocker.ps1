<#
.SYNOPSIS
  Fails if any log line is a Phase I risk-dial release blocker.

.DESCRIPTION
  A line is a BLOCKER if it contains ALL of:
    - [risk_dial_ops]
    - event=dial_persisted
    - mode=authoritative

  Phase I rolls out the canonical risk dial in shadow mode only.
  An ``authoritative`` dial log line means the authoritative cutover
  (Phase I.2) leaked into a deploy that has not been approved for
  live sizing modulation. Until I.2 the legacy sizing paths are the
  sole authority; Phase I only *logs* the dial.

  Optionally, a JSON dump of the
  /api/trading/brain/risk-dial/diagnostics endpoint can be read
  from disk via -DiagnosticsJson. The gate fails when:
    * ``dial_events_total < MinEvents``
      (use to enforce that the dial is actually being resolved)
    * ``mean_dial_value`` outside [MinMean, MaxMean] range

  Exit 0 = no blocker lines found (and gates pass, if provided).
  Exit 1 = one or more blocker lines / failed diagnostics gate.
  Exit 2 = file not found (when using -Path or -DiagnosticsJson).
  Exit 3 = malformed JSON passed via -DiagnosticsJson.

.EXAMPLE
  docker compose logs chili --since 30m 2>&1 | .\scripts\check_risk_dial_release_blocker.ps1

.EXAMPLE
  .\scripts\check_risk_dial_release_blocker.ps1 -Path .\saved-chili.log

.EXAMPLE
  curl -sk https://localhost:8000/api/trading/brain/risk-dial/diagnostics -o rd.json
  .\scripts\check_risk_dial_release_blocker.ps1 -DiagnosticsJson .\rd.json -MinEvents 1 -MinMean 0.1 -MaxMean 1.5
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
    [int] $MinEvents = 0,
    [Parameter()]
    [double] $MinMean = 0.0,
    [Parameter()]
    [double] $MaxMean = [double]::PositiveInfinity
)

begin {
    $blockers = [System.Collections.Generic.List[string]]::new()

    function Test-ReleaseBlockerLine {
        param([string] $Line)
        if ([string]::IsNullOrEmpty($Line)) { return $false }
        return $Line.Contains("[risk_dial_ops]") -and
               $Line.Contains("event=dial_persisted") -and
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
        Write-Error "Release blocker: $($blockers.Count) line(s) match [risk_dial_ops] event=dial_persisted mode=authoritative"
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
        $rd = $payload.risk_dial
        if ($null -eq $rd) { $rd = $payload }

        $total = [int]($rd.dial_events_total | ForEach-Object { $_ })
        $mean = [double]($rd.mean_dial_value | ForEach-Object { $_ })

        if ($MinEvents -gt 0 -and $total -lt $MinEvents) {
            Write-Error "Release blocker: dial_events_total=$total < MinEvents=$MinEvents"
            exit 1
        }
        if ($mean -lt $MinMean) {
            Write-Error "Release blocker: mean_dial_value=$mean < MinMean=$MinMean"
            exit 1
        }
        if ($mean -gt $MaxMean) {
            Write-Error "Release blocker: mean_dial_value=$mean > MaxMean=$MaxMean"
            exit 1
        }
    }

    exit 0
}

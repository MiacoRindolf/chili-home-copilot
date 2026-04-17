<#
.SYNOPSIS
  Fails if any log line is a Phase G bracket-reconciliation release blocker.

.DESCRIPTION
  A line is a BLOCKER if it contains BOTH:
    - [bracket_reconciliation_ops]
    - AND any of (event=submit, event=cancel, event=modify, mode=authoritative)

  Phase G's reconciliation service is strictly read-only against the
  broker. Any submit/cancel/modify event from this ops log means Phase
  G.2's writer path leaked into a non-authoritative deploy.

  Optionally, a JSON dump of the
  /api/trading/brain/bracket-reconciliation/diagnostics endpoint can be
  piped in via -DiagnosticsJson. The gate fails when:
    * ``rows_total < MinRows`` (use to enforce the sweep ran at least
      MinRows times), OR
    * ``by_kind.broker_down`` dominates (> MaxBrokerDownFraction of
      rows_total; default 0.20 per rollout rollback criterion).

  Exit 0 = no blocker lines found (and gates pass, if provided).
  Exit 1 = one or more blocker lines / failed diagnostics gate.
  Exit 2 = file not found.
  Exit 3 = malformed JSON.

.EXAMPLE
  docker compose logs chili --since 30m 2>&1 | .\scripts\check_bracket_reconciliation_release_blocker.ps1
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
    [int] $MinRows = 0,
    [Parameter()]
    [double] $MaxBrokerDownFraction = 0.20
)

begin {
    $blockers = [System.Collections.Generic.List[string]]::new()

    function Test-ReleaseBlockerLine {
        param([string] $Line)
        if ([string]::IsNullOrEmpty($Line)) { return $false }
        if (-not $Line.Contains("[bracket_reconciliation_ops]")) { return $false }
        return ($Line.Contains("event=submit") -or
                $Line.Contains("event=cancel") -or
                $Line.Contains("event=modify") -or
                $Line.Contains("mode=authoritative"))
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
        Write-Error "Release blocker: $($blockers.Count) line(s) match [bracket_reconciliation_ops] submit/cancel/modify or authoritative"
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
        $br = $payload.bracket_reconciliation
        if ($null -eq $br) { $br = $payload }

        $rows = [int]($br.rows_total | ForEach-Object { $_ })
        if ($MinRows -gt 0 -and $rows -lt $MinRows) {
            Write-Error "Release blocker: rows_total=$rows < MinRows=$MinRows"
            exit 1
        }

        $down = 0
        if ($br.by_kind -and $br.by_kind.broker_down) {
            $down = [int]$br.by_kind.broker_down
        }
        if ($rows -gt 0) {
            $frac = [double]$down / [double]$rows
            if ($frac -gt $MaxBrokerDownFraction) {
                Write-Error ("Release blocker: broker_down fraction={0:N3} > {1:N3} (rows_total={2}, broker_down={3})" -f $frac, $MaxBrokerDownFraction, $rows, $down)
                exit 1
            }
        }
    }

    exit 0
}

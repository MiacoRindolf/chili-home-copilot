<#
.SYNOPSIS
  Fails if any log line is a Phase M.1 pattern x regime performance
  release blocker.

.DESCRIPTION
  A line is a BLOCKER if it contains ANY of:
    - [pattern_regime_perf_ops] event=pattern_regime_perf_persisted mode=authoritative
    - [pattern_regime_perf_ops] event=pattern_regime_perf_refused_authoritative

  Phase M.1 ships the pattern x regime performance ledger (the first
  consumer of L.17 - L.22 regime snapshots, stratifying closed paper
  trade performance across 8 regime dimensions) in shadow mode only.
  Phase M.2 will open the authoritative path (e.g. NetEdgeRanker
  reading per-regime expectancy to tilt sizing) behind explicit
  governance + parity window. Until then an authoritative
  pattern_regime_perf event or a refusal line means config drift got
  into a deploy.

  Optionally, a JSON dump of the
  /api/trading/brain/pattern-regime-performance/diagnostics endpoint
  can be read from disk via -DiagnosticsJson. The gate fails when:
    * ``ledger_rows_total < MinLedgerRows`` (only if MinLedgerRows > 0)
    * ``confident_cells_total < MinConfidentCells`` (only if MinConfidentCells > 0)

  Exit 0 = no blocker lines found (and gates pass, if provided).
  Exit 1 = one or more blocker lines / failed diagnostics gate.
  Exit 2 = file not found (when using -Path or -DiagnosticsJson).
  Exit 3 = malformed JSON passed via -DiagnosticsJson.

.EXAMPLE
  docker compose logs chili --since 30m 2>&1 | .\scripts\check_pattern_regime_perf_release_blocker.ps1

.EXAMPLE
  curl.exe -sk https://localhost:8000/api/trading/brain/pattern-regime-performance/diagnostics -o prp.json
  .\scripts\check_pattern_regime_perf_release_blocker.ps1 -DiagnosticsJson .\prp.json -MinLedgerRows 1
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
    [int] $MinLedgerRows = 0,
    [Parameter()]
    [int] $MinConfidentCells = 0
)

begin {
    $blockers = [System.Collections.Generic.List[string]]::new()

    function Test-ReleaseBlockerLine {
        param([string] $Line)
        if ([string]::IsNullOrEmpty($Line)) { return $false }
        if (-not $Line.Contains("[pattern_regime_perf_ops]")) { return $false }
        if ($Line.Contains("event=pattern_regime_perf_persisted") -and $Line.Contains("mode=authoritative")) {
            return $true
        }
        if ($Line.Contains("event=pattern_regime_perf_refused_authoritative")) {
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
        Write-Error "Release blocker: $($blockers.Count) line(s) match [pattern_regime_perf_ops] authoritative/refused patterns"
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
        $prp = $payload.pattern_regime_performance
        if ($null -eq $prp) { $prp = $payload }

        $rows = [int]($prp.ledger_rows_total | ForEach-Object { $_ })
        $confident = [int]($prp.confident_cells_total | ForEach-Object { $_ })

        if ($MinLedgerRows -gt 0 -and $rows -lt $MinLedgerRows) {
            Write-Error "Release blocker: ledger_rows_total=$rows < MinLedgerRows=$MinLedgerRows"
            exit 1
        }
        if ($MinConfidentCells -gt 0 -and $confident -lt $MinConfidentCells) {
            Write-Error "Release blocker: confident_cells_total=$confident < MinConfidentCells=$MinConfidentCells"
            exit 1
        }
    }

    exit 0
}

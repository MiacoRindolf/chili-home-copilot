<#
.SYNOPSIS
  Fails if any log line is a Phase-F execution-cost release blocker.

.DESCRIPTION
  A line is a BLOCKER if it contains BOTH:
    - [execution_cost_ops]
    - mode=authoritative

  Phase F rolls out the execution-cost model in shadow mode only; an
  ``authoritative`` log line means the cutover leaked into a non-
  authoritative deploy.

  Separately (optional via -DiagnosticsJson) a JSON dump of the
  /api/trading/brain/execution-cost/diagnostics endpoint can be piped in
  or read from disk. The gate fails when ``estimates_total < MinEstimates``
  (use to enforce that the estimator actually ran) or when stale
  estimates dominate the set (``stale_estimates >= estimates_total`` with
  ``estimates_total > 0``).

  Exit 0 = no blocker lines found (and gates pass, if provided).
  Exit 1 = one or more blocker lines / failed diagnostics gate.
  Exit 2 = file not found (when using -Path or -DiagnosticsJson).
  Exit 3 = malformed JSON passed via -DiagnosticsJson.

.EXAMPLE
  docker compose logs chili --since 30m 2>&1 | .\scripts\check_execution_cost_release_blocker.ps1

.EXAMPLE
  .\scripts\check_execution_cost_release_blocker.ps1 -Path .\saved-chili.log

.EXAMPLE
  # Enforce "estimator wrote at least 5 rows" gate
  curl -sk https://localhost:8000/api/trading/brain/execution-cost/diagnostics -o ec.json
  .\scripts\check_execution_cost_release_blocker.ps1 -DiagnosticsJson .\ec.json -MinEstimates 5
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
    [int] $MinEstimates = 0
)

begin {
    $blockers = [System.Collections.Generic.List[string]]::new()

    function Test-ReleaseBlockerLine {
        param([string] $Line)
        if ([string]::IsNullOrEmpty($Line)) { return $false }
        return $Line.Contains("[execution_cost_ops]") -and
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
        Write-Error "Release blocker: $($blockers.Count) line(s) match [execution_cost_ops] mode=authoritative"
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
        $ec = $payload.execution_cost
        if ($null -eq $ec) { $ec = $payload }

        $total = [int]($ec.estimates_total | ForEach-Object { $_ })
        $stale = [int]($ec.stale_estimates | ForEach-Object { $_ })

        if ($MinEstimates -gt 0 -and $total -lt $MinEstimates) {
            Write-Error "Release blocker: estimates_total=$total < MinEstimates=$MinEstimates"
            exit 1
        }

        if ($total -gt 0 -and $stale -ge $total) {
            Write-Error "Release blocker: estimates_total=$total but stale_estimates=$stale (all rows are stale)"
            exit 1
        }
    }

    exit 0
}

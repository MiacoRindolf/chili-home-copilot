<#
.SYNOPSIS
  Fails if any log line is a Phase-D triple-barrier release blocker.

.DESCRIPTION
  A line is a BLOCKER if it contains BOTH:
    - [triple_barrier_ops]
    - mode=authoritative

  Phase D rolls out triple-barrier labelling in shadow mode only; an
  ``authoritative`` log line means the cutover leaked into a non-
  authoritative deploy.

  Separately (optional via -DiagnosticsJson) a JSON dump of the
  /api/trading/brain/triple-barrier/diagnostics endpoint can be piped in or
  read from disk. The gate fails when ``labels_total < MinLabels`` (use to
  enforce that the labeler actually ran) or when the distribution is
  entirely empty in a non-empty universe.

  Exit 0 = no blocker lines found (and gates pass, if provided).
  Exit 1 = one or more blocker lines / failed diagnostics gate.
  Exit 2 = file not found (when using -Path or -DiagnosticsJson).
  Exit 3 = malformed JSON passed via -DiagnosticsJson.

.EXAMPLE
  docker compose logs chili --since 30m 2>&1 | .\scripts\check_triple_barrier_release_blocker.ps1

.EXAMPLE
  .\scripts\check_triple_barrier_release_blocker.ps1 -Path .\saved-chili.log

.EXAMPLE
  # Enforce "labeler wrote at least 10 labels" gate
  curl -sk https://localhost:8000/api/trading/brain/triple-barrier/diagnostics -o tb.json
  .\scripts\check_triple_barrier_release_blocker.ps1 -DiagnosticsJson .\tb.json -MinLabels 10
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
    [int] $MinLabels = 0
)

begin {
    $blockers = [System.Collections.Generic.List[string]]::new()

    function Test-ReleaseBlockerLine {
        param([string] $Line)
        if ([string]::IsNullOrEmpty($Line)) { return $false }
        return $Line.Contains("[triple_barrier_ops]") -and
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
        Write-Error "Release blocker: $($blockers.Count) line(s) match [triple_barrier_ops] mode=authoritative"
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
        $tb = $payload.triple_barrier
        if ($null -eq $tb) { $tb = $payload }

        $total = [int]($tb.labels_total | ForEach-Object { $_ })

        if ($MinLabels -gt 0 -and $total -lt $MinLabels) {
            Write-Error "Release blocker: labels_total=$total < MinLabels=$MinLabels"
            exit 1
        }

        if ($total -gt 0) {
            $tp = [int]($tb.by_barrier.tp | ForEach-Object { $_ })
            $sl = [int]($tb.by_barrier.sl | ForEach-Object { $_ })
            $to = [int]($tb.by_barrier.timeout | ForEach-Object { $_ })
            if (($tp + $sl + $to) -eq 0) {
                Write-Error "Release blocker: labels_total=$total but all barrier categories are zero"
                exit 1
            }
        }
    }

    exit 0
}

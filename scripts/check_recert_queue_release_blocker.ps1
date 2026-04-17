<#
.SYNOPSIS
  Fails if any log line is a Phase J recert-queue release blocker.

.DESCRIPTION
  A line is a BLOCKER if it contains ANY of:
    - [recert_queue_ops] event=recert_persisted mode=authoritative
    - [recert_queue_ops] event=recert_refused_authoritative

  Phase J ships the re-cert queue in shadow mode only. Phase J.2 will
  open the authoritative path with explicit governance approval;
  until then an authoritative recert event means a config drift got
  into a deploy.

  Optionally, a JSON dump of the
  /api/trading/brain/recert-queue/diagnostics endpoint can be read
  from disk via -DiagnosticsJson. The gate fails when:
    * ``recert_events_total < MinRecertEvents``
    * ``patterns_queued_distinct > MaxPatternsQueuedDistinct``

  Exit 0 = no blocker lines found (and gates pass, if provided).
  Exit 1 = one or more blocker lines / failed diagnostics gate.
  Exit 2 = file not found (when using -Path or -DiagnosticsJson).
  Exit 3 = malformed JSON passed via -DiagnosticsJson.

.EXAMPLE
  docker compose logs chili --since 30m 2>&1 | .\scripts\check_recert_queue_release_blocker.ps1

.EXAMPLE
  curl -sk https://localhost:8000/api/trading/brain/recert-queue/diagnostics -o rq.json
  .\scripts\check_recert_queue_release_blocker.ps1 -DiagnosticsJson .\rq.json -MinRecertEvents 0
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
    [int] $MinRecertEvents = 0,
    [Parameter()]
    [int] $MaxPatternsQueuedDistinct = [int]::MaxValue
)

begin {
    $blockers = [System.Collections.Generic.List[string]]::new()

    function Test-ReleaseBlockerLine {
        param([string] $Line)
        if ([string]::IsNullOrEmpty($Line)) { return $false }
        if (-not $Line.Contains("[recert_queue_ops]")) { return $false }
        if ($Line.Contains("event=recert_persisted") -and $Line.Contains("mode=authoritative")) {
            return $true
        }
        if ($Line.Contains("event=recert_refused_authoritative")) {
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
        Write-Error "Release blocker: $($blockers.Count) line(s) match [recert_queue_ops] authoritative/refused patterns"
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
        $rq = $payload.recert_queue
        if ($null -eq $rq) { $rq = $payload }

        $total = [int]($rq.recert_events_total | ForEach-Object { $_ })
        $patterns = [int]($rq.patterns_queued_distinct | ForEach-Object { $_ })

        if ($MinRecertEvents -gt 0 -and $total -lt $MinRecertEvents) {
            Write-Error "Release blocker: recert_events_total=$total < MinRecertEvents=$MinRecertEvents"
            exit 1
        }
        if ($patterns -gt $MaxPatternsQueuedDistinct) {
            Write-Error "Release blocker: patterns_queued_distinct=$patterns > MaxPatternsQueuedDistinct=$MaxPatternsQueuedDistinct"
            exit 1
        }
    }

    exit 0
}

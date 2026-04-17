<#
.SYNOPSIS
  Fails if any log line is a Phase K divergence-panel release blocker.

.DESCRIPTION
  A line is a BLOCKER if it contains ANY of:
    - [divergence_ops] event=divergence_persisted mode=authoritative
    - [divergence_ops] event=divergence_refused_authoritative

  Phase K ships the divergence panel in shadow mode only. Phase K.2
  will open the authoritative path with explicit governance approval;
  until then an authoritative divergence event means a config drift
  got into a deploy.

  Optionally, a JSON dump of the
  /api/trading/brain/divergence/diagnostics endpoint can be read from
  disk via -DiagnosticsJson. The gate fails when:
    * ``divergence_events_total < MinDivergenceEvents``
    * ``patterns_red > MaxPatternsRed``
    * ``patterns_yellow > MaxPatternsYellow``

  Exit 0 = no blocker lines found (and gates pass, if provided).
  Exit 1 = one or more blocker lines / failed diagnostics gate.
  Exit 2 = file not found (when using -Path or -DiagnosticsJson).
  Exit 3 = malformed JSON passed via -DiagnosticsJson.

.EXAMPLE
  docker compose logs chili --since 30m 2>&1 | .\scripts\check_divergence_release_blocker.ps1

.EXAMPLE
  curl -sk https://localhost:8000/api/trading/brain/divergence/diagnostics -o dv.json
  .\scripts\check_divergence_release_blocker.ps1 -DiagnosticsJson .\dv.json -MinDivergenceEvents 0
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
    [int] $MinDivergenceEvents = 0,
    [Parameter()]
    [int] $MaxPatternsRed = [int]::MaxValue,
    [Parameter()]
    [int] $MaxPatternsYellow = [int]::MaxValue
)

begin {
    $blockers = [System.Collections.Generic.List[string]]::new()

    function Test-ReleaseBlockerLine {
        param([string] $Line)
        if ([string]::IsNullOrEmpty($Line)) { return $false }
        if (-not $Line.Contains("[divergence_ops]")) { return $false }
        if ($Line.Contains("event=divergence_persisted") -and $Line.Contains("mode=authoritative")) {
            return $true
        }
        if ($Line.Contains("event=divergence_refused_authoritative")) {
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
        Write-Error "Release blocker: $($blockers.Count) line(s) match [divergence_ops] authoritative/refused patterns"
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
        $dv = $payload.divergence
        if ($null -eq $dv) { $dv = $payload }

        $total = [int]($dv.divergence_events_total | ForEach-Object { $_ })
        $red = [int]($dv.patterns_red | ForEach-Object { $_ })
        $yellow = [int]($dv.patterns_yellow | ForEach-Object { $_ })

        if ($MinDivergenceEvents -gt 0 -and $total -lt $MinDivergenceEvents) {
            Write-Error "Release blocker: divergence_events_total=$total < MinDivergenceEvents=$MinDivergenceEvents"
            exit 1
        }
        if ($red -gt $MaxPatternsRed) {
            Write-Error "Release blocker: patterns_red=$red > MaxPatternsRed=$MaxPatternsRed"
            exit 1
        }
        if ($yellow -gt $MaxPatternsYellow) {
            Write-Error "Release blocker: patterns_yellow=$yellow > MaxPatternsYellow=$MaxPatternsYellow"
            exit 1
        }
    }

    exit 0
}

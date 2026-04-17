<#
.SYNOPSIS
  Fails if any log line is a Phase I capital-reweight release blocker.

.DESCRIPTION
  A line is a BLOCKER if it contains ANY of:
    - [capital_reweight_ops] event=sweep_persisted mode=authoritative
    - [capital_reweight_ops] event=sweep_refused_authoritative
        (the refusal line itself is a loud signal that someone
        tried to run the sweep authoritatively; it is logged for
        visibility but it must not happen in a shipping build)

  Phase I rolls out the weekly capital re-weighter in shadow mode
  only. Phase I.2 will open the authoritative path with explicit
  governance approval; until then an authoritative sweep means a
  config drift got into a deploy.

  Optionally, a JSON dump of the
  /api/trading/brain/capital-reweight/diagnostics endpoint can be
  read from disk via -DiagnosticsJson. The gate fails when:
    * ``sweeps_total < MinSweeps``
    * ``single_bucket_cap_trigger_count > MaxSingleBucketCapTriggers``
    * ``concentration_cap_trigger_count > MaxConcentrationCapTriggers``

  Exit 0 = no blocker lines found (and gates pass, if provided).
  Exit 1 = one or more blocker lines / failed diagnostics gate.
  Exit 2 = file not found (when using -Path or -DiagnosticsJson).
  Exit 3 = malformed JSON passed via -DiagnosticsJson.

.EXAMPLE
  docker compose logs chili --since 30m 2>&1 | .\scripts\check_capital_reweight_release_blocker.ps1

.EXAMPLE
  curl -sk https://localhost:8000/api/trading/brain/capital-reweight/diagnostics -o cr.json
  .\scripts\check_capital_reweight_release_blocker.ps1 -DiagnosticsJson .\cr.json -MinSweeps 0
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
    [int] $MinSweeps = 0,
    [Parameter()]
    [int] $MaxSingleBucketCapTriggers = [int]::MaxValue,
    [Parameter()]
    [int] $MaxConcentrationCapTriggers = [int]::MaxValue
)

begin {
    $blockers = [System.Collections.Generic.List[string]]::new()

    function Test-ReleaseBlockerLine {
        param([string] $Line)
        if ([string]::IsNullOrEmpty($Line)) { return $false }
        if (-not $Line.Contains("[capital_reweight_ops]")) { return $false }
        if ($Line.Contains("event=sweep_persisted") -and $Line.Contains("mode=authoritative")) {
            return $true
        }
        if ($Line.Contains("event=sweep_refused_authoritative")) {
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
        Write-Error "Release blocker: $($blockers.Count) line(s) match [capital_reweight_ops] authoritative/refused patterns"
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
        $cr = $payload.capital_reweight
        if ($null -eq $cr) { $cr = $payload }

        $total = [int]($cr.sweeps_total | ForEach-Object { $_ })
        $single = [int]($cr.single_bucket_cap_trigger_count | ForEach-Object { $_ })
        $conc = [int]($cr.concentration_cap_trigger_count | ForEach-Object { $_ })

        if ($MinSweeps -gt 0 -and $total -lt $MinSweeps) {
            Write-Error "Release blocker: sweeps_total=$total < MinSweeps=$MinSweeps"
            exit 1
        }
        if ($single -gt $MaxSingleBucketCapTriggers) {
            Write-Error "Release blocker: single_bucket_cap_trigger_count=$single > MaxSingleBucketCapTriggers=$MaxSingleBucketCapTriggers"
            exit 1
        }
        if ($conc -gt $MaxConcentrationCapTriggers) {
            Write-Error "Release blocker: concentration_cap_trigger_count=$conc > MaxConcentrationCapTriggers=$MaxConcentrationCapTriggers"
            exit 1
        }
    }

    exit 0
}

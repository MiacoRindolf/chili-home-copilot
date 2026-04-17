<#
.SYNOPSIS
  Fails if any log line is a Phase H position-sizer release blocker.

.DESCRIPTION
  A line is a BLOCKER if it contains ALL of:
    - [position_sizer_ops]
    - event=proposal
    - mode=authoritative

  Phase H rolls out the canonical position sizer in shadow mode only.
  An ``authoritative`` proposal log line means the authoritative
  cutover (Phase H.2) leaked into a deploy that has not been approved
  for live sizing. Until H.2 the legacy sizers are the sole
  authority; Phase H only *logs*.

  Optionally, a JSON dump of the
  /api/trading/brain/position-sizer/diagnostics endpoint can be read
  from disk via -DiagnosticsJson. The gate fails when:
    * ``proposals_total < MinProposals``
      (use to enforce that the emitter actually ran)
    * ``mean_divergence_bps > MaxMeanDivergenceBps``
      (use to enforce that the shadow and legacy sizers do not
      diverge catastrophically before authoritative cutover)

  Exit 0 = no blocker lines found (and gates pass, if provided).
  Exit 1 = one or more blocker lines / failed diagnostics gate.
  Exit 2 = file not found (when using -Path or -DiagnosticsJson).
  Exit 3 = malformed JSON passed via -DiagnosticsJson.

.EXAMPLE
  docker compose logs chili --since 30m 2>&1 | .\scripts\check_position_sizer_release_blocker.ps1

.EXAMPLE
  .\scripts\check_position_sizer_release_blocker.ps1 -Path .\saved-chili.log

.EXAMPLE
  curl -sk https://localhost:8000/api/trading/brain/position-sizer/diagnostics -o ps.json
  .\scripts\check_position_sizer_release_blocker.ps1 -DiagnosticsJson .\ps.json -MinProposals 1 -MaxMeanDivergenceBps 50000
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
    [int] $MinProposals = 0,
    [Parameter()]
    [double] $MaxMeanDivergenceBps = [double]::PositiveInfinity
)

begin {
    $blockers = [System.Collections.Generic.List[string]]::new()

    function Test-ReleaseBlockerLine {
        param([string] $Line)
        if ([string]::IsNullOrEmpty($Line)) { return $false }
        return $Line.Contains("[position_sizer_ops]") -and
               $Line.Contains("event=proposal") -and
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
        Write-Error "Release blocker: $($blockers.Count) line(s) match [position_sizer_ops] event=proposal mode=authoritative"
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
        $ps = $payload.position_sizer
        if ($null -eq $ps) { $ps = $payload }

        $total = [int]($ps.proposals_total | ForEach-Object { $_ })
        $meanDiv = [double]($ps.mean_divergence_bps | ForEach-Object { $_ })

        if ($MinProposals -gt 0 -and $total -lt $MinProposals) {
            Write-Error "Release blocker: proposals_total=$total < MinProposals=$MinProposals"
            exit 1
        }
        if ($meanDiv -gt $MaxMeanDivergenceBps) {
            Write-Error "Release blocker: mean_divergence_bps=$meanDiv > MaxMeanDivergenceBps=$MaxMeanDivergenceBps"
            exit 1
        }
    }

    exit 0
}

<#
.SYNOPSIS
  Fails if any log line is a Phase M.2-autopilot release blocker, or if
  the diagnostics snapshot shows an unsafe state.

.DESCRIPTION
  A log line is a BLOCKER if it contains ANY of:
    - [pattern_regime_autopilot_ops] event=autopilot_advance
        to_mode=authoritative
        and either approval_live=false OR approval_id=none
    - [pattern_regime_autopilot_ops] event=autopilot_revert
        reason_code=authoritative_approval_missing
    - [pattern_regime_autopilot_ops] event=autopilot_revert
        reason_code=anomaly_refused_authoritative

  Phase M.2-autopilot advances slices through shadow->compare->authoritative
  by writing to the runtime-mode override table. Authoritative mode
  MUST be paired with a live approval row in
  ``trading_governance_approvals``. Any advance to authoritative
  without a live approval is an invariant violation.

  If -DiagnosticsJson points at the /api/trading/brain/m2-autopilot/status
  payload, additional gates run:
    - any slice with stage='authoritative' must have approval_live=true
    - kill=true is not a blocker (kill-flag is a safety feature).

  Exit 0 = no blocker lines found (and gates pass, if provided).
  Exit 1 = one or more blocker lines / failed diagnostics gate.
  Exit 2 = file not found.
  Exit 3 = malformed JSON.

.EXAMPLE
  docker compose logs chili --since 30m 2>&1 |
    .\scripts\check_pattern_regime_autopilot_release_blocker.ps1

  .\scripts\check_pattern_regime_autopilot_release_blocker.ps1 `
    -DiagnosticsJson diag.json
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromPipeline = $true)]
    [psobject] $InputObject,
    [Parameter()]
    [string] $Path,
    [Parameter()]
    [string] $DiagnosticsJson
)

begin {
    $blockers = [System.Collections.Generic.List[string]]::new()

    function Test-ReleaseBlockerLine {
        param([string] $Line)
        if ([string]::IsNullOrEmpty($Line)) { return $false }
        if (-not $Line.Contains("[pattern_regime_autopilot_ops]")) { return $false }

        if ($Line.Contains("event=autopilot_advance") -and $Line.Contains("to_mode=authoritative")) {
            if ($Line -match "approval_live=false" -or $Line -match "approval_id=(none|null)\b") {
                return $true
            }
            if (-not ($Line -match "approval_id=\d+")) {
                return $true
            }
        }
        if ($Line.Contains("event=autopilot_revert") -and ($Line.Contains("reason_code=authoritative_approval_missing") -or $Line.Contains("reason_code=anomaly_refused_authoritative"))) {
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
        Write-Error "Release blocker: $($blockers.Count) line(s) match [pattern_regime_autopilot_ops] authoritative/invariant patterns"
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
        $m = $payload.m2_autopilot
        if ($null -eq $m) { $m = $payload }
        if ($null -ne $m.slices) {
            foreach ($name in 'tilt','promotion','killswitch') {
                $s = $m.slices.$name
                if ($null -eq $s) { continue }
                if ($s.stage -eq 'authoritative' -and -not $s.approval_live) {
                    Write-Error "Release blocker: slice=$name stage=authoritative but approval_live=false"
                    exit 1
                }
            }
        }
    }

    exit 0
}

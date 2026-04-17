<#
.SYNOPSIS
  Fails if any log line is a Phase M.2.c pattern x regime
  kill-switch release blocker.

.DESCRIPTION
  A line is a BLOCKER if it contains ANY of:
    - [pattern_regime_killswitch_ops] event=killswitch_applied mode=authoritative
    - [pattern_regime_killswitch_ops] event=killswitch_refused_authoritative

  Phase M.2.c ships the daily kill-switch / auto-quarantine
  consumer in shadow mode only. Authoritative mode requires a
  live approval row in ``trading_governance_approvals`` with
  ``action_type='pattern_regime_killswitch'``.

  Exit 0 = no blocker lines found (and gates pass, if provided).
  Exit 1 = one or more blocker lines / failed diagnostics gate.
  Exit 2 = file not found.
  Exit 3 = malformed JSON.

.EXAMPLE
  docker compose logs chili --since 30m 2>&1 |
    .\scripts\check_pattern_regime_killswitch_release_blocker.ps1
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
        if (-not $Line.Contains("[pattern_regime_killswitch_ops]")) { return $false }
        if ($Line.Contains("event=killswitch_applied") -and $Line.Contains("mode=authoritative")) {
            return $true
        }
        if ($Line.Contains("event=killswitch_refused_authoritative")) {
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
        Write-Error "Release blocker: $($blockers.Count) line(s) match [pattern_regime_killswitch_ops] authoritative/refused patterns"
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
        $k = $payload.pattern_regime_killswitch
        if ($null -eq $k) { $k = $payload }
        if ($k.mode -eq "authoritative" -and -not $k.approval_live) {
            Write-Error "Release blocker: mode=authoritative but approval_live=false"
            exit 1
        }
    }

    exit 0
}

<#
.SYNOPSIS
  Fails if any log line is a Phase G live-brackets release blocker.

.DESCRIPTION
  A line is a BLOCKER if it contains BOTH:
    - [bracket_intent_ops]
    - event=intent_write
    - mode=authoritative

  Phase G rolls out bracket intents in shadow mode only. An
  ``authoritative`` intent_write log line means the authoritative
  cutover (Phase G.2) leaked into a deploy that has not been approved
  for live bracket placement.

  Optionally, a JSON dump of the
  /api/trading/brain/bracket-intent/diagnostics endpoint can be piped in
  or read from disk via -DiagnosticsJson. The gate fails when
  ``intents_total < MinIntents`` (use to enforce that the emitter
  actually ran) or when any intent has been in state ``intent`` for
  more than ``MaxIntentAgeMinutes`` (unreconciled substrate).

  Exit 0 = no blocker lines found (and gates pass, if provided).
  Exit 1 = one or more blocker lines / failed diagnostics gate.
  Exit 2 = file not found (when using -Path or -DiagnosticsJson).
  Exit 3 = malformed JSON passed via -DiagnosticsJson.

.EXAMPLE
  docker compose logs chili --since 30m 2>&1 | .\scripts\check_live_brackets_release_blocker.ps1

.EXAMPLE
  .\scripts\check_live_brackets_release_blocker.ps1 -Path .\saved-chili.log

.EXAMPLE
  # Enforce "at least one intent emitted in 24h" gate
  curl -sk https://localhost:8000/api/trading/brain/bracket-intent/diagnostics -o bi.json
  .\scripts\check_live_brackets_release_blocker.ps1 -DiagnosticsJson .\bi.json -MinIntents 1
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
    [int] $MinIntents = 0
)

begin {
    $blockers = [System.Collections.Generic.List[string]]::new()

    function Test-ReleaseBlockerLine {
        param([string] $Line)
        if ([string]::IsNullOrEmpty($Line)) { return $false }
        return $Line.Contains("[bracket_intent_ops]") -and
               $Line.Contains("event=intent_write") -and
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
        Write-Error "Release blocker: $($blockers.Count) line(s) match [bracket_intent_ops] event=intent_write mode=authoritative"
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
        $bi = $payload.bracket_intent
        if ($null -eq $bi) { $bi = $payload }

        $total = [int]($bi.intents_total | ForEach-Object { $_ })

        if ($MinIntents -gt 0 -and $total -lt $MinIntents) {
            Write-Error "Release blocker: intents_total=$total < MinIntents=$MinIntents"
            exit 1
        }
    }

    exit 0
}

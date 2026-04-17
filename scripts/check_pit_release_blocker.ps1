<#
.SYNOPSIS
  Fails if any log line is a Phase-C PIT-audit release blocker.

.DESCRIPTION
  A line is a BLOCKER if it contains BOTH:
    - [pit_ops]
    - mode=authoritative

  Phase C rolls out the PIT hygiene audit in shadow mode only; an
  ``authoritative`` log line means the cutover leaked into a non-
  authoritative deploy.

  Separately (optional via -PatternsJson) a JSON dump of the
  /api/trading/brain/pit/diagnostics endpoint can be piped in or read
  from disk to fail when ``patterns_violating > 0`` in shadow mode,
  giving CI a pre-cutover enforcement gate.

  Exit 0 = no blocker lines found (and no violating patterns, when
          -PatternsJson is supplied).
  Exit 1 = one or more blocker lines / violating patterns found.
  Exit 2 = file not found (when using -Path or -PatternsJson).
  Exit 3 = malformed JSON passed via -PatternsJson.

.EXAMPLE
  docker compose logs chili --since 30m 2>&1 | .\scripts\check_pit_release_blocker.ps1

.EXAMPLE
  .\scripts\check_pit_release_blocker.ps1 -Path .\saved-chili.log

.EXAMPLE
  # Enforce zero-violator gate from a captured diagnostics payload
  curl -sk https://localhost:8000/api/trading/brain/pit/diagnostics `
      -o pit.json
  .\scripts\check_pit_release_blocker.ps1 -PatternsJson .\pit.json
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromPipeline = $true)]
    [psobject] $InputObject,
    [Parameter()]
    [string] $Path,
    [Parameter()]
    [string] $PatternsJson
)

begin {
    $blockers = [System.Collections.Generic.List[string]]::new()

    function Test-ReleaseBlockerLine {
        param([string] $Line)
        if ([string]::IsNullOrEmpty($Line)) { return $false }
        return $Line.Contains("[pit_ops]") -and
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
        Write-Error "Release blocker: $($blockers.Count) line(s) match [pit_ops] mode=authoritative"
        foreach ($b in $blockers) {
            [Console]::Error.WriteLine($b)
        }
        exit 1
    }

    if ($PatternsJson) {
        if (-not (Test-Path -LiteralPath $PatternsJson)) {
            Write-Error "File not found: $PatternsJson"
            exit 2
        }
        try {
            $payload = Get-Content -Raw -LiteralPath $PatternsJson | ConvertFrom-Json
        } catch {
            Write-Error "Malformed JSON in $PatternsJson : $($_.Exception.Message)"
            exit 3
        }
        $pit = $payload.pit
        if ($null -eq $pit) { $pit = $payload }
        $violating = [int]($pit.patterns_violating | ForEach-Object { $_ })
        if ($violating -gt 0) {
            Write-Error "Release blocker: patterns_violating=$violating in shadow mode"
            if ($pit.top_violators) {
                $pit.top_violators | ForEach-Object {
                    [Console]::Error.WriteLine(
                        "  pattern_id=$($_.pattern_id) name=$($_.name) non_pit=$(($_.non_pit_fields -join ',')) unknown=$(($_.unknown_fields -join ','))"
                    )
                }
            }
            exit 1
        }
    }

    exit 0
}

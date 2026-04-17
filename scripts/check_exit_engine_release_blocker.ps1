<#
.SYNOPSIS
  Fails if any log line is a Phase-B ExitEngine release blocker.

.DESCRIPTION
  A line is a BLOCKER if it contains BOTH:
    - [exit_engine_ops]
    - mode=authoritative

  while brain_exit_engine_mode is not supposed to be "authoritative" in the
  environment being inspected. Phase B rolls out the canonical ExitEvaluator
  in shadow mode only; an ``authoritative`` log line means the cutover
  leaked into a non-authoritative deploy.

  This script does not read the setting itself; callers must invoke it only
  against environments where the evaluator is meant to be off / shadow /
  compare. A positive result is a clear signal that authoritative wiring
  leaked into that environment.

  Exit 0 = no blocker lines found.
  Exit 1 = one or more blocker lines found (printed to stderr).
  Exit 2 = file not found (when using -Path).

.EXAMPLE
  docker compose logs chili --since 30m 2>&1 | .\scripts\check_exit_engine_release_blocker.ps1

.EXAMPLE
  .\scripts\check_exit_engine_release_blocker.ps1 -Path .\saved-chili.log
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromPipeline = $true)]
    [psobject] $InputObject,
    [Parameter()]
    [string] $Path
)

begin {
    $blockers = [System.Collections.Generic.List[string]]::new()

    function Test-ReleaseBlockerLine {
        param([string] $Line)
        if ([string]::IsNullOrEmpty($Line)) { return $false }
        return $Line.Contains("[exit_engine_ops]") -and
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
        Write-Error "Release blocker: $($blockers.Count) line(s) match [exit_engine_ops] mode=authoritative"
        foreach ($b in $blockers) {
            [Console]::Error.WriteLine($b)
        }
        exit 1
    }

    exit 0
}

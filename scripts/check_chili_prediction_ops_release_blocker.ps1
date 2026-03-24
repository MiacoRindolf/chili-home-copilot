<#
.SYNOPSIS
  Fails if any log line matches the Phase 7/8 release blocker for prediction ops logs.

.DESCRIPTION
  A line is a BLOCKER if it contains ALL of:
    - [chili_prediction_ops]
    - read=auth_mirror
    - explicit_api_tickers=false

  Exit 0 = no blocker lines found.
  Exit 1 = one or more blocker lines found (printed to stderr).
  Exit 2 = file not found (when using -Path).

.EXAMPLE
  docker compose logs chili --since 30m 2>&1 | .\scripts\check_chili_prediction_ops_release_blocker.ps1

.EXAMPLE
  .\scripts\check_chili_prediction_ops_release_blocker.ps1 -Path .\saved-chili.log
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
        return $Line.Contains("[chili_prediction_ops]") -and
               $Line.Contains("read=auth_mirror") -and
               $Line.Contains("explicit_api_tickers=false")
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
        Write-Error "Release blocker: $($blockers.Count) line(s) match read=auth_mirror with explicit_api_tickers=false under [chili_prediction_ops]"
        foreach ($b in $blockers) {
            [Console]::Error.WriteLine($b)
        }
        exit 1
    }

    exit 0
}

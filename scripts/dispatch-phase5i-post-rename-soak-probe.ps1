param(
    [int]$LogScanTimeoutSeconds = 45
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$out = Join-Path $PSScriptRoot "dispatch-phase5i-post-rename-soak-probe-out.txt"
$schemaPatterns = "NoReferencedTableError|UndefinedTable|relation .*trading_|trading_trades.*does not exist|trading_management_envelopes.*does not exist|PendingRollbackError|psycopg2.errors|sqlalchemy.exc|cannot truncate|not a table"
$code = 2
$locationPushed = $false

function Add-OutputLine {
    param([AllowNull()][string]$Line)
    $Line | Add-Content -Path $out -Encoding utf8
}

function Format-SentinelValue {
    param([AllowNull()][string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return "none"
    }

    return ($Value -replace "[\r\n]+", " ").Trim()
}

function Invoke-SchemaLogScan {
    param(
        [string]$RepoRoot,
        [string]$Patterns,
        [int]$TimeoutSeconds
    )

    $process = $null
    try {
        $dockerCommand = Get-Command docker -ErrorAction Stop
        $timeoutMillis = [Math]::Max(1, $TimeoutSeconds) * 1000
        $processInfo = [System.Diagnostics.ProcessStartInfo]::new()
        $dockerSource = $dockerCommand.Source
        $dockerExtension = [System.IO.Path]::GetExtension($dockerSource)

        if ($dockerExtension -in @(".bat", ".cmd")) {
            $cmdPath = $env:ComSpec
            if ([string]::IsNullOrWhiteSpace($cmdPath)) {
                $cmdPath = "cmd.exe"
            }

            $quotedSource = '"' + ($dockerSource -replace '"', '""') + '"'
            $processInfo.FileName = $cmdPath
            $processInfo.Arguments = '/d /s /c "' + $quotedSource + ' compose logs --since 1h chili scheduler-worker autotrader-worker broker-sync-worker"'
        } else {
            $processInfo.FileName = $dockerSource
            $processInfo.Arguments = "compose logs --since 1h chili scheduler-worker autotrader-worker broker-sync-worker"
        }

        $processInfo.WorkingDirectory = $RepoRoot
        $processInfo.UseShellExecute = $false
        $processInfo.RedirectStandardOutput = $true
        $processInfo.RedirectStandardError = $true
        $processInfo.CreateNoWindow = $true

        $process = [System.Diagnostics.Process]::new()
        $process.StartInfo = $processInfo
        [void]$process.Start()

        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()

        if (-not $process.WaitForExit($timeoutMillis)) {
            try {
                $process.Kill()
                [void]$process.WaitForExit(5000)
            } catch {
                # The sentinel below is more important than kill diagnostics.
            }

            return [pscustomobject]@{
                status = "TIMEOUT"
                detail = "docker compose logs timed out after ${TimeoutSeconds}s"
                lines = @()
            }
        }

        $stdout = $stdoutTask.Result
        $stderr = $stderrTask.Result
        $dockerOutput = @(
            @($stdout -split "\r?\n") +
            @($stderr -split "\r?\n") |
                Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
        )

        if ($process.ExitCode -ne 0) {
            return [pscustomobject]@{
                status = "FAILED"
                detail = "docker compose logs exit code $($process.ExitCode)"
                lines = @($dockerOutput)
            }
        }

        $hits = @(
            $dockerOutput |
                Select-String -Pattern $Patterns |
                ForEach-Object { $_.Line }
        )

        return [pscustomobject]@{
            status = "OK"
            detail = "ok"
            lines = $hits
        }
    } catch {
        return [pscustomobject]@{
            status = "UNAVAILABLE"
            detail = $_.Exception.Message
            lines = @()
        }
    } finally {
        if ($null -ne $process) {
            $process.Dispose()
        }
    }
}

"# $(Get-Date -Format o) -- phase5i post-rename soak probe" | Set-Content -Path $out -Encoding utf8

try {
    Push-Location $repo
    $locationPushed = $true

    try {
        $cmdOut = & conda run -n chili-env python "$repo\scripts\d-phase5i-post-rename-soak-probe.py" 2>&1
        $code = $LASTEXITCODE
        $cmdOut | Add-Content -Path $out -Encoding utf8
    } catch {
        $code = 2
        Add-OutputLine "VERDICT_STATUS=ALERT"
        Add-OutputLine ("VERDICT_REASON=dispatcher probe unavailable: {0}" -f (Format-SentinelValue $_.Exception.Message))
    }

    Add-OutputLine ""
    Add-OutputLine "# schema-specific log scan"

    $logScan = Invoke-SchemaLogScan -RepoRoot $repo -Patterns $schemaPatterns -TimeoutSeconds $LogScanTimeoutSeconds
    Add-OutputLine "LOG_SCHEMA_SCAN_STATUS=$($logScan.status)"
    Add-OutputLine ("LOG_SCHEMA_SCAN_DETAIL={0}" -f (Format-SentinelValue $logScan.detail))

    if ($logScan.status -eq "OK") {
        $logHits = @($logScan.lines)
        Add-OutputLine "LOG_SCHEMA_ERRORS=$($logHits.Count)"
        $logHits | Select-Object -First 40 | ForEach-Object { Add-OutputLine $_ }
    } else {
        Add-OutputLine "LOG_SCHEMA_ERRORS=UNKNOWN"
    }
} catch {
    if ($null -eq $code) {
        $code = 2
    }
    Add-OutputLine ("DISPATCH_ERROR={0}" -f (Format-SentinelValue $_.Exception.Message))
} finally {
    Add-OutputLine "EXIT_CODE=$code"
    if ($locationPushed) {
        Pop-Location
    }
}

exit $code

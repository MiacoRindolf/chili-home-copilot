$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$out = Join-Path $PSScriptRoot "dispatch-phase5i-post-rename-soak-probe-out.txt"

"# $(Get-Date -Format o) -- phase5i post-rename soak probe" | Set-Content -Path $out -Encoding utf8
Push-Location $repo
try {
    $oldErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $cmdOut = & conda run -n chili-env python "$repo\scripts\d-phase5i-post-rename-soak-probe.py" 2>&1
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldErrorActionPreference
    }
    $cmdOut | Add-Content -Path $out -Encoding utf8
    "" | Add-Content -Path $out -Encoding utf8
    "# schema-specific log scan" | Add-Content -Path $out -Encoding utf8
    $patterns = "NoReferencedTableError|UndefinedTable|UndefinedColumn|relation .*trading_(trades|management_envelopes|phase5b)|trading_trades.*does not exist|trading_management_envelopes.*does not exist|cannot truncate|not a table"
    $logHits = docker compose logs --since 1h chili scheduler-worker autotrader-worker broker-sync-worker 2>&1 |
        Select-String -Pattern $patterns
    if ($logHits) {
        "LOG_SCHEMA_ERRORS=$($logHits.Count)" | Add-Content -Path $out -Encoding utf8
        $logHits | Select-Object -First 40 | ForEach-Object { $_.Line } | Add-Content -Path $out -Encoding utf8
    }
    else {
        "LOG_SCHEMA_ERRORS=0" | Add-Content -Path $out -Encoding utf8
    }
    "EXIT_CODE=$code" | Add-Content -Path $out -Encoding utf8
    exit $code
}
finally {
    Pop-Location
}

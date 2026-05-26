$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$out = Join-Path $PSScriptRoot "dispatch-phase5e-reporting-soak-probe-out.txt"

"# $(Get-Date -Format o) -- phase5e reporting soak probe" | Set-Content -Path $out -Encoding utf8
Push-Location $repo
try {
    $cmdOut = & conda run -n chili-env python "$repo\scripts\d-phase5e-reporting-soak-probe.py" 2>&1
    $code = $LASTEXITCODE
    $cmdOut | Add-Content -Path $out -Encoding utf8
    "EXIT_CODE=$code" | Add-Content -Path $out -Encoding utf8
    exit $code
}
finally {
    Pop-Location
}

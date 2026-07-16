$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$out = Join-Path $PSScriptRoot "dispatch-phase5e-reporting-soak-probe-out.txt"

"# $(Get-Date -Format o) -- phase5e reporting soak probe" | Set-Content -Path $out -Encoding utf8
Push-Location $repo
try {
    # 2>&1 under $ErrorActionPreference="Stop" makes the first native stderr
    # line a terminating error (conda prints its exit-status wrapper on
    # stderr), so the out-file used to end at the header and the probe
    # verdict was lost. Relax EAP for the native call; exit code is captured.
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $cmdOut = & conda run -n chili-env python "$repo\scripts\d-phase5e-reporting-soak-probe.py" 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    $cmdOut | Add-Content -Path $out -Encoding utf8
    "EXIT_CODE=$code" | Add-Content -Path $out -Encoding utf8
    exit $code
}
finally {
    Pop-Location
}

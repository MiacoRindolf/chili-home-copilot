$out = "scripts/dispatch-r23-offline-output.txt"
"# r23 offline self-test $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "offline self-test (no DB; runs writer logic via mocks)" {
    $env:PYTHONPATH = (Get-Location).Path
    conda run -n chili-env python scripts/_r23_offline_test.py
}

S "exit code" { "LASTEXITCODE = $LASTEXITCODE" }

Write-Host "offline test complete -- see $out"

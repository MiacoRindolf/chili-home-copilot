$out = "scripts/dispatch-r23-r24-validate2-output.txt"
"# r23 + r24 validate2 $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "py-compile gate" {
    conda run -n chili-env python -m py_compile `
        app/config.py `
        app/migrations.py `
        app/services/broker_service.py `
        app/services/trading/venue/robinhood_spot.py `
        app/services/trading/bracket_writer_g2.py `
        app/services/trading/bracket_reconciliation_service.py
    if ($LASTEXITCODE -eq 0) { "py-compile OK" } else { "py-compile FAILED" }
}

S "import + flag smoke (via script file)" {
    conda run -n chili-env python scripts/_r23_smoke.py
}

S "pytest tests/test_bracket_writer_g2.py (asyncio plugin disabled)" {
    $env:TEST_DATABASE_URL = "postgresql://chili:chili@localhost:5433/chili_test"
    conda run -n chili-env python -m pytest tests/test_bracket_writer_g2.py -v -p no:asyncio --no-header
}

S "pytest fallback: try -p no:pytest_asyncio if first failed" {
    $env:TEST_DATABASE_URL = "postgresql://chili:chili@localhost:5433/chili_test"
    conda run -n chili-env python -m pytest tests/test_bracket_writer_g2.py -v -p no:pytest_asyncio --no-header
}

Write-Host "validate2 complete -- see $out"

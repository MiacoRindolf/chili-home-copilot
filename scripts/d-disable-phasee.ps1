# EMERGENCY: disable Phase E sweep to prevent re-cancellation of the
# 14 trades we just reverted. The broker API was returning empty list
# silently while RH actually held positions. Phase E's heuristic
# (last_fill_at IS NULL + entry > 2h old) is unsafe until we fix the
# broker-API-trust issue.

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\d-disable-phasee-out.txt"
"# d-disable-phasee $(Get-Date -Format o)" | Out-File $out -Encoding utf8

"# step 1: append disable flags to .env" | Add-Content $out
$envFile = ".env"
$marker = "# === EMERGENCY 2026-05-08: Phase E disabled (broker API silent-empty bug) ==="

if (-not (Select-String -Path $envFile -Pattern "EMERGENCY 2026-05-08: Phase E disabled" -Quiet)) {
    Add-Content -Path $envFile -Value "`n$marker"
    Add-Content -Path $envFile -Value "CHILI_CRYPTO_ENTRY_FILL_WINDOW_HOURS=0"
    Add-Content -Path $envFile -Value "CHILI_CRYPTO_BROKER_ZERO_QTY_STREAK_MIN=0"
    "appended Phase E disable flags to .env" | Add-Content $out
} else {
    "Phase E disable flags already present in .env" | Add-Content $out
}

"# step 2: show the appended block" | Add-Content $out
Get-Content $envFile | Select-String -Pattern "Phase E disabled" -Context 0,3 | Add-Content $out

"# step 3: force-recreate autotrader-worker + scheduler-worker + chili to load new env" | Add-Content $out
docker compose up -d --force-recreate autotrader-worker scheduler-worker chili 2>&1 | Add-Content $out

Start-Sleep -Seconds 12

"# step 4: confirm settings loaded with disabled values" | Add-Content $out
docker exec chili-home-copilot-chili-1 python -c @"
from app.config import settings
print('chili_crypto_entry_fill_window_hours:', getattr(settings, 'chili_crypto_entry_fill_window_hours', 'MISSING'))
print('chili_crypto_broker_zero_qty_streak_min:', getattr(settings, 'chili_crypto_broker_zero_qty_streak_min', 'MISSING'))
"@ 2>&1 | Add-Content $out

"# step 5: confirm 14 trades still open after restart" | Add-Content $out
docker exec chili-home-copilot-postgres-1 psql -U chili -d chili -P pager=off -c "SELECT id, ticker, status, exit_reason FROM trading_trades WHERE id IN (1807,1808,1809,1810,1823,1824,1826,1827,1828,1831,1832,1835,1836,1837) ORDER BY id;" 2>&1 | Add-Content $out

"# step 6: dry-run the sweep (with disabled flags) to confirm no-op" | Add-Content $out
docker exec chili-home-copilot-chili-1 python -c @"
from app.db import SessionLocal
from app.services.trading.bracket_reconciliation_service import run_crypto_stale_trade_close
db = SessionLocal()
try:
    result = run_crypto_stale_trade_close(db)
    db.commit()
    print('result:', result)
finally:
    db.close()
"@ 2>&1 | Add-Content $out

"# end" | Add-Content $out

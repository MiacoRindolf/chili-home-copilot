$out = "scripts/dispatch-round21-deploy-output.txt"
"# round-21 deploy $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "git add + commit" {
    git add app/services/yf_session.py app/services/trading/data_quality.py app/services/trading/market_data.py app/services/trading/learning.py app/services/backtest_service.py scripts/_commit_msg_round21.txt scripts/dispatch-round21-deploy.ps1
    git commit -F scripts/_commit_msg_round21.txt
}

S "force-recreate workers (R21 is in app/, ./app mount picks it up; restart for fresh imports)" {
    docker compose up -d --force-recreate chili scheduler-worker brain-worker autotrader-worker
}

S "wait 20s" { Start-Sleep -Seconds 20; "ok" }

S "smoke test: VIX fetch should now return non-empty" {
    docker compose exec -T chili python -c "from app.services.trading.market_data import fetch_ohlcv_df; df = fetch_ohlcv_df('^VIX', interval='1d', period='5d'); print('VIX rows:', len(df)); print(df.tail(3) if not df.empty else 'EMPTY')"
}

S "smoke test: SPY fetch should still return non-empty (regression check)" {
    docker compose exec -T chili python -c "from app.services.trading.market_data import fetch_ohlcv_df; df = fetch_ohlcv_df('SPY', interval='1d', period='5d'); print('SPY rows:', len(df))"
}

S "smoke test: yf_session cache key now includes end" {
    docker compose exec -T chili python -c "
import inspect
from app.services import yf_session
src = inspect.getsource(yf_session.get_history)
print('cache_key includes end:', 'end' in src and ':{end}' in src)
"
}

S "git push" { git push origin main }

Write-Host "done"

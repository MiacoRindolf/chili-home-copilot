# Reset the drawdown breaker after Phase E's first-sweep cleanup tripped
# the wipeout-burst detector (3-in-5s) on the 14-row backlog clear.
#
# This is a one-shot operator action. After running this, the breaker is
# clean and the autotrader/scheduler resume normal entry decisions.
#
# Per docs/DRAWDOWN_BREAKER_RUNBOOK.md, manual reset is operator-only.
# This script just automates the SQL + the in-memory reset across the
# two long-running worker processes.

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\d-breaker-reset-out.txt"
"# d-breaker-reset $(Get-Date -Format o)" | Out-File $out -Encoding utf8

"# step 1: pre-reset breaker state (latest persisted row)" | Add-Content $out
docker exec chili-home-copilot-postgres-1 psql -U chili -d chili -P pager=off -c "SELECT user_id, snapshot_date, breaker_tripped, breaker_reason, regime FROM trading_risk_state WHERE regime IN ('circuit_breaker', 'breaker_heartbeat') ORDER BY snapshot_date DESC LIMIT 5;" 2>&1 | Add-Content $out

"# step 2: reset via reset_circuit_breaker() in chili + autotrader-worker + scheduler-worker" | Add-Content $out
foreach ($container in @('chili-home-copilot-chili-1', 'chili-home-copilot-autotrader-worker-1', 'chili-home-copilot-scheduler-worker-1')) {
    "## $container" | Add-Content $out
    docker exec $container python -c @"
from app.services.trading.portfolio_risk import reset_breaker, is_breaker_tripped
print('before:', is_breaker_tripped())
reset_breaker()
print('after:', is_breaker_tripped())
"@ 2>&1 | Add-Content $out
}

"# step 3: post-reset breaker state in DB" | Add-Content $out
docker exec chili-home-copilot-postgres-1 psql -U chili -d chili -P pager=off -c "SELECT user_id, snapshot_date, breaker_tripped, breaker_reason, regime FROM trading_risk_state WHERE regime IN ('circuit_breaker', 'breaker_heartbeat') ORDER BY snapshot_date DESC LIMIT 5;" 2>&1 | Add-Content $out

"# end" | Add-Content $out

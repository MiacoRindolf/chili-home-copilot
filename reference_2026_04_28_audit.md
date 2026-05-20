# CHILI Trading Brain Comprehensive Audit - 2026-04-28

Canonical repo copy: `docs/AUDITS/2026_04_28.md`.

This memory file mirrors the audit punch list and finding index so future sessions can orient quickly. For full evidence and per-finding details, read `docs/AUDITS/2026_04_28.md`.

## Punch List

CRITICAL (fix before next trading session):
  1. Autotrader entry gate can return `None`, crashing entry processing - at `app/services/trading/autopilot_scope.py:237` and `app/services/trading/auto_trader.py:745` - fix: complete the no-owner branch and always return an `allowed/reason` dict.
  2. Robinhood exit dup-coid recovery crashes on non-option exits - at `app/services/trading/robinhood_exit_execution.py:608` and `:834` - fix: remove the inner `logger` assignment so the module logger is used.
  3. Crypto exit monitor imports the wrong broker module, so crypto exits fail - at `app/services/trading/crypto/exit_monitor.py:60` and `:176` - fix: import `app.services.broker_service` or use `from ... import broker_service`.
  4. Open live trades already have terminal exit values, and one ETH trade has `entry_price=0` - in `trading_trades` rows 386, 388, 393, 404 - fix: reconcile terminal rows to closed and quarantine zero-entry trades before monitor ticks.
  5. `scan_patterns.oos_win_rate` contains `NaN` and percent values in a fraction column - in `scan_patterns.oos_win_rate` - fix: scrub `NaN`, divide values greater than 1 by 100, and add a range CHECK.

HIGH (this week):
  1. Promoted pattern catalog stats are stale or misleading - promoted rows all have `trade_count=0`, some with `win_rate` near 5 percent or null - fix: sync catalog metrics from raw backtest/realized evidence after promotions.
  2. Migration 199 promotes regime-conditional patterns from a single winning cell while global evidence can be negative - at `app/migrations.py:12941` and `:13012` - fix: require positive raw global EV or mark/enforce truly regime-conditional eligibility.
  3. Regime read path is live while most writer/action modes are still shadow - runtime settings show only `chili_regime_gate_mode='live'` - fix: align settings or make the UI/operator state explicitly say shadow.
  4. Massive is unreachable from the container while Polygon is disabled and appears to share the same key - runtime `USE_POLYGON='false'`, Massive TCP refused, Polygon TCP OK - fix: enable a validated Polygon fallback or add a hard Massive circuit breaker.
  5. Live DB has historical migration numeric-ID collisions even though current file registry passes - `schema_version` duplicates 101, 152, 153, 154 - fix: add a schema-version audit/repair plan that checks live history, not only current registry.
  6. Scheduler jobs are not stuck, but orphaned/timeouts are piling up and scanners run long - `brain_batch_jobs` last 48h has 55 orphaned jobs and scanner durations up to 2131s - fix: tighten job ownership/heartbeat expiry and surface failed scans.
  7. Postgres has idle-in-transaction sessions older than 20 minutes - six live idle transactions observed, longest 24m36s - fix: tune/verify `db_watchdog` and close read-only sessions promptly.

MEDIUM (this month):
  1. Regime ledger has many confident `unknown` cells - e.g. backtest `ticker_regime/unknown` 155 cells, 154 confident - fix: exclude unknown cells from promotion or label them separately.
  2. Intraday and macro regime snapshots were stale relative to the audit date - intraday/macro max `as_of_date=2026-04-27` while other snapshots had 2026-04-28 - fix: increase cadence or mark consumers stale.
  3. Non-`pattern_imminent` breakout alerts have no autotrader audit rows - 320 breakout alerts in 24h, 264 without autotrader rows - fix: either log explicit non-candidate skips or document that only `pattern_imminent` enters the funnel.
  4. Closed trades often lack exit reasons - last 14d: 65 closed rows missing `exit_reason` - fix: require close helpers to provide a reason and backfill from monitor/audit rows.
  5. Active pattern dedupe is still fractured - 636 active patterns, 100 missing signatures, and many duplicate signatures - fix: retroactively merge/retire duplicates after signature generation.
  6. No durable backtest queue table exists, and promoted `last_backtest_at` is stale/null - `promoted=15`, `no_last_backtest=2`, oldest 2026-04-19 - fix: add a durable queue/audit or expose queue state from existing scheduler records.

LOW (informational):
  1. Large repo-root logs are an unbounded storage leak - `backtest_refresh_scheduled.log` is about 1.0 GB and `backtest_refresh.log` about 346 MB - fix: rotate or move generated logs under ignored retention.
  2. `yf_session` cache is not hard-capped when all entries are fresh - at `app/services/yf_session.py:142` - fix: evict LRU/oldest until under `_MAX_CACHE_SIZE`.
  3. Massive WebSocket cache has no explicit cap - at `app/services/massive_client.py:959` - fix: cap by subscribed symbols or age out stale symbols.

## Finding Index

1. Data hygiene: `oos_win_rate` has 17 `NaN` rows and 13 values greater than 1; open terminal trades 386/388/393/404; 65 closed trades in 14d missing exit reasons; promoted catalog stats stale.
2. Provider chain: Massive TCP refused from container; Polygon TCP OK but `USE_POLYGON=false`; Massive and Polygon keys appear identical; crypto yfinance fallback is skipped in list path.
3. Pattern lifecycle: migrations 168/170 rescue 1047; migrations 197/199 promote by backtest/regime evidence; active signatures are missing/duplicated.
4. Regime ledger/gates: only `chili_regime_gate_mode` live; most brain regime modes shadow; ledger has many confident `unknown` cells; intraday/macro snapshots stale.
5. Autotrader entry: `check_autopilot_entry_gate` falls off end; caller uses `gate.get`; non-pattern-imminent alerts are intentionally not audited.
6. Exit monitor: Robinhood local logger shadowing crashes dup-coid path; crypto exit import wrong; duplicate recovery loops flood audit rows.
7. Brain-worker/scheduler: orphaned/timeouts in `brain_batch_jobs`; long scanner durations; idle DB transactions.
8. Backtest queue: no durable queue table; promoted `last_backtest_at` stale/null.
9. Migrations: live `schema_version` duplicate numeric IDs 101, 152, 153, 154; rescue/promotion migrations are policy overrides.
10. Settings drift: expected live modes are mostly still shadow; provider env differs from advertised chain.
11. Memory: root logs about 1.35 GB across two files; yfinance and Massive WS caches need hard caps/eviction.
12. Audit trails: DB error rows need stack context; alert audit semantics need to distinguish candidate-funnel coverage from full-alert coverage.

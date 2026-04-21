# Trading brain — service level objectives

This doc defines the latency / freshness / correctness budgets for CHILI's trading subsystem so "the brain feels slow" can be answered with a number instead of a hunch. Each SLO names the log line that measures it so operators can grep against raw logs without touching dashboards.

All prefixes referenced here are defined in `app/services/trading/ops_log_prefixes.py`. The release-blocker grep that gates Phase-8 rollout lives in `scripts/check_chili_prediction_ops_release_blocker.ps1`.

## Top-level budgets

| # | SLO | Target | Alert rule stub | Observed via |
|---|---|---|---|---|
| 1 | Learning cycle wall-clock | P50 ≤ 45s · P95 ≤ 120s | WARN if P95 > 90s for 3 cycles · ERROR if any cycle > 300s | `[chili_brain_io] learning_cycle_end elapsed_s=...` |
| 2 | Prediction staleness (mirror reads) | max 900s (configurable via `brain_prediction_read_max_age_seconds`) | Any `fallback=stale` in `[chili_prediction_ops]` for >5 min sustained | `[chili_prediction_ops] read=fallback_stale` |
| 3 | Reconciliation lag (broker truth vs DB) | sweep P50 ≤ 10s · P95 ≤ 30s · watchdog escalation at 5 consecutive non-self-healing hits | ERROR on any `[drift_escalation]` log; WARN on sweep P95 > 20s sustained 10 min | `[bracket_reconciliation]` sweep summary + `[drift_escalation]` |
| 4 | Execution event lag (order → ACK) | P50 ≤ 500ms · P95 ≤ 1500ms · ERROR at P95 > 3000ms | Mapped directly in `execution_event_lag.py` → `breach=warn\|error` | `[execution_event_lag] lag breach=...` |
| 5 | Kill-switch activation → gate effective | end-to-end ≤ 5s (call `activate_kill_switch` → next `auto_trader` tick blocks) | ERROR if any `place_market_order` succeeds after `KILL SWITCH ACTIVATED` | `[governance] KILL SWITCH ACTIVATED` then absence of `[auto_trader] placed` |
| 6 | Drawdown breaker trip persistence | 100% — must survive app restart | ERROR if `[circuit_breaker] TRIPPED` observed pre-restart but `get_breaker_status().tripped == False` post-restart | `trading_risk_state` table row query |
| 7 | Broker-equity TTL cache freshness (Phase B) | `hit_fresh` within 5 min · `stale_serve` at most 15 min beyond TTL · `stale_expired` WARN | WARN if `stale_expired` rate > 1/min | `[chili_risk_cache] hit_fresh\|stale_serve\|stale_expired` |
| 8 | Market-data fetch resilience (Phase B) | Per-call success rate ≥ 99% under healthy providers · `exhausted` escalates to WARN | ERROR if `exhausted` rate > 5/min sustained 10 min (auto-trader is starving) | `[chili_market_data] source=... kind=exhausted` |

## Definitions

### 1 — Learning cycle wall-clock

Measured from `run_learning_cycle` entry to `finally`-block exit. The ending log line emits `elapsed_s=<seconds> correlation_done=1 error=<str\|None>`. Cycles are throttled by `learning_interval_hours` (default 1h); a P95 > 120s indicates either:

- Pattern backtest queue depth exceeds configured parallelism (tune `brain_backtest_parallel`),
- Market-data provider is throttling (check for `[chili_market_data] kind=timeout` bursts), or
- DB contention on `brain_prediction_snapshot` / mesh tables.

No hard kill — a slow cycle is degraded, not unsafe. But two consecutive WARNs suggest it's time to look.

### 2 — Prediction staleness

Phase 5 authoritative reads ONLY serve mirror data when the snapshot's `as_of_ts` is within `brain_prediction_read_max_age_seconds` (default 900s = 15 min). Older → falls back to legacy with `read=fallback_stale` in `[chili_prediction_ops]`.

Sustained `fallback_stale` means the learning cycle is not completing fresh predictions — check SLO 1 and `[chili_brain_io]` for cycle completion.

**Release blocker (hard, do not ship if violated):** any `[chili_prediction_ops] read=auth_mirror` line with `explicit_api_tickers=false`. See `docs/TRADING_BRAIN_PREDICTION_MIRROR_ROLLOUT.md`.

### 3 — Reconciliation lag

`bracket_reconciliation_service.run_reconciliation_sweep` runs every 120s (scheduler). Each sweep enumerates open brackets, fetches broker truth, and classifies drift. P50/P95 are measured across sweeps per 15-min window (not per-trade).

`[drift_escalation]` fires when the same non-agree kind persists across `chili_drift_escalation_min_count` sweeps (default 5) — that's an order that's stuck in drift for ≥10 min. Feature flag: `chili_drift_escalation_enabled`.

### 4 — Execution event lag

Measured by `execution_event_lag.py::measure_execution_event_lag`. The SQL query derives p50/p95 from `trading_venue_truth_log` where each row is a submitted→ack pair. Thresholds are in settings:

- `chili_execution_event_lag_warn_ms` (default 1500)
- `chili_execution_event_lag_error_ms` (default 3000)

Log lines:
- `[execution_event_lag] ok p50=... p95=... samples=...` — within budget
- `[execution_event_lag] lag breach=WARN ...` — p95 > warn
- `[execution_event_lag] lag breach=ERROR ...` — p95 > error

Sustained error-breach = broker API or network latency problem, NOT an application issue.

### 5 — Kill-switch effectiveness

Hard Rule 1 (CLAUDE.md): every `place_*_order` path consults `is_kill_switch_active()` before contacting the broker. A tripped switch persists via `_persist_kill_switch_state` to `trading_risk_state` and is restored on startup by `restore_kill_switch_from_db()`.

SLO assertion: after `activate_kill_switch("reason")` is called, the NEXT auto-trader tick (default 10s interval) must observe `is_kill_switch_active() == True` and short-circuit. Any `[auto_trader] placed` log after the activation timestamp is a correctness violation. See `docs/KILL_SWITCH_RUNBOOK.md`.

### 6 — Drawdown breaker trip persistence

Hard Rule 2. `check_drawdown_breaker` + `_persist_breaker_state` writes the tripped state to `trading_risk_state` with `regime IN ('default', 'risk_off', 'risk_on')`. On startup, `get_breaker_status()` is expected to return the last persisted state. SLO: 100% — any drift here is an unhandled restart or a DB-write failure (look for `[circuit_breaker] Failed to persist` in logs).

### 7 — Broker-equity TTL cache freshness

Phase B addition. Cache kinds logged by `resolve_effective_capital` (gated behind `chili_autotrader_broker_equity_cache_enabled`):

- `hit_fresh` — within TTL; ideal
- `miss_refresh` — cache miss or expired; broker call succeeded
- `stale_serve` — broker unreachable; served aged value within `ttl + max_stale`
- `stale_expired` — broker unreachable AND cache older than budget; fell back to env default
- `miss_no_data` — no prior cache AND broker unreachable; fallback

`stale_expired` means sizing is now happening against the static `chili_autotrader_assumed_capital_usd` — treat as a real broker outage.

### 8 — Market-data fetch resilience

Phase B addition. `_ohlcv_summary` and `_current_price` retry 3× with exponential backoff (0.5s, 1s). Kinds:

- `ok` — value returned
- `empty` — no rows / null price
- `timeout` / `transport` / `upstream` — classified exceptions
- `exhausted` — all 3 attempts failed; WARNING

Sustained `exhausted` is the auto-trader starving for quotes — a tripped kill switch is appropriate if it persists across multiple tickers.

## Where the log prefixes live

All prefixes above except `[governance]` and `[circuit_breaker]` are sourced from `app/services/trading/ops_log_prefixes.py`. `[governance]` lives in `governance.py`; `[circuit_breaker]` lives in `portfolio_risk.py`. These two are venue-specific (kill-switch + breaker) and live with their respective modules — adding them to the registry would be cosmetic.

## Relationship to the release-blocker grep

`scripts/check_chili_prediction_ops_release_blocker.ps1` is the single automated gate. It is NOT an SLO — it is a hard correctness check. Passing it is necessary for a Phase-8 rollout but does not replace the SLOs above.

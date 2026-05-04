# Fast Path (Crypto Scalping) — Handoff Doc

**Last updated:** 2026-05-01 (end of Cowork session that built F1–F4)
**Current branch:** `main` is the active branch; all F1–F4 commits are on it.

This doc exists so Claude Code can pick up the fast-path subsystem without re-deriving context. The Cowork session that built F1–F4 had persistent memory in `%APPDATA%\Claude\local-agent-mode-sessions\...\memory\`, but those memory files are Cowork-specific. This doc ports the load-bearing facts into the repo so future sessions inherit them.

---

## Why this exists

CHILI's swing-trade lane (`Trade` table, `auto_trader`, `bracket_*`, etc.) holds positions for hours-to-days. The user wanted a **separate, parallel fast lane** for sub-minute crypto scalping (Ross-Cameron-style momentum) on Coinbase. Same brain conceptually, different timescale and different infra requirements (sub-second placement, in-memory order book, async event-driven).

Architecture contract: `docs/ARCHITECTURE-fast-path.md` (read this first if you haven't).

---

## What's shipped (commits on main)

| Commit | Phase | What it does |
|--------|-------|--------------|
| `46b94c2` | F1 | Coinbase Advanced Trade WS `candles` channel → `fast_snapshots` (1m bars). Migration 215 creates the partitioned table set. Container service `fast-data-worker`. |
| `1522417` | F1 fix | sys.path injection bug, env override, WS diagnostic counters |
| `dda20d2` | F2 | Coinbase `level2` channel → in-memory `OrderBookAggregator` → sampled `fast_orderbook` rows every 250ms with imbalance + spread_bps. websockets `max_size` raised to 32MB. |
| `80d1551` | F3 | `MomentumScanner` emits `volume_breakout_long`, `imbalance_long/short`, `spread_squeeze` alerts to `fast_alerts`. Migration 216. |
| `1431cb9` | F4 | Paper-mode `FastPathExecutor` polls fast_alerts, runs gates, writes `fast_executions`. Migration 217. Live placement was a stub (`LiveExecutionNotAuthorized`). |
| `f420ea6` | F4 UI | Autopilot page section showing real-time paper P/L. Polls 3 endpoints every 5s. |
| `8f18be5` | F4 live | Replaced live stub with real Coinbase calls behind 8 safety belts. Default still paper. |

---

## Current state (as of last evaluation)

**System health (clean):**
- `fast-data-worker` container Up healthy, ~138 MiB / 512 MiB RSS, flat for 10+ min
- 0 reconnects, 0 errors_60s, 0 db_errors, 0 drops
- WS throughput: ~30k messages/min, ~700 candle events, ~30k L2 updates → ~7–8k books emitted after throttling
- Sub-millisecond decision latency (avg 0.18 ms from alert row to decision row)

**Paper book:** 7 open paper positions, $175 notional in, **+$0.11 floating P/L (+0.063%)**.

**Decision distribution:** 7 fills / 176 rejects = 96% reject rate.
- 60% `capacity:pair_already_held` (1-position-per-pair cap saturated)
- 31% `recency:alert_too_old` (snapshot-replay alerts blocked — working as designed)
- 7% `min_score:score_below_threshold`
- 2% `short_unsupported_in_spot`

---

## Safety contract for live Coinbase placement (DO NOT BYPASS)

The live path is wired (`_place_coinbase_order_live` in `app/services/trading/fast_path/executor.py`) but **gated by 8 layers of defence-in-depth**:

1. **Compose default** keeps `CHILI_FAST_PATH_MODE=paper` and `CHILI_FAST_PATH_EXEC_LIVE_AUTHORIZED` unset
2. **`gate_mode_interlock`** in `gates.py` forces paper if either is missing — has no bypass path
3. **Point-of-place re-check** in executor re-reads `is_live_authorized()` at the moment of placement (catches mid-flight env changes)
4. **`LIVE_FIRST_TRADE_USD_HARD_CAP = 10.0`** — even with auth set, single orders >$10 reject unless `CHILI_FAST_PATH_LIVE_NOTIONAL_OK=1` is also set (third flag)
5. **Broker connectivity check** — raises if `coinbase_service.is_connected()` returns False after one auto-retry
6. **Side validation** — only `buy`/`sell`
7. **Post-placement verification poll** — polls `get_order_by_id` for 3s; accepts `open`/`pending`/`filled` as confirmed; raises on terminal-reject (mirrors swing-path `verify_order_landed` pattern, learned from the ELTX incident)
8. **CRITICAL log lines** on every live attempt and every verified placement

**Verified end-to-end via probe** that all 4 reachable safety belts trip correctly:
- No auth flag → blocks
- $100 notional with cap-not-overridden → blocks at $10 cap
- Bad side → blocks
- Cap-overridden, no broker connection → blocks

**To activate live (operator checklist, in this exact order):**
1. Validate paper P/L on autopilot UI for several hours
2. Configure Coinbase credentials (vault preferred over .env — see `BrokerCredential` rows; same pattern as swing-trade Robinhood path)
3. Set `CHILI_FAST_PATH_MODE=live`
4. Set `CHILI_FAST_PATH_EXEC_LIVE_AUTHORIZED=1`
5. Restart fast-data-worker; first orders capped at $10 regardless of `EXEC_NOTIONAL_USD` setting
6. Watch container logs for `[fast_path] LIVE PLACED+VERIFIED` lines and verify order_ids appear in Coinbase web UI
7. Only after 3–5 successful small live orders confirm correct behavior, set `CHILI_FAST_PATH_LIVE_NOTIONAL_OK=1` for normal sizing

**To kill switch live without restart:** unset `CHILI_FAST_PATH_EXEC_LIVE_AUTHORIZED` — gate re-reads env each call, so next decision downgrades to paper. Already-placed orders live at Coinbase and must be cancelled manually.

**DO NOT** modify `LIVE_FIRST_TRADE_USD_HARD_CAP`, `is_live_authorized`, or remove any safety belt. If a higher cap is needed, the operator should set `LIVE_NOTIONAL_OK=1` and `EXEC_NOTIONAL_USD` themselves.

---

## Two-lens evaluation findings (to fix in priority order)

### Algo trader concerns
1. **No edge can be measured without exits.** Every paper position has been "buy on first imbalance signal per pair, hold forever." +0.063% across 30 min is indistinguishable from random walk. **F5 (exit manager) is the unblocker for everything else.**
2. **Magic numbers in `gates.py`** — `MIN_SIGNAL_SCORE=0.30`, `MAX_SPREAD_BPS=8.0`, `DEFAULT_NOTIONAL_USD=25.0`, etc. should be derived from chili brain, not constants.
3. **Risk frame is missing** — no stop, no target, no time stop. Should call `app/services/trading/stop_engine.py` (existing ATR + regime engine).
4. **Position sizing is fixed $25** — should call `app/services/trading/position_sizer_model.py` (existing Kelly fraction).
5. **No correlation gate** — could long all 5 crypto pairs simultaneously (BTC/ETH/SOL/AVAX/DOGE move together). Should reuse `app/services/trading/correlation_budget.py`.
6. **Pattern mining doesn't see fast lane** — `learning.py` mines patterns from `trading_snapshots` (1d/1h equity bars). Should add a parallel miner for `fast_snapshots` + `fast_alerts` to discover 1m crypto patterns.
7. **OB imbalance is a 1–5s predictor** — by the time we hold for minutes the edge is gone or flipped. Without paired tight exits, we've inverted the trade. F5 with sub-minute exits will fix this structurally.

### Dev architect concerns
1. **Polling beats event-driven.** Executor polls `fast_alerts` every 1s; should switch to Postgres `LISTEN/NOTIFY`. The pub/sub pattern already exists in `app/services/trading/price_bus.py` and `app/services/code_dispatch/notifier.py`.
2. **`_open_positions` is in-memory dict** — resets on container restart. Should query DB on boot or persist.
3. **No portfolio-level concurrency cap** — only per-pair. Need `gate_portfolio_concurrency` (max N total open across all pairs).
4. **No realized P/L schema.** `fast_executions` only records the entry row. Add an exit_decision_id linkback or a sister `fast_realized_pnl` table.
5. **No per-asyncio-task heartbeat watchdog.** If scanner blocks on a sync call, the whole pipeline halts silently. Mirror the watchdog pattern in `app/services/trading/db_watchdog.py` and friends.
6. **Recency window 60s is too wide for scalping.** Tighten to 5–10s once F5 is shipping fresh signals.
7. **Stale `last_error`** persists in `fast_path_status` after pair has recovered (`error_count_60s=0` and `reconnect_count=0`). Clear after N successful streaming minutes.
8. **Autopilot UI polls every 5s** instead of using existing `/ws/autopilot/live` WebSocket pattern. Convert to push-based feed.
9. **Compose `enabled=1` default** — fast lane comes up streaming on first boot of a fresh deploy. Probably what we want, but flag for review when productionizing.

---

## Integration inventory — what to reuse from chili brain

| Feature | File | Public function | Reusable for fast-path |
|---------|------|-----------------|------------------------|
| Stop/target sizing | `app/services/trading/stop_engine.py` | `compute_stop_distance()` (ATR + regime + lifecycle) | Replace gates.py constants |
| Position sizing | `app/services/trading/position_sizer_model.py` | `compute_proposal(input)` (Kelly fraction, capped) | Replace `DEFAULT_NOTIONAL_USD=25` |
| Correlation gate | `app/services/trading/correlation_budget.py` | `bucket_for(symbol)`, `compute_correlation_budget(db, user, ticker)` | Add `gate_correlation_bucket` |
| Daily loss circuit breaker | `app/services/trading/governance.py` | `is_kill_switch_active()`, `activate_kill_switch(reason)` | Add `gate_kill_switch` reading `trading_risk_state` |
| Pattern mining | `app/services/trading/learning.py`, `learning_cycle_steps/*` | `get_current_predictions()`, `run_secondary_miners_phase()` | Add a parallel miner for `fast_snapshots`/`fast_alerts` → `fast_patterns` table |
| Postgres pub/sub | `app/services/trading/price_bus.py`, `app/services/code_dispatch/notifier.py` | `pg_notify('channel', payload)` pattern | NOTIFY on `fast_alerts` insert; executor LISTENs |
| Watchdog | `app/services/trading/db_watchdog.py`, `backtest_watchdog.py`, `stuck_order_watchdog.py` | Periodic check + escalation | Add `fast_path_watchdog` to supervisor |
| Realized P/L on Trade rows | `app/models/trading.py` (Trade), `realized_stats_sync.py`, `bracket_writer_g2.py` | exit_price, pnl, exit_date columns + writers | F5 closes the loop |
| WebSocket UI | `app/routers/trading.py` `/ws/autopilot/live` | Existing infra | Push fast_executions in real time |

---

## Suggested implementation order (F5 onward)

1. **F5 — Exit manager (unblocks everything).** Even a minimal version (60s time-stop + 0.5% take-profit + 0.3% stop-loss using stop_engine) closes the loop. Schema: extend `fast_executions` with `exit_decided_at`, `exit_price`, `exit_reason`, `realized_pnl_usd`, OR new `fast_exits` table joined on entry id. Pick whichever partitions cleaner.

2. **F4-revisit — Wire dynamic thresholds.** Replace constants in `gates.py`:
   - `DEFAULT_NOTIONAL_USD` → `position_sizer_model.compute_proposal()` (needs equity + stop distance)
   - Stop distance → `stop_engine.compute_stop_distance(ticker, regime, lifecycle_state)`
   - `MIN_SIGNAL_SCORE` → derived from rolling win-rate × avg-win vs (1-WR) × avg-loss in recent `fast_realized_pnl`
   - `IMBALANCE_LONG_THRESHOLD` / `VOL_BREAKOUT_MULT` → ditto, mined per-pair

3. **Postgres LISTEN/NOTIFY** for executor — drops poll latency from up to 1000ms to <10ms.

4. **Persist `_open_positions` to DB** — query on boot from `fast_executions` where decision='paper_fill' and not yet exited.

5. **Portfolio + correlation gates** — `gate_portfolio_concurrency` (max N total) + `gate_correlation_bucket` (use `correlation_budget.bucket_for`).

6. **Watchdog task in supervisor** — heartbeat per asyncio task; logs WARN if scanner sees no books for 30s while ws is connected.

7. **WebSocket UI feed** — push fast_executions inserts to autopilot page instead of 5s poll.

8. **Pattern miner extension** — wire `learning_cycle_steps` to also process `fast_snapshots` + `fast_alerts` and emit 1m crypto patterns. This is the "chili brain learns the fast lane" piece the user asked for.

9. **Tighten recency to 10s** once F5 + dynamic thresholds prove stable.

10. **Clear stale `last_error`** in status_tracker.

---

## Files to know

- `app/services/trading/fast_path/` — the fast-path package
  - `settings.py` — env-driven `FastPathSettings`
  - `status_tracker.py` — per-pair circuit breaker, persists to `fast_path_status`
  - `db_writer.py` — bounded asyncio queue + batch writer for bars/books/alerts
  - `ws_client.py` — Coinbase WS subscriber + scanner integration
  - `order_book.py` — `OrderBookAggregator` (in-memory L2 mirror + sampled emission)
  - `scanner.py` — `MomentumScanner` (volume_breakout, imbalance, spread_squeeze)
  - `gates.py` — execution gate suite + `is_live_authorized()` helper
  - `executor.py` — `FastPathExecutor` (alert listener + decision pipeline + live placement)
  - `healthz.py` — aiohttp /healthz on port 8090
  - `supervisor.py` — boots/owns the asyncio loop
- `scripts/fast_data_worker.py` — container entrypoint
- `app/migrations.py` — migrations 215 (tables), 216 (fast_alerts), 217 (fast_executions)
- `docker-compose.yml` — `fast-data-worker` service definition
- `app/routers/trading_sub/fast_path_api.py` — read-only endpoints for autopilot UI
- `app/templates/trading/_autopilot_fast_path.html` — UI partial

---

## DB tables

- `fast_snapshots` (mig 215) — partitioned by `bar_close_at`. 1m OHLCV bars from Coinbase. ON CONFLICT (ticker, interval, bar_close_at, source) DO NOTHING.
- `fast_orderbook` (mig 215) — partitioned by `snapshot_at`. Top-N L2 levels + imbalance + spread_bps, sampled every 250ms.
- `fast_path_status` (mig 215) — single row per ticker with state machine (streaming/degraded/paused/halted), error_count_60s, last_error, etc.
- `fast_alerts` (mig 216) — partitioned by `fired_at`. Scanner emissions: ticker, alert_type, signal_score, features JSONB.
- `fast_executions` (mig 217) — partitioned by `decided_at`. Every executor decision (paper_fill / live_placed / rejected) with full gate JSONB + latency_ms.

---

## Don't break these contracts

1. **The 8 safety belts in `_place_coinbase_order_live`** — see Safety Contract section.
2. **`gate_mode_interlock` runs first** in `DEFAULT_GATES` and may MUTATE `ctx.mode` (the only side-effect in the gate suite, deliberately scoped).
3. **`enqueue_alert` and `enqueue_bar` NEVER drop silently** — bar-close events and alerts are execution-relevant. Only `enqueue_book` drops on backpressure (sub-bar-close granularity).
4. **`websockets max_size=32MB`** in `ws_client.py` — Coinbase L2 snapshots for major pairs can be 8–15MB. Don't lower this.
5. **`BAR_CLOSE_GRACE_S=3.0`** in `ws_client.py` — bars are persisted only after `start + 60 + 3` seconds. Don't shorten.
6. **`fast_alerts.fired_at` may be stale on snapshot replay.** Any consumer (executor, F5, future F6 miner that wants live-only patterns) must filter recency. The recency gate in F4 does this.

---

## Where the Cowork session's persistent memory lives

`%APPDATA%\Claude\local-agent-mode-sessions\4a2f2d05-2509-493b-b062-7be884eec9b1\1ba839d2-...\spaces\f421ac6c-.../memory\`

Key files (informational; Claude Code can't read these but you can):
- `MEMORY.md` — index of all memories
- `project_fast_path_live_wiring_pending.md` — full live-activation contract (all 8 safety belts and the operator checklist)
- `reference_fast_path_f1_live.md`, `_f2_live.md`, `_f3_live.md` — phase-by-phase facts
- `feedback_no_hardcoded_fallbacks.md` — user's general "no magic numbers" feedback (applies to fast-path gates too)
- `feedback_take_initiative.md` — when tools cover it, do it; only hand back to user for things outside the sandbox

The most load-bearing of those (the safety contract) is reproduced in this doc.

---

## Bracket writer cover-policy (2026-05-04)

**One-sell-per-share constraint (architectural).** Robinhood retail accepts only one open sell order per held share. A SELL_STOP and a take-profit limit cannot both reserve the same shares -- whichever is placed first holds them.

**Default policy: surface the conflict, do not auto-cancel.** When the bracket writer's `place_missing_stop` finds `held_for_sells == broker_qty` (every share already committed to an existing sell), it now writes a structured `pending_decision` row into `trading_bracket_intents.payload_json` and returns. **The writer never cancels covering limit-sells unilaterally.** The 2026-05-04 19:14 deploy demonstrated the cost of the prior auto-cancel behavior: 5 operator-authored profit-targets were cancelled to free shares for stops, a strategy shift the operator did not authorize.

**`CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL` defaults to `0` going forward.** The auto-cancel branch was removed from the writer in `bracket-writer-respect-upside-targets`. Even if the env var is flipped to 1 again, the code no longer reads it; the pending-decision surface is the single decision point.

**Operator-input mechanism: pending-decision endpoint.** `POST /api/admin/bracket-decisions/<bracket_intent_id>` accepts `{"choice": "keep_target" | "replace_with_stop" | "convert_to_trailing_stop"}` and validates against the row's current `options` list. The reconciler reads `payload_json.pending_decision.operator_choice` on the next sweep and routes to the corresponding resolution path:

- `keep_target` -> intent transitions to `accepted_no_stop`, no broker action
- `replace_with_stop` -> cancels listed covering orders, places stop at brain `stop_price`, clears pending
- `convert_to_trailing_stop` -> NOT_IMPLEMENTED (no broker-side trailing-stop helper as of 2026-05-04); option only appears when a helper is detected dynamically

**Forward pointer: autopilot-settings UI (Phase 7).** The pending-decision endpoint is the data-layer contract the future autopilot-settings page will consume to surface decisions to the operator. UI work is out of scope for this surface; the data contract above is stable for that consumption.

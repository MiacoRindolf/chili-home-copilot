# Trading Brain durable work ledger (event-first)

## Verdict (repo-grounded)

- **Mesh (`brain_activation_events`)** remains the **observable neural surface** (propagation, momentum hooks). It is **not** the durable orchestration queue.
- **`brain_work_events`** is the **work + outcome ledger**: idempotency via `dedupe_key` + partial unique index on **open** work rows, retries, leases, dead-letter.
- **`run_learning_cycle`** does **not** take market snapshots inline; snapshot counts in the cycle report stay zero. **Scheduler** job `brain_market_snapshots` runs `run_scheduled_market_snapshots` and emits **`market_snapshots_batch`** (outcome).
- When `brain_work_ledger_enabled` and `brain_work_delegate_queue_from_cycle` are both true, the in-cycle **ScanPattern queue drain is skipped** so the same patterns are not backtested twice (worker ledger dispatch owns queue backtests).

## Authoritative emit boundaries

| Event | Emits from | Dedupe key (stable) |
|-------|------------|---------------------|
| `backtest_requested` | `ensure_mined_scan_pattern` (new mined row only), `boost_pattern` | `bt_req:pattern:{id}` |
| `backtest_completed` | `brain_work.dispatcher` after `execute_queue_backtest_for_pattern` | `bt_done:req:{work_event_id}` |
| `promotion_changed` | **`promotion_surface.emit_promotion_surface_change`** only — called from `pattern_engine.update_pattern`, `lifecycle.transition`, queue BT dispatcher, prescreen reject fallback, `run_live_pattern_depromotion` fallback | `promo:p{id}:{sha256...}` |
| `market_snapshots_batch` | `trading_scheduler._run_brain_market_snapshot_job` after successful snapshot run | `mkt_snap_batch:{job_id}` or time bucket |
| `paper_trade_closed` | `brain_work.execution_hooks.on_paper_trade_closed` (from `check_paper_exits`) | `paper_closed:{id}:{reason}` |
| `live_trade_closed` | `on_live_trade_closed` (`portfolio.close_trade`) | `live_closed:{id}:{source}` |
| `broker_fill_closed` | `on_broker_reconciled_close` (`broker_service` RH sync / manual cleanup) | `broker_closed:{id}:{source}` |
| `execution_quality_updated` | Dispatcher handler `execution_feedback_digest` after `compute_execution_stats`, `suggest_adaptive_spread`, `run_live_pattern_depromotion` | `exec_quality:u{user}:{hour}` |
| Work row `execution_feedback_digest` | Debounced refresh via `enqueue_or_refresh_debounced_work` on paper/broker/live close | `exec_fb_digest:user:{user_id}` |

## Handler contracts

### `backtest_requested` → queue BT

| Field | Value |
|-------|--------|
| **Lease scope** | `lease_scope=backtest` on enqueue |
| **Emits** | `backtest_completed`, `promotion_changed` (via promotion surface); mesh `publish_brain_work_outcome` |
| **Budget** | `brain_work_dispatch_batch_size` per dispatch round |

### `execution_feedback_digest` (work)

| Field | Value |
|-------|--------|
| **Consumes** | Work row; `payload.user_id` |
| **Emits** | Outcome `execution_quality_updated`; mesh observation (best-effort) |
| **Dedupe** | Open row `exec_fb_digest:user:{id}`; debounce refreshes `next_run_at` |
| **Lease scope** | `execution_feedback` |
| **Budget** | `brain_work_exec_feedback_batch_size` per round |
| **Debounce** | `brain_work_exec_feedback_debounce_seconds` |

## Schema

- `109_brain_work_events` — base table.
- `110_brain_work_lease_scope` — `lease_scope` column + index `(lease_scope, status, next_run_at)`.

## Worker / dispatch

- **`run_brain_work_dispatch_round`** processes **`execution_feedback_digest` first**, then **`backtest_requested`** (same lease/release/stale logic).
- **Lean cycle**: dispatch runs **before** each `run_learning_cycle` (first-class, not only post-cycle).
- **Activation loop**: dispatch runs **before** each neural activation batch.

## API / UI

- **`/api/trading/scan/status` → `work_ledger`**: `pending_work`, `retry_wait`, `dead_last_24h`, `pending_by_type`, `processing`, `last_done_by_type`, `recent_completions`, `recent_meaningful_outcomes`.
- **Brain desk** renders handler-centric summary from the above.

## Compatibility / risks

- **Double drain**: `brain_work_delegate_queue_from_cycle` + ledger enabled.
- **Poison pill**: missing payload → retry/dead.
- **Promotion double emit**: surface helper no-ops when `(promotion_status, lifecycle_stage)` unchanged; `update_pattern` + `lifecycle.transition` may both fire in one flow when each mutates a different part of the surface (intentional audit granularity).

## Files (slices 1–2)

- `app/migrations.py` — `109`, `110`
- `app/models/trading.py` — `BrainWorkEvent`
- `app/services/trading/brain_work/` — ledger, emitters, dispatcher, `promotion_surface`, `execution_hooks`
- `app/services/trading/pattern_engine.py` — `update_pattern` → promotion surface
- `app/services/trading/lifecycle.py` — `transition` → promotion surface
- `app/services/trading/learning.py` — cycle docstring; depromotion fallback emit; removed duplicate `test_pattern_hypothesis` promotion emit
- `app/services/trading/paper_trading.py`, `portfolio.py`, `broker_service.py` — execution hooks
- `app/services/trading_scheduler.py` — `market_snapshots_batch` outcome
- `app/config.py` — ledger + debounce + snapshot outcome flags
- `scripts/brain_worker.py` — dispatch before cycle / activation batch
- `app/routers/trading_sub/ai.py`, `app/templates/brain.html` — richer `work_ledger`
- `tests/test_brain_work_ledger.py`

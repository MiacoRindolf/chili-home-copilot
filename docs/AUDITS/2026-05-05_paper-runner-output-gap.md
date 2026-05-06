# Paper-runner output gap diagnostic

**Date**: 2026-05-05
**Brief**: `f-diagnose-paper-runner-output-gap` (Phase 3 of `f-overnight-cleanup`)
**Question**: Why does `trading_paper_trades` have 0 rows total, ever, despite the brain producing 691 BreakoutAlerts and 700 AutoTraderRun records in the last 24h?

## Executive summary

The brief's "paper-runner output gap" framing is a misnomer. There are **two independent paper-trading systems** in the brain, and they use different storage:

1. **The "momentum paper runner"** (the system that emits `Momentum paper runner: ticked N session(s)` log lines) writes to `trading_automation_sessions` / `trading_automation_events`. It does **not** write to `trading_paper_trades`. Searched the entire `app/services/trading/momentum_neural/` tree for `PaperTrade(` constructions — zero hits.
2. **The legacy `auto_trader.py` BreakoutAlert path** is the only writer of `trading_paper_trades`, via `paper_trading.open_paper_trade(...)` at one site (`auto_trader.py:1526`). Verified by grep across `app/services/`: only one `PaperTrade(` constructor in the whole repo, at `paper_trading.py:166`.

The legacy auto_trader IS firing (700 runs / 24h), but every decision is **blocked or skipped** before reaching the paper-fallback at `auto_trader.py:1517`. The recent decisions:

```
decision=blocked reason=broker:Robinhood crypto endpoint returned no order_id
decision=blocked reason=pre_broker:venue_unsupported_crypto:XDC
decision=skipped reason=synergy_disabled_second_signal
decision=blocked reason=broker:Robinhood crypto endpoint returned no order_id
```

These all fail in the **live** branch of `_process_one_alert`, before the function falls through to paper. Because `runtime.live_orders_effective == True` (desk row or `chili_autotrader_live_enabled=true`), the auto_trader attempts a real broker call first; when that fails, it returns from the function (recording the block) — it does NOT fall back to paper.

**So the root cause is operational, not architectural**: the auto_trader is running in live-mode-attempt while the configured live-broker endpoint (Robinhood crypto) is not actually placing orders. Every alert gets blocked at the broker call. No paper fallback exists for the "live attempt failed" case.

## Per-question findings

### 1. What does the paper-runner-batch job actually call?

`scripts/trading_scheduler.py:_run_momentum_paper_runner_batch_job` (line 349) calls:
- `list_runnable_paper_sessions(db, limit=30)` — selects from `trading_automation_sessions` where `mode='paper'` and state in PAPER_RUNNER_RUNNABLE_STATES.
- For each session id: `tick_paper_session(db, sid)` (`paper_runner.py:375`).

`tick_paper_session` updates the session's `risk_snapshot_json` with paper-execution state (entry/exit prices, position dict). It writes `TradingAutomationEvent` rows for `paper_filled` / `paper_exited`. **It does NOT call `open_paper_trade` or insert into `trading_paper_trades`**.

### 2. What does "ticked N session(s)" mean?

The log line at `trading_scheduler.py:395`:
```python
logger.info("[scheduler] Momentum paper runner: ticked %d session(s)", ticked)
```

"Sessions" here = `TradingAutomationSession` rows in `mode='paper'`, NOT `trading_paper_trades` rows. The paper-runner-batch loops through queued/active automation sessions and advances each by one step (entry → position → exit). The output is recorded in the session's JSON snapshot + `TradingAutomationEvent` rows.

### 3. Is there a separate "paper sessions" concept in the schema?

Yes:
- `trading_automation_sessions` (ORM `TradingAutomationSession`) — the per-session state machine, mode in `('paper', 'live')`.
- `trading_automation_events` — append-only event stream for each session.
- `momentum_symbol_viability` — per-symbol paper-eligibility evidence.
- `momentum_strategy_variants` — the parameterized strategy variants.

The momentum paper-runner system is conceptually a self-contained simulator that records its results in its own tables. **`trading_paper_trades` is the legacy schema for the BreakoutAlert/auto_trader paper path**, not the momentum runner.

### 4. Is the auto_trader path gated on something currently false?

No — the auto_trader IS firing (700 AutoTraderRun rows / 24h). The audit shows `runtime.live_orders_effective == True` (inferred from the broker-call decisions), so the live branch runs first. The phantom-trade guard at `auto_trader.py:1428` returns early when the live broker call returns no `order_id`:

```python
order_id_raw = res.get("order_id") or ""
if not str(order_id_raw).strip():
    _audit(... decision="blocked", reason="broker:place_no_order_id" ...)
    out["skipped"] += 1
    return  # <-- never falls through to paper
```

The paper branch at `auto_trader.py:1517` is only reached when `if live:` is False (line 1370). Since `live=True` is set from the desk row or `chili_autotrader_live_enabled`, the paper branch never runs.

### 5. Were there ever paper trades historically?

`SELECT COUNT(*) FROM trading_paper_trades` returns 0 — never. So it's not a regression from a specific commit; the table has just never been used in this DB. This is consistent with a brain that's been running in live-attempt mode since deploy; the legacy paper-fallback has simply never been the active branch.

(Caveat: I cannot rule out that the table was wiped at some point. But given there are zero rows AND the live-attempt branch dominates, the most parsimonious explanation is that the paper branch has just never been hit.)

### 6. Relationship between "ticked N sessions" and `trading_paper_trades` row creation?

**There is no relationship.** They are separate systems. The momentum paper-runner ticks `TradingAutomationSession` rows; the legacy auto_trader writes to `trading_paper_trades`. Each is independently gated on its own settings:
- `chili_momentum_paper_runner_enabled` + `chili_momentum_paper_runner_scheduler_enabled` (momentum side)
- `chili_autotrader_enabled` + `live_orders_effective` (auto_trader side)

## Root cause

**Two findings**, in priority order:

**A. The brief's premise is wrong** — the gap isn't between "paper-runner ticks" and `trading_paper_trades` rows. Those are separate systems. The momentum paper-runner is doing its work correctly (recording in `trading_automation_sessions` / `trading_automation_events`); `trading_paper_trades` would only get rows from the legacy auto_trader BreakoutAlert path.

**B. The legacy auto_trader IS firing but its live branch dominates and blocks** — every alert tries Robinhood crypto and fails (`no order_id` returned), then returns without falling through to paper. To get `trading_paper_trades` rows from the auto_trader path, either:
- (i) Set `chili_autotrader_live_enabled=false` (so `if live:` is False at `auto_trader.py:1370`), forcing the paper branch.
- (ii) OR add a "fall through to paper on broker failure" handler in the live branch (currently missing — the live branch returns instead).

Option (i) is one operator setting flip. Option (ii) is a behavioral change.

## Suggested follow-up brief

`f-fix-autotrader-paper-fallback`: when `live=True` and the broker call fails with `no_order_id`, fall through to the paper-trade path so the brain still records WHAT it would have done. Today's behaviour silently drops 100% of would-have-been-trades on live-broker failure.

This is a behavioral change (the operator might WANT the current "block on live failure" behaviour as a safety mechanism). Surface for explicit operator decision before implementing.

If the operator instead wants the simpler interpretation — "I want the paper-trades table populated for evidence collection" — the right action is just `UPDATE brain_runtime_mode ... payload_json = jsonb_set(payload_json, '{live_orders}', 'false')` at the desk-row level. No code change.

## Updated PHASE2_HANDLER_BACKLOG.md entry

(See same-named file; section "Diagnostic findings (2026-05-05)" added.)

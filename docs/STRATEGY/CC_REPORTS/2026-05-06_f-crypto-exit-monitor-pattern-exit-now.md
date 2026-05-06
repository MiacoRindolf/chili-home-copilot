# CC_REPORT: f-crypto-exit-monitor-pattern-exit-now

**Outcome: SHIPPED — direct patch (not via standard Cowork→CC brief loop). Verified live.**

## Provenance note

This was an unbriefed direct patch authored by Cowork during a live debug session. The user reported "I'm seeing exit now on TRUMP-USD but it's not exiting in Robinhood." Diagnosis surfaced a real architecture gap, the user authorized direct patching (Option 1 of the choice presented), and the fix landed within minutes. This file is filed retroactively to keep the CC_REPORTS trail consistent with what actually happened in the repo.

## What was wrong

The crypto exit lane (`crypto/exit_monitor.run_crypto_exit_pass`) only fires exits on price-based triggers (stop_loss_hit / take_profit_hit). It does NOT consume `trading_pattern_monitor_decisions.action='exit_now'`.

The equity exit lane (`auto_trader_monitor.tick_auto_trader_monitor`) DOES consume that decision (lines 413-453, helpers `_latest_monitor_decisions_by_trade` + `_fresh_monitor_exit_meta`). When the LLM/pattern monitor flags a position's thesis as dead, the equity lane exits with `pending_exit_reason='pattern_exit_now'` even when price has not hit stop/target.

Crypto was missing this branch because Task HHH (which split crypto out of the equity exit monitor) ported the price-trigger logic but never the LLM/pattern-health branch. The gap was silent for the lifetime of the crypto exit monitor.

## Surfaced case

Trade 1829 — TRUMP-USD long, qty 124, entry $2.42, stop $1.99, target $2.87. Pattern monitor recorded `action='exit_now'` at:
- 2026-05-05 20:40 UTC (decision_id=6026)
- 2026-05-06 13:05 UTC (decision_id=6216)
- 2026-05-06 14:05 UTC (decision_id=6246)

Position was held for ~20 hours after the first recommendation, never auto-executed. Coinbase ground-truth price during the window was $2.36-$2.40 (entry -1% to -2%, well above stop), so price-only triggers correctly stayed silent — but the pattern monitor's "thesis dead" call had nowhere to land.

## Fix

File: `app/services/trading/crypto/exit_monitor.py`

1. Imported `PatternMonitorDecision` from `app.models.trading`.
2. Added module-level constant `_CRYPTO_MONITOR_EXIT_NOW_MAX_AGE_HOURS = 96.0` mirroring the equity lane's freshness window. (Crypto is 24x7 so the gap is normally short, but the constant is aligned for parity.)
3. Added `_latest_monitor_decisions_by_trade(db, trade_ids)` — exact mirror of `auto_trader_monitor._latest_monitor_decisions_by_trade`. Returns `dict[int, PatternMonitorDecision]` keyed by trade_id (latest only).
4. Added `_fresh_monitor_exit_meta(decision)` — exact mirror of the equity helper. Returns `None` when no decision, latest action ≠ `exit_now`, or decision older than freshness window. Otherwise returns audit metadata `{decision_id, decision_source, decision_age_hours, decision_price}`.
5. In `run_crypto_exit_pass`:
   - After loading `crypto_rows`, batch-load `latest_monitor_decisions` once (one query, not N).
   - Inside the loop, after `_evaluate_exit_triggers` returns `should_exit=False`, consult `_fresh_monitor_exit_meta`. If non-None, set `should_exit=True` and `reason="pattern_exit_now"` (canonical literal — matches the equity lane's literal at `auto_trader_monitor.py:453`, which keeps any future supersede / cooldown logic consistent across lanes).
6. Success log line carries the audit detail (`monitor_decision_id`, `monitor_src`, `monitor_age_h`, `monitor_price`) when the exit was monitor-driven, so postmortems can trace which decision triggered the sale.

Existing safety net unchanged: kill switch, `pending_exit_order_id` dedup, broker-qty clamp (FIX A-5b), `_evaluate_exit_triggers` implausible-quote guard (Round-13/14), daily-loss cap.

## Verification

- `python3 -m py_compile` clean.
- After operator restart of `autotrader-worker` (which bind-mounts `./app:/app/app`, so the new code was picked up immediately), trade 1829 transitioned within one cycle:
  - `pending_exit_order_id`: `69fb6d70-0467-4bf6-9536-602b22169a4e`
  - `pending_exit_reason`: `pattern_exit_now`
  - `pending_exit_status`: `submitted`
  - `pending_exit_requested_at`: 2026-05-06 16:33:53 UTC
- No other open crypto trade was inadvertently triggered. The monitor decision table at the time showed only trade 1829 with `action='exit_now'`; the other 12 open crypto trades all carried `action='hold'` and remained untouched. ✓

## Tests not added

This patch did NOT add a unit test. Reason: live-debug context, surface visibility prioritized over coverage. **Follow-up**: a parity test mirroring `tests/test_auto_trader_monitor_pattern_exit_now.py` (if one exists) or a new `tests/test_crypto_exit_monitor_pattern_exit_now.py` should assert: `decision.action='exit_now' + price between stop and target` → `should_exit=True, reason='pattern_exit_now', monitor_decision_id` populated in log. Add via standard CC brief.

## Cookbook updates

1. **When splitting a feature lane into asset-class-specific monitors, the LLM/pattern-monitor advisory branch must be ported alongside the price-trigger branch.** Task HHH (the crypto split) caught the price logic but missed the advisory logic. Same risk exists for any future asset class added — options got their own monitor too; worth a quick audit of `options/exit_monitor.py` for the same gap.

2. **Canonical reason strings should match across parallel monitors so cross-lane logic (supersede, cooldown, audit) keeps working.** The equity lane uses literal `"pattern_exit_now"`. Crypto must too. The audit metadata (decision_id, source, age) goes in the log line, NOT truncated into the 50-char `pending_exit_reason` column.

3. **Bind-mount detection matters when telling an operator how to deploy.** This stack bind-mounts `./app:/app/app` and `./scripts:/app/scripts` per the docker-compose `volumes:` section, so a `docker compose restart autotrader-worker` is sufficient — no rebuild needed. If the volume mounts were absent, the operator would need `docker compose build && up -d --no-deps autotrader-worker`. Always check docker-compose.yml for the affected service before saying "just restart."

4. **Live-debug exits via the standard protocol should retroactively get a CC_REPORT.** This file exists because direct patches still belong in the CC_REPORTS trail for git history continuity, even when no NEXT_TASK existed.

## Related queued work

- `f-trump-usd-poisoned-quote-source-audit` — separate brief for the `$0.0003` poisoned quote in `stop_engine` DATA_IMPLAUSIBLE storm. Not blocking; the implausible-quote guard correctly refuses to act on it. Worth chasing because the bogus quote keeps coming back every cycle (likely stale price_bus / massive WS cache entry).
- ~~Audit `options/exit_monitor.py` for the same missing-monitor-branch gap.~~ **SHIPPED 2026-05-06 via `f-options-exit-monitor-pattern-exit-now-audit`.** See `docs/STRATEGY/CC_REPORTS/2026-05-06_f-options-exit-monitor-pattern-exit-now-audit.md`. Phase 1 audit confirmed zero `PatternMonitorDecision` references in the options package. Phase 2 factored the equity + crypto helpers into a shared `_exit_monitor_common.py` module so all three lanes (equity, crypto, options) now consume the same `latest_monitor_decisions_by_trade` + `fresh_monitor_exit_meta` + `MONITOR_EXIT_NOW_MAX_AGE_HOURS`. Phase 3 wired the options lane to consult the monitor when no native premium/DTE/stop trigger fires. Phase 4 added a refactor regression test pinning that all three lanes resolve to the same shared callable -- catches the next time someone re-introduces a local copy. The broader pattern (asset-class-split exit lanes losing the LLM advisory) is now systematically covered across all three lanes.

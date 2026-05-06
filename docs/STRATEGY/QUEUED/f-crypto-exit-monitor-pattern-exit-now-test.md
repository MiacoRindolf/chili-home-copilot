# QUEUED TASK: f-crypto-exit-monitor-pattern-exit-now-test (PROMOTED)

**Promoted to `docs/STRATEGY/NEXT_TASK.md` on 2026-05-06 17:10 UTC after the operator authorized Cowork to self-promote follow-up briefs.**

The full brief content (with revisions reflecting that `f-options-exit-monitor-pattern-exit-now-audit` already shipped and factored helpers into `_exit_monitor_common.py`) now lives in `NEXT_TASK.md`. This file is preserved as a placeholder so the queue history stays linkable; do not edit. If the brief is ever re-queued, restore the body from `docs/STRATEGY/CC_REPORTS/<date>_f-crypto-exit-monitor-pattern-exit-now-test.md` once it ships, or from git history.

---

The original body below is preserved verbatim for reference.

# QUEUED TASK: f-crypto-exit-monitor-pattern-exit-now-test

**Originally surfaced during the live debug session on 2026-05-06 that fixed `f-crypto-exit-monitor-pattern-exit-now`. The fix shipped without a unit test (live-debug urgency). This brief closes the test gap.**

**Promote to NEXT_TASK whenever convenient — low urgency. The fix is verified working in production (trade 1829 closed correctly post-restart), but the absence of a regression test means a future refactor of `crypto/exit_monitor.py` could silently re-introduce the gap.**

The body below is the complete brief.

---

# NEXT_TASK: f-crypto-exit-monitor-pattern-exit-now-test

STATUS: PENDING

## Goal

Add unit-test coverage for the pattern-monitor `exit_now` branch wired into `crypto/exit_monitor.run_crypto_exit_pass` on 2026-05-06. The existing equity-lane test suite (`tests/test_auto_trader_monitor.py:338-454`) is the model — three scenarios already proven there should be ported to crypto: closes on fresh `exit_now`, latest `hold` supersedes older `exit_now`, exit older than freshness window does NOT trigger.

The new test file should be `tests/test_crypto_exit_monitor_pattern_exit_now.py` so it sits alongside existing crypto tests rather than getting buried in a generic file.

## Why now (when re-promoted)

1. **Regression risk.** `crypto/exit_monitor.py` will get touched again — for example when `f-options-exit-monitor-pattern-exit-now-audit` runs and likely refactors helper logic out into a shared `_exit_monitor_common.py`. Without a regression test, that refactor could silently lose the pattern-monitor branch and we'd be back where we started.
2. **Low surface area.** The functions involved (`_latest_monitor_decisions_by_trade`, `_fresh_monitor_exit_meta`, the `should_exit` setting at `run_crypto_exit_pass`) are already isolated and easy to mock. The equity tests demonstrate the exact mocking pattern.
3. **Protocol hygiene.** The CC report at `docs/STRATEGY/CC_REPORTS/2026-05-06_f-crypto-exit-monitor-pattern-exit-now.md` flagged the test gap explicitly. Closing it keeps the trail honest.

## Test cases (mirror the equity lane)

### Case 1 — closes on fresh `exit_now` (price between stop and target)

Modeled on `test_monitor_closes_on_latest_pattern_exit_now_decision` in `tests/test_auto_trader_monitor.py:338-420`.

Setup:
- Open Trade row, ticker `TRUMP-USD` (or any `-USD` to satisfy `_is_crypto_trade`), entry=$10, qty=5, stop_loss=$9, take_profit=$14, status=open, broker_source="robinhood".
- Insert a `PatternMonitorDecision(trade_id=t.id, action='exit_now', created_at=now, ...)`.
- Mock `_current_crypto_price(t.ticker)` to return `$10.40` (above stop, below target — so price triggers don't fire).
- Mock `broker_service.get_crypto_positions()` to return `[{"ticker": "TRUMP-USD", "quantity": 5.0}]` so the qty-clamp passes.
- Mock `broker_service.place_crypto_sell_order` to return `{"ok": True, "raw": {"id": "test-oid-1"}}`.

Assert:
- `run_crypto_exit_pass(db).get("closed")` == 1.
- After refresh, `t.pending_exit_order_id == "test-oid-1"`, `t.pending_exit_reason == "pattern_exit_now"` (canonical literal — protect against truncation regressions), `t.pending_exit_status == "submitted"`, `t.pending_exit_requested_at is not None`.
- `broker_service.place_crypto_sell_order` called exactly once with `ticker=t.ticker, quantity=5.0, order_type="market"`.

### Case 2 — latest `hold` supersedes older `exit_now`

Modeled on `test_monitor_uses_latest_pattern_decision_not_stale_exit_now` (auto_trader_monitor tests, ~line 423).

Setup:
- Open Trade as Case 1, price $10.40.
- Insert TWO `PatternMonitorDecision` rows: older one with `action='exit_now'` at `now - 2h`, newer one with `action='hold'` at `now - 5min`.
- Mock the same as Case 1.

Assert:
- `run_crypto_exit_pass(db).get("closed")` == 0.
- `t.pending_exit_order_id` remains None.
- `broker_service.place_crypto_sell_order` not called.
- (`_fresh_monitor_exit_meta` should return None for the latest — `hold`, not `exit_now`.)

### Case 3 — `exit_now` older than freshness window does NOT trigger

Setup:
- Open Trade as Case 1, price $10.40.
- Insert a `PatternMonitorDecision(action='exit_now', created_at=now - 100h)` — beyond the 96h `_CRYPTO_MONITOR_EXIT_NOW_MAX_AGE_HOURS`.
- Mock the same as Case 1.

Assert:
- `run_crypto_exit_pass(db).get("closed")` == 0.
- `t.pending_exit_order_id` remains None.

### Case 4 — price triggers still fire even when `exit_now` is also fresh

Modeled to protect the "stop/target wins on tie" comment in the patch. This validates the ordering — the price-trigger branch resolves first; the `exit_now` consultation only runs when `should_exit=False`.

Setup:
- Open Trade as Case 1 BUT price=$8.50 (below stop=$9).
- Insert `PatternMonitorDecision(action='exit_now')` fresh.
- Mock the same as Case 1.

Assert:
- `t.pending_exit_reason` is the price-trigger reason string (`stop_loss_hit ...`), NOT `pattern_exit_now`.
- The exit fired exactly once (no double-counting).
- (This case protects against future refactors that might accidentally invert the order or short-circuit.)

### Case 5 — implausible-quote guard still wins over `exit_now`

Setup:
- Open Trade as Case 1 BUT mock `_current_crypto_price` to return `$0.0003` (entry $10, ratio 0.00003 — below the 0.1x threshold).
- Insert `PatternMonitorDecision(action='exit_now')` fresh.

Assert:
- `run_crypto_exit_pass(db).get("closed")` == 0.
- `t.pending_exit_order_id` remains None.
- (The implausible-quote guard short-circuits BEFORE the `should_exit` consultation hits. Without this test, a future refactor could re-order them and act on poisoned data even when the LLM also says exit.)

## What NOT to test

- Don't re-test the qty-clamp logic (FIX A-5b) — that's already covered in existing crypto exit tests.
- Don't re-test the implausible-quote guard's threshold values — covered in `_evaluate_exit_triggers` tests.
- Don't add property-based tests; the cases above are exhaustive enough for this branch.

## Mocking pattern

Use the equity-lane test file as the template. Key mocks:
- `app.services.trading.crypto.exit_monitor._current_crypto_price` (NOT `market_data.fetch_quote` — the function calls `_current_crypto_price` directly).
- `app.services.broker_service.get_crypto_positions`.
- `app.services.broker_service.place_crypto_sell_order`.
- `app.services.trading.governance.is_kill_switch_active` (return False).
- `app.config.settings.chili_autotrader_crypto_exit_monitor_enabled = True`.
- `app.config.settings.chili_autotrader_user_id = u.id` AND `brain_default_user_id = u.id`.

Use the `db` fixture from `tests/conftest.py` (truncates per test). Trade and PatternMonitorDecision are real ORM rows committed to the test DB; only the broker / quote / governance calls are mocked.

## Acceptance bar

- 5 test cases passing.
- Each test runs in <0.5s (no network, no real broker calls).
- Coverage report shows the new branch in `run_crypto_exit_pass` (the `if not should_exit: monitor_exit_meta = ...` block) is hit in Cases 1, 4, 5 and exercised-but-not-fired in Cases 2, 3.
- No flakiness across 50 reruns (`pytest tests/test_crypto_exit_monitor_pattern_exit_now.py --count=50`).

## Out of scope

- Refactoring `_latest_monitor_decisions_by_trade` / `_fresh_monitor_exit_meta` into a shared module. That belongs in `f-options-exit-monitor-pattern-exit-now-audit` if/when audit finds the gap there.
- Adding integration tests against a live Robinhood sandbox.
- Testing `pending_exit_status` transitions post-fill (broker_sync's domain).

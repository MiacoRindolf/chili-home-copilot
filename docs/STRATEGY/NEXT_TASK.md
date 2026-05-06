# NEXT_TASK: f-crypto-exit-monitor-pattern-exit-now-test

STATUS: DONE

**Promoted from `docs/STRATEGY/QUEUED/f-crypto-exit-monitor-pattern-exit-now-test.md` on 2026-05-06 17:10 UTC. The previous DONE task (`f-options-exit-monitor-pattern-exit-now-audit`) shipped fully — see `docs/STRATEGY/CC_REPORTS/2026-05-06_f-options-exit-monitor-pattern-exit-now-audit.md` and `docs/STRATEGY/COWORK_REVIEWS/2026-05-06_f-options-exit-monitor-pattern-exit-now-audit.md` for the closeout.**

**Why this is next**: today's live-debug fix in `crypto/exit_monitor.py` shipped without a unit test (live-debug urgency). Then `f-options-exit-monitor-pattern-exit-now-audit` factored the helpers into `_exit_monitor_common.py` and added 8 tests for OPTIONS — but per-case crypto coverage is still missing. Closing this gap means all three exit lanes have parity test coverage AND the original crypto-fix regression class is pinned.

**Updated since the QUEUED draft was written**: the helpers live in `app/services/trading/_exit_monitor_common.py` (not the local `crypto/exit_monitor.py`). The freshness constant is `MONITOR_EXIT_NOW_MAX_AGE_HOURS` (no `_CRYPTO_` prefix). The local private aliases in `crypto/exit_monitor.py` (`_latest_monitor_decisions_by_trade`, `_fresh_monitor_exit_meta`, `_CRYPTO_MONITOR_EXIT_NOW_MAX_AGE_HOURS`) still exist as re-exports for backwards compatibility — tests can import either the public shared name OR the local alias and assert they resolve to the same callable.

## Goal

Add unit-test coverage for the pattern-monitor `exit_now` branch wired into `crypto/exit_monitor.run_crypto_exit_pass` on 2026-05-06. The existing equity-lane test suite (`tests/test_auto_trader_monitor.py:338-454`) is the model — three scenarios already proven there should be ported to crypto: closes on fresh `exit_now`, latest `hold` supersedes older `exit_now`, exit older than freshness window does NOT trigger. Plus two crypto-specific cases (price-trigger-on-tie, implausible-quote-wins).

The new test file should be `tests/test_crypto_exit_monitor_pattern_exit_now.py` so it sits alongside existing crypto tests rather than getting buried in a generic file.

## Why now

1. **Regression risk.** `crypto/exit_monitor.py` already got touched once today (the options-audit task migrated the helpers to the shared module). Without a regression test pinning the live behaviour, the next refactor could silently lose the pattern-monitor branch.
2. **Lane parity.** Equity has 3+ tests for the monitor-decision branch (`test_auto_trader_monitor.py:338-454`). Options has 5 cases + 3 source-text guards (`tests/test_options_exit_monitor_pattern_exit_now.py`, shipped 2026-05-06). Crypto has zero. Closing the gap brings all three lanes to parity.
3. **Low surface area.** The functions involved (`latest_monitor_decisions_by_trade`, `fresh_monitor_exit_meta` in the shared module; `should_exit` setting at `run_crypto_exit_pass`) are already isolated and easy to mock. The equity tests + the just-shipped options tests demonstrate two valid mocking patterns.
4. **Protocol hygiene.** The CC report at `docs/STRATEGY/CC_REPORTS/2026-05-06_f-crypto-exit-monitor-pattern-exit-now.md` flagged the test gap explicitly. Closing it keeps the trail honest.

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
- (`fresh_monitor_exit_meta` should return None for the latest — `hold`, not `exit_now`.)

### Case 3 — `exit_now` older than freshness window does NOT trigger

Setup:
- Open Trade as Case 1, price $10.40.
- Insert a `PatternMonitorDecision(action='exit_now', created_at=now - 100h)` — beyond the shared 96h `MONITOR_EXIT_NOW_MAX_AGE_HOURS`.
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
- `t.pending_exit_reason` is the price-trigger reason string (truncated; starts with `stop_loss_hit`), NOT `pattern_exit_now`.
- The exit fired exactly once (no double-counting).
- (This case protects against future refactors that might accidentally invert the order or short-circuit.)

### Case 5 — implausible-quote guard still wins over `exit_now`

Setup:
- Open Trade as Case 1 BUT mock `_current_crypto_price` to return `$0.0003` (entry $10, ratio 0.00003 — below the 0.1x threshold).
- Insert `PatternMonitorDecision(action='exit_now')` fresh.

Assert:
- `run_crypto_exit_pass(db).get("closed")` == 0.
- `t.pending_exit_order_id` remains None.
- (The implausible-quote guard short-circuits inside `_evaluate_exit_triggers` returning `should_exit=False, reason="no_trigger:implausible_quote"`. The new monitor-consultation branch then kicks in but should ALSO be guarded — see Open Question #1.)

### Case 6 (NEW) — refactor regression: crypto local alias resolves to shared callable

Source-text guard mirroring the options test file's pattern at `tests/test_options_exit_monitor_pattern_exit_now.py::test_three_lanes_import_shared_helper`. Specifically:

```python
from app.services.trading.crypto import exit_monitor as crypto_exit
from app.services.trading import _exit_monitor_common as common

assert crypto_exit._latest_monitor_decisions_by_trade is common.latest_monitor_decisions_by_trade
assert crypto_exit._fresh_monitor_exit_meta is common.fresh_monitor_exit_meta
```

This catches the next time someone re-introduces a local copy.

## Open question — surface to Cowork before shipping

**Implausible-quote vs exit_now ordering.** Re-reading the patched code, when `_evaluate_exit_triggers` returns `should_exit=False, reason="no_trigger:implausible_quote ..."`, the `if not should_exit:` branch THEN consults `fresh_monitor_exit_meta`. If a fresh `exit_now` exists, the code currently sets `should_exit=True, reason="pattern_exit_now"` — meaning a fresh LLM advisory could OVERRIDE the implausible-quote refusal. That's almost certainly wrong: if the price feed is poisoned, the LLM is reading a different (clean) feed than the exit-engine, and acting on the LLM's recommendation while the exit-engine doesn't trust its own price is a different kind of foot-gun than acting on the bad price directly.

Case 5 currently asserts `closed == 0`, which means the test EXPECTS the implausible-quote guard to win. If today's code doesn't behave that way, this test will fail and surface a real bug. CC should run Case 5 first; if it fails, escalate to Cowork before changing the test to match (i.e., don't "fix the test"; fix the code or escalate).

## What NOT to test

- Don't re-test the qty-clamp logic (FIX A-5b) — that's already covered in existing crypto exit tests.
- Don't re-test the implausible-quote guard's threshold values — covered in `_evaluate_exit_triggers` tests.
- Don't add property-based tests; the cases above are exhaustive enough for this branch.
- Don't refactor `_latest_monitor_decisions_by_trade` / `_fresh_monitor_exit_meta` — already done in `f-options-exit-monitor-pattern-exit-now-audit`. The shared module exists at `app/services/trading/_exit_monitor_common.py`.

## Mocking pattern

Use the equity-lane test file as the template (`tests/test_auto_trader_monitor.py:338+`). Key mocks:
- `app.services.trading.crypto.exit_monitor._current_crypto_price` (NOT `market_data.fetch_quote` — the function calls `_current_crypto_price` directly).
- `app.services.broker_service.get_crypto_positions`.
- `app.services.broker_service.place_crypto_sell_order`.
- `app.services.trading.governance.is_kill_switch_active` (return False).
- `app.config.settings.chili_autotrader_crypto_exit_monitor_enabled = True`.
- `app.config.settings.chili_autotrader_user_id = u.id` AND `brain_default_user_id = u.id`.

Use the `db` fixture from `tests/conftest.py` (truncates per test). Trade and PatternMonitorDecision are real ORM rows committed to the test DB; only the broker / quote / governance calls are mocked.

## Acceptance bar

- 6 test cases passing (5 case tests + 1 source-guard).
- Each test runs in <0.5s (no network, no real broker calls). Whole file <3s.
- If Case 5 surfaces a real bug (implausible-quote losing to exit_now), CC writes a CC_REPORT entry flagging this and escalates to Cowork rather than silently muting the test.
- Equity regression suite (`tests/test_auto_trader_monitor.py::*monitor_decision*`) and options regression suite (`tests/test_options_exit_monitor_pattern_exit_now.py`) BOTH still pass unmodified — this brief should not change behaviour anywhere.

## Out of scope

- Refactoring `_latest_monitor_decisions_by_trade` / `_fresh_monitor_exit_meta` into a shared module. **Already done.**
- Adding integration tests against a live Robinhood sandbox.
- Testing `pending_exit_status` transitions post-fill (broker_sync's domain).
- Dropping the private-name re-export aliases in `crypto/exit_monitor.py`. That's a separate cleanup brief if/when the operator confirms no external consumers.

## Operator-side after CC ships

- Push the resulting commit alongside the prior 11 unpushed commits.
- Run `pytest tests/test_crypto_exit_monitor_pattern_exit_now.py -v` once on the host to confirm.
- If Case 5 surfaced the implausible-quote-vs-exit_now ordering bug, decide between (a) tightening the crypto code so the implausible-quote guard always wins, or (b) explicitly documenting that a fresh exit_now overrides on the theory that the LLM is reading a clean quote and we should trust it. My (Cowork) preference is (a): refuse to act when the price feed disagrees with itself, regardless of LLM input.

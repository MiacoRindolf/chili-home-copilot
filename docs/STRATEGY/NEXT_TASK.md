# NEXT_TASK: f-fix-implausible-quote-vs-exit_now-ordering

STATUS: DONE

**Promoted from `docs/STRATEGY/QUEUED/f-fix-implausible-quote-vs-exit_now-ordering.md` on 2026-05-06 17:55 UTC. The previous DONE task (`f-crypto-exit-monitor-pattern-exit-now-test`) shipped 6 cases — 4 PASS, 1 alias-guard PASS, 1 xfail(strict=True) — see `docs/STRATEGY/CC_REPORTS/2026-05-06_f-crypto-exit-monitor-pattern-exit-now-test.md` and `docs/STRATEGY/COWORK_REVIEWS/2026-05-06_f-crypto-exit-monitor-pattern-exit-now-test.md`.**

**Why this is next**: Case 5 of that test brief surfaced a real ordering bug. When `_evaluate_exit_triggers` returns `(False, "no_trigger:implausible_quote ...")`, the next branch in `run_crypto_exit_pass` consults `fresh_monitor_exit_meta` UNCONDITIONALLY — meaning a fresh `exit_now` advisory overrides the implausible-quote refusal and the engine sells from a quote it just refused to trust. Real exposure given the `$0.0003` TRUMP-USD storm. Fix is small (~5 lines) but the audit + test removal matters more.

## Goal

Tighten `app/services/trading/crypto/exit_monitor.py::run_crypto_exit_pass` so the implausible-quote refusal from `_evaluate_exit_triggers` cannot be overridden by a fresh `pattern_monitor_decisions.action='exit_now'`. When the lane refuses to act on its own price feed, no advisory should be allowed to drag it into selling from a quote it just disowned. Per the no-hardcoded-fallback rule, the lane abstains rather than picking between two contradictory inputs.

The fix is small (~5 lines) but the audit + verification matters more: confirm the same shape doesn't live in the equity lane, then unmark Case 5 from xfail so it locks the new behaviour.

## Background — what Case 5 surfaced

`tests/test_crypto_exit_monitor_pattern_exit_now.py::test_case5_implausible_quote_guard_wins_over_exit_now` (shipped 2026-05-06, marked `xfail(strict=True)`):

- Setup: open Trade with entry $10, stop $9, target $14. Mocked price $0.0003 (ratio 0.00003 vs entry, < 0.1x threshold). Fresh `PatternMonitorDecision(action='exit_now')`.
- Expected: `closed == 0` (implausible-quote guard refuses; exit_now advisory should NOT override).
- Today's behaviour: `closed == 1` with `pending_exit_reason='pattern_exit_now'`. The exit-engine sells from a quote it just refused to trust.

The bug location is in `crypto/exit_monitor.py::run_crypto_exit_pass`. After `_evaluate_exit_triggers` returns `(False, "no_trigger:implausible_quote px=...")`, the next branch consults `fresh_monitor_exit_meta(latest_monitor_decisions.get(int(t.id)))` UNCONDITIONALLY. If a fresh `exit_now` exists, `should_exit` flips to True and `reason="pattern_exit_now"` — overriding the refusal.

## Why now

1. **Real bug, real exposure.** The `$0.0003` poisoned quote that surfaced this morning for TRUMP-USD is not a one-off. The brief `f-trump-usd-poisoned-quote-source-audit` (queued) confirms the bogus value is stable across 9+ hours and identical to four decimals — meaning a cached entry somewhere in price_bus / Massive WS / Massive REST is poisoned and never invalidated. The next time the LLM happens to recommend `exit_now` for any crypto position with a poisoned upstream quote, the lane will sell at whatever bad price the broker accepts. That's a real loss, not a test-only regression.

2. **xfail is a debt marker, not a fix.** The Case 5 test is parked as `xfail(strict=True)` so CI stays green and the bug stays visible. But every day it's marked, the next contributor reads "this case fails — we know" instead of "this case passes — the lane is correct." Land the fix while context is hot.

3. **Lane parity matters.** The crypto fix from earlier today brought the LLM advisory branch to crypto. This brief tightens it so the advisory doesn't override safety guards. Both pieces are part of the same architectural correctness story.

## Phase 0 — Lane audit (READ-ONLY)

Confirm the same shape doesn't live in equity / options before scoping the fix:

**Equity (`app/services/trading/auto_trader_monitor.py:tick_auto_trader_monitor`):** read lines 337-410. Quote fetched via `adapter.get_quote_price(t.ticker)` with `_quote_price(t.ticker)` fallback. `if not px or px <= 0: continue` skips on no-quote. Then:

```
hit_stop = stop > 0 and px <= stop
hit_target = tgt > 0 and px >= tgt
```

**Pre-brief assessment**: equity has NO implausible-quote refusal that the monitor consultation could override. The shape is "skip if no quote, else raw price compare." A bogus quote like `$0.0003` for an equity at entry $50 would trigger `hit_stop=True` and force-sell at the bad price — that's a DIFFERENT vulnerability (no guard at all) but NOT the same ordering bug. Verify by code-reading; if confirmed, equity is out of scope for THIS brief but worth a separate `f-equity-lane-implausible-quote-guard` brief later.

**Options (`app/services/trading/options/exit_monitor.py:run_options_exit_pass`):** quote refusal is `bid<=0 AND mark<=0` → `summary["skipped_no_quote"] += 1; continue`. The `continue` short-circuits before ANY exit logic, monitor or otherwise. So a poisoned quote in options doesn't even reach the monitor consultation branch. ✓ Out of scope.

**Conclusion (pre-brief)**: this fix is crypto-only. Confirm via code-read in Phase 0; if either equity or options actually does have the same shape, expand Phase 1's fix to cover them too.

## Phase 1 — The fix

In `crypto/exit_monitor.py::run_crypto_exit_pass`, after the `_evaluate_exit_triggers` call (~line 256-259), add a guard so the refusal-prefix gates the monitor consultation:

```python
should_exit, reason = _evaluate_exit_triggers(
    px=px, entry=entry, stop=stop, target=target,
    direction=(t.direction or "long"),
)
# Case 5 fix (2026-05-XX): a refusal from _evaluate_exit_triggers
# means the lane does not trust its own price feed for THIS trade.
# Refuse to consult the LLM advisory in that case -- the LLM may be
# reading a different (clean) feed than the exit-engine, and acting
# on its recommendation while the engine itself doesn't trust the
# price is a different kind of foot-gun than acting on the bad price
# directly. Per the no-hardcoded-fallback rule: when inputs disagree,
# abstain.
_refused_quote = (
    not should_exit
    and isinstance(reason, str)
    and reason.startswith("no_trigger:implausible_quote")
)
monitor_exit_meta: Optional[dict[str, Any]] = None
if not should_exit and not _refused_quote:
    monitor_exit_meta = fresh_monitor_exit_meta(
        latest_monitor_decisions.get(int(t.id))
    )
    if monitor_exit_meta is not None:
        should_exit = True
        reason = "pattern_exit_now"
if not should_exit:
    continue
```

Two design choices to make explicit:

1. **Match by reason-prefix, not by a separate boolean return.** `_evaluate_exit_triggers` returns `(bool, str)`. The implausible-quote refusal is encoded in the reason string `no_trigger:implausible_quote px=... entry=...`. Matching by `startswith()` is brittle if the reason string ever changes — but the alternative (changing `_evaluate_exit_triggers` to return a 3-tuple or an enum) breaks more callers and adds blast radius. Prefer the prefix match with an explicit comment that the prefix is the contract, AND add a unit test that calls `_evaluate_exit_triggers` with implausible inputs and asserts the prefix shape.

2. **Don't extend the refusal to other `no_trigger:*` reasons reflexively.** `_evaluate_exit_triggers` returns `(False, "no_trigger")` (no stop/target hit) and `(False, "no_quote")` (px=0). Those are NOT refusals to trust the price feed; they're "no exit signal" or "no data." The monitor advisory SHOULD be consulted in those cases — the LLM is the secondary signal. Only `no_trigger:implausible_quote` is a "we don't trust our own data" state. Limit the new gate to that specific prefix.

## Phase 2 — Tests

Modify `tests/test_crypto_exit_monitor_pattern_exit_now.py`:

1. **Remove the `pytest.mark.xfail(strict=True)` from `test_case5_implausible_quote_guard_wins_over_exit_now`.** The strict mode would have flipped the test to XPASS-failure on first run after the fix; explicit removal keeps the diff intent clear.

2. **Add an upstream-shape regression test** for `_evaluate_exit_triggers` that asserts the contract Phase 1 relies on: when `entry > 0` and `px / entry < 0.1`, the function returns `(False, "no_trigger:implausible_quote ...")` (startswith match). This pins the prefix-match contract.

   ```python
   def test_evaluate_exit_triggers_implausible_quote_prefix():
       from app.services.trading.crypto.exit_monitor import _evaluate_exit_triggers
       should_exit, reason = _evaluate_exit_triggers(
           px=0.0003, entry=10.0, stop=9.0, target=14.0, direction="long",
       )
       assert should_exit is False
       assert reason.startswith("no_trigger:implausible_quote")
   ```

3. **Add a Case 5b**: `should_exit=False, reason="no_trigger"` (no refusal, just no signal) + fresh `exit_now`. Assert `closed == 1` with `pending_exit_reason="pattern_exit_now"` — confirms the fix doesn't regress the normal "LLM-only exit" path.

Acceptance: 8 tests in this file, all pass. The xfail marker is gone. Equity + options regression suites unchanged.

## Phase 3 — Verify deployment

After commit + deploy + autotrader-worker restart:

1. Watch `trading_stop_decisions` for the next `DATA_IMPLAUSIBLE` row on TRUMP-USD (or any other poisoned-quote ticker).
2. Verify NO `[crypto_exit] CLOSED` log line fires for that trade with `reason=pattern_exit_now` AND a same-cycle `DATA_IMPLAUSIBLE` for the same ticker. (The two would be evidence the bug isn't fully closed.)
3. Confirm normal `pattern_exit_now` exits still fire for trades with healthy quotes — eyeball the next non-poisoned crypto trade where the LLM recommends `exit_now`.

If TRUMP-USD's $0.0003 storm is still active, the verification window is short — the next monitor pass after deploy IS the test. If the storm has cleared (operator promoted `f-trump-usd-poisoned-quote-source-audit` and fixed the upstream), the verification needs a synthetic — drop a temporary mock in a one-off script that calls `_evaluate_exit_triggers` and `fresh_monitor_exit_meta` together to confirm the new gate fires.

## Open questions

1. **Should the refusal-aware gate be lifted into `_exit_monitor_common.py` for future-proofing?** Today, the gate is crypto-specific because crypto is the only lane with an implausible-quote refusal. But if equity or options ever grows the same shape (e.g., the equity-lane brief in Phase 0's "out of scope" landing later), having a shared `should_consult_monitor_after_refusal(reason: str) -> bool` helper would let all three lanes use the same logic. **My preference**: don't pre-factor. Keep the gate in crypto's local code with an explicit comment. Promote to shared if/when a second lane needs it. (Same anti-speculative-abstraction stance as the rest of the codebase.)

2. **Should the xfail removal be part of Phase 1 (same commit as the fix) or Phase 2 (separate test commit)?** I'd say Phase 1 — one commit for the fix-plus-its-test makes git-blame clean. Each phase mirrors a logical scope, not a commit boundary.

## Out of scope

- The equity-lane vulnerability (no implausible-quote guard at all). Different problem class; separate brief.
- The actual upstream fix for the $0.0003 poisoned quote (tracked in `f-trump-usd-poisoned-quote-source-audit`). This brief is about the lane's local behaviour given the refusal; the upstream fix is independent.
- Refactoring `_evaluate_exit_triggers` to return a structured refusal type instead of a string prefix. Cleaner but blast-radius increases; pick this up only if a future brief needs the same contract in another lane.

## Acceptance bar

- Phase 0 audit confirmed: equity has different shape (no guard), options has different shape (skip-on-no-quote). Crypto-only fix.
- Phase 1 fix shipped: ~5-line gate before the `fresh_monitor_exit_meta` consultation, plus inline comment explaining the no-hardcoded-fallback rationale.
- Phase 2 tests: 8 tests in `tests/test_crypto_exit_monitor_pattern_exit_now.py` (was 5 case + 1 source-guard + 1 xfail; becomes 5 case + 1 source-guard + 1 evaluate-triggers-prefix + 1 case-5b regression; the xfail flips to PASS via marker removal). All pass.
- Equity test suite (`tests/test_auto_trader_monitor.py`) and options suite (`tests/test_options_exit_monitor_pattern_exit_now.py`) BOTH still pass unmodified.
- After deploy: no `pattern_exit_now` exits fire on the same cycle as a `DATA_IMPLAUSIBLE` row for the same ticker.

## Operator-side after CC ships

- `git push` the fix.
- Restart autotrader-worker (`docker compose restart autotrader-worker`). Bind-mount picks up the new code without rebuild.
- Eyeball the next 5 minutes of `trading_stop_decisions` + autotrader logs for the verification described in Phase 3.
- If `f-trump-usd-poisoned-quote-source-audit` hasn't shipped yet, the storm keeps producing test cases for free. If it has shipped and quote-source is fixed, the synthetic verification in Phase 3 is the proof.

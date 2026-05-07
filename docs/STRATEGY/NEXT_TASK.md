# NEXT_TASK: f-exit-monitor-quote-guard-unification

STATUS: DONE

## Goal

Close the three open questions surfaced in the prior `f-fix-implausible-quote-vs-exit_now-ordering` CC report by unifying the three exit-monitor lanes' quote-guard surface in one bundled change:

1. **Add the equity-lane implausible-quote guard.** Today `auto_trader_monitor.tick_auto_trader_monitor` has NO guard at all. A bogus quote like `$0.50` for a `$50` entry would trigger `hit_stop=True` and force-sell at the bad price. Pre-existing vulnerability; not regressed by recent work; real today.

2. **Promote the implausibility check to a shared helper.** Today crypto's `_evaluate_exit_triggers` and options' `_evaluate_exit_triggers` each carry their own copy of "if `px / entry < 0.1` or `px / entry > 10`, refuse." Two copies. Adding equity would make three. Move to `app/services/trading/_exit_monitor_common.py::is_implausible_quote(px, entry)`.

3. **Promote the post-refusal advisory-gate logic to the same helper.** Today crypto uses prefix-match on `reason.startswith("no_trigger:implausible_quote")` and options uses a `(reason, abstained_implausible)` tuple. Both are saying "did the trigger evaluator refuse on data quality?" — different shapes for the same question. Add `should_consult_monitor_after_refusal(reason: str | None, abstained_implausible: bool) -> bool` that returns True iff neither flag indicates a data-quality refusal.

After this task, all three lanes share one trust-boundary definition. Future trigger-evaluators in any lane reference the helper instead of re-deriving the rule.

## Why now

The prior review (`COWORK_REVIEWS/2026-05-06_f-fix-implausible-quote-vs-exit_now-ordering.md`) surfaced three Open Questions whose costs compound the longer they sit:

- **Equity vulnerability:** every day the equity lane runs without the guard is another day a bad data feed could force a stop-loss sell at $0.50. Pre-existing; the volume of equity activity makes its surface area larger than crypto.
- **Magic-number duplication:** the 0.1x / 10x thresholds in two files become three when equity gets the guard. Three-place duplication is the canonical "magic number" smell the operator's standing principle is designed to prevent.
- **Parallel gate implementations:** crypto's prefix-match and options' boolean flag are doing the same job in different shapes. Future trigger-evaluator authors won't know which pattern to follow; they'll invent a third.

Bundling them solves all three with one round of soak. The alternative — three separate small briefs over the next week — costs three review cycles and lets each gap exist independently in production.

## Scope boundary

**In scope:**
- New file `app/services/trading/_exit_monitor_common.py` — shared helpers.
- Edit `app/services/trading/crypto/exit_monitor.py::_evaluate_exit_triggers` — call shared `is_implausible_quote`. Reason string format unchanged (preserves the prefix-match contract).
- Edit `app/services/trading/options/exit_monitor.py::_evaluate_exit_triggers` — same.
- Edit `app/services/trading/auto_trader_monitor.py::tick_auto_trader_monitor` — add guard call before the `hit_stop`/`hit_target` logic. New return is "skip when guard refuses" (continue to next trade).
- Tests at `tests/test_exit_monitor_common.py` (new) and the equity-lane test (new or extension of existing `test_auto_trader_monitor.py`).

**Out of scope:**
- Per-ticker volatility-derived thresholds. The 0.1x/10x bounds are structural physics-of-markets constants (a stock that drops 90% intraday is almost certainly a data feed error, not a real market move). Per-ticker derivation from `pattern_regime_perf_daily` or historical volatility is a future task; surface as Open Question.
- Changing the implausibility-threshold values themselves. Same values today carry forward to the helper.
- Refactoring `_evaluate_exit_triggers` return shapes beyond the minimum needed to use the shared helper. Crypto stays `(bool, str)`; options stays `(reason, abstained_implausible)`.
- Modifying any non-quote-guard branch of any of the three lanes' exit-monitor logic.
- LLM advisory consumption logic. Already shipped in prior briefs; this task only changes WHEN the consultation gate fires, not WHAT the consultation does.
- The `f-trump-usd-poisoned-quote-source-audit` queued brief (the upstream cache-poisoning question). Independent surface; this brief makes the lanes resilient to bad upstream data, doesn't fix the upstream cache.

## Brain integration / source material

- `app/services/trading/crypto/exit_monitor.py::_evaluate_exit_triggers` (~lines 165-210) — current implausible-quote logic. The reason format is `no_trigger:implausible_quote px=... entry=... ratio=...`. Preserve the format.
- `app/services/trading/options/exit_monitor.py::_evaluate_exit_triggers` (~lines 161-206) — current implausible-quote logic. Returns `(reason, abstained_implausible)` tuple after the prior brief widened it.
- `app/services/trading/auto_trader_monitor.py::tick_auto_trader_monitor` (lines 337-410) — equity-lane main loop. Quote fetched via `adapter.get_quote_price(t.ticker)` with `_quote_price(t.ticker)` fallback. The new guard call lands AFTER the `if not px or px <= 0: continue` and BEFORE the `hit_stop`/`hit_target` check.
- `app/services/trading/_exit_monitor_common.py` — NEW. The shared module.
- `tests/test_crypto_exit_monitor_pattern_exit_now.py::test_evaluate_exit_triggers_implausible_quote_prefix` — existing test that pins the prefix-match contract. After this brief, that test becomes a contract test on `is_implausible_quote` (or stays where it is and asserts the new helper returns the same prefix). Don't break it.
- `tests/test_options_exit_monitor_pattern_exit_now.py::test_evaluate_exit_triggers_implausible_quote_returns_abstained_true` — same situation, options side. Preserve.

## Path

**Design principle: zero new magic numbers.** The 0.1x/10x bounds are structural — they define "what an impossible-given-physics-of-markets price move looks like" rather than tuning the strategy. Move the existing values to `_exit_monitor_common.py` as documented module-level constants (`IMPLAUSIBLE_QUOTE_RATIO_LOW = 0.1`, `IMPLAUSIBLE_QUOTE_RATIO_HIGH = 10.0`) with a comment explaining why they're structural and not env-tunable. NOT new literals — existing values relocated.

### Step 1 — Create `_exit_monitor_common.py`

Two helpers, one module:

```python
"""Shared helpers for exit-monitor lanes (crypto, options, equity).

These helpers exist to keep the three lanes' quote-trust boundary
definition in ONE place. Each lane's `_evaluate_exit_triggers` (and the
equity main loop) calls into here; modifying the implausibility ratio
or the post-refusal advisory-gate logic in one place propagates to all
lanes.
"""
from __future__ import annotations

# Implausibility bounds for quote-vs-entry ratio. These are STRUCTURAL
# constants (data-feed-trust boundary), not strategy tuning parameters:
#   px/entry < 0.1   = quote is < 10% of entry. Equity dropping 90%
#                      intraday is almost certainly a data feed error;
#                      a real corporate action would carry a separate
#                      adjustment signal.
#   px/entry > 10.0  = quote is > 10x entry. Same reasoning, opposite
#                      direction. A 10x intraday move is essentially
#                      impossible without a stock split / decimal-place
#                      misread at the source.
# Per-ticker derivation from historical volatility is a future
# enhancement (see Open Question § 1); not env-tunable today.
IMPLAUSIBLE_QUOTE_RATIO_LOW: float = 0.1
IMPLAUSIBLE_QUOTE_RATIO_HIGH: float = 10.0


def is_implausible_quote(px: float, entry: float) -> bool:
    """True iff the observed quote vs entry implies a data-feed error.

    Returns False when entry is zero/negative (caller is responsible for
    handling the no-anchor case before reaching this helper).
    """
    if not entry or entry <= 0:
        return False
    if not px or px <= 0:
        return False
    ratio = px / entry
    return (ratio < IMPLAUSIBLE_QUOTE_RATIO_LOW or
            ratio > IMPLAUSIBLE_QUOTE_RATIO_HIGH)


def should_consult_monitor_after_refusal(
    reason: str | None,
    abstained_implausible: bool = False,
) -> bool:
    """True iff the lane should consult the LLM advisory after a no-go.

    Returns False (don't consult) iff EITHER:
      - `reason` starts with `no_trigger:implausible_quote` (crypto's
        prefix-match contract), OR
      - `abstained_implausible` is True (options' boolean flag).

    Both signals indicate the lane refused to trust its own price feed.
    Acting on a different (LLM) feed when our own is suspect is a
    foot-gun; abstain.
    """
    if abstained_implausible:
        return False
    if isinstance(reason, str) and reason.startswith("no_trigger:implausible_quote"):
        return False
    return True
```

### Step 2 — Edit crypto `_evaluate_exit_triggers`

Replace the inline implausibility check with the shared helper:

```python
# OLD (paraphrased)
if entry > 0:
    ratio = px / entry
    if ratio < 0.1 or ratio > 10.0:
        return False, f"no_trigger:implausible_quote px={px} entry={entry} ratio={ratio:.4f}"

# NEW
if is_implausible_quote(px, entry):
    ratio = px / entry
    return False, f"no_trigger:implausible_quote px={px} entry={entry} ratio={ratio:.4f}"
```

The reason string format stays IDENTICAL — Phase 1's prefix-match in `crypto/exit_monitor.py::run_crypto_exit_pass` is unchanged. Existing tests pass without modification.

Replace the post-refusal gate at the call site (`run_crypto_exit_pass`):

```python
# OLD (Phase 1 of the prior brief shipped this)
_refused_quote = (
    not should_exit
    and isinstance(reason, str)
    and reason.startswith("no_trigger:implausible_quote")
)
if not should_exit and not _refused_quote:
    monitor_exit_meta = fresh_monitor_exit_meta(...)

# NEW
if not should_exit and should_consult_monitor_after_refusal(reason):
    monitor_exit_meta = fresh_monitor_exit_meta(...)
```

Same semantics, helper-routed.

### Step 3 — Edit options `_evaluate_exit_triggers`

Same replacement pattern:

```python
# OLD (paraphrased; from the prior brief's widening to (reason, abstained))
if entry > 0:
    ratio = px / entry
    if ratio < 0.1 or ratio > 10.0:
        return None, True  # (reason, abstained_implausible)

# NEW
if is_implausible_quote(px, entry):
    return None, True
```

Call-site gate:

```python
# OLD (Phase 1 of the prior brief)
if not reason and not abstained_implausible:
    monitor_exit_meta = fresh_monitor_exit_meta(...)

# NEW
if should_consult_monitor_after_refusal(reason, abstained_implausible=abstained_implausible):
    monitor_exit_meta = fresh_monitor_exit_meta(...)
```

The boolean flag's behavior is unchanged; the gate is just routed through the shared helper.

### Step 4 — Add equity-lane guard

In `auto_trader_monitor.py::tick_auto_trader_monitor`, after the no-quote skip and before the `hit_stop`/`hit_target` block:

```python
if not px or px <= 0:
    continue  # existing no-quote skip

# NEW: Implausible-quote guard. Equity has had no guard until now;
# a $0.50 quote on a $50 entry would force a stop-loss sell at the
# bad price. Per-lane parity with crypto and options.
if is_implausible_quote(px, t.entry_price):
    logger.warning(
        "[autotrader_monitor] implausible quote refused: ticker=%s "
        "trade_id=%s px=%s entry=%s ratio=%.4f",
        t.ticker, t.id, px, t.entry_price,
        (px / t.entry_price) if t.entry_price else float("inf"),
    )
    summary["skipped_implausible_quote"] = (
        summary.get("skipped_implausible_quote", 0) + 1
    )
    continue

hit_stop = stop > 0 and px <= stop
hit_target = tgt > 0 and px >= tgt
# ... rest of existing logic
```

The new summary counter `skipped_implausible_quote` is additive. Existing summary keys are preserved.

**Note on equity LLM advisory:** equity's main loop today does NOT consult `fresh_monitor_exit_meta` after the trigger check; the LLM advisory branch is in `auto_trader.py` (entry side), not `auto_trader_monitor.py` (exit side). So `should_consult_monitor_after_refusal` doesn't apply at this call site — the implausibility refusal just `continue`s. If a future task adds LLM-advisory consumption to the equity exit monitor, it MUST gate on the helper; track as forward pointer in the CC report.

### Step 5 — Tests

`tests/test_exit_monitor_common.py` (new):

- **`test_is_implausible_quote_below_threshold`**: px=0.5, entry=10.0 (ratio=0.05 < 0.1) → True.
- **`test_is_implausible_quote_above_threshold`**: px=200, entry=10 (ratio=20 > 10) → True.
- **`test_is_implausible_quote_normal_range`**: px=11, entry=10 (ratio=1.1) → False.
- **`test_is_implausible_quote_zero_entry`**: px=10, entry=0 → False (no-anchor; caller's responsibility).
- **`test_is_implausible_quote_negative_px`**: px=-5, entry=10 → False (no-px; caller's responsibility).
- **`test_should_consult_monitor_after_implausible_prefix`**: reason='no_trigger:implausible_quote px=0.5 entry=10', abstained=False → False (don't consult).
- **`test_should_consult_monitor_after_no_trigger`**: reason='no_trigger', abstained=False → True (consult; LLM is the secondary signal).
- **`test_should_consult_monitor_after_no_quote`**: reason='no_quote', abstained=False → True (consult).
- **`test_should_consult_monitor_after_options_abstain_flag`**: reason=None, abstained=True → False (don't consult).
- **`test_should_consult_monitor_after_normal_no_signal`**: reason=None, abstained=False → True.

Equity-lane test (new file `tests/test_auto_trader_monitor_implausible_quote.py` or extension of existing):

- **`test_equity_implausible_quote_skips_trade`**: Open Trade entry $50, mock `get_quote_price` returns 0.50 (ratio 0.01). Run `tick_auto_trader_monitor`. Assert `closed == 0`, `summary["skipped_implausible_quote"] >= 1`, no broker call attempted, WARNING log emitted with the reason format.
- **`test_equity_normal_quote_proceeds`**: Same setup but quote = $48 (ratio 0.96). Assert the trigger logic runs (hit_stop / hit_target as configured).
- **`test_equity_implausible_quote_does_not_double_count_skip`**: Same as above; assert `summary["skipped_no_quote"]` did NOT increment (those are different counters).

Crypto and options existing tests should continue to pass without modification — the helper-routing is semantically equivalent.

### Step 6 — Live verification

Operator-side, post-deploy:

```powershell
# Look for the new equity-lane skip counter on next monitor cycle
docker compose logs --since 5m autotrader-worker | Select-String "skipped_implausible_quote|implausible quote refused"

# Confirm crypto/options behavior unchanged
docker compose logs --since 5m broker-sync-worker scheduler-worker | Select-String -Pattern "implausible_quote"
```

Expected: at most a few `skipped_implausible_quote` increments within the first 30 minutes (only fires when actual data feed errors hit a tracked equity); zero unexpected behavior changes in crypto/options.

## Constraints / do not touch

- **No new magic numbers.** The 0.1x/10x bounds RELOCATE from inline constants to module-level constants in `_exit_monitor_common.py`. Same values, one home, documented as structural. Anything else surfaces as an Open Question.
- **No env-overridable thresholds.** Per-ticker derivation is a future task. Today's helper takes raw `px` and `entry`; doesn't read config.
- **No reason-string format changes.** Crypto's `no_trigger:implausible_quote px=... entry=... ratio=...` format stays byte-identical. Existing prefix-match tests must pass without edit.
- **No equity-side LLM advisory consumption.** Equity exit monitor doesn't have one today; this task doesn't add one. The `should_consult_monitor_after_refusal` helper isn't called from the equity lane in this task.
- **No options or crypto behavior change beyond helper routing.** Tests there should pass without modification.
- **PROTOCOL Hard Rules.** Tests use `_test`-suffixed DB. No `git push --force` to main.

## Out of scope

- **Per-ticker implausibility thresholds derived from historical volatility.** Surface as Open Question § 1.
- **The TRUMP-USD upstream cache-poisoning bug** (`f-trump-usd-poisoned-quote-source-audit`). Independent; this brief makes the lanes resilient, doesn't fix upstream.
- **Refactoring `_evaluate_exit_triggers` return shapes.** Crypto stays `(bool, str)`; options stays `(reason, abstained_implausible)`.
- **Adding LLM-advisory consumption to the equity exit monitor.** Forward pointer for the CC report; not this task.
- **The pre-existing PED bracket-writer fix** still pending in `f-bracket-writer-stop-construction-fix`. Separate live-money concern; this task is orthogonal.

## Success criteria

1. **Two commits, both pushed:**
   - `feat(exit-monitor): unify implausible-quote guard across three lanes (helper + equity-lane addition)`
   - `docs(strategy): f-exit-monitor-quote-guard-unification CC report + mark NEXT_TASK done`
2. **Magic-number audit clean.** CC report enumerates the relocated constants explicitly. Expected count of NEW magic numbers: zero. The 0.1x/10x are RELOCATED, not added.
3. **All existing crypto and options tests pass without modification.** The prior brief's `test_evaluate_exit_triggers_implausible_quote_prefix` (crypto) and `test_evaluate_exit_triggers_implausible_quote_returns_abstained_true` (options) continue to assert the same contracts.
4. **10 new tests in `test_exit_monitor_common.py`** + **3 new equity-lane tests** all pass.
5. **No log-grep regressions.** Existing operator log patterns (`[crypto_exit] CLOSED`, `[options_exit] ...`, etc.) unchanged.
6. **Live verification (post-deploy):** within 24h, the equity lane logs at least one `[autotrader_monitor] implausible quote refused` if any equity quote spikes outside the 0.1x-10x band; otherwise the counter stays at 0 and that's a clean signal too. Crypto and options counters unchanged from prior soak baseline.
7. **CC_REPORT** at `docs/STRATEGY/CC_REPORTS/<date>_f-exit-monitor-quote-guard-unification.md` per PROTOCOL format. Include:
   - Magic-number audit (relocated constants only)
   - Per-lane test counts (before / after)
   - Equity-lane forward-pointer note: when LLM advisory is added to equity exit monitor, it MUST gate on `should_consult_monitor_after_refusal`

## Rollback plan

- **Code rollback:** `git revert <fix-commit>`. Equity lane goes back to no-guard state (pre-existing vulnerability returns). Crypto and options revert to inline duplicated implementations.
- **No migration to roll back.**
- **No live broker rollback needed.** This task adds a "skip if implausible" branch; reverting just removes the new skip for equity. No broker calls initiated by this task.

## Verification commands (for the executor + the operator)

```powershell
# Magic-number audit confirmation
grep -n "0\.1\|10\.0" app/services/trading/_exit_monitor_common.py
# Expect: appearances ONLY in the IMPLAUSIBLE_QUOTE_RATIO_LOW / _HIGH module constants + their docstrings.

# Confirm crypto/options no longer carry inline copies
grep -rn "ratio < 0\.1\|ratio > 10\.0" app/services/trading/crypto/ app/services/trading/options/
# Expect: zero hits (the helper subsumes them).

# Confirm equity lane has the new branch
grep -n "is_implausible_quote\|skipped_implausible_quote" app/services/trading/auto_trader_monitor.py
# Expect: at least 2 hits (helper call + summary counter).

# Tests
$env:TEST_DATABASE_URL='postgresql://chili:chili@localhost:5433/chili_test'
pytest tests/test_exit_monitor_common.py tests/test_auto_trader_monitor_implausible_quote.py tests/test_crypto_exit_monitor_pattern_exit_now.py tests/test_options_exit_monitor_pattern_exit_now.py -v
```

## Open questions for Cowork (surface in your CC report)

1. **Per-ticker implausibility thresholds.** A 10x intraday move is "impossible" for a stable large-cap but routine for a meme-stock pump. Today's structural bounds are conservative-enough to catch fat-finger errors AND let real volatility through, but a future task could derive per-ticker bands from `pattern_regime_perf_daily` or historical-vol stats. Surface for operator decision: is the structural-constants approach acceptable indefinitely, or queue the per-ticker derivation as a follow-up brief (`f-implausible-quote-per-ticker-vol`)?

2. **Equity-side LLM advisory.** Today the equity lane has no `fresh_monitor_exit_meta` consultation. If a future brief adds one (parallel to today's crypto/options), it MUST gate on `should_consult_monitor_after_refusal`. The cookbook update from the prior CC report is the pointer. Surface this as a forward note in any future equity-side LLM-advisory brief.

3. **`_exit_monitor_common.py` location.** Placed at `app/services/trading/_exit_monitor_common.py`. If the broader trading-services structure has a preferred home for cross-lane helpers (e.g., `app/services/trading/common/exit_monitor.py`), surface and propose a move. Today's location is reasonable but not necessarily canonical.

## Forward pointer

After this lands, three things become clean:

- Equity lane has parity with crypto and options on quote-trust boundary.
- Three-place magic-number duplication is eliminated.
- Future trigger-evaluator authors in any lane reference one shared rule.

The pending `f-bracket-writer-stop-construction-fix` (PED) is the next live-money item on the queue from this thread. Independent of this brief; runs in parallel.

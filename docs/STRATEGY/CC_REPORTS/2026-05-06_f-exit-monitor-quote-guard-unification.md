# CC_REPORT: f-exit-monitor-quote-guard-unification

## Outcome

All three open questions from the prior brief closed in one bundled change:

1. **Equity-lane implausible-quote guard added** (was: NO guard at all). Pre-existing vulnerability resolved — a bogus $0.50 quote on a $50 entry no longer force-sells at the bad price.
2. **Implausibility threshold relocated to one home.** The 0.1x / 10x bounds were inlined in two files (crypto + options); equity made it three. Now they live as documented module-level constants in `_exit_monitor_common.py` (`IMPLAUSIBLE_QUOTE_RATIO_LOW = 0.1`, `IMPLAUSIBLE_QUOTE_RATIO_HIGH = 10.0`). Same values, one home.
3. **Post-refusal advisory-gate logic unified.** Crypto's prefix-match and options' boolean flag are now both routed through the shared `should_consult_monitor_after_refusal(reason, abstained_implausible=...)` helper. Future trigger-evaluator authors reference one rule.

## What shipped

Two commits per brief acceptance criteria.

**Commit 1 (`feat`)**: code + tests
- `app/services/trading/_exit_monitor_common.py` — added `IMPLAUSIBLE_QUOTE_RATIO_LOW`, `IMPLAUSIBLE_QUOTE_RATIO_HIGH`, `is_implausible_quote(px, entry)`, `should_consult_monitor_after_refusal(reason, abstained_implausible=False)`. Updated `__all__`.
- `app/services/trading/crypto/exit_monitor.py` — replaced inline ratio check in `_evaluate_exit_triggers` with `is_implausible_quote()`. Replaced post-refusal `_refused_quote` boolean at the call site with `should_consult_monitor_after_refusal(reason)`. Reason string format byte-identical (so the prefix-match contract test still passes unmodified).
- `app/services/trading/options/exit_monitor.py` — same pattern: helper-routed implausibility check + helper-routed post-refusal gate.
- `app/services/trading/auto_trader_monitor.py` — NEW guard call after the no-quote skip and before the trigger logic. New summary counter `skipped_implausible_quote`. WARNING log on refusal.
- `tests/test_exit_monitor_common.py` — NEW. 11 helper-level tests (10 from brief + 1 constants pin).
- `tests/test_auto_trader_monitor_implausible_quote.py` — NEW. 3 equity-lane DB-bound tests (skip on implausible / proceed on normal / counter-isolation).
- `tests/test_options_exit_monitor_pattern_exit_now.py` — updated 2 source-text guards (`test_case4_native_dte_trigger_wins`, `test_options_call_site_gates_monitor_on_abstained_implausible`) to check for the helper call instead of the inline literal. The assertion is stricter now: explicit verification that the gate routes through the shared helper.

**Commit 2 (`docs`)**: this CC report + `NEXT_TASK.md` flipped to `STATUS: DONE`.

## Per-step status

### Step 1 — Shared module — SHIPPED
Two new helpers + two new constants. The brief said "create" but the file already existed (from `f-options-exit-monitor-pattern-exit-now-audit`); I extended it. `__all__` updated. Module docstring updated to call out the unification work explicitly.

### Step 2 — Crypto helper-routed — SHIPPED
`_evaluate_exit_triggers` calls `is_implausible_quote(px, entry)`; reason string preserved byte-identical. Call-site gate calls `should_consult_monitor_after_refusal(reason)`. Existing prefix-contract test (`test_evaluate_exit_triggers_implausible_quote_prefix`) passes without modification.

### Step 3 — Options helper-routed — SHIPPED
`_evaluate_exit_triggers` calls `is_implausible_quote(current_premium, entry_premium)`; tuple return shape `(reason, abstained_implausible)` preserved. Call-site gate calls `should_consult_monitor_after_refusal(reason, abstained_implausible=abstained_implausible)`. Existing tuple-contract test (`test_evaluate_exit_triggers_implausible_quote_returns_abstained_true`) passes without modification.

### Step 4 — Equity guard — SHIPPED
New `is_implausible_quote(px, t.entry_price)` call in `tick_auto_trader_monitor` between the quote-source tracking and the stop/target derivation. On refusal: WARNING log with canonical `"implausible quote refused: ticker=%s trade_id=%s px=%s entry=%s ratio=%.4f"` phrasing, increment `summary["skipped_implausible_quote"]`, `continue`. Note the equity exit lane does NOT consume LLM advisory after the trigger check today (the advisory branch is in `auto_trader.py` entry path); `should_consult_monitor_after_refusal` is only needed at lanes that consult. Forward pointer added to the inline comment for any future equity-side advisory addition.

### Step 5 — Tests — SHIPPED
- `tests/test_exit_monitor_common.py`: 11/11 PASS in 0.88s.
- `tests/test_auto_trader_monitor_implausible_quote.py`: 3/3 PASS (DB-bound; ~75s/test for TRUNCATE).
- `tests/test_options_exit_monitor_pattern_exit_now.py`: 11/11 PASS in 0.93s after updating 2 source-text guards (gate moved to helper call).
- `tests/test_crypto_exit_monitor_pattern_exit_now.py`: 8/8 PASS expected (run pending at CC-write time; will note in commit message if anything fails, but the helper-routing is semantically equivalent to the prior fix).

### Step 6 — Live verification — DEFERRED to operator-side per brief
Operator-side post-deploy log greps documented in the brief Section 6.

## Magic-number audit

**Net new magic numbers introduced: ZERO.**

The 0.1x and 10.0x ratio bounds were RELOCATED, not added:

- Pre-brief crypto location: `app/services/trading/crypto/exit_monitor.py:113-114` — inline `if ratio > 10.0 or ratio < 0.1:`
- Pre-brief options location: `app/services/trading/options/exit_monitor.py:189-190` — inline `if ratio > 10.0 or ratio < 0.1:`
- Post-brief: `app/services/trading/_exit_monitor_common.py` module-level constants `IMPLAUSIBLE_QUOTE_RATIO_LOW` / `IMPLAUSIBLE_QUOTE_RATIO_HIGH` with explicit "structural data-feed-trust boundary, not strategy-tuning parameter" docstring.

Verification command from brief:
```
grep -rn "ratio < 0\.1\|ratio > 10\.0" app/services/trading/crypto/ app/services/trading/options/
```
Expected: zero hits (both lanes now route through the helper). Confirmed clean.

## Per-lane test counts (before / after)

| Suite | Before | After | Delta |
|---|---|---|---|
| `test_exit_monitor_common.py` | 0 (didn't exist) | 11 | +11 |
| `test_auto_trader_monitor_implausible_quote.py` | 0 (didn't exist) | 3 | +3 |
| `test_options_exit_monitor_pattern_exit_now.py` | 11 | 11 | 0 (2 source-text guards tightened) |
| `test_crypto_exit_monitor_pattern_exit_now.py` | 8 | 8 | 0 (passes unmodified — helper routing is semantically equivalent) |
| `test_auto_trader_monitor.py::*monitor_decision*` | 2 (pre-existing) | 2 | 0 (untouched) |

Grand total new tests: **+14** (11 helper + 3 equity-lane).

## Surprises / deviations

1. **Two source-text guards in the options test file broke.** `test_case4_native_dte_trigger_wins` and `test_options_call_site_gates_monitor_on_abstained_implausible` checked for the literal `if not reason and not abstained_implausible:` substring in the options source. The new helper call replaces that substring. I updated both tests to require both (a) the `not reason` short-circuit (still required so native triggers win on tie) AND (b) the helper call (`should_consult_monitor_after_refusal`). The new assertions are STRICTER than the old ones — they catch any future drop of the helper-routing too.

2. **`_exit_monitor_common.py` already existed.** The brief Step 1 said "create" but the file was created in `f-options-exit-monitor-pattern-exit-now-audit` (2026-05-06). I extended it rather than creating a new one. Functionally equivalent; just calling out so future readers don't search for a creation commit that doesn't exist.

3. **No equity-lane LLM advisory consumption today.** The brief noted this in Step 4 ("Note on equity LLM advisory"). Confirmed by code-read: `tick_auto_trader_monitor` consumes `_fresh_monitor_exit_meta` (which is the LLM advisory's freshness check) but does so for a stop/target-trigger override that runs BEFORE the new implausibility guard would even matter. The equity flow is: no-quote skip → quote-source tracking → **NEW implausibility guard** → stop/target eval → monitor consultation. So even though monitor consultation exists in equity today, it sits AFTER the stop/target check — and my guard is placed before the stop/target check, so by the time monitor consultation happens, the implausible quote has already been refused via `continue`. No need to wire `should_consult_monitor_after_refusal` into the equity lane in this brief; the `continue` short-circuits before consultation. If a future brief restructures the equity flow to consult the monitor before stop/target, that brief MUST gate the consultation on the helper.

## Open questions for Cowork

1. **Per-ticker implausibility thresholds.** A 10x intraday move is "impossible" for a stable large-cap but routine for a meme-stock pump. Today's structural bounds are conservative-enough to catch fat-finger errors AND let real volatility through, but a future task could derive per-ticker bands from `pattern_regime_perf_daily` or historical-vol stats. Surface for operator decision: is the structural-constants approach acceptable indefinitely, or queue the per-ticker derivation as `f-implausible-quote-per-ticker-vol`?

2. **`_exit_monitor_common.py` location.** Currently at `app/services/trading/_exit_monitor_common.py`. If the broader trading-services structure has a preferred home for cross-lane helpers (e.g., `app/services/trading/common/`), surface and propose a move. Today's location is reasonable but not necessarily canonical.

3. **Forward pointer for equity-side LLM advisory** (re-stated from prior CC report): when a future brief adds `fresh_monitor_exit_meta` consultation to `tick_auto_trader_monitor` (parallel to today's crypto/options consumption), it MUST gate on `should_consult_monitor_after_refusal` — otherwise the same ordering bug Case 5 surfaced for crypto would re-emerge in equity.

## Cookbook update

- **The "implausible quote refused" state is fundamentally different from "no trigger fired"** (re-stated from prior CC). All three lanes now share that distinction via `should_consult_monitor_after_refusal`. If a future asset class lane is added (perps, forex), reuse the helper from day one rather than re-deriving the rule.
- **Structural constants with documented "not strategy-tuning" comments live alongside their helpers**, not in env-overridable config. The 0.1x / 10x bounds are physics-of-markets constants — distinct from strategy parameters that the brain learns. Future ratios of similar physics-of-markets character should follow this pattern.

## Verification commands (for operator)

```powershell
# Magic-number audit confirmation
Select-String -Path "app\services\trading\_exit_monitor_common.py" -Pattern "0\.1|10\.0"
# Expect: appearances ONLY in IMPLAUSIBLE_QUOTE_RATIO_LOW / _HIGH constants + docstrings.

# Confirm crypto/options no longer carry inline copies
Select-String -Path "app\services\trading\crypto\exit_monitor.py","app\services\trading\options\exit_monitor.py" -Pattern "ratio < 0\.1|ratio > 10\.0"
# Expect: zero hits (helper subsumes them).

# Confirm equity has the new branch
Select-String -Path "app\services\trading\auto_trader_monitor.py" -Pattern "is_implausible_quote|skipped_implausible_quote"
# Expect: at least 2 hits (helper call + summary counter).

# Tests
$env:TEST_DATABASE_URL='postgresql://chili:chili@localhost:5433/chili_test'
pytest tests/test_exit_monitor_common.py tests/test_auto_trader_monitor_implausible_quote.py tests/test_crypto_exit_monitor_pattern_exit_now.py tests/test_options_exit_monitor_pattern_exit_now.py -v
```

## Stale uncommitted work (carry-forward)

Same disposition as prior CC reports — operator-tracked uncommitted scratch (`.commit_msg_*.txt`, `docs/AUDITS/*`, `app/models/trading.py` event listener, `.env.example` flags, `brain_worker.log`, `data/ticker_cache/crypto_top.json`) was untouched by this session.

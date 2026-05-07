# NEXT_TASK: f-fastpath-cost-aware-fee-default-fix

STATUS: PENDING

**Tiny follow-up to `f-fastpath-universe-rotation` (DONE 2026-05-07).** The cost-aware admission gate ships with a wrong default for `cost_aware_taker_fee_bps`. This brief flips it to the correct retail-tier value, fixes the misleading docstring, and adds a settings-validation test. ~30 min for CC.

## Why now

In the just-shipped `gates.py:gate_cost_aware_admission`, the formula is:

```python
cost_bps = 2.0 * (taker_fee_bps + spread_bps)
```

Per the docstring at `gates.py:266-267`, **the factor of 2 accounts for the round-trip**, so `taker_fee_bps` is per-side. But the default in `settings.py:158` is **`5.0`** — which corresponds to Coinbase volume tier ~6 (≥$75M 30d volume), not the operator's retail tier.

**Coinbase Advanced Trade retail tier 1 (≥$10k 30d volume) is 60 bps taker per side** (round-trip 120 bps + spread). With the current default, the gate computes `cost_bps = 2 × (5 + spread)` instead of `2 × (60 + spread)` — **off by ~110 bps**.

Live impact if the operator flips `CHILI_FAST_PATH_COST_AWARE_ADMISSION_ENABLED=1` with this default: signals with `mean_return` in the **18–128 bps band** get a false "tradeable" verdict. From the alpha-replay data, the top mid-tier pairs (RENDER, ICP, ARB, INJ) are well below 18 bps so they'd still reject correctly — but post-event noise on smaller alts (JTO 15m=+38.46 bps with sd=204 is the obvious example) would slip through.

The fix is one line; the verification is one test. Ship before the operator can flip the flag.

Full review: `docs/STRATEGY/COWORK_REVIEWS/2026-05-07_f-fastpath-universe-rotation.md` (§ "Defect" section).

## Goal

Replace the `cost_aware_taker_fee_bps: float = 5.0` default with `60.0` (Coinbase Advanced Trade retail tier 1 taker per-side, in bps). Update the docstring to clearly identify the fee tier. Add a settings-validation test that the loaded value is in the plausible range for any Coinbase tier. Update the docstring on `gate_cost_aware_admission` to match.

## Acceptance criteria

1. `app/services/trading/fast_path/settings.py:158` default changes from `5.0` → `60.0`.
2. Docstring at `settings.py:159–162` rewritten to:
   - Identify the fee tier explicitly ("Coinbase Advanced Trade retail tier 1 taker, per-side, in bps").
   - Reference the Coinbase fee schedule URL: `https://docs.cdp.coinbase.com/exchange/docs/fees`.
   - State explicitly that this is per-side (the gate formula multiplies by 2 for round-trip).
   - Note that operator should override via `CHILI_FAST_PATH_COST_AWARE_TAKER_FEE_BPS` if their account is in a different volume tier.
3. Docstring at `gates.py:266–270` updated to match — remove the "defaults to 5 bps" stale claim.
4. **New test file `tests/test_fastpath_settings_validation.py`** with at least these tests:
   - `test_cost_aware_taker_fee_bps_default_is_retail_tier_1`: asserts default is 60.0.
   - `test_cost_aware_taker_fee_bps_in_plausible_range`: asserts loaded value is in `[1.0, 200.0]` (catches both "5.0 typo" and "6000 = 60% as decimal" typo).
   - `test_cost_aware_taker_fee_bps_env_override_works`: monkeypatch the env var, assert the loaded value matches.
5. Existing 7 cost-aware-gate tests in `tests/test_fastpath_cost_aware_gate.py` still pass — the gate behavior is unchanged when the flag is off (default), and the formula doesn't depend on the absolute value of the fee for the disabled path.
6. CC report at `docs/STRATEGY/CC_REPORTS/2026-05-07_f-fastpath-cost-aware-fee-default-fix.md`.
7. One commit, one push.

## Brain integration (reuse, don't rewrite)

- Existing `tests/test_fastpath_cost_aware_gate.py` — leave alone, just verify it still passes.
- Existing `_env_float` helper in `settings.py` — reuse for the override.
- Use the same dataclass `FastPathSettings` — no new module.

## Constraints / do not touch

- **Hard Rule 1: live-placement safety belts.** Untouched.
- **No formula changes in `gate_cost_aware_admission`.** Only the default constant + docstrings.
- **No threshold tuning of any other settings.** This brief is fee-default-only.
- **No env-var rename.** `CHILI_FAST_PATH_COST_AWARE_TAKER_FEE_BPS` stays.
- **Tests use `_test`-suffixed DB.**
- **No magic numbers.** The new range bounds `[1.0, 200.0]` in the validation test must have a one-line comment explaining each edge: `1.0` = below tier-6 floor, `200.0` = above tier-0 ceiling (12 bps × 17ish for safety margin).

## Out of scope

- Maker-fee handling. Maker-only mode is `f-fastpath-maker-only`, separate brief.
- Per-tier auto-detection from broker API. Operator's call.
- Adjusting other fee-related defaults (none exist anyway).
- Tightening other gates' cost models.
- Migration changes.

## Sequencing

1. Edit `settings.py:158` default + docstring.
2. Edit `gates.py:266-270` docstring.
3. Write `tests/test_fastpath_settings_validation.py` with 3 tests.
4. Run tests against `chili_test` — assert pass.
5. Run existing fast-path test suite — assert no regression.
6. Commit + push.
7. Write CC report.

## Operator-side after CC ships

1. Pull the commit.
2. Restart `chili` + `fast-data-worker` to pick up the new default.
3. **Now safe to set `CHILI_FAST_PATH_COST_AWARE_ADMISSION_ENABLED=1`** if the universe-rotation soak shows enough decay rows accumulating (≥24h post universe-rotator activation).
4. Continue with the universe-rotation operator checklist from the prior CC report.

## Rollback plan

`git revert` the commit. The default reverts to 5.0; the docstring reverts; the new test file is removed. Zero schema or behavior dependencies.

## Open questions for Cowork (surface in CC report only if relevant)

1. **Should the validation test be runtime-asserted at boot** (i.e., raise on out-of-range)? Currently the brief asks for a unit test only. Boot-time assertion is stricter but introduces a startup-blocker if the operator typoes the env var. Default to test-only; flag if there's a reason to escalate.
2. **The 5.0 default came from somewhere.** Worth a one-line check in the CC report: was it from a stale Hyperliquid value (~3.5), a typo for 50, or unintentional? Not blocking — but if there's a reasonable explanation, surface it so we know whether the same class of error might exist elsewhere.

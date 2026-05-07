# COWORK_REVIEW: f-fastpath-universe-rotation

CC report: `docs/STRATEGY/CC_REPORTS/2026-05-07_f-fastpath-universe-rotation.md`
Commits: `22cb7bd`, `d83ff03`, `a096651`, `107c349` (4 commits, mig 231)

## Verdict

**Ship it — but DO NOT enable `cost_aware_admission` until the fee
default is fixed.** The structural work is sound: clean schema, off-by-default
flags, helper-tested rotator with injectable I/O fns, status endpoint, hysteresis,
shadow window. One important defect in the cost-aware gate's default value
that would silently let losing trades through if flipped.

## What's good (algo-trader lens)

1. **Off-by-default for both flags.** `universe_rotation_enabled=False` and
   `cost_aware_admission_enabled=False` mean the switchover is bit-identical.
   No risk of accidental behavior change at deploy time. This is the right
   way to ship infra changes that touch a live execution path.

2. **Cold-start `no_data` allows through.** New shadow pairs aren't
   auto-rejected before `decay_miner` accumulates rows. Without this, the
   gate would deadlock the entire shadow-window mechanism. Good call.

3. **Live `ctx.spread_bps` instead of stale rolling-median.** The brief
   said "use rolling-median" to avoid mid-snapshot blips. CC went with
   live spread, which is **better** for a momentary-spike defense — the
   gate fires when conditions are wrong *right now*, not on average. The
   only risk is over-rejection during normal book widening; mitigated by
   the gate being off-by-default and observable via `gates_json`. Accept.

4. **Hysteresis at 3 ranks** is conservative-but-reasonable. Will revisit
   after observing churn in the first 48h.

5. **Migration ID 231 instead of 230.** CC caught the conflict with
   `f-exit-parity-metric-v2` shipping mig 230 earlier today and adapted.
   Following the brief's own constraint correctly.

## What's concerning (algo-trader lens)

### 🔴 Defect: `cost_aware_taker_fee_bps` default is 5.0 — should be 60

`settings.py:158-162`:

```python
cost_aware_taker_fee_bps: float = 5.0
"""Coinbase Advanced Trade taker fee in bps. Maker-only mode...
in taker mode this is the round-trip cost component we have to clear."""
```

`gates.py:301-304`:

```python
taker_fee_bps = float(fp_settings.cost_aware_taker_fee_bps or 0.0)
spread_bps = float(ctx.spread_bps or 0.0)
cost_bps = 2.0 * (taker_fee_bps + spread_bps)
```

The factor `2 * (taker_fee + spread)` is **per-side ×2 for round trip**, so
`taker_fee_bps` is per-side. Coinbase Advanced Trade retail-tier-1 taker
is **60 bps per side** (round-trip 120). CC's default of 5 bps is off by
**12×** — corresponds to volume tier ~6+ ($75M+ 30d volume), which is not
the operator's account.

**Live impact if the operator flips `cost_aware_admission_enabled=True` with
this default:**

- A signal with `mean_return = 10 bps` at horizon=300s and live
  `spread_bps = 4` would currently be **admitted** (gate computes
  cost_bps = 2*(5+4) = 18, mean 10*1e-4 < 18*1e-4? Wait — actually the
  formula compares mean_return as a fraction; 10 bps = 1e-3, 18 bps =
  1.8e-3, so the gate would reject. Let me re-check).

Re-reading the formula: `cost_return = cost_bps / 10000.0`. With taker=5,
spread=4: cost_return = 18/10000 = 0.0018. mean_return for "10 bps" =
0.001. So 0.001 < 0.0018 → **reject**. OK so the gate isn't allowing
trades that are NEGATIVE on the right cost basis — it's just letting
through trades that are too narrow vs the *correct* 128 bps cost basis.

**Actual live impact:** signals with `mean_return` between **18 bps and
128 bps** would be **falsely admitted**. From the alpha-replay data, the
top mid-tier pairs (RENDER, ICP, ARB, INJ) have 5m mean_return in the
2–7 bps range — those are correctly rejected by either default. But any
pair that does have mean_return in the 18–128 bps band (most likely
post-event noise on JTO-USD, smaller alts, or larger horizons) would
slip through with a false "tradeable" verdict and lose ~110 bps per
round trip.

**Fix:** flip the default to 60.0 (or expose the right tier-1 value).
Either is a one-line change, but the docstring also needs to be honest
about which fee tier this represents.

### 🟡 Watch: composite-score formula vs Sharpe (CC Open Q #1)

CC raised this themselves. Composite = `volume_24h_usd / max(spread_bps, 0.5)`
is a *liquidity proxy*, not an *alpha proxy*. Once `fast_signal_decay`
accumulates rows on the new pairs, we should switch the rotator's
selection to Sharpe-based ranking. But that's a separate brief and not
blocking — the liquidity proxy is the right default for a cold-start
rotator that has no realized data to score on yet.

### 🟡 Watch: the rotator runs *every 60 min* but the WS reconnect
**caches the active set on connect/reconnect** (CC Step 4 note).

This means a newly-shadow-promoted pair waits up to one reconnect cycle
to be subscribed, even though `fast_path_universe` was updated. Not
blocking — Coinbase WS reconnects on idle within minutes — but worth
adding a "force reconnect on rotation pass" hook later if churn is
noticeable.

### 🟡 Watch: DB-bound rotator tests deferred

CC notes 2 tests deferred due to per-test 75s truncate cost. Same
pattern as prior briefs. Acceptable but accumulating debt — the tests
would catch a regression where the rotator silently writes nothing on a
real DB. Add to a future "test infra cleanup" brief; don't block on it
now.

## What's concerning (dev-architect lens)

1. **Test coverage at the right altitude.** 16 helper tests in 2.1s vs
   2 DB-bound tests deferred. The pragmatic call. But: mark the deferred
   tests with `@pytest.mark.db` or similar so the next dev knows they
   exist + why they're skipped. Currently they're just absent.

2. **Public API endpoint** `GET /api/trading/fast-path/universe` —
   verify it has the same auth pattern as the other `/api/trading/...`
   endpoints. If it's wide open, there's a small disclosure surface
   (operator's selected universe is not super sensitive but not nothing).

3. **Schema CHECK constraint on `status`.** Good defensive write. But if
   we ever add a 4th status (`promoted_pending`, `quarantined`), the
   migration path is "new mig with ALTER + recreate constraint." Document
   this in `migrations.py` so the next person knows.

4. **No migration-fingerprint test.** I'd expect a `tests/test_migration_231_fast_path_universe.py`
   that just opens a fresh `chili_test`, runs migrations, and asserts the
   table+columns+indexes exist. Two lines of code. Not blocking, but cheap
   protection against silent migration drift.

## What's next

The fee-default issue makes the cost-aware gate **unsafe to enable**
right now. Two paths:

**Path A (recommended): tiny follow-up brief, then operator deploys.**
Write a 1-commit brief to flip `cost_aware_taker_fee_bps: 5.0 → 60.0`
default, fix docstring to read "Coinbase Advanced Trade taker fee
(retail tier 1) in bps", and add a settings-validation test that
`cost_aware_taker_fee_bps >= 5.0` (a smell test against future
typos). This unblocks Steps 8–11.

**Path B (alternative): deploy as-is with the gate disabled, let the
operator soak universe-rotation for 48h, then bundle the fee fix into
the maker-only brief.** Slightly slower but consolidates the
fee-related work into the maker-only brief (where fee tiering is
discussed naturally).

I'll go with **Path A** because:
1. It's literally one line + a test.
2. Without the fix, any operator who reads the brief and flips the
   gate flag is exposed.
3. The maker-only brief shouldn't carry unrelated cleanup.

## Operator action items

The CC report's "Operator-side after CC ships" checklist is correct,
but with one modification:

1. ✅ Push 4 commits — already pushed by CC.
2. ✅ Restart `chili` + `fast-data-worker` — operator action.
3. ⚠️ **Set `CHILI_FAST_PATH_UNIVERSE_ROTATION_ENABLED=1` ONLY** in
   compose. Do **NOT** set `CHILI_FAST_PATH_COST_AWARE_ADMISSION_ENABLED=1`
   yet — the default fee is wrong, the gate would mis-fire (in fact
   over-admit) on signals with mean_return between 18–128 bps.
4. Wait for first 60-min scheduler tick.
5. Eyeball `GET /api/trading/fast-path/universe` for active+shadow lists.
6. After 24h: shadows auto-promote.
7. After 48h: queue follow-up CC brief for verdict report.

I'm writing the **fee-default fix brief next** so it's queued before
the operator hits the cost-aware-gate flag.

## Cookbook updates (additions to memory)

1. **Off-by-default flag pattern for new gates is the right way to
   ship.** CC's docstring + structure here is good template material.
2. **When CC defaults a numeric constant that maps to a real-world
   value (fees, prices, rates), Cowork must verify the default in the
   review.** This case wasn't caught earlier because the value isn't a
   "magic number" in the code-smell sense — it's a config default with
   a docstring claiming the right semantics. A wrong default is a
   different kind of defect than a magic number.

## Files to follow up

- `app/services/trading/fast_path/settings.py:158` — default fix
- `app/services/trading/fast_path/gates.py:268` — docstring "5 bps" claim
- `tests/test_fastpath_settings_validation.py` — new (add range
  validation for `cost_aware_taker_fee_bps`)

## Status

- f-fastpath-universe-rotation: **ACCEPTED with caveat**. Universe
  rotation flag safe to enable. Cost-aware gate flag NOT safe until
  fee-default fix ships.
- Next NEXT_TASK candidates: (a) `f-fastpath-cost-aware-fee-default-fix`
  (1-line + test, ~30 min for CC); then (b) `f-fastpath-maker-only` or
  (c) wait 48h on universe-rotation soak before promoting maker-only.

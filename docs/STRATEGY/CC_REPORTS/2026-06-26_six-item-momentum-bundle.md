# 2026-06-26 — Six-item momentum-lane bundle (all kill-switched, DEFAULT OFF)

Worktree `project_ws/_worktrees/etfrank` (HEAD b0d5739). All six items ship with a kill
switch defaulting OFF ⇒ byte-identical to b0d5739 when disabled. The two order-path items
preserve the exact dedupe-on-broker_order_id / single-writer / orphan-late-fill safety net
of the current single-leg placement.

## Items

1. **ANTICIPATION STARTER** (`chili_momentum_anticipation_starter_enabled`, OFF) — equity-only
   probe-then-add. At entry-qty compute the intended qty is split into a small PROBE leg
   (`chili_momentum_anticipation_probe_fraction`, 0.25) submitted on the break; the remainder
   is added ONCE after the probe CONFIRMS (held position GREEN) via a self-contained in-flight
   slot (`anticipation_add_order_id`) that merges through the SHARED, tested
   `pyramid_blend_on_fill` helper. Dedupe-safe (one in-flight remainder at a time, idempotent);
   orphan-safe (the remainder child order_id is folded into `entry_order_ids_all` so the
   existing late-fill sweep + pre-submit guard track it to terminal — no stranded naked leg).
   Probe split FALLS BACK to a single full entry when either leg < base_min_size. OFF ⇒ no
   split, qty unchanged, no remainder add (byte-identical). `live_runner.py`.

2. **ORDER CHUNKING** (`chili_momentum_order_chunking_enabled`, OFF;
   `chili_momentum_order_chunking_blocks`, 1) — a protocol-preserving venue-adapter WRAPPER
   (`venue/chunking_adapter.py`) inserted at the live-runner factory site via
   `maybe_wrap_chunking`, which returns the base factory UNCHANGED (identity-equal) when the
   flag is OFF or blocks<=1 ⇒ byte-identical. When active it splits `place_limit_order_gtc`
   into N equal blocks, each a FRESH `client_order_id` (no idempotency collision) summing
   EXACTLY to the parent, collects every child `order_id` into `chunk_order_ids`, and every
   child is folded into `entry_order_ids_all` for reconciliation. Fail-closed-to-single on any
   split/parse error. **RECOMMENDATION: do NOT enable until dedupe/reconcile safety is proven
   on the agentic rail** (marginal benefit for a small cash account; the rail's duplicate-fill
   history). `venue/chunking_adapter.py`, `live_runner.py`.

3. **GREEN-DAY GRADUATION** (`chili_momentum_green_day_graduation_enabled`, OFF) — a bounded
   UPWARD size multiplier, NOT a hard live-block. `green_day_graduation_multiplier` auto-derives
   the consecutive-green-day streak (realized daily PnL > 0, bucketed by ET calendar, today
   excluded — lookahead-free) and applies `1 + step*(streak-1)` capped at
   `chili_momentum_green_day_max_multiplier` (2.0). Folded as one factor into the live-runner's
   `_eff_max_loss` product, still bounded by the existing `*3.0` combined-multiplier ceiling.
   Applied at entry-quantity compute — never a veto. OFF ⇒ multiplier 1.0, DB never queried
   (byte-identical sizing). `risk_policy.py`, `live_runner.py`.

4. **PROCESS-OVER-PROFITS SCORE** (`chili_momentum_process_score_enabled`, OFF) — a LOGGED-ONLY
   rule-adherence score (entered-on-trigger / honored-stop / no-chase) over the last N real
   entered closed trades, scored from the deterministic `outcome_labels` classes (success/
   small_win/timed_exit = 1.0, stop_loss = 0.5 honored, bailout/governance_exit = 0.0), filtered
   by `is_real_entry_outcome` so never-entered cancels are excluded. NEVER gates/sizes/vetoes.
   `metrics_surface.py`.

5. **OVERHEAD-SUPPLY CEILING** (`chili_momentum_overhead_supply_tilt_enabled`, OFF) — a composable
   0.10-weight FIFTH selection pillar (`overhead_supply`) folded onto the active Ross weight-set
   in `score_universe` (self-renormalises). The bridge (`pipeline.py`) reuses the daily context's
   `overhead_supply_atr(ctx, entry)` to stamp an `overhead_supply_pct` sub-score (pinned at the
   overhead level ⇒ 0.0 max de-weight, clear sky >= room-ATR ⇒ 1.0, already-broken-above ⇒ 1.0).
   A re-rank tilt ONLY — never blocks a fill or removes a name from the pool. DISTINCT from the
   pre-existing entry-side `chili_momentum_overhead_veto_enabled` (left untouched, still default
   True). OFF ⇒ no sub-score stamped, pillar absent ⇒ byte-identical ranking. `ross_momentum.py`,
   `pipeline.py`.

6. **METRICS SURFACE** (`chili_momentum_challenge_metrics_enabled`, OFF) — read-only KPI surface:
   accuracy% (rule-adherence), profit-loss ratio (capped 5.0), consecutive-green-day streak.
   Exposed at `GET /api/trading/momentum/metrics`. No trading impact. OFF ⇒ returns `{}`.
   `metrics_surface.py`, `routers/trading_sub/momentum_api.py`.

## Dedupe / orphan-safety proof (order-path items)

- **Chunking**: each child uses a fresh `uuid4`-suffixed `client_order_id` (verified 4 distinct
  cids for a 4-block split); child sizes sum EXACTLY to the parent (1000 → 250×4; 1003 → exact
  via remainder-onto-last); ALL child `order_id`s collected into `chunk_order_ids` AND fed
  through `_record_entry_order_placed` so each leg lands in the existing late-fill/orphan sweep.
  Degrade-to-single when a clean split is impossible (byte-identical). Identity-preserving
  factory when OFF.
- **Anticipation**: the remainder rides the SAME proven in-flight-merge mechanism the pyramid
  add uses (`pyramid_blend_on_fill`), on a separate slot, idempotent (one in-flight at a time),
  with the remainder child order_id folded into `entry_order_ids_all` (orphan-safe). The
  position is mutated ONLY on a confirmed-fill PHASE-1 merge, never on submit.
- No separate fills table / orphan registry invented — all reconciliation stays in the existing
  `entry_order_ids_all` + late-fill-sweep + `pyramid_blend_on_fill` pattern.

## Verification

- `py_compile` clean on all 8 touched/new files.
- Flag defaults asserted: all order-path + graduation + additive flags default OFF; pre-existing
  `chili_momentum_overhead_veto_enabled` untouched (still True).
- Byte-identical proofs (flag OFF): `score_universe` ranking unchanged; green-day mult 1.0 with
  DB never queried; metrics surface returns None/{}; chunking factory identity-preserved.
- Flags-ON proofs: chunking distinct cids + exact-sum + all oids collected; blocks==1 single
  order; transparent delegation; green-day streak=3 ⇒ 1.2x and cap respected at 2.0; process
  score excludes cancels and scores adherence (0.625) correctly.
- `pytest tests/test_entry_feature_parity.py tests/test_momentum_pyramid.py` → **81 passed**
  (incl. `test_D_full_add_lifecycle_submit_adopt_blend_idempotent`).
- `pytest tests/test_ross_momentum.py` → 11 passed, 1 pre-existing failure
  (`test_liquidity_biased_weights_lift_fillable_names`) confirmed to fail on clean HEAD b0d5739
  too — NOT a regression from this bundle.

No existing gate weakened; no scattered magic (every knob is one documented config field); no
local-reimport shadowing (module-level names reused; lazy imports are function-local only where
the existing code already does so).

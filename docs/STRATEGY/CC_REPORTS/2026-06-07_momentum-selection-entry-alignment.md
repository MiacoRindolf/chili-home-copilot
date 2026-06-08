# CC_REPORT: momentum-selection-entry-alignment (ME-4 selection half)

Direct operator brief (not a Cowork `NEXT_TASK` — `NEXT_TASK.md` still points at the
unrelated position-identity Phase 5I soak, left untouched). Implements the
**selection→entry alignment** half of the keystone in
`docs/DESIGN/MOMENTUM_LANE_ENTRY_STOP_REALIGNMENT.md` §3 ME-4.

## What shipped

- Commit `38d87e8` (rebased onto `46a159b` = main after PR #511).
- **Root cause** (confirmed by the live diagnostic + the dry-run): the lane SELECTS
  24h-cumulative movers (`ross_momentum.score_universe` ranks RVOL/gap/daily-change over
  the whole day), but by entry time many have FADED into a deep intraday retrace, so the
  pullback-break gate reads `pullback_too_deep` and never fires. `auto_arm` then pins the
  single live slot on the **highest-24h-viability** name — which is exactly the faded
  leader (live snapshot: top name `DOGINME-USD` score 0.167 / retrace 0.833, while fresh
  movers `TRAC`/`SUKU`/`IOTX` sat lower by 24h rank).
- **Fix — selection path only** (does NOT touch `entry_gates.py` / `live_runner.py`; the
  entry-gate retrace + break-retest refinements are the complementary half, landed in #511):
  - `ross_momentum.intraday_impulse_freshness()` — new **pure, adaptive** measure: position
    of the current price within the recent intraday range (the SAME window the gate uses).
    `is_fresh` reuses the gate's own `retracement_threshold` (0.50) as the "near-high" bar,
    so the filter and the gate share one definition of "shallow" — **no new magic number**.
    Returns `is_fresh` + a ranking score.
  - `auto_arm.run_auto_arm_pass` — probes trigger + freshness concurrently, then: (1) arm
    the freshest name whose break is FIRING now; (2) else WATCH the freshest name we
    positively know is in a fresh up-impulse; faded non-firing names are dropped. The live
    runner still confirms the real break + viability + market-open + belts before any order.
  - One documented knob `CHILI_MOMENTUM_AUTO_ARM_REQUIRE_FRESH_IMPULSE` (default **on**;
    set `0` to restore arm-only-on-active-break).
  - `scripts/d-momentum-selection-entry-alignment-dryrun.py` — read-only ME-3-style harness.
- Files touched: `ross_momentum.py` (+111), `auto_arm.py` (+~104), `config.py` (+12 knob),
  `tests/test_momentum_auto_arm.py` (+5 tests), `tests/test_intraday_impulse_freshness.py`
  (new, 8 tests), dry-run script (new). **No migrations.**

## Verification

- **Dry-run on live candidates** (read-only; 25 live-eligible crypto names, 1440 bar-evals
  per interval; gate swept over recent bars to reproduce the audit's fire-rate). Run twice:
  on the pre-#511 gate, then on the merged #511 gate.

  | Interval | Baseline (all) | **FADED-only** | **FRESH-only** | Fire retrace mean/max |
  |----------|---------------:|---------------:|---------------:|----------------------:|
  | 5m (pre-#511) | 0.62% | **0.00%** | **1.16%** | 0.224 / 0.500 |
  | 1m (pre-#511) | 0.62% | **0.00%** | **1.25%** | 0.149 / 0.262 |
  | 5m (with #511 gate) | 0.69% | **0.00%** | **1.33%** | 0.201 / 0.500 |
  | 1m (with #511 gate) | 0.69% | **0.00%** | **1.37%** | 0.134 / 0.262 |

  - **Faded names NEVER fire (0.00%)** — `pullback_too_deep`-dominated; every single fire
    lives in the fresh partition. This is the proof the freshness filter removes pure dead
    weight, not signal.
  - **Fires are genuine shallow** (retrace ≤ 0.50, NOT loosened — the 0.50 cap is untouched).
  - **1m fires materially shallower** than 5m (mean 0.13 vs 0.20) at a marginally higher
    rate → confirms 5m is too coarse to catch the shallow pullback before it deepens.
- **Tests**: 68 passed (8 new freshness unit tests + 5 new auto_arm selection tests +
  #511's giveback/pullback tests + all existing), post-rebase, on `chili_test`.
- **Import smoke**: `ross_momentum`, `auto_arm`, `viability`, `pipeline`, `trading_scheduler`
  all import; new fn + probe present; knob defaults on.

## Surprises / deviations

- **PR #511 merged mid-task** (the parallel codex agent's `momentum-entry-quality-refinements`
  + profit-giveback). It rewrote `entry_gates.pullback_break_confirmation` (retrace vs the
  current impulse leg + break-AND-retest + sustained-volume) and added a Guard-5 profit-
  giveback halt to `auto_arm`. I deliberately scoped my change to the **selection path**
  (`ross_momentum` + `auto_arm` selection block) to stay disjoint from the gate internals.
  Rebased onto the new main; auto-merged with zero conflicts (different hunks). The two
  fixes are **complementary and compose** — verified by re-running the dry-run on the merged
  gate.
- **The absolute fresh-fire-rate is still ~1.3%/bar**, not dramatically higher, because
  even a "fresh" (close-near-high) name can be `pullback_too_deep` when its last-3-bar
  pullback LOW dipped deep (a V-recovery). The decisive win is not the absolute rate but
  the **0.00% vs 1.33% split** + the **re-rank**: the single slot now watches a name that
  *can* fire instead of a faded leader that *cannot*. The arm-to-watch change is what
  converts "rarely catch a fire at the 30s scan tick" into "watch the freshest mover and
  enter on its break."
- The repo `.env` has `entry_trigger_mode=hybrid` / `pullback_entry_interval=5m`; the
  deployed scheduler container overrides to `pullback_break`. The fix is mode-agnostic
  (freshness is a candidate filter, not the trigger).

## Deferred

- **Flip the live entry interval to 1m** — `CHILI_MOMENTUM_PULLBACK_ENTRY_INTERVAL=1m`.
  Evidence-backed (shallower, tighter-stop fires; marginally higher rate). Left as an
  operator env flip (no code) since the interval knob is shared with the #511 gate path and
  the operator should observe the first 5m fresh-filtered entries before A/B-ing 1m.
- **`live_runner` WATCHING_LIVE freshness re-check + faster reap of a fading watch** — once
  a fresh name is armed-to-watch, the runner does not re-check freshness during the watch;
  a name that fades mid-watch pins the slot until the 30-min reaper. Belongs in `live_runner`
  (the parallel agent's surface) — flagged, not done here.
- **Selection-pool enrichment (lever a)** — NOT needed now: the live-eligible pool already
  contains fresh names (the binding constraint was ordering, fixed by the re-rank). Would
  only matter if fresh-but-modest-24h breakouts were being excluded from the top-30 bridge;
  revisit if a future dry-run shows a starved fresh partition.

## Open questions for Cowork

1. Confirm the **1m interval flip** (evidence favors it; it's a one-line env change, fully
   reversible).
2. Should the `live_runner` get a freshness re-check during WATCHING_LIVE (reap a watch that
   fades before it breaks), or is the 30-min reaper + re-rank-on-next-arm sufficient?
3. The arm-to-watch change makes the single live slot ~always occupied by the freshest
   mover's watch. Confirm that posture is desired (it is strictly better than today's idle/
   faded-watch slot, but it does increase begin/confirm churn — all pre-entry, no orders).

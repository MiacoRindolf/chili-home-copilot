# CC_REPORT: momentum-entry-quality-refinements

> Note: this task arrived as a **direct operator brief in the Claude Code session**,
> not via `NEXT_TASK.md` (which still holds the unrelated phase-5i soak, left
> untouched — same as the 2026-06-07 shakeout-learner and portfolio-DD reports).
> Source: the 2026-06-07 Ross-methodology research; three RECENT (post-book,
> live-practice) entry-logic refinements for the `momentum_neural` `pullback_break`
> gate.

## What shipped

PR [#512](https://github.com/MiacoRindolf/chili-home-copilot/pull/512) — squash-merged
to `main` as **`69cfc87`**. One atomic commit (the three are one capability:
better-quality entries + a tighter failed-breakout exit). 6 files, **0 migrations**.

Files: `app/config.py` (+65, 10 knobs), `app/services/trading/momentum_neural/entry_gates.py`
(break-retest + sustaining-volume gates + pure bailout-decision helper),
`app/services/trading/momentum_neural/live_runner.py` (pass knobs to the trigger,
stash the breakout level at entry, the held-position fast-bail), `tests/test_pullback_break.py`
(+13 tests), `scripts/dryrun-momentum-entry-refinements.py` (new validator),
`docs/DESIGN/MOMENTUM_LANE.md` (§8).

### #1 Break-AND-retest (`require_retest`, default on)
Anchors a **stable** breakout level on the consolidation that ends
`retest_lookback_bars` back (so it doesn't slide across the runner's per-tick
re-evaluations), then requires break → shallow retest (dip to ~level within
`retest_tolerance`) → hold-on-closes → current-bar reclaim. EMA-9 support is checked
at the base, not the current bar (a strong continuation would otherwise reject a valid
retest). Replaces buying the raw first break, which wicks out.

### #2 Breakout-or-bailout fast exit (`breakout_bailout_enabled`, default on)
The broken pullback HIGH is stashed as `le["breakout_level_price"]` at the entry
candidate transition. In the held block (after the C1 max-loss check, before the
structural-stop check), within `breakout_bailout_max_bars × interval` seconds, if the
bid falls below `level × (1 − buffer_pct)`, transition to `BAILOUT` (reuses the
existing flatten machinery; next tick sells). Pure decision in
`entry_gates.breakout_failed_to_hold(...)` for unit-testability. Guarded: only with a
recorded level (pullback_break entry, not the momentum_volume fallback), only while
plainly `ENTERED`, only inside the window — so it never fights the normal stop/target.

### #3 Sustaining-volume gate (`require_sustained_volume`, default on)
At the entry tick, mean `volume_ratio` over `sustain_lookback_bars` must exceed
`sustained_rvol_floor` (default 1.0). `volume_ratio` is rel-vol vs the instrument's own
trailing average, so the floor is adaptive (self-relative, a FLOOR the system can
raise), not a fixed share count. Rejects a faded 24h mover (hot at selection, dead by
entry) — the ESTR −$30,942 guardrail — and tightens the selection↔entry alignment the
audit flagged. Fails OPEN on thin data.

Raw-mode behavior is byte-identical when the knobs are off (the original first-break
path was extracted unchanged into `_evaluate_raw_break`).

## Verification

**OHLCV walk-forward dry-run** (`scripts/dryrun-momentum-entry-refinements.py`, 10
crypto symbols, 5d, run live this session — Massive→Polygon→yfinance fetch works on
this box). Each bar treated as the live "current" tick; per-fire outcome uses the
lane's own risk model (structural pullback-low stop, 2:1 target, 24-bar horizon):

| Variant | 5m win-rate | 5m avg-ret | 1m win-rate | 1m avg-ret |
|---|---|---|---|---|
| baseline (raw) | 29.6% | −0.18% | 28.6% | −0.03% |
| +retest (#1) | 41.7% | −0.05% | 50.9% | +0.13% |
| +sustain (#3) | 25.0% | −0.24% | 31.6% | −0.01% |
| **+both** | **44.1%** | **−0.03%** | **54.3%** | **+0.16%** |

- **#1 retest** lifts win-rate hard on both timeframes; `+both` is best on **every**
  metric. The single clearest quality win.
- **#2 breakout-bailout** (over the SAME 27 baseline 5m fires): triggered on 37%, cut
  aggregate loss ~23% (−4.81% → −3.72%, net +1.09% avoided). On the live 5m timeframe
  it is unambiguously positive.
- **#3 sustaining** is ~neutral-to-slightly-negative ALONE in a 5-day sample but
  improves the combined config; its real value is ESTR-class tail risk a 5-day window
  can't surface.

**Tests:** 13 new in `tests/test_pullback_break.py`; suites green —
`test_pullback_break` + `test_structural_pullback_stop` (21) and
`test_momentum_live_runner` + `test_live_runner_exit_gating` + `test_momentum_auto_arm`
+ `test_ross_momentum` + `test_momentum_import_paths` (81) = **102 passed, 0 failures**.
`py_compile` clean; `live_runner` imports with no cycle from the new top-level
`entry_gates` import.

**Not yet exercised live:** no new image built / scheduler recreated this session — the
change is on `main` but the running `chili-clean-recovery-scheduler` is still on
`main-clean-4276cb9`. Deploy is the operator's call (build `chili-app:main-clean-69cfc87`,
recreate the scheduler). No migration, so deploy is image-only.

## Surprises / deviations

- **Retest fires MORE, not fewer, than raw** (+33% on 5m, +8% on 1m) — counter to the
  naive "stricter ⇒ fewer." The retest detector uses a stable base-window level and so
  surfaces break-retest setups the narrow raw "current bar pierces the last-3-bar high"
  misses. The win-rate is markedly higher, so the extra fires are net higher-quality —
  good for a supply-starved lane (0W/15L all-time). Flagging because it's the opposite
  of the intuition in the brief.
- **#2 breakout-bailout is marginally NEGATIVE on 1m** (−0.33%, triggered 8%): a 2-bar
  window is only ~2 min on 1m and single-bar dips revert. Defaulted on because the lane
  runs **5m** (where it's clearly positive); the `_buffer_pct` / `_max_bars` knobs let
  the operator harden it for 1m. The dry-run sim uses bar-CLOSE-below-level; the live
  runner uses bid-below-level-minus-buffer per tick (the runner has no bar-close
  semantics) — the buffer is the lever that keeps live from over-bailing.
- **Defaults set AFTER the dry-run, not before.** Per the brief's "verify each BEFORE
  changing live behavior." All three default **on** because `+both` dominated on
  win-rate and avg-return on both timeframes and #2 cut 5m losses ~23%. This honors
  both the validate-first instruction and the no-dark-flags work-style: shipped on +
  fallback-safe, with the evidence in hand.
- Used the env interpreter directly (`chili-env\python.exe`); `conda run` still faults
  on this box (noted in prior reports). Unrelated to this change.

## Deferred

- **Lane-scoped sizing/throughput review.** The refinements improve entry *quality*;
  they don't change *why the lane is net-negative* — that's the open strategy question
  from the portfolio-DD report (0W/15L). Out of this brief's scope.
- **1m-specific breakout-bailout tuning.** If the operator runs 1m, raise
  `CHILI_MOMENTUM_BREAKOUT_BAILOUT_BUFFER_PCT` (or `..._MAX_BARS`) and re-run the dry-run
  on `--interval 1m`. Left at the 5m-validated defaults.
- **Live exercise of all three.** Code + dry-run + unit-tested; not observed on a real
  fill (no deploy this session; lane currently produces almost only cancels).

## Open questions for Cowork

1. **Deploy now?** Build `chili-app:main-clean-69cfc87` and recreate the scheduler, or
   hold for a Cowork review of the dry-run first? (No migration; image-only; fully
   reversible via the `CHILI_MOMENTUM_*` env knobs.)
2. **Retest firing more, not fewer** — is the higher-quality-but-more-frequent behavior
   what you want, or do you want retest to be a strict *subset* of raw (fewer, only the
   highest-conviction)? Easy to tighten (`retest_tolerance` down / require a deeper
   retest) if so.
3. **Sustaining floor** — default 1.0 (must hold its own trailing average). Raise toward
   the selection RVOL band if you want only still-explosive names through? It's the most
   conservative current setting.

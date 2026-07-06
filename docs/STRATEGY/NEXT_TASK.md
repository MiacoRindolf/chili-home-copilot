# NEXT_TASK: ross-capture-parity-P0-evidence-and-integrity

STATUS: PENDING

> Supersedes `f-position-identity-phase-5i-post-rename-soak` (2026-06-02, stale — the 14-day
> soak window has long elapsed; its watcher `CHILI-phase5i-post-rename-soak-probe` still runs
> and its gate state was green at last record. If the watcher shows regressions, flag them,
> but do not resume that task without operator direction).

## Read FIRST

`docs/DESIGN/ROSS_CAPTURE_PARITY.md` — the full design (mission, verified baseline, phases
P0-P3, per-lever RIGOR, DO-NOT guardrails, live-bindings appendix). This task = **P0 only**.
The executor contract in that doc is binding: one change per deploy, FSM-gate before live,
STOP at any failed gate and report, never violate §4.

## Goal (P0 — evidence + integrity; NO trading change)

1. **Baseline reproduction:** re-run the FSM replay (instrument
   `project_ws/_worktrees/fsmdriver/scripts/replay_v3_fsm_window.py`, dense `TICK_STRIDE=2`,
   `TEST_DATABASE_URL` ending `_test`) over the 10 scorecard movers
   (06-26 ZDAI · 06-30 SVRE/JEM/CELZ · 07-01 TC/LHAI/DXF/CANF/JEM · 07-02 CLRO).
   PASS = net **+$264.25** with JEM **+$314.53** reproduced.
   FAIL = STOP EVERYTHING and diagnose oracle drift — nothing downstream is trustworthy.
2. **First-prod-fill capture check (gates P2 / the meta-label lever):** after the first LIVE
   fill on image ≥ `main-clean-d98c924`, verify `momentum_automation_outcomes.entry_regime_snapshot_json`
   populated OR a `live_entry_feature_capture_error` event exists (exact SQL in the design doc §2/P0).
   Neither ⇒ #851's wiring is wrong on the scheduler-arm path — debug `live_runner.py` ~12103-12127
   before anything else.
3. Confirm the design doc's §5 bindings appendix still matches the live container
   (`chili-clean-recovery-momentum-exec`; report BINDINGS, not config defaults).

## Done means

- The replay reproduction table (per-day scorecard format) in a new CC_REPORT.
- The capture-check verdict (populated / error-event-named-cause / FAILED-both + diagnosis).
- `docs/STRATEGY/CC_REPORTS/YYYY-MM-DD_ross-capture-P0.md` written; this file marked STATUS: DONE; commit.

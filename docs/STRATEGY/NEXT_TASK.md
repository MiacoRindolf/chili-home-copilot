# NEXT_TASK: ross-capture-P1a-entry-snapshot-durability

STATUS: PENDING

> P0 is DONE — see `docs/STRATEGY/CC_REPORTS/2026-07-06_ross-capture-P0.md`. Oracle proven
> faithful; the meta-label data-gate was root-caused to a real, fixable defect that is
> shared-root with criterion ③ (fill-then-cancel wipes the frozen entry snapshot).

## Read FIRST
`docs/DESIGN/ROSS_CAPTURE_PARITY.md` (mission, phases, RIGOR, DO-NOT §4) and the P0 report
above. The executor contract is binding: one change per deploy, FSM-gate before live, STOP at
any failed gate and report, never violate §4.

## Goal — durable entry-snapshot capture (unblocks the #1 lever = meta-label; shared-root with ③)

**Root cause (proven in P0):** on a live fill, `live_runner.py:8293` writes the entry regime
snapshot onto the mutable `le` (`risk_snapshot_json->'momentum_live_execution'`), but sessions
that **fill then immediately `live_cancelled`** have their `le` wiped by the cancel/close path
— the persisted `le` on all 7 of today's fills had NO `entry_regime_snapshot_json`, NO
`entry_features`, and NO `position`. So `momentum_automation_outcomes.entry_regime_snapshot_json`
is `{}` (2/942 all-time). The FSM replay (96/96) never sees this because its fills are
synchronous + held.

### Step 1 — CONFIRM the hypothesis (do NOT skip; cheapest falsifier)
On a stable lane, trace ONE session that **fills AND holds** to a clean managed exit. Query its
`risk_snapshot_json->'momentum_live_execution'` for `entry_regime_snapshot_json`.
- Present → the gap is purely a fill-then-cancel symptom → Step 2 (durable snapshot) + it also
  improves as ③ churn drops (#854 reconcile-not-terminalize).
- Absent on a HELD fill too → a second persistence defect exists; trace `_commit_le` across the
  post-fill ticks before writing any fix.

### Step 2 — Fix (small, additive, insulated)
Persist the frozen entry snapshot durably at fill so a later cancel cannot erase it: write
`entry_regime_snapshot_json` (+ `entry_features` when captured) to the outcome/a dedicated field
at fill time, NOT only onto the `le` that the cancel path clears. Reuse the existing
`capture_entry_features` output (`live_runner.py:8308`) and the `regime` already fetched at 8140.

### Gates
- **FSM replay parity:** the change is additive/post-fill — JEM must stay +$314.53 byte-identical,
  net ≥ +$264.25, exit parity byte-identical. Run on an ISOLATED sink per the P0 A/B recipe
  (source=chili, sink=a dedicated `*_test` DB, not the parallel session's `chili_test`).
- **Live proof-of-life:** after deploy, the next held live fill populates
  `momentum_automation_outcomes.entry_regime_snapshot_json` (non-`{}`) OR emits
  `live_entry_feature_capture_error`.
- **Deploy:** compose-canonical only (`.env` pin `CHILI_MOMENTUM_EXEC_IMAGE` + `docker compose
  --profile live-momentum up -d --no-deps momentum-exec-worker`); verify single `DATABASE_URL=…/chili`
  in-container; do not deploy with open positions.

## Done means
CC_REPORT written; the capture confirmed populating on a held fill (or the second defect traced);
`NEXT_TASK` marked DONE; commit. Then the data accumulates toward P2 (meta-label training, N≥100
fills / ≥15 winners).

## ⚠️ Coordination
A parallel session is upgrading the FSM replay + shipped #854/#855 to the lane (image churn
`d98c924→bc36984→d8cc05d`). Use the isolated sink; verify EXEC-worker bindings after any deploy;
avoid touching the parallel session's `chili_test` or the replay worktree.

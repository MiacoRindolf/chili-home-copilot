# NEXT_TASK: f-position-identity-phase-5k-live-path-cutover-brief

STATUS: PENDING

## Goal

Write the Phase 5K cutover brief for live paths that still intentionally use
the `trading_trades` compatibility view or `Trade` ORM class after Phase 5J.

Do not implement live-path code changes in this task. The purpose is to decide
which references should remain permanent compatibility contracts and which ones
deserve a future feature-flagged, owner-reviewed cutover.

## Current Gate State

- Phase 5I post-rename soak: `COMPLETE_POSITIVE`
- Phase 5J slices 1-5 shipped.
- Phase 5J remaining-reference audit closed:
  - no more safe reader-only conversions
  - compatibility view remains required
  - Python `Trade` ORM class remains required
- Pid 537 watcher closed and scheduled task disabled.

## Scope

1. Re-read:

   - `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5j-remaining-reference-audit-closeout.md`
   - `docs/RUNBOOKS/PHASE5J_SELECTIVE_READER_CLEANUP.md`
   - `docs/DESIGN/POSITION_IDENTITY.md`

2. Build a live-path matrix for remaining app references:

   - **permanent keep:** ORM/FK/API compatibility
   - **feature-flag future:** live capital/promotion readers where the semantic
     base table may be useful but behavior must be proven neutral
   - **writer boundary:** broker/order/reconcile/stop paths that should keep
     `Trade`/compatibility until a larger envelope-writer cutover is designed
   - **dirty/defer:** files with unrelated local edits

3. Propose the smallest safe Phase 5K implementation slice, if any.

4. Keep Phase 5I green:

   ```powershell
   python -m pytest tests\test_phase5j_reader_cleanup.py tests\test_phase5i_post_rename_probe.py
   python scripts\d-phase5i-post-rename-soak-probe.py
   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\dispatch-phase5i-post-rename-soak-probe.ps1
   ```

## Guardrails

- Do not drop the `trading_trades` compatibility view.
- Do not rename the Python `Trade` ORM class.
- Do not edit live broker/order/stop/reconcile paths in this brief.
- Do not edit dirty local files unless their current diff has been inspected and
  deliberately included.
- No live trading behavior change without a separate operator-approved
  implementation task.

## Acceptance

- A CC report exists with the live-path matrix and recommendation.
- No code behavior changes unless explicitly split into a later task.
- Phase 5I still reports `COMPLETE_POSITIVE`.

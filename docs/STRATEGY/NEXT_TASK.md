# NEXT_TASK: f-cpcv-gate-coverage-audit

STATUS: DONE

## Goal

**Phase 0 of the adaptive-promotion-architecture initiative.** Read-only
audit to prove or disprove the hypothesis that the CPCV gate handler is
failing to reach 275 patterns that have ≥ 30 PTR rows but NULL
`cpcv_n_paths`. No code changes.

## Why this is next

The brain has only 3 promoted patterns out of 586. Pattern 585 fires
1293/1294 `pattern_breakout_imminent` alerts — the live signal funnel is
effectively single-pattern. Probes (commit `e70bc5c`) showed the CPCV
gate has produced verdicts for only 39 of 586 patterns, and the
hardcoded thresholds (DSR≥0.95, PBO≤0.2) have zero discriminatory power
on the patterns that do have data. The drought is a gate-coverage
problem, not a threshold problem.

Phase 0 establishes WHICH lane of the funnel is broken before any code
ships — Phase 1 (backfill) vs handler-side fix have different solutions.

## Brief

`docs/STRATEGY/QUEUED/f-cpcv-gate-coverage-audit.md`

Parent architectural brief:
`docs/STRATEGY/QUEUED/f-adaptive-promotion-architecture-2026-05-11.md`

## Deliverables (all in scripts/ or docs/ — NO code under app/)

1. `scripts/audit-cpcv-gate-coverage.ps1` — classifies the 275 patterns
   by where the funnel breaks (event missing / event-but-no-handler-log
   / handler-logged-but-no-persist / unknown)
2. `scripts/audit-cpcv-gate-force-eval.ps1` — dry-run the gate handler
   against a single pattern; rolls back; reports would-pass + reasons
3. `scripts/audit-cpcv-gate-coverage-out.txt` — committed run output
4. `docs/AUDITS/2026-05-11_cpcv_gate_coverage.md` — one-page memo with
   classification counts + concrete recommendation for what Phase 1
   should enqueue
5. `docs/STRATEGY/CC_REPORTS/2026-05-11_cpcv-gate-coverage-audit.md`

## Hard constraints

- **READ-ONLY.** No writes to DB. No `app/` code changes. No restarts.
  No new migrations / tables / columns. No env edits.
- `psql -c` SELECT-only; `docker exec python -c` blocks must
  `sess.rollback()` in finally
- No changes to `cpcv_gate.py` or any handler — read it, don't edit it
- Memo must quote exact percentages per classification, not hand-waves

## Next in queue

- ~~`f-cowork-watcher-truncation-fix` (priority 210)~~ — SHIPPED out
  of band on 2026-05-11 via operator override. CC_REPORT at
  `docs/STRATEGY/CC_REPORTS/2026-05-11_f-cowork-watcher-truncation-fix.md`.
  Canonical watcher prompt now at
  `docs/STRATEGY/COWORK_WATCHER_PROMPT.md`; helper at
  `scripts/watcher-check-truncation.ps1`; runbook at
  `docs/runbooks/WATCHER_TRUNCATION_HEURISTIC.md`. Operator needs to
  paste the canonical prompt into the live routine at
  https://claude.ai/code/routines.
- `f-supervisor-auto-relaunch-investigation` (priority 220) — daemon
  supervisor didn't auto-relaunch after the 4h self-restart
- `f-cpcv-gate-backfill` (Phase 1) — written after Phase 0 lands and the
  memo informs Phase 1 design

## Side-shipped this session

`f-cowork-watcher-truncation-fix` was completed under operator
override at 2026-05-11T15:13-15:26Z (plan-gate APPROVED autonomous
at 15:20Z). No `app/` files, DB, or shared tooling touched — the
fix is additive (one helper PS1, one canonical prompt doc, one
runbook, one reference JSON sample, one CC_REPORT).

# CC Report: shadow-vetting finalizer PendingRollbackError (FIX 46 family)

**Date:** 2026-06-11
**Type:** Operator-directed incident fix (not a NEXT_TASK item; NEXT_TASK
`f-position-identity-phase-5i-post-rename-soak` untouched and still PENDING)
**Scope:** certification/learning machinery only — no live trading path changes

## Incident

`pattern_shadow_vetting_finalizer` failed once at 2026-06-11 07:10 UTC after
175s with `sqlalchemy.exc.PendingRollbackError: Can't reconnect until invalid
transaction is rolled back`, raised at
`run_shadow_vetting_cycle` (~1419) → `select_shadow_vetting_candidates` (~760)
→ `_score_threshold_from_pool` (~635) → `db.execute(...)`.

The PendingRollbackError is a *symptom*: an earlier statement in the same
Session failed (timeout/disconnect), the error was swallowed without a
rollback, and the cycle kept using the poisoned session.

## Root cause (where a failed statement could be swallowed without rollback)

The failure surfaced at the **second** `select_shadow_vetting_candidates`
call, so the poison was planted between the first select and that line —
i.e. inside `refresh_blocked_shadow_promotion_gates`. Audit of the module
found three swallow-without-rollback sites:

1. `persist_cpcv_shadow_eval` (`promotion_gate.py`) — telemetry INSERT;
   swallows its own DB errors with `logger.debug` and **no rollback**. Called
   per-candidate during the blocked-gate refresh. This is the most likely
   culprit for the 07:10 incident (a `not patch → continue` path lets the
   poisoned session escape the per-candidate rollback handler).
2. `_load_directional_evidence` — best-effort paper-dynamic evidence query;
   `except: paper_rows = []` with **no rollback**.
3. `emit_promotion_surface_change` (via `brain_work/promotion_surface.py`) —
   swallows DB errors internally; can poison the finalize loop and the final
   `db.commit()`.

## Fix (FIX 46 pattern: rollback before reuse/close; SAVEPOINT for best-effort side-writes)

`app/services/trading/promotion_gate.py`
- `persist_cpcv_shadow_eval`: the INSERT now runs under `db.begin_nested()`
  (SAVEPOINT, established repo pattern) so a failed statement cannot doom the
  caller's transaction. `getattr` guard keeps db-like test stubs working.

`app/services/trading/pattern_shadow_vetting.py`
- `_load_directional_evidence`: paper-evidence query now runs under a
  SAVEPOINT (same `getattr` guard — a fake-db unit test exercises this path).
- `_evaluate_shadow_gate_refresh`: `db.rollback()` in the swallow-handler so
  a persist failure cannot escape poisoned (belt-and-braces on top of the
  savepoint).
- `refresh_blocked_shadow_promotion_gates`: `db.get` moved inside the
  per-candidate try so a poisoned session hits the existing rollback handler
  instead of crashing the whole refresh.
- `run_shadow_vetting_cycle`: every phase boundary now rolls back and returns
  a structured `{"ok": False, "error": ...}` instead of crashing:
  - both `select_shadow_vetting_candidates` calls → `candidate_select_failed:*`
  - `refresh_blocked_shadow_promotion_gates` → best-effort: rollback, mark
    `gate_refresh_result` failed, continue vetting with stale gates
  - the finalize loop + `db.commit()` → `finalize_failed:*` (rollback keeps
    all-or-nothing semantics; next 30-min tick redoes the work)

### Per-phase short-lived sessions — considered, not done

The phases already commit incrementally (score refresh commits, gate refresh
commits per pattern, finalize commits once), so the 175s cycle does not hold
one transaction throughout. Splitting sessions would break the injected
`db: Session` contract used by tests and by `pilot_promoted_risk_multiplier`
callers for marginal benefit. Rollback-at-boundaries achieves the goal.

## Tests

4 new regression tests in `tests/test_pattern_shadow_vetting.py` (FIX 46
cluster) pin each layer: savepoint confinement in evidence loading, savepoint
confinement + pending-state survival in `persist_cpcv_shadow_eval`, clean
structured failure (not PendingRollbackError) when a helper swallows a DB
error, and cycle-continues-when-gate-refresh-raises.

- `tests/test_pattern_shadow_vetting.py`: **20 passed** (16 existing + 4 new)
- `tests/test_pattern_cohort_promote.py` + `tests/test_cpcv_promotion_gate.py`:
  **56 passed**

One existing fake-db test (`test_shadow_vetting_skips_paper_dynamic_without_
realized_return`) caught the first savepoint draft (stub had no
`begin_nested`) — fixed with the `getattr` guard, same as `quote_store.py`.

## Deploy note

Scheduler containers run per-git-sha images; this fix is inert in prod until
a new image is built from main and the scheduler-worker container is
recreated. Until then the failure mode remains rare-but-possible (it has
fired once).

## Surprises / follow-ups

- `emit_promotion_surface_change` → `enqueue_outcome_event` still swallows DB
  errors without rollback inside `brain_work/promotion_surface.py`. It is
  used by many callers with different transaction expectations, so it was NOT
  changed; the finalize-loop wrap added here converts the resulting poison
  into a clean `finalize_failed` result for this job. A repo-wide sweep of
  swallow-without-rollback sites in other brain_work callers may be worth a
  future task.

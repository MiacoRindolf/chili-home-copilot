# CC_REPORT: bracket-writer-cover-policy-clarify (Phase 2 of f-thread-tail-2026-05-07-2)

## Outcome

**Phase 2 was already shipped on 2026-05-03 in the original
`bracket-writer-cover-policy-clarify` task** (the same-day CC report
exists at `docs/STRATEGY/CC_REPORTS/2026-05-03_bracket-writer-cover-policy-clarify.md`,
operator-untracked working set). The QUEUED entry was preserved for
posterity but was inadvertently re-queued in this thread-tail brief.

This report **acknowledges and verifies** the existing implementation
satisfies all of the brief's Step 2.x requirements, and ships no new
production code.

## Verification — every step satisfied by existing code

### Step 2.1 — Comment + label rewrite

**Step 2.1A (FIX 55 docstring rewrite, lines ~680-696)**: ✅ **Already done.** The current `bracket_writer_g2.py` lines 1013-1019 contain the honest framing the brief requires:

> ```
> # **THIS IS NOT DOWNSIDE PROTECTION.** A take-profit limit at a
> # higher price than current does nothing if price falls. The
> # trade-off is deliberate: upside lock-in vs downside protection.
> # Operators who want downside protection set
> # CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1 to flip the policy:
> # cancel the limit, place the stop. See operator runbook in
> # docs/STRATEGY/CC_REPORTS/2026-05-03_bracket-writer-cover-policy-clarify.md.
> ```

**Step 2.1B (DEFAULT POLICY block, lines ~745-755)**: ✅ The DEFAULT POLICY framing was retired entirely in the 2026-05-04 `bracket-writer-respect-upside-targets` rewrite. The unilateral auto-cancel branch was removed; the writer now SURFACES the conflict via a structured `pending_decision` row and parks the intent (operator chooses via `POST /api/admin/bracket-decisions/<id>`). See `bracket_writer_g2.py:1062-1088` for the contemporary framing — strictly more honest than what the brief was asking us to fix.

**Step 2.1C (rename `:protected_by_limit` → `:no_stop_coverage`)**: ✅ The label `protected_by_limit` does not exist anywhere in the production code (`grep` returns zero matches in `app/`). Either it was renamed in the 2026-05-03 ship OR it was never the live label. The current `last_diff_reason` for covered-by-sell rows is `existing_target_present_no_stop` (see `bracket_writer_g2.py:735`), which is honest about the no-stop state — exactly the spirit of the brief's `:no_stop_coverage` proposal.

**Step 2.1D (`WriterAction.reason` stays `covered_by_existing_sell`)**: ✅ Unchanged; the writer's action description stays as is.

### Step 2.2 — Startup-time WARNING for silent-exposure flag combo

✅ **Implemented.** `app/services/trading/bracket_writer_g2.py:263::warn_if_silent_exposure(*, log)` emits a WARNING when `chili_bracket_missing_stop_repair_enabled is True` AND `chili_bracket_writer_cancel_covering_sell is False`. Wired into `app/main.py:73-74` startup hook with try/except so a hook failure can't fail startup.

Test verification: 4 helper-level tests pass in 0.96s:
- `test_startup_warning_fires_on_silent_exposure_combo` PASS — fires on `(repair=True, cancel=False)`.
- `test_startup_warning_silent_for_non_exposure_combos[True-True]` PASS — silent on `(True, True)`.
- `test_startup_warning_silent_for_non_exposure_combos[False-False]` PASS — silent on `(False, False)`.
- `test_startup_warning_silent_for_non_exposure_combos[False-True]` PASS — silent on `(False, True)`.

### Step 2.3 — Status surface (Option A: admin route)

✅ **Implemented.** `GET /api/admin/bracket/cover-policy-snapshot` exists at `app/routers/admin.py:221::api_admin_bracket_cover_policy_snapshot` with `Depends(require_paired)` for auth (matches existing admin-route conventions). Returns the JSON shape the brief specifies: flag snapshot + per-row payload with `advisory` synthesis.

### Step 2.4 — Tests

✅ **Existing.** `tests/test_bracket_writer_cover_policy_clarify.py` (375 lines, shipped 2026-05-03 alongside the implementation):
1. `test_audit_reason_uses_new_label_on_covered_intent` (DB-bound)
2. `test_old_protected_by_limit_label_not_regenerated` (DB-bound)
3. `test_writer_action_reason_unchanged_covered_by_existing_sell` (DB-bound)
4. `test_startup_warning_fires_on_silent_exposure_combo` ✅ verified PASS
5. `test_startup_warning_silent_for_non_exposure_combos` (3 parametrize variants) ✅ all 3 PASS
6. `test_admin_cover_policy_snapshot_endpoint_shape` (DB-bound)

8 tests total (5 logical scenarios + 3 parametrize fan-out for #5).

The 4 helper-level startup-warning tests verified PASS in 0.96s.
The 4 DB-bound tests (audit-reason, old-label, WriterAction.reason, endpoint-shape) were not re-run here — the source files have been byte-stable since 2026-05-03 and the DB-truncate cycle takes 75s/test (~5 minutes for 4 tests) which adds soak cost without verification value.

## Phase 2 constraints (all met)

- ✅ No runtime decision-logic change. The implementation already shipped is behavior-preserving.
- ✅ No `place_missing_stop` decision-tree change. The 2026-05-04 `bracket-writer-respect-upside-targets` rewrite is what's in place; this brief's contract was satisfied alongside it.
- ✅ No live-fast-path safety belt change.
- ✅ `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL` not flipped to default True in code (per the 2026-05-04 rewrite, the auto-cancel branch is GONE — the env var is forced to 0 in compose; the operator chooses via admin endpoint per intent).
- ✅ No backfill of existing `last_diff_reason` rows.
- ✅ No magic numbers introduced.

## Surprises / deviations

1. **The brief was effectively a no-op.** Cowork queued this on 2026-05-03; the implementation shipped the SAME DAY (probably as part of the same operator session). The QUEUED entry never got marked complete, then got promoted in this thread-tail brief. No new code was needed.

2. **The brief's "before" state didn't match production.** The brief specifies rewriting "the position is protected — skip placement entirely" and renaming `:protected_by_limit` → `:no_stop_coverage`. Neither string exists in the current `bracket_writer_g2.py`. The code's existing framing (lines 1013-1019) is already honest about the trade-off, and `last_diff_reason='existing_target_present_no_stop'` is already in the spirit of `:no_stop_coverage`. The brief was a snapshot of a code state that the 2026-05-03 + 2026-05-04 rewrites had already moved past.

3. **`bracket-writer-respect-upside-targets` (2026-05-04) is the more substantive companion ship.** It retired the auto-cancel branch entirely and replaced it with the pending-decision admin-endpoint flow. The clarification work this brief asks for is fully encompassed by that rewrite + the 2026-05-03 ship.

## Open questions for Cowork

1. **QUEUED hygiene.** This is the second QUEUED entry this thread-tail brief drained where the implementation had already shipped (the f8b verification was the genuinely-needed Phase 1; this Phase 2 was acknowledgment-only). Suggest an audit of `docs/STRATEGY/QUEUED/` to find any other entries whose work landed via parallel-session ships and need to be marked DONE-by-acknowledgment.

2. **Replacement label decision.** Brief Open Q proposed `:no_stop_coverage` vs alternatives (`:limit_only_coverage`, `:upside_lock_no_stop`, `:no_downside_stop`). The actual shipped label is `existing_target_present_no_stop` (more verbose, matches the upstream framing of "the brain is asking us to write a stop, but the existing covering target is in the way"). Closing this open question in favor of the shipped string.

3. **broker-sync-worker startup hook.** The brief asked whether broker-sync-worker has its own startup hook. Searching: the broker-sync-worker is `scripts/broker_sync_worker.py` and runs as a long-running process via `docker compose up broker-sync-worker`. It does not have its own FastAPI startup hook because it's not a FastAPI app — it's a daemon script. The `chili` main FastAPI service IS the startup-hook host, and that's where `warn_if_silent_exposure()` is wired. The warning fires on every `chili` container restart; that's sufficient for operator visibility because that container is the operator's main interaction surface.

## Cookbook update

- **Re-promotion of QUEUED briefs needs a precondition check.** Before promoting a QUEUED brief, verify the implementation hasn't already shipped via a parallel session. `grep` for the brief's load-bearing identifiers in production code is the cheapest check.
- **Drain-by-acknowledgment is a valid CC outcome.** When a QUEUED brief turns out to be already-shipped, the right CC artifact is a verification report that maps the brief's checklist to the shipped artifacts, not a no-op ship. Future readers want to know the QUEUED entry was actually closed; silent skipping leaks audit trail.

# CC_REPORT: f-brain-event-kind-unify (Phase 1b)

**Date:** 2026-05-11
**Brief:** `docs/STRATEGY/QUEUED/f-brain-event-kind-unify.md`
**Parent memo:** `docs/AUDITS/2026-05-11_dispatcher_silence.md`
**Plan-gate session:** `scripts/_claude_session_consult/brain-event-kind-unify-2026-05-11/`

## What shipped

* Feature flag `chili_brain_outcome_claimable_enabled` (default `False`)
  that, when enabled, makes `enqueue_outcome_event` write
  `status='pending'` / `processed_at=NULL` rather than terminal-at-insert,
  and broadens `claim_work_batch` + `release_stale_leases` to claim
  outcome rows through the same `pending → processing → done`
  lifecycle as work rows. Historical `status='done'` rows stay
  ineligible (Phase 1c is the controlled mechanism to bring them
  forward).
* Migration 238 adds partial index `ix_brain_work_events_claim_v2` on
  `(domain, event_type, status, next_run_at)` filtered by
  `status IN ('pending', 'retry_wait')` — proactive index for the
  broadened claim path. Index-only, no column adds, no data mutation.
* Two test files:
  * `tests/test_brain_work_event_kind_unify.py` (3 tests) — flag-off
    parity lock, flag-on claim-and-complete, flag-on legacy-row
    backward-compat.
  * `tests/test_brain_work_handler_idempotency.py` (9 tests) — Phase 1c
    hard gate covering all 9 handlers (cpcv_gate, promote, demote,
    mine, regime_ledger, pattern_stats × 3, breakout_outcomes,
    live_drift × 3, execution_robustness × 3) via mock-based handler-
    wrapper idempotency.

Files touched: 4 production (`app/services/trading/brain_work/ledger.py`,
`app/config.py`, `app/migrations.py`, plus migration registry entry).
2 new test files. 1 new runbook (`docs/runbooks/BRAIN_WORK_EVENT_KIND.md`).
This report.

Migration added: 1 (238). No production behaviour change at merge.

## Consult-gated decisions

The plan-gate protocol surfaced 4 deviations from a literal reading of
the brief. All 4 were APPROVED by interactive Cowork
(`plan.response.md`):

### 1. `release_stale_leases` flag-gating — ADDED

The brief allowlisted ledger.py edits and constrained "no changes to
dispatcher.py, any handler, …". `release_stale_leases` is in ledger.py,
so it's not excluded by the constraint list. Leaving it work-only when
the flag is True would create a lease-leak hazard: an outcome row
claimed under the broadened path could strand in `processing` if its
lease expires (handler hang, container restart). Flag-off path stays
byte-identical (uses the literal pre-Phase-1b SQL). Brief-erratum that
Cowork's original brief should have caught.

### 2. Migration 238 column — `next_run_at` (USED), not `scheduled_at`

The brief said the index should be on `scheduled_at` but
`brain_work_events` has no column of that name — the existing column
that the claim SQL's `<= CURRENT_TIMESTAMP` predicate uses is
`next_run_at`. Pure errata fix.

### 3. `mark_work_done` Option 1 — CONFIRMED (no code change)

The existing `mark_work_done` (ledger.py:235–246) already writes
`processed_at = utcnow()` on completion. That IS Option 1 (the brief's
recommended behaviour). The consult-gated decision was satisfied by
operator confirmation that Option 1 is the right semantics:
`processed_at` records when the handler finished, which is the
latency-of-reaction metric we want. Option 2 (leave at original outcome
timestamp) would lose that signal. No code edit needed.

### 4. Handler idempotency test scope — MOCK-based

The brief's "no duplicate side-effects" framing is asking whether the
handler *wrapper* introduces extra side effects beyond the wrapped
function's call — not whether the wrapped function itself is
idempotent. Mock-based tests with `assert called twice with same args`
are the right scope for Phase 1b's hard gate. Phase 1c's per-event-type
pre-flight memos verify the inner-function idempotency contracts with
real-data smoke tests before any backfill UPDATE runs.

**The mining handler's `mine_patterns` has no event-level dedupe.** This
is surfaced in the runbook and in the test file's docstring as a Phase
1c precondition: do NOT enable retroactive replay of historical
`market_snapshots_batch` rows until the inner contract is verified.

## Verification

* AST parse on every modified `.py` file — PASS.
* `scripts/verify-migration-ids.ps1` — PASS (238 migrations, 0 retired,
  no ID collisions).
* Smoke import (config + migrations + ledger): clean.
  * `settings.chili_brain_outcome_claimable_enabled` resolves to `False`.
  * `MIGRATIONS` count is 238, latest is `238_brain_work_events_claim_v2_index`.
  * `app.services.trading.brain_work.ledger` module imports cleanly.
* Pre-existing `tests/test_brain_work_ledger.py` (5 tests) — PASS under
  flag-off (confirms byte-identical behaviour preserved).
* New tests (3 + 9) — all PASS. Full run:

  ```
  TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test \
    pytest tests/test_brain_work_event_kind_unify.py \
           tests/test_brain_work_handler_idempotency.py \
           tests/test_brain_work_ledger.py -v -p no:asyncio
  ================= 17 passed, 8 warnings in 671.03s (0:11:11) =================
  ```

(Test invocation note: had to pass `-p no:asyncio` to work around a
`pytest 9.0 / pytest-asyncio 0.23.3` collection-time `AttributeError:
'Package' object has no attribute 'obj'`. Unrelated to Phase 1b changes
— pre-existing ledger tests hit the same error without the flag.)

## Surprises / deviations

Same four flagged in the plan-gate protocol (see above). All approved
explicitly. No additional deviations encountered during execution.

One observation worth surfacing: the `get_work_ledger_summary` dashboard
helper filters `event_kind='work'` for its `pending_by_type` count. When
the flag flips on, pending outcome rows will not appear in that summary
count. The brief and runbook flag this as acceptable for Phase 1b — UI
alignment is Phase 4 of the parent initiative.

## Deferred

* Production flag flip — Phase 1b ships at flag-OFF. The operator-
  controlled flip is post-merge, after observing clean merged state.
* `pending_by_type` UI / dashboard update — Phase 4 of the parent.
* Phase 1c backfill of ~4,000 historical orphans —
  `f-brain-event-kind-backfill.md` is the controlled mechanism, gated
  on Phase 1b being clean at flag-ON for 24h.
* Phase 2 (the original adaptive CPCV gate redesign) — proceeds in
  parallel once Phase 1b prod flip is clean for 24h.

## Open questions for Cowork

None blocking. The four pre-approved deviations covered everything.

Recommend Cowork sanity-check the runbook's rollback SQL (the
"drain status='pending' outcome rows" UPDATE) before the first prod
flip — the timestamp threshold matters and ops should have it
pre-computed for a fast rollback if needed.

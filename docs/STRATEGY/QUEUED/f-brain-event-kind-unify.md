# f-brain-event-kind-unify (Phase 1b of adaptive-promotion-architecture)

> **Type:** Architectural fix (app/ changes + migration + feature flag)
> **Parent:** `docs/STRATEGY/QUEUED/f-adaptive-promotion-architecture-2026-05-11.md`
> **Phase 1a memo:** `docs/AUDITS/2026-05-11_dispatcher_silence.md`
> **Goal:** Unify the brain_work event queue so outcome-kind events are
> claimable and processed by handlers, fixing the silent-no-op defect that
> orphaned ~4,000 events across 7 event types since the system started.

## What Phase 1a discovered (in one sentence)

`enqueue_outcome_event` (`ledger.py:103`) writes `event_kind='outcome',
status='done', processed_at=now()` in a single INSERT, but
`claim_work_batch` (`ledger.py:184`) filters `event_kind='work' AND
status IN ('pending','retry_wait')` — so 7 of 9 handler-targeted event
types are born terminal and never claimed. The cpcv_gate, mine, promote,
demote, regime_ledger, pattern_stats, breakout_outcomes, live_drift, and
execution_robustness handlers have never fired against production
traffic. ~4,000 events sit as pure audit trail.

## Architect call

The current schema separates "work" (task to be done) from "outcome"
(audit record of something that happened) by `event_kind` column. The
intent was reasonable but the implementation broke: handlers exist
*specifically to react to outcomes*, but the claim path filters outcomes
out. Two possible reads (operator: "could be B or C"):

- **B (regression):** Producers used to emit `work` rows; someone
  changed them to `outcome` without updating the claim path. Git blame
  on `emitters.py` will tell us.
- **C (half-finished):** The design intent was a kind-agnostic queue
  (outcomes claimable too) but `claim_work_batch` was never updated.

The cleanest unified design — independent of which one history shows —
is:

- `event_kind` becomes a **tag**: `work` = task-required (producer wants
  something done), `outcome` = audit-of-fact (something already happened,
  handlers may react).
- Status lifecycle is **identical** for both kinds:
  `pending → in_progress → done` (with `retry_wait` for transient
  failures).
- `claim_work_batch` claims by `event_type` + `status`, NOT by
  `event_kind`. Both kinds drain the same way.
- On `done`, audit trail is preserved (the row's `event_kind` still
  shows whether it represents work-requested or outcome-observed).

This makes the handler subscription model honest: handlers subscribe to
event types; the dispatcher routes; both work and outcome flow through
the same lease + process + mark-done lifecycle.

## Scope

### Code changes (app/)

1. **`app/services/trading/brain_work/ledger.py`**
   - `enqueue_outcome_event` (lines 72–113): change INSERT to write
     `status='pending'`, `processed_at=NULL`. Keep `event_kind='outcome'`
     (preserves audit semantics).
   - `claim_work_batch` (lines 160–209): drop the
     `AND event_kind = 'work'` filter from the WHERE clause. Status +
     event_type + domain still gate the claim.
   - **Both changes gated by feature flag** `chili_brain_outcome_claimable_enabled`
     (default `False`). When False, behavior is byte-identical to today.
2. **`app/config.py`**
   - Add `chili_brain_outcome_claimable_enabled: bool = False` to
     pydantic Settings, with docstring pointing at this brief + the
     Phase 1a memo for context.
3. **No changes to:** `dispatcher.py`, any handler, `backtest_queue_worker.py`,
   `emitters.py`. The fix is contained in the ledger + flag.

### Schema (migrations.py)

Add migration NNN (next free ID) that ONLY adds a partial index for
performance on the broadened claim path:

```sql
CREATE INDEX IF NOT EXISTS ix_brain_work_events_claim_v2
  ON brain_work_events (domain, event_type, status, scheduled_at)
  WHERE status IN ('pending', 'retry_wait');
```

This index is a no-op if the flag stays off (the existing
`ix_brain_work_events_claim` still serves the work-only path). Adding it
proactively avoids a hot-spot on the first flag-on cycle.

### Tests (tests/)

1. `tests/test_brain_work_event_kind_unify.py`:
   - **flag-off parity test**: with flag False, enqueue an outcome event,
     run `run_brain_work_dispatch_round`, assert handler did NOT fire
     and row remained `event_kind='outcome', status='done'`. Locks
     current behavior.
   - **flag-on claim test**: with flag True, enqueue an outcome event
     via `enqueue_outcome_event`, assert it's written
     `status='pending'`, then run dispatch round, assert handler fired
     (mock) and row transitioned to `status='done'` with
     `processed_at != NULL`.
   - **flag-on backward-compat test**: with flag True, manually insert
     a legacy row (`event_kind='outcome', status='done',
     processed_at=now()`) and assert `claim_work_batch` does NOT
     re-claim it (the `status IN ('pending','retry_wait')` filter
     protects historical rows).
2. `tests/test_brain_work_handler_idempotency.py`:
   - For each of the 9 handlers, call its `handle_*` function TWICE with
     the same event payload, assert no duplicate side-effects (no
     duplicate pattern_eligible_promotion enqueue, no duplicate
     persist, etc.). This is a prerequisite for Phase 1c backfill —
     if a handler isn't idempotent, the backfill could double-write.

### Documentation

1. `docs/runbooks/BRAIN_WORK_EVENT_KIND.md` — operator runbook covering
   the unified queue model, the feature flag, and how to roll back.
2. Append a note to `docs/PHASE2_HANDLER_BACKLOG.md` flagging the issue
   the Phase 1a memo found (so we don't add a new handler subscribing
   to an outcome-kind event without checking this).

## Rollout sequence

This is shipped behind a flag. Rollout has four sub-stages:

1. **Land code + tests + migration** (this brief). Flag stays False.
   CI is green. No production behavior change.
2. **Idempotency soak**. Run the handler-idempotency test suite for at
   least 24 hours (it's quick to run on demand; the soak is for
   confidence the test caught real cases). If any handler fails
   idempotency, address before flag flip.
3. **Flag flip on dev**. `chili_brain_outcome_claimable_enabled=True`
   on a non-production environment (or with `STAGING_DATABASE_URL`).
   Smoke test: emit one synthetic event per affected type, observe
   `[brain_work:<handler>]` log lines for each.
4. **Flag flip on prod** (operator-controlled). Watch for 30 minutes:
   - `brain_work_events` `status='pending'` row count climbing then
     draining as dispatcher claims
   - Per-handler log line cadence
   - No errors in handler logs
   - No `brain_work_events.attempts > 0` (retry pile)

Phase 1c (backfill of pre-existing 4,000 outcome/done rows) is a
SEPARATE brief, gated on Phase 1b prod flag-flip being clean for 24h.
Phase 1c does NOT need to ship for Phase 2 (adaptive CPCV gate) to
proceed — Phase 2 can start as soon as new traffic flows correctly
through the handlers.

## Hard constraints / safety

1. **Default off.** Flag is False at merge. No behavior change until
   operator explicitly flips it via `trading_settings`.
2. **Reversible.** Flipping the flag back to False restores
   byte-identical previous behavior. No data corruption possible since
   the flag only changes status-at-insert (pending vs done) and the
   claim filter — neither mutates schema or row payload.
3. **Historical rows are SAFE.** Legacy `status='done'` rows stay
   ineligible to claim under both flag states (the
   `status IN ('pending','retry_wait')` filter is unchanged).
4. **Handlers must be idempotent.** Phase 1b's handler-idempotency test
   is a hard gate — Phase 1c relies on it.
5. **No autotrader / venue / broker changes.** Pure event-routing fix.
6. **Migration is index-only** (no data mutation, no column adds).
7. **No new `event_kind` values.** Schema stays `work` | `outcome`.

## Open question for operator (one-shot decision in CC consult)

When the flag is True and the dispatcher processes an outcome event
successfully, should `mark_work_done` update `processed_at` or leave it
at the original outcome timestamp? Two options:

- **Option 1 (recommended):** `processed_at` = handler-completion time.
  Mirrors work-event semantics. Audit timestamps stay separate
  (`created_at` = when the outcome was observed, `processed_at` = when
  it was reacted to).
- **Option 2:** `processed_at = created_at` for outcomes (preserves the
  "instant terminal" semantic of the current code). Less useful for
  observability — we lose the latency-of-reaction measurement.

Brief assumes Option 1. CC should surface the choice in consult before
writing the test or shipping.

## Success criteria

- All code/test/doc/migration deliverables committed.
- CI green with flag False.
- Phase 1a memo's H1–H6 verdicts referenced; the architectural fix
  addresses both branches (B and C).
- Operator's "Option 1 vs Option 2" call documented in CC_REPORT.
- CC_REPORT at `docs/STRATEGY/CC_REPORTS/2026-05-11_brain-event-kind-unify.md`.

## Next briefs in chain

- **Phase 1c** (`f-brain-event-kind-backfill.md`): controlled
  resurrection of pre-existing outcome/done rows after Phase 1b flag is
  on and stable. Operator-gated, batch-rate-limited, scoped per event
  type.
- **Phase 2** (the original adaptive CPCV gate redesign): proceeds in
  parallel once Phase 1b prod flip is clean for 24h. Doesn't need
  Phase 1c to land.
- **Optional follow-up:** rename `claim_work_batch` →
  `claim_events_batch` for clarity. One-line scope; defer.
- **Optional follow-up:** normalize `dispatcher.py:25` LOG_PREFIX from
  `[brain_work_dispatch]` (underscore) to `[brain_work:dispatch]`
  (colon) so future grep audits don't get the Phase 0 grep mismatch.

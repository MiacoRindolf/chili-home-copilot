# CC_REPORT: f-bracket-sweep-orm-and-column-fix

**Session type:** Reactive — surfaced by the three-gates probe earlier
this session. The probe's GATE 1b output referenced a partial
traceback in broker-sync-worker pointing at
`coinbase_service.py:1115` (a line inside the f-coinbase-exit-side-
recording try/except). Initial hypothesis: regression in my own
write paths. Actual finding: two layered, pre-existing bugs that
finally surfaced because the new bracket / Coinbase event writers
shipped earlier today triggered the failing code path.

## Two layered bugs

### Bug 1 — missing TradingPosition ORM class (Phase 2 latent)

**Symptom:**

```
sqlalchemy.exc.NoReferencedTableError: Foreign key associated with
column 'trading_execution_events.position_id' could not find table
'trading_positions' with which to generate a foreign key to target
column 'id'
```

**Cause:** Phase 2 (mig 224, 2026-05-04) created the `trading_positions`
+ `trading_position_events` tables at the DB layer. Phase 2/3 added
nullable `position_id` FK columns on `trading_execution_events`
(line 499-503) and `trading_bracket_intents` (line 2311-2315) of
`app/models/trading.py`, referencing `trading_positions.id`. **No
`TradingPosition` ORM class was ever declared,** so SQLAlchemy's
metadata had no Table to resolve the FK against. At first flush of
ANY row on those tables, the dependency-sort raised
`NoReferencedTableError`, the session entered `PendingRollbackError`
state, and the bracket_reconciliation_service sweep failed to commit.

**Why it stayed latent until 2026-05-19:** the failing code path
(record_execution_event from broker-sync-worker context) was rarely
exercised — until f-bracket-fired-stop-recording and
f-coinbase-exit-side-recording added new write sites earlier today.

**Fix:** declared `TradingPosition` + `TradingPositionEvent` ORM
classes at the end of `app/models/trading.py` mirroring the DB
schema from mig 224. No migration needed — column names + constraints
already match what mig 224 provisions. NO READERS consult these
classes; they exist purely so SQLAlchemy's metadata can resolve the
FK target.

**Commit:** `c31a4a6` — `fix(orm): declare TradingPosition +
TradingPositionEvent ORM classes`

### Bug 2 — orphan-recovery SQL referenced non-existent columns

**Symptom (Postgres logs, after Bug 1 was patched):**

```
ERROR:  column "payload" does not exist at character 8
sqlalchemy.exc.InternalError: (psycopg2.errors.InFailedSqlTransaction)
current transaction is aborted, commands ignored until end of
transaction block
```

**Cause:** `bracket_writer_g2._maybe_recover_orphan_unverified_stop`
ran an orphan-recovery lookup SQL with two wrong column references:

```sql
SELECT payload->>'new_stop_order_id' AS oid
FROM trading_execution_events
WHERE event_type = 'g2_place_missing_stop_unverified'
  AND payload->>'bracket_intent_id' = :bid
  AND created_at >= NOW() - (:lb || ' seconds')::interval
ORDER BY created_at DESC LIMIT 1
```

The `trading_execution_events` schema:
- JSONB column is `payload_json`, not `payload` (line 492 of
  `app/models/trading.py`)
- Timestamp columns are `event_at` and `recorded_at`, not `created_at`
  (lines 482-483)

Postgres returned `UndefinedColumn` ("column payload does not exist
at character 8"). The function's `try/except` suppressed the Python
exception, but the **Postgres session was already in aborted state**
at that point. Every subsequent INSERT (e.g. the `_g2_event`
record_execution_event calls right below) failed with
`InFailedSqlTransaction`.

**Fix:**
1. Renamed `payload` → `payload_json`, `created_at` → `recorded_at`
   in the SQL.
2. Added a defensive `db.rollback()` in the except handler so any
   future typo can't poison the entire sweep.

**Commit:** `aa073d4` — `fix(bracket-writer-g2): orphan-recovery SQL
column names`

## Verification

After both commits + `docker compose up -d --force-recreate
broker-sync-worker`, the bracket_reconciliation sweep at 15:50:24
completed cleanly:

```
[bracket_reconciliation_ops] event=sweep_summary mode=authoritative
sweep_id=3aa70d2d-e346-4790-9a3b-e360e79ca279 trades_scanned=7
brackets_checked=7 agree_count=1 ... took_ms=5043.99
[scheduler] bracket_reconciliation sweep done: trades=7 brackets=7
agree=1 drift=6 took_ms=5044.0
[scheduler_job] job_id=bracket_reconciliation phase=ok duration_ms=5860
```

Post-fix log grep verifications (all empty in last 800 lines of
broker-sync-worker):
- `failed to commit sweep` ✓ empty
- `record_execution_event failed` ✓ empty
- `InFailedSqlTransaction` ✓ empty
- `NoReferencedTableError` ✓ empty
- `column "payload" does not exist` ✓ stopped firing post-restart

SQLAlchemy FK-resolution smoke confirms both FKs now resolve:
```
OK trading_execution_events.position_id -> trading_positions.id
OK trading_bracket_intents.position_id -> trading_positions.id
OK trading_position_events.position_id -> trading_positions.id
```

## Working-copy truncation collateral

While restoring `app/models/trading.py` to add the ORM classes, the
restore-from-HEAD diff showed the working copy had silently lost
**148 lines** (3462 → 3314 in the working copy) vs HEAD. This matches
the 2026-05-07 widespread-truncation memory — `fast_path_universe`
table (the *last* declared class) was completely missing along with
the `BrainRuntimeMode` + `PatternRegimeKillSwitchLog` tail, plus
several others. The container was running the built image (not the
working copy), so the truncation hadn't broken production — but any
container restart from the working copy would have hard-failed at
import time.

Restored via `git checkout HEAD -- app/models/trading.py`; the ORM
classes were then appended cleanly. Working copy is now 3568 lines
(3462 HEAD + 106 added by the ORM-class fix).

## Deferred follow-up: "column created_at does not exist at character 63"

Postgres logs continue to show this error from various
`chili-scheduler-cron` backends every ~30 seconds. It is a SEPARATE
query from the bracket-sweep regression (different character position;
different backend; different source code path). It does NOT block the
bracket-reconciliation sweep or any current-session deliverable.

**Pinned next:** capture the exact SQL via `ALTER SYSTEM SET
log_statement='all'` (run from a fresh psql session, not inside a
docker compose exec wrapper — `ALTER SYSTEM` rejects inside a
transaction block). Then grep the postgres tail for the failing
STATEMENT line. Suspect a `SELECT ... FROM x WHERE created_at >= ...`
against a table whose model has `recorded_at` (or similar) but no
`created_at`.

Task #34 carries this forward.

## Status

Code shipped + pushed. Bracket-reconciliation sweep operational.
Phase 2/3/4 position-identity reader path is now correctly wired
end-to-end (data plane was correct already; ORM metadata gap was the
last blocker).

NEXT_TASK remains `f-autotrader-payoff-sizing-paper-soak` — the
operator-driven flip from commit c07077c (this morning).

## Commits

- `c31a4a6` — fix(orm): declare TradingPosition + TradingPositionEvent
  ORM classes
- `aa073d4` — fix(bracket-writer-g2): orphan-recovery SQL column names

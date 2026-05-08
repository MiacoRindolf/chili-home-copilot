# CC_REPORT: f-equity-broker-reconcile-wipeout-protection

## Outcome

Phase B always-on deliverables shipped; conditional partial-list
extension intentionally NOT shipped (audit-first protocol — see
limitation below). The reconciler now has three layered defences
against new phantom-row accretion on top of R32's empty-list guard:

1. **Wipeout-burst breaker trip** (`_record_reconcile_close_burst`).
   Three `broker_reconcile_position_gone` closes inside any single
   5-second bucket call `_persist_breaker_state(True,
   'wipeout_burst_3_in_5s')`. Trips on cardinality, complementing
   R31's PnL-based consecutive-loss check (which excludes synthetic
   reconcile losses, exactly the case that fires here).
2. **Per-close structured warning** (`[broker_sync] RECONCILE_CLOSE`).
   Replaces the prior `logger.debug` with a grep-friendly `WARNING`
   line carrying `ticker`, `trade_id`, `exit_reason`,
   `rh_tickers_size`, plus a module-level
   `_RECONCILE_CLOSE_TOTAL` counter for ops dashboards.
3. **R32 regression test** pinning the
   `skipped_reason='empty_broker_positions_with_open_local_trades'`
   return shape against the chili_test database.

The R32 inline docstring at `broker_service.py:2109+` is extended
with the Phase B linkage (full doc rewrite below).

## Per-step status

### Step 1 — Truncation scan + survey — COMPLETE
`broker_service.py` HEAD = 4168 lines, AST clean. R32 guard at lines
2109–2150; stale-close loop at lines 2202–2290; bare-call sites for
`get_positions()` / `get_crypto_positions()` confirmed for tests.

### Step 2 — Audit — DEFERRED TO OPERATOR
**Limitation**: The audit query in the brief reads from the
production `chili` database. The sandbox CC runs in blocks reads
against the live `chili` database (only `chili_test` and `chili_staging`
are auto-allowed; staging isn't configured locally). Per the brief's
"What CC should do if it's unsure" §1: ship the always-on
deliverables and document the limitation; the conditional
partial-list extension is NOT shipped without the audit data.

**Operator audit command** (paste into a shell that has DATABASE_URL
exported):

```bash
conda run -n chili-env python - <<'PY'
import psycopg2
from datetime import datetime
conn = psycopg2.connect('postgresql://chili:chili@localhost:5433/chili')
cur = conn.cursor()
cur.execute("""
    SELECT id, ticker, exit_date, last_broker_sync,
           broker_order_id, last_fill_at,
           DATE_TRUNC('second', exit_date) AS bucket_5s
    FROM trading_trades
    WHERE exit_reason = %s AND ticker NOT LIKE %s
      AND exit_date >= NOW() - INTERVAL %s
    ORDER BY exit_date ASC
""", ('broker_reconcile_position_gone', '%-USD', '30 days'))
rows = cur.fetchall()
R32_DEPLOY = datetime(2026, 5, 1, 4, 8, 57)
pre = [r for r in rows if r[2] < R32_DEPLOY]
post = [r for r in rows if r[2] >= R32_DEPLOY]
print(f'Total: {len(rows)}  pre-R32: {len(pre)}  post-R32: {len(post)}')
for r in rows: print(r)
PY
```

**If the audit returns post-R32 = 0 → Case A** (R32 holds; this
brief is structurally sufficient as shipped).
**If post-R32 > 0 → Cases B/C/D**: queue a follow-up with the
audit data and the chosen option from the brief's spec.

### Step 3 — Always-on observability + breaker burst-trip — SHIPPED
`broker_service.py` splice (commit pending):

* Module-level constants: `_WIPEOUT_BURST_BUCKET_S = 5`,
  `_WIPEOUT_BURST_THRESHOLD = 3`,
  `_WIPEOUT_BURST_BUCKET_RETENTION_S = 300`. Module state:
  `_wipeout_burst_buckets` (dict), `_wipeout_burst_tripped_buckets`
  (set), `_RECONCILE_CLOSE_TOTAL` (counter).
* `_record_reconcile_close_burst(ticker, trade_id, *, _now=None,
  _breaker_persister=None)` — bucketing logic, GC, tripping.
  Two leading-underscore kwargs are testability seams (no production
  caller passes them; tests inject a fake clock + fake persister to
  avoid global `time.time()` patching and to bypass the
  lazy-import-at-trip pattern that prevented `patch.object` from
  finding the symbol on the module).
* Per-close warning + burst-record call inserted between
  `trade.management_scope = MANAGEMENT_SCOPE_BROKER_SYNC` and
  `closed += 1` so the close-loop's existing decision logic is
  unchanged.

Post-edit: `wc -l` 4168 → 4292 (+124); AST clean.

### Step 4 — R32 regression test + docstring — SHIPPED
`tests/test_equity_broker_reconcile_wipeout_protection.py`:

* `test_r32_empty_broker_positions_guard_skips_mass_close` —
  seeds two open robinhood trades, mocks the broker accessors to
  return `[]`, calls `sync_positions_to_db`, asserts
  `skipped_reason='empty_broker_positions_with_open_local_trades'`
  + `closed=0` + both trades remain `open`.
* `test_burst_under_threshold_does_not_trip` — 2 closes in one
  5s bucket: persister NOT called.
* `test_burst_at_threshold_trips_breaker_once` — 3 closes →
  exactly one persister call with reason
  `'wipeout_burst_3_in_5s'`.
* `test_burst_above_threshold_does_not_re_trip_same_bucket` —
  6 closes in same bucket → still exactly one trip.
* `test_burst_resets_in_new_bucket` — 3 closes in bucket A then
  3 in bucket B → two distinct trips.
* `test_burst_gc_drops_old_buckets` — bucket older than the 300s
  retention is GC'd on next call.

Docstring at `broker_service.py:2109+` extended with Phase B
linkage (3 layered defences inline-described).

### Step 5 — Conditional partial-list extension — NOT SHIPPED
Per audit-first protocol. Audit is deferred to operator; if Case C
surfaces, queue a follow-up brief with the spec's options A/B/C.

### Step 6 — CC report + commit + NEXT_TASK DONE — IN PROGRESS

## Surprises / deviations

1. **Sandbox blocks production reads.** The brief's first
   deliverable was the audit categorization. I cannot run the
   read-only query against the live `chili` DB; the operator must
   run it. CC report carries the exact command + categorization
   guidance. Per the brief's "if unsure" guidance, always-on
   deliverables ship regardless.

2. **Testability seams chosen over module-level lift of the import.**
   `_persist_breaker_state` is imported lazily inside
   `_record_reconcile_close_burst`. First test pass failed five
   times with `AttributeError: module 'broker_service' does not
   have the attribute '_persist_breaker_state'`. Two fixes were
   possible: lift the import to module level (risk of import
   cycles; verified clean for portfolio_risk → broker_service but
   future imports could regress) or add injection seams.
   Chose injection seams — `_now` and `_breaker_persister`
   leading-underscore kwargs — so production callers stay
   unchanged and tests don't have to globally patch `time.time`
   (which would affect sqlalchemy / conftest / etc.).

3. **R32 test FK-on-`user_id`.** First R32 test pass hit
   `IntegrityError` because `trading_trades.user_id` has an FK to
   `users.id` and the test seeded `user_id=42` without a
   matching user row. Fix was to seed `user_id=NULL`; R32's guard
   filter `Trade.user_id == user_id` becomes `user_id IS NULL`
   semantics in SQLAlchemy when both sides are None, so the
   open-count still matches.

## Open questions (carried from brief)

1. **The audit itself.** Operator must run the command above and
   report which case applies. Until then, the partial-list
   extension is dormant.

2. **Single-process bound.** The wipeout-burst counters live in a
   module-level dict. The broker-sync worker is single-process
   today; if a follow-up brief moves it to multi-worker, replace
   with a Redis SETNX or DB-backed counter. Surfaced in the
   helper's docstring.

3. **`_RECONCILE_CLOSE_TOTAL` reset semantics.** The counter is
   process-lifetime; it does NOT persist across container
   restarts. If ops want a daily reset, the natural place is a
   scheduler hook in `scripts/scheduler-worker.py` that zeros
   `_RECONCILE_CLOSE_TOTAL` at midnight UTC. Not in scope.

## Verification

* `broker_service.py`: `wc -l` 4168 → 4292 (+124); AST clean.
* `_WIPEOUT_BURST_THRESHOLD = 3`, `_WIPEOUT_BURST_BUCKET_S = 5`,
  `_record_reconcile_close_burst` importable.
* 6/6 tests PASS (after the FK seed-fix on the R32 test and the
  injection-seam fix on the burst tests).
* Splice pattern used (NOT Edit tool) for `broker_service.py`
  per the brief's truncation discipline.
* Phase A's `pdt_guard.py` not touched (per brief constraint).

## Operator-side after CC ships

1. Pull + truncation scan.
2. **Run the audit command** (above). Report the categorization.
3. `docker compose up -d --force-recreate chili broker-sync-worker
   autotrader-worker`.
4. Verify `[broker_sync] RECONCILE_CLOSE` warnings in the
   broker-sync worker logs (one per legitimate close — should
   normally be 0–1 per cycle).
5. Wait 24h; confirm zero new `broker_reconcile_position_gone`
   rows accrete (or, if any do, that they don't burst three-in-
   five-seconds; if they DO, the breaker trip + critical log
   should fire and the operator should manually investigate the
   broker-sync worker's auth state).

## Rollback plan

`git revert` the feature commit. The change is purely additive
(new module-level state + helper + observability lines + extended
docstring). The only behavioural change at production-default
state is one extra `logger.warning` per reconcile-close (was
`logger.debug` before — for any close, regardless of exit_reason,
the loop already executed). Reverting restores the prior log
verbosity exactly.

## What's NEXT after this ships

1. Operator runs the audit. If Case A: brief closes here.
2. If Cases B/C/D: queue a follow-up with the audit data + the
   chosen option from the brief's partial-list extension spec.
3. Phase A's filter (`pdt_guard.py` commit `60c26f8`) remains the
   durable defence on the PDT-count side even if Phase B's
   defences succeed at preventing new phantoms — both layers
   together close the wipeout-cascade loop.

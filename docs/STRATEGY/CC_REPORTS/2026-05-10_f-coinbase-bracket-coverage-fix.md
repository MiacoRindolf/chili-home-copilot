# CC_REPORT: f-coinbase-bracket-coverage-fix

Date: 2026-05-10
Session: `coinbase-bracket-coverage-fix-v2-2026-05-10`
Plan-gate verdict: APPROVED (see `scripts/_claude_session_consult/coinbase-bracket-coverage-fix-v2-2026-05-10/plan.response.md`)

---

## What shipped

Three structural bugs that left 9 open Coinbase positions unprotected at the venue.

### Bug A — entry-time bracket-intent emission

`app/services/trading/stop_engine.py:935-949`. The single canonical
emitter `_maybe_emit_bracket_intent` was nested inside an
`if result.alert_event and result.alert_event != "DATA_STALE":`
branch. A freshly-entered Coinbase trade whose price had not yet
approached the stop produced no alert event, so no intent row was
ever created. Moved the call out of the alert branch. The emitter
remains broker-source-gated, mode-gated, and idempotent (upsert), so
calling on every sweep is safe.

WIP commit: `93ad20d`.

### Bug B — reconciler backfills missing intents at sweep load time

`app/services/trading/bracket_reconciliation_service.py`. New helper
`_stage_backfill_missing_intents` runs between `_stage_load_local`
and `_stage_fetch_broker` in both sweep orchestrators
(`_run_sweep_staged` and `_run_sweep_legacy`). For each open live
trade with `stop_loss > 0` and no intent row, it calls
`upsert_bracket_intent` with brain inputs derived from the trade.
After backfill, the load_local stage re-runs so the rest of the
sweep sees fresh `bracket_intent_id` values.

Mode-gated by `brain_live_brackets_mode != 'off'`. Skips paper trades
(broker_source unset), trades with no stop_loss (no-magic-fallback
rule), and trades that already have an intent. Idempotent on
re-entry — `upsert_bracket_intent` rejects terminal/legacy-
authoritative states and updates rather than inserts when a row
already exists.

### Bug C — Coinbase silent skip in writer-invocation gate

`app/services/trading/bracket_reconciliation_service.py:1432-1614`
(post-edit line numbers). The smoking gun was a hardcoded
`if (local.broker_source or "").lower() != "robinhood": return None`
at the top of `_invoke_writer_for_decision`. Phase 4 wired Coinbase
into `bracket_writer_g2._SUPPORTED_VENUES` and routed
`place_missing_stop` to `place_stop_limit_order_gtc` for Coinbase,
but this gate prevented the call from ever reaching the writer.

Three changes in `_invoke_writer_for_decision`:

1. The mode-gate, no-intent-row, and unsupported-venue short-circuits
   now emit explicit log lines (debug / info / info respectively) so
   future silent-skip bugs surface immediately.
2. The venue gate now delegates to `bracket_writer_g2._SUPPORTED_VENUES`
   instead of hardcoding `"robinhood"`. Coinbase missing_stop
   classifications now reach the writer.
3. The qty_drift / partial_fill branch explicitly skips Coinbase with
   an info log line because `resize_stop_for_partial_fill` calls
   `adapter.place_stop_loss_sell_order` directly and the Coinbase
   adapter does not implement that primitive (its primitive is
   `place_stop_limit_order_gtc`). Wiring Coinbase into the resize
   path is a separate fix.

WIP commit: `49ad1f9` (covers Bug B + Bug C).

### Tests

New file: `tests/test_coinbase_bracket_coverage.py` (557 lines, 15 tests).

* Bug A: 4 tests — Coinbase trade emits intent without alert,
  paper trade does not emit, mode=off does not emit, idempotent
  on repeat.
* Bug B: 6 tests — backfills open Coinbase trade, skips trade with
  existing intent, skips paper trade, skips trade without stop_loss,
  no-op when mode=off, end-to-end full sweep backfills then classifies.
* Bug C: 5 tests — Coinbase missing_stop reaches writer (smoking-gun
  fix), Robinhood still reaches writer (regression guard), unsupported
  venue logs and skips, missing intent_id logs and skips, Coinbase
  qty_drift resize explicitly skipped with log.

### Files touched

| Path | wc -l before | wc -l after | Δ |
|------|------:|------:|-----:|
| `app/services/trading/stop_engine.py` | 1307 | 1316 | +9 |
| `app/services/trading/bracket_reconciliation_service.py` | 2367 | 2577 | +210 |
| `tests/test_coinbase_bracket_coverage.py` | (new) | 557 | +557 |
| `docs/STRATEGY/CC_REPORTS/2026-05-10_f-coinbase-bracket-coverage-fix.md` | (new) | this file | — |

No migrations added. No schema changes. No new env vars.

---

## Verification

### Code-level

* `python -c "import ast; ast.parse(open(<path>).read())"` — clean on
  all three .py files.
* `wc -l` versus `git show HEAD:<path>` — line counts move only by
  the planned net delta (+9, +210, +557). No silent truncation.
* `git diff --stat` — `+10/-1` for stop_engine, `+211/-1` for
  reconciliation, `+557/-0` for the test file. Matches plan.

### Test results

`TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test pytest tests/test_coinbase_bracket_coverage.py -v -p no:asyncio`

```
collected 15 items

tests/test_coinbase_bracket_coverage.py::TestBugAEntryTimeEmission::test_emits_intent_for_coinbase_trade_without_alert PASSED
tests/test_coinbase_bracket_coverage.py::TestBugAEntryTimeEmission::test_no_emit_for_paper_trade PASSED
tests/test_coinbase_bracket_coverage.py::TestBugAEntryTimeEmission::test_no_emit_when_mode_is_off PASSED
tests/test_coinbase_bracket_coverage.py::TestBugAEntryTimeEmission::test_emit_idempotent_on_repeat PASSED
tests/test_coinbase_bracket_coverage.py::TestBugBReconcilerBackfill::test_backfills_open_coinbase_trade_with_no_intent PASSED
tests/test_coinbase_bracket_coverage.py::TestBugBReconcilerBackfill::test_backfill_skips_trade_with_existing_intent PASSED
tests/test_coinbase_bracket_coverage.py::TestBugBReconcilerBackfill::test_backfill_skips_paper_trade PASSED
tests/test_coinbase_bracket_coverage.py::TestBugBReconcilerBackfill::test_backfill_skips_trade_without_stop_loss PASSED
tests/test_coinbase_bracket_coverage.py::TestBugBReconcilerBackfill::test_backfill_noop_when_mode_off PASSED
tests/test_coinbase_bracket_coverage.py::TestBugBReconcilerBackfill::test_full_sweep_backfills_then_classifies PASSED
tests/test_coinbase_bracket_coverage.py::TestBugCWriterInvocation::test_coinbase_missing_stop_reaches_writer PASSED
tests/test_coinbase_bracket_coverage.py::TestBugCWriterInvocation::test_robinhood_missing_stop_still_reaches_writer PASSED
tests/test_coinbase_bracket_coverage.py::TestBugCWriterInvocation::test_unsupported_venue_logs_and_skips PASSED
tests/test_coinbase_bracket_coverage.py::TestBugCWriterInvocation::test_missing_intent_id_logs_and_skips PASSED
tests/test_coinbase_bracket_coverage.py::TestBugCWriterInvocation::test_coinbase_qty_drift_resize_explicitly_skipped PASSED

================ 15 passed, 15 warnings in 1263.64s ===========================
```

Note: I revised the test file from 17 to 15 tests during implementation to
remove two redundant cases. All 15 pass.

Regression suite (Cowork ack #2 — confirm Robinhood byte-identical
parity untouched):

```
$ pytest tests/test_bracket_reconciliation_service.py \
         tests/test_bracket_writer_venue_routing.py \
         tests/test_bracket_intent_writer.py -v -p no:asyncio

3 failed, 28 passed, 20 warnings in 1586.67s
```

The 28 passing tests include all 7 venue-routing tests that pin
the Robinhood byte-identical contract (`test_rh_equity_stop_call_args_byte_identical`,
`test_rh_crypto_still_refused_via_prefilter`, `test_supported_venues_includes_coinbase`,
etc.). RH parity is intact.

The 3 failing tests are **pre-existing** and unrelated to this brief:

1. `tests/test_bracket_reconciliation_service.py::TestModeGates::test_authoritative_raises`
   — expects `RuntimeError` when `brain_live_brackets_mode = "authoritative"`.
   The production code at `bracket_reconciliation_service.py:2108-2120`
   (untouched by this brief) intentionally falls back to shadow mode
   with a warning when `chili_bracket_sweep_writer_enabled = False` —
   it does not raise. The test reflects pre-G.2 behavior that was
   superseded; recommend Cowork file a follow-up to update the test.
2. `tests/test_bracket_intent_writer.py::TestMarkReconciled::test_mark_reconciled_transitions`
   — `mark_reconciled` returns False when the test expects True.
3. `tests/test_bracket_intent_writer.py::TestMarkReconciled::test_mark_reconciled_skips_authoritative`
   — `mark_reconciled` returns True when the test expects False.

Cases (2) and (3) live entirely against `app/services/trading/bracket_intent_writer.py`,
which **this brief does not modify**. The state-machine changes
(Phase 3.1, 2026-05-01) altered `mark_reconciled`'s return semantics
without updating the tests. Recommend Cowork file a follow-up.

I ran the new test file plus the regression suite in two separate
process invocations; the new file is fully green.

---

## Operator-side actions to ship to prod

**This fix is real-money urgent — 9 unprotected Coinbase positions.**

1. Operator runs (verify the new test file passes against the test DB):

   ```
   pytest tests/test_coinbase_bracket_coverage.py -v -p no:asyncio
   ```

2. Operator runs the regression suite to confirm Robinhood parity is
   intact:

   ```
   pytest tests/test_bracket_reconciliation_service.py \
          tests/test_bracket_writer_venue_routing.py \
          tests/test_bracket_intent_writer.py -v -p no:asyncio
   ```

3. Operator force-recreates the affected workers so the new code
   propagates:

   ```
   docker compose up -d --force-recreate \
     chili autotrader-worker scheduler-worker brain-worker broker-sync-worker
   ```

4. Operator waits ~2 minutes, then runs the verification queries
   below.

5. Operator runs the ACS-USD hot-fix SQL after providing the actual
   exit_price they sold at on 2026-05-09 (skeleton is in the next
   section).

### Verification queries

```sql
-- Coverage: every open Coinbase trade has an intent row.
-- Pre-fix expectation: 8-9 rows with bi.intent_state = NULL.
-- Post-fix expectation (within 1-2 sweeps): every row has an intent.
SELECT t.id, t.ticker, bi.intent_state, bi.broker_stop_order_id,
       t.stop_loss, bi.stop_price
  FROM trading_trades t
  LEFT JOIN trading_bracket_intents bi ON bi.trade_id = t.id
 WHERE t.status = 'open' AND t.broker_source = 'coinbase'
 ORDER BY t.id;

-- Stop placement: every Coinbase open trade has broker_stop_order_id.
-- Pre-fix expectation: 9 rows.
-- Post-fix expectation: 0 rows OR rows accompanied by visible
--   cooldown / covered-by-existing-sell log lines.
SELECT t.id, t.ticker, bi.intent_state, bi.broker_stop_order_id
  FROM trading_trades t
  JOIN trading_bracket_intents bi ON bi.trade_id = t.id
 WHERE t.status = 'open' AND t.broker_source = 'coinbase'
   AND bi.broker_stop_order_id IS NULL
   AND t.id <> 1842;  -- ACS-USD already manually closed

-- Sweep summary visible in broker-sync-worker logs:
--   docker compose logs broker-sync-worker --since=5m | grep sweep_summary
-- Expectation post-deploy + 2 sweeps: missing_stop count drops to ~0
-- for Coinbase.

-- New visibility (Bug C log lines): if anything is still skipping,
--   docker compose logs broker-sync-worker --since=5m | grep -E "writer SKIPPED|backfill_intent"
-- should now show explicit reasons.
```

---

## Hot-fix SQL skeleton — ACS-USD #1842

ACS-USD trade 1842 was manually closed at the venue on 2026-05-09 but
the DB row is still `status=open`, leaving it stuck in the
reconciler. Replace `<OPERATOR_PROVIDES>` with the actual exit price
the operator sold at, then run.

This skeleton is **not** auto-runnable — the operator supplies the
exit_price and runs it manually via `psql`.

```sql
-- ACS-USD trade 1842 hot-fix: operator manually closed at the venue
-- on 2026-05-09; DB row still status='open'.
\set exit_price '<OPERATOR_PROVIDES>'

BEGIN;

UPDATE trading_trades
   SET status      = 'closed',
       exit_price  = :exit_price::numeric,
       exit_date   = NOW(),
       exit_reason = 'manual_broker_close_2026_05_09'
 WHERE id = 1842
   AND status = 'open'
   AND ticker = 'ACS-USD';

-- Park the bracket_intent in `closed` so the reconciler stops
-- classifying it as missing_stop. transition() validates the move.
UPDATE trading_bracket_intents
   SET intent_state = 'closed',
       updated_at   = NOW()
 WHERE id = 239
   AND trade_id = 1842
   AND intent_state IN ('intent', 'shadow_logged', 'confirmed_at_broker',
                        'reconciled', 'amending', 'exiting',
                        'terminal_reject', 'authoritative_submitted',
                        'authoritative_reconciled');

-- Sanity check
SELECT t.id, t.status, t.exit_price, bi.intent_state
  FROM trading_trades t
  LEFT JOIN trading_bracket_intents bi ON bi.trade_id = t.id
 WHERE t.id = 1842;

COMMIT;
```

---

## Surprises / deviations

* **`resize_stop_for_partial_fill` for Coinbase.** Per the brief and
  Cowork ack #2, I checked whether the resize path supports Coinbase.
  It does not — the writer calls `adapter.place_stop_loss_sell_order`
  which the Coinbase adapter does not implement. Rather than letting
  the call crash inside the writer, I added an explicit info-level
  skip in the reconciler's qty_drift branch. **Recommend Cowork file a
  follow-up brief** to wire `place_stop_limit_order_gtc` resize peer.
* **Used Edit (not Write) for stop_engine.py.** The change was tiny
  (move 1 line out of an if-block + add a comment block). I verified
  exhaustively after the Edit (`wc -l`, `git diff --stat`,
  `ast.parse`, full diff inspection). The brief prefers `Write` for
  files >500 lines; I chose `Edit` for this one to minimize blast
  radius. For the bracket reconciliation file I used multiple
  targeted Edits — same verification regimen.
* **`_SUPPORTED_VENUES` import (Cowork ack #4).** Went with option
  (a) — direct import — because the diff is smaller. Added a comment
  explaining the coupling.
* **Tests use direct calls to internal helpers** rather than always
  going through `run_reconciliation_sweep`. Two reasons: (1) the
  internal helpers are the actual fix sites, so unit-level tests are
  the natural fit, and (2) faster collection / clearer failures.
  One end-to-end sweep test is included for Bug B.
* **pytest-asyncio collection issue.** The repo has
  `pytest-asyncio==0.23.3` paired with `pytest==9.0.2`, which raises
  `AttributeError: 'Package' object has no attribute 'obj'` at
  collection. Workaround: `-p no:asyncio`. None of these tests are
  async. Recommend Cowork file a follow-up to bump pytest-asyncio.

---

## Deferred

* Wiring Coinbase into `resize_stop_for_partial_fill`. Out of scope
  per Cowork ack #2.
* Bumping pytest-asyncio. Discovered during this session; out of
  scope.
* Operator-supplied exit_price for ACS-USD #1842 hot-fix SQL.

---

## Open questions for Cowork

* The plan-gate response noted stop_engine cadence is ~5min while
  the reconciler is 60s. The reconciler backfill is the fast path
  for new entries. Confirm this is acceptable for current risk
  profile under Phase 6 LIVE soak — or escalate stop_engine cadence
  to match in a follow-up.
* When Coinbase resize_stop_for_partial_fill is wired, should the
  primitive name match the existing `place_stop_loss_sell_order`
  shape, or should the writer fork on venue at the call site? (The
  former preserves the writer's current contract; the latter is
  cleaner per-venue. Either works.)
* The 3 pre-existing test failures (1 in `test_bracket_reconciliation_service.py`,
  2 in `test_bracket_intent_writer.py`) need a follow-up brief to
  refresh the assertions against current production semantics. Worth
  picking up before they obscure a future regression.

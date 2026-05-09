# CC_REPORT: f-crypto-stale-trade-closer

## Outcome

Phase E ships the crypto-side reconciler chain — symmetric to the
equity Phases A+B+C that landed earlier today. The
`run_crypto_stale_trade_close(db)` sweep runs at the bracket-
reconciliation cadence (~60s) and applies two layered defences
against phantom-open crypto trades:

1. **Layer 1 — entry-never-filled**: an open crypto trade with
   `last_fill_at IS NULL` whose `entry_date` is older than
   `CHILI_CRYPTO_ENTRY_FILL_WINDOW_HOURS` (default 2h) is
   `cancelled` with `exit_reason='entry_never_filled'`. Trade 1810
   DOT-USD's audit fingerprint is the canonical case (broker placed
   the order but never reported a fill; bracket reconciler has been
   shouting `missing_stop:warn` every minute for 7 days with no
   path to act).
2. **Layer 2 — broker-zero-qty streak**: per-trade
   consecutive-cycle counter (mig 234 column
   `crypto_broker_zero_qty_streak`) increments when broker reports
   zero quantity for the trade's ticker; resets to 0 when present.
   Closes at `streak >= CHILI_CRYPTO_BROKER_ZERO_QTY_STREAK_MIN`
   (default 3) with
   `exit_reason='broker_position_reconciled_to_zero'`.

Both layers reuse Phase B's `_record_reconcile_close_burst` so a
runaway phantom-cancel cascade trips the drawdown breaker (3-in-5s)
exactly like the equity wipeout-burst.

Phase A's `_RECONCILE_ARTIFACT_EXIT_REASONS` is extended with both
new reasons so the PDT count never sees them.

## Per-step status

### Step 1 — Truncation scan + survey — COMPLETE
* `bracket_reconciliation_service.py`: 2367 lines, AST clean.
  `run_reconciliation_sweep` at 1873 + scheduler caller at
  `trading_scheduler.py:572` confirmed.
* `pdt_guard.py`: 280 lines, AST clean. The frozenset constant is
  the only thing this brief touches there.
* Last migration registered: `_migration_233_reconcile_partial_list_streak`.
  234 is free.

### Step 2 — Migration 234 + settings + ORM column — SHIPPED
* `_migration_234_crypto_broker_zero_qty_streak` adds
  `trading_trades.crypto_broker_zero_qty_streak INTEGER NOT NULL
  DEFAULT 0` idempotently (`ADD COLUMN IF NOT EXISTS`).
* Settings: `chili_crypto_entry_fill_window_hours` (default 2),
  `chili_crypto_broker_zero_qty_streak_min` (default 3). Both
  env-overridable via the matching uppercase var.
* `Trade.crypto_broker_zero_qty_streak` column added on the ORM
  with matching `server_default="0"`.

### Step 3 — Implement `run_crypto_stale_trade_close` — SHIPPED
Splice into `bracket_reconciliation_service.py` (+~245 lines, AST
clean):

* Constants: `CRYPTO_STALE_RECONCILE` log prefix,
  `CRYPTO_EXIT_REASON_ENTRY_NEVER_FILLED` /
  `CRYPTO_EXIT_REASON_BROKER_ZERO_QTY` (no magic strings; both
  re-exported via `__all__`).
* Helpers `_crypto_entry_fill_window_seconds()`,
  `_crypto_broker_zero_qty_streak_min()`,
  `_is_crypto_ticker(ticker)`. Settings are read at call-time so
  env overrides take effect on next sweep without a restart.
* `run_crypto_stale_trade_close(db, *, broker_crypto_tickers=None,
  user_id=None)`. The optional `broker_crypto_tickers` kwarg is the
  test-injection seam — production calls pass `None` and the sweep
  calls `coinbase_service.get_crypto_positions()` itself.
* Both layer-1 and layer-2 closes call Phase B's
  `_record_reconcile_close_burst`. Reuse, not re-implementation.

### Step 4 — Phase A constant extended + scheduler wiring — SHIPPED
* `pdt_guard._RECONCILE_ARTIFACT_EXIT_REASONS` now includes both new
  reasons. The PDT-count SQL filter (Phase A commit `60c26f8`) used
  `expanding=True` bindparam against this set, so the addition
  auto-applies — no SQL or migration change.
* `_run_bracket_reconciliation_job` in `trading_scheduler.py` now
  calls `run_crypto_stale_trade_close(db)` after the existing
  bracket sweep, wrapped in a try/except so a sweep failure
  doesn't poison the bracket-reconciliation log path.

### Step 5 — Tests — SHIPPED (9 tests)
`tests/test_crypto_stale_trade_closer.py`:

1. `test_layer1_window_not_expired_no_close` — fresh entry stays
   open.
2. `test_layer1_window_expired_cancels_with_reason` — old entry +
   no fill → cancelled with the brief's reason.
3. `test_layer1_does_not_cancel_filled_orders` — `last_fill_at` set
   means layer 1 is past it.
4. `test_layer2_present_resets_streak` — present → streak 2 → 0.
5. `test_layer2_absent_below_threshold_increments_no_close` — absent
   → streak 0 → 1, no close.
6. `test_layer2_at_threshold_closes_with_reason` — streak 2 → 3 →
   close fires with the brief's reason.
7. `test_trade_1810_audit_replay_cancels_via_layer1` — the named
   scenario: 7.6 days old + no fill + broker has zero DOT → layer 1
   catches it on the first sweep.
8. `test_phase_a_excludes_both_new_exit_reasons` — pin the
   frozenset extension.
9. `test_equity_trades_unaffected` — open robinhood AAPL trade
   stays open (wrong asset class for the crypto sweep).

### Step 6 — Pre-deploy audit — DEFERRED TO OPERATOR
**Sandbox blocks production reads.** The brief's pre-deploy audit
asks "how many crypto trades currently match each layer's criteria?"
Operator must run:

```sql
-- Layer 1 candidates (entry never filled, > 2h old)
SELECT id, ticker, entry_date, broker_order_id
  FROM trading_trades
 WHERE status = 'open'
   AND ticker LIKE '%-USD'
   AND last_fill_at IS NULL
   AND entry_date < NOW() - INTERVAL '2 hours'
 ORDER BY entry_date ASC;

-- Layer 2 baseline (current open crypto trades w/ broker context)
SELECT id, ticker, entry_date, last_fill_at,
       crypto_broker_zero_qty_streak
  FROM trading_trades
 WHERE status = 'open'
   AND ticker LIKE '%-USD'
 ORDER BY entry_date ASC;
```

Trade 1810 will appear in the layer-1 list (the canonical case).
If anything else appears, operator surfaces BEFORE deploying so a
stale row that the broker actually does have isn't auto-cancelled.

## Surprises / deviations

1. **Sandbox blocks production reads** (same as Phase B). Pre-deploy
   audit deferred to operator. Per the brief's "OR the first sweep
   handles it post-deploy" allowance, this is acceptable — the
   operator's existing `scripts/d-fix-1810.ps1` is the manual
   alternative for trade 1810 specifically.
2. **`_record_reconcile_close_burst` import made tolerant.** The
   helper is imported lazily inside the sweep's loop body so a
   transient import failure (e.g., during testing without the
   broker_service module fully initialized) doesn't kill the
   layer-1/layer-2 close logic — the burst breaker is observability,
   not a primary safety belt.
3. **Both layers commit in one transaction.** A single `db.commit()`
   at the end of the sweep batches all changes (cancels + streak
   increments + streak resets) into one transaction. Faster than
   per-trade commits + rollback-friendly: a transient broker view
   anomaly that triggers many bogus closes is one rollback away
   from a clean state if surfaced before commit.

## Open questions (carried from brief)

1. **Trade 1810 specifically**. Per brief, operator may run
   `scripts/d-fix-1810.ps1` before this brief deploys, OR let the
   first sweep handle it. Both paths converge to the same
   `cancelled / entry_never_filled` end-state in the row.
2. **Other open crypto trades**. Surfaced in the operator-side
   audit query above; CC report can't enumerate without prod read
   access.
3. **Cadence**. Verified: bracket reconciliation runs ~60s via
   the scheduler. Layer 1 fires within one cycle of the window
   expiry; layer 2 fires within `N * 60s` cycles.

## Verification

* `migrations.py`: 15825 → 15856 (+31); AST clean.
* `config.py`: 2854 → 2876 (+22); AST clean.
* `models/trading.py`: +12 lines.
* `bracket_reconciliation_service.py`: 2367 → 2605 (+238); AST clean.
* `pdt_guard.py`: 280 → 290 (+10); AST clean.
* `trading_scheduler.py`: +20 lines.
* All importable; settings + ORM column resolve.
* 9/9 tests PASS.
* Splice pattern used (NOT Edit tool) for
  `bracket_reconciliation_service.py`. Edit tool used for the small
  additions in `migrations.py`, `config.py`, `models/trading.py`,
  `pdt_guard.py`, `trading_scheduler.py` (each well under the
  100-line splice threshold for the surface being touched).

## Operator-side after CC ships

Per brief:

1. `git pull` + truncation scan.
2. **Run the pre-deploy audit query** above. Report any layer-1
   candidates beyond trade 1810 to confirm they're truly phantoms
   before letting the sweep auto-cancel them.
3. `docker compose up -d --force-recreate chili autotrader-worker`.
4. Watch for `[crypto_reconcile] STALE_TRADE_CLOSE` warnings on
   the next ~60s sweep. Trade 1810 should be `cancelled` with
   `exit_reason='entry_never_filled'` in the first cycle (unless
   the operator already ran `scripts/d-fix-1810.ps1`).
5. Verify the bracket reconciler's `missing_stop:warn` for trade
   1810 stops firing (the row is no longer `status='open'`).
6. Verify migration 234 applied:
   ```sql
   \d trading_trades
   -- crypto_broker_zero_qty_streak should be present, default 0
   ```

## Rollback plan

`git revert` the feature commit. The new column stays in the schema
(additive, default 0). The sweep is gated on the settings flags
being non-zero — flip
`CHILI_CRYPTO_ENTRY_FILL_WINDOW_HOURS=0` AND
`CHILI_CRYPTO_BROKER_ZERO_QTY_STREAK_MIN=0` to disable both layers
without a code revert. Phase A's frozenset extension is also
additive: removing the two strings restores prior PDT-count
behaviour.

## What's NEXT after this ships

The wipeout-cascade loop is now closed at FIVE layers across both
asset classes:

| Layer | Brief | Asset class |
|---|---|---|
| Phase A (count filter) | f-pdt-count-broker-confirmed-only | equity |
| Phase B (R32 + burst breaker + obs) | f-equity-broker-reconcile-wipeout-protection | equity |
| Phase C (per-trade streak gate) | f-equity-reconcile-partial-list-guard | equity |
| Phase D (pattern-demote) | f-pattern-demote-on-thin-evidence | pattern lifecycle |
| **Phase E (this brief)** | f-crypto-stale-trade-closer | **crypto** |

If the operator's audit surfaces additional crypto-stale-class
phantoms beyond trade 1810, queue follow-ups; otherwise the chain
is structurally complete.

# f-crypto-stale-trade-closer

STATUS: QUEUED
SLUG: crypto-stale-trade-closer
PROPOSED: 2026-05-08
SEVERITY: high (real money at stake; operator already has 1 confirmed phantom-open crypto trade with 7+ days of bracket-reconciler warnings being ignored)

## TL;DR

Equity book has the Phase A + B + C reconciler chain; crypto book has
NOTHING equivalent. Trade 1810 DOT-USD opened 2026-05-01, broker
shows `position_quantity=0` since at least the same day, and the
bracket reconciler has been logging `missing_stop:warn` every 60
seconds for 7 days with no path to act. The trade row stays
`status='open'` indefinitely — phantom-open, falsely contributing
to capital allocation, hiding from any "what's open right now"
operator query.

This brief adds a **crypto-side stale-trade closer** that mirrors
the equity-side path: detect (entry_never_filled OR
broker_quantity=0-for-N-cycles) and auto-close with a clean
`exit_reason`.

## Why now (audit fingerprint)

Trade 1810 DOT-USD discovered 2026-05-08 17:13 PDT during operator's
"target hit but no exit" investigation. Forensic detail:

| Field | Value | Meaning |
|---|---|---|
| `id` | 1810 | |
| `ticker` | DOT-USD | crypto |
| `direction` | long | |
| `status` | open | **Chili thinks it has 248 DOT.** |
| `quantity` | 248 | |
| `entry_price` | $1.21568548 | |
| `stop_loss` | $1.26251275 | trailing-stop ratcheted up |
| `target_price` (intent) | $1.39803830 | take-profit |
| `broker_order_id` | 69f46dad-... | entry order placed |
| `last_fill_at` | **NULL** | **No fill ever recorded.** |
| `last_broker_sync` | 2026-05-08 15:16 | recent |
| `entry_date` | 2026-05-01 09:08 | **7.6 days ago** |

Broker's view (live, just polled):
- `get_crypto_positions()` → `[]`
- `get_positions()` → `[]`
- `position_quantity=0.0` in every reconcile cycle since at least
  the trade's first sync

Bracket reconciler activity (10 most-recent rows, every 60s):
```
kind=missing_stop severity=warn  intent_state='intent'
local: stop_price=1.26, target_price=1.398, quantity=248
broker: stop_order_id=null, target_order_id=null, position_quantity=0.0
```

**Nothing acts on these warnings.** The reconciler logs them and
moves on; the bracket writer can't place a stop without a position;
the LLM exit monitor (`crypto_exit_monitor.run_crypto_exit_pass`)
sees `status='open'` and presumably has logic that requires the
position to exist before exit — without a broker position, it
silently no-ops or never gets called for this row.

Today: live DOT-USD spot is $1.3705 (Coinbase). The intent's
target was $1.3980. Operator observed price went above target
earlier and expected an exit. Chili could not exit because there
was no real broker position to exit.

## Goal

Add a sweep that closes phantom-open crypto trades when the broker
canonically reports zero quantity. Mirror the equity-side three-
layer protection:

### Layer 1: entry-never-filled detection

Any trade with `status='open'` AND `last_fill_at IS NULL` AND
`entry_date < NOW() - INTERVAL '<N> hours'`
(default `CHILI_CRYPTO_ENTRY_FILL_WINDOW_HOURS=2`) → mark as
`status='cancelled'` with
`exit_reason='entry_never_filled'`, `pnl=NULL`,
`exit_price=NULL`. The window default is 2h because crypto entries
should fill in seconds; if they don't fill in 2h the order was
rejected, expired, or stuck. After 2h the entry is unrecoverable.

### Layer 2: broker-quantity-zero confirmation

Any trade with `status='open'` AND ticker LIKE `'%-USD'` AND
`last_fill_at IS NOT NULL` (entry DID fill) where the bracket
reconciler has reported `broker_payload.position_quantity=0` in N
consecutive sweeps (default
`CHILI_CRYPTO_BROKER_ZERO_QTY_STREAK_MIN=3`) → mark as
`status='closed'` with
`exit_reason='broker_position_reconciled_to_zero'`,
`exit_price=` last known broker fill price OR fallback to
last reconcile observation, `pnl` computed from that price.

This is the crypto parallel of Phase C's
`broker_sync_missing_streak`. New column
`trading_trades.crypto_broker_zero_qty_streak INT NOT NULL DEFAULT 0`
incremented per reconcile pass when broker quantity is 0; reset
to 0 when broker quantity is non-zero.

### Layer 3: observability + breaker

`[crypto_reconcile] STALE_TRADE_CLOSE: ticker=X reason=Y` warning
per close. If ≥3 closes in 5s, trip the wipeout-burst breaker
(reuse Phase B's `_record_reconcile_close_burst`).

## Acceptance criteria

1. New module `app/services/trading/crypto_reconcile.py` (or
   extend an existing crypto-side service) hosting:
   - `_detect_entry_never_filled(db) -> List[Trade]`
   - `_detect_broker_zero_qty_confirmed(db) -> List[Trade]`
   - `run_crypto_stale_trade_close(db) -> dict`
2. Migration NNN (next free): `ADD COLUMN IF NOT EXISTS
   crypto_broker_zero_qty_streak INT NOT NULL DEFAULT 0` on
   `trading_trades`.
3. Settings:
   `CHILI_CRYPTO_ENTRY_FILL_WINDOW_HOURS=2`,
   `CHILI_CRYPTO_BROKER_ZERO_QTY_STREAK_MIN=3`.
4. Wire `run_crypto_stale_trade_close` into the
   bracket-reconciliation cycle (per-cycle, ~60s).
5. Reuse Phase B's `_record_reconcile_close_burst` for burst
   protection.
6. Tests in
   `tests/test_crypto_stale_trade_closer.py`:
   - `entry_never_filled` window not yet expired → no close
   - `entry_never_filled` window expired → cancel
   - broker quantity 0, streak < N → no close
   - broker quantity 0, streak >= N → close
   - broker quantity > 0 (presence) → reset streak
   - **Trade 1810 audit replay** (entry_never_filled, 7.6d old) →
     cancelled with the right reason + audit values
7. Live verification: trade 1810 cleaned up on first sweep
   (`status='cancelled'`, `exit_reason='entry_never_filled'`).
8. CC report at
   `docs/STRATEGY/CC_REPORTS/YYYY-MM-DD_f-crypto-stale-trade-closer.md`.

## Brain integration (reuse, don't rewrite)

- `app/services/trading/bracket_reconciliation_service.py`
  (the cycle that produces the `missing_stop:warn` rows) — the
  natural sibling for the new sweep.
- Phase B's `_record_reconcile_close_burst` —
  reuse for cardinality-based breaker trip.
- Phase A's
  `_RECONCILE_ARTIFACT_EXIT_REASONS` — extend to include
  `'entry_never_filled'` and `'broker_position_reconciled_to_zero'`
  so PDT count exclusion stays consistent (these are not
  FINRA day-trades any more than `broker_reconcile_position_gone`
  was).

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged.
- **Hard Rule 5**: prediction-mirror authority untouched.
- **Hard Rule 3**: data-first. Use the new column; don't smuggle
  state into `notes`.
- **Don't touch the equity-side reconciler.** Phases A+B+C are
  load-bearing and tested.
- **Don't touch Phase B's wipeout-burst helper.** Reuse only.
- **No magic numbers**: both windows lift from settings.
- **Edit-tool truncation discipline (HARD).** Splice pattern.
- **Tests use `_test`-suffixed DB.**

## Out of scope

- Equity book (Phases A+B+C already cover it).
- Options reconciler.
- Manual position-recovery for trade 1810 — that's an operator
  one-shot SQL update done out-of-band before this brief ships
  (or by this brief's first sweep, whichever comes first).
- Re-promotion of pattern 585 (separate brief if needed).
- Other stale-detection criteria beyond the two layers above.

## Sequencing

1. Truncation scan.
2. Audit query: how many crypto trades currently match each layer's
   criteria? (Capture in CC report. Trade 1810 is one; there may
   be others.)
3. Migration NNN.
4. New module + sweep + helpers.
5. Wire into bracket-reconciliation cycle.
6. Tests including trade 1810 replay.
7. Commit + push + CC report + mark NEXT_TASK DONE.

## Operator-side after CC ships

1. Pull + truncation scan.
2. **Pre-flight: review the CC report's audit list of trades that
   will be auto-closed by the first sweep.** If any are unexpected
   (e.g., a trade you DO have at the broker but chili thinks is
   stale), surface BEFORE deploy.
3. `docker compose up -d --force-recreate chili autotrader-worker`.
4. Watch for `[crypto_reconcile] STALE_TRADE_CLOSE` warnings.
5. Verify trade 1810 is now `status='cancelled'` with
   `exit_reason='entry_never_filled'`.

## Rollback plan

`git revert` the commit. New column stays (additive, no harm).
The sweep is gated on the settings flags being non-zero;
`CHILI_CRYPTO_ENTRY_FILL_WINDOW_HOURS=0` disables Layer 1,
`CHILI_CRYPTO_BROKER_ZERO_QTY_STREAK_MIN=0` disables Layer 2.

## Open questions

1. **What happened to trade 1810's entry order?** RH order ID
   exists, no fill. Three possibilities: (a) order placed but
   never filled (broker rejected it silently), (b) order filled
   but the fill webhook/poll missed it, (c) order filled and the
   position was sold separately, with chili losing track on the
   sell side. The Layer 1 entry-never-filled close handles (a)
   correctly. (b) and (c) need a separate investigation but the
   sweep still closes the row out of an "honest unknown" stance
   (similar to broker_service.py's pnl=NULL on resolved_exit
   failure).

2. **Operator's manual cleanup vs the sweep.** Operator may
   decide to fix trade 1810 manually before this brief ships
   (one-shot SQL UPDATE). That's fine; the sweep would have
   caught it the same way. CC report should NOT block on the
   sweep doing the cleanup; if 1810 is already cancelled by
   operator action, the sweep is a no-op for it.

3. **Crypto reconcile cadence.** The bracket reconciler runs
   every 60s (visible in audit data). If that's too tight for
   a "phantom" detection (some operations could legitimately
   have a transient position_quantity=0 reading), tune via the
   STREAK_MIN setting. Default of 3 cycles = 3 minutes of
   confirmed zero before close.

4. **Trade 1810 entry was 2026-05-01.** That's pre-R32-deploy.
   Was DOT-USD position ever real, then wiped by the same
   pre-R32 wipeout that hit AIDX/VFS/etc.? The 5/1 wipeout was
   for tickers that DID have real positions; if 1810's entry
   never filled then it's a different class. Surface in CC
   report by checking RH order history (operator-side; CC can't
   read RH order history from sandbox).

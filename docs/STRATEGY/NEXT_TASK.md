# NEXT_TASK: f-crypto-stale-trade-closer

STATUS: DONE

## Goal

Close the crypto-side reconciler gap: add a sweep that auto-closes
phantom-open crypto trades (entry never filled OR broker reports
zero quantity for N consecutive cycles). Mirrors the equity-side
Phase A+B+C protection chain. **Trade 1810 DOT-USD is the audit
fingerprint** — it's been `status='open'` for 7.6 days with
`last_fill_at IS NULL` and broker reporting `position_quantity=0`,
and the bracket reconciler has been logging `missing_stop:warn`
every 60s with no path to act.

The full brief is at
`docs/STRATEGY/QUEUED/f-crypto-stale-trade-closer.md`
— read it first.

## Why now (real-money exposure)

Operator surfaced 2026-05-08 17:13 PDT: "DOT-USD position hit its
target earlier but chili didn't exit it." Forensic investigation
(see brief for full table):

* Trade 1810 DOT-USD: entry $1.21568548, target intent $1.39803830,
  current Coinbase spot $1.3705 (above and below target intermittently
  in last hours).
* Trade row says `status='open'` with `quantity=248`.
* `last_fill_at IS NULL` despite `broker_order_id` being set —
  the entry order was placed but **the broker never reported a fill**.
* `get_crypto_positions()` returns `[]`. Broker has zero DOT.
* Bracket reconciler has logged `missing_stop:warn` every minute
  for 7 days. Nothing acts on these warnings — there's no
  crypto-side stale-close path.

The "missed exit" is misframed: chili can't exit a position the
broker doesn't have. The real bug is that chili thinks it has
the position when it doesn't. Equity book has R31/R32 + Phase B
wipeout-burst breaker + Phase C streak counter for this exact
class of phantom; crypto book has nothing.

## Why this is the right next move

* **Phase E (this brief)** vs `f-pattern-demote-sweep-wiring-fix`:
  pattern-demote sweep DOES run a few times per day on its
  current event-driven hook (24h ledger shows 3
  `execution_feedback_digest` events) — not fully dead, just
  intermittent. The wiring fix is correct but not urgent.
  Crypto-stale-trade-closer addresses real-money exposure that's
  active right now.
* **vs `f-pdt-crypto-bypass-cleanup`**: hygiene only; doesn't
  change observable behavior.
* **vs `f-autotrader-pdt-aware-exit-deferral`**: based on a
  flawed premise; needs rewriting.
* **vs new alpha work**: capital efficiency improvement before
  signal-quality work; phantom-opens are unaccounted exposure.

## The change

### Layer 1 — entry-never-filled detection

Trade with `status='open'` AND `last_fill_at IS NULL` AND
`entry_date < NOW() - INTERVAL <N> hours` (default
`CHILI_CRYPTO_ENTRY_FILL_WINDOW_HOURS=2`) → mark as `cancelled`,
`exit_reason='entry_never_filled'`, `pnl=NULL`, `exit_price=NULL`.

### Layer 2 — broker-quantity-zero confirmation streak

Mirrors Phase C (equity). New column
`trading_trades.crypto_broker_zero_qty_streak INT NOT NULL DEFAULT 0`.
Increment when broker reports `position_quantity=0` for an open
crypto trade; reset to 0 when present. Close at
`streak >= CHILI_CRYPTO_BROKER_ZERO_QTY_STREAK_MIN` (default 3) with
`exit_reason='broker_position_reconciled_to_zero'`.

### Layer 3 — observability + reuse Phase B's burst-breaker

`[crypto_reconcile] STALE_TRADE_CLOSE` warning per close. ≥3 in
5s trips the breaker (reuse Phase B's
`_record_reconcile_close_burst`).

### Phase A integration

Extend `_RECONCILE_ARTIFACT_EXIT_REASONS` in `pdt_guard.py` to
include `'entry_never_filled'` and
`'broker_position_reconciled_to_zero'` so they don't pollute the
PDT count (these are not FINRA day-trades any more than
`broker_reconcile_position_gone` was).

## Acceptance criteria (per brief)

1. New module/sweep `run_crypto_stale_trade_close(db)` shipped.
2. Migration NNN (next free) adds the streak column.
3. Settings constants for both window/threshold.
4. Wired into bracket-reconciliation cycle (~60s cadence).
5. Reuses Phase B's burst breaker.
6. Phase A's `_RECONCILE_ARTIFACT_EXIT_REASONS` extended.
7. Tests in `tests/test_crypto_stale_trade_closer.py`:
   - entry-never-filled window not expired → no close
   - entry-never-filled window expired → cancel
   - broker qty=0, streak < N → no close
   - broker qty=0, streak >= N → close
   - broker qty > 0 → reset streak
   - **Trade 1810 audit replay** → cancelled with right reason
8. Live verification: trade 1810 cleaned up on first sweep
   (or earlier via operator's manual SQL fix from
   `scripts/d-fix-1810.ps1`).
9. CC report at
   `docs/STRATEGY/CC_REPORTS/2026-05-08_f-crypto-stale-trade-closer.md`.

## Brain integration (reuse, don't rewrite)

- `app/services/trading/bracket_reconciliation_service.py` —
  natural sibling for the new sweep. Already runs every 60s and
  is the source of the `missing_stop:warn` audit data.
- Phase B's `_record_reconcile_close_burst` — reuse for
  cardinality-based breaker trip.
- Phase A's `_RECONCILE_ARTIFACT_EXIT_REASONS` — extend to
  include the two new reasons.
- Phase C's `broker_sync_missing_streak` — pattern only;
  the crypto column is parallel, not shared (different reconciler
  cadence and different broker source).

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged.
- **Hard Rule 5**: prediction-mirror authority untouched.
- **Hard Rule 3**: data-first. New column via migration; no
  off-schema state.
- **Don't touch the equity-side reconciler** (Phases A+B+C are
  load-bearing).
- **Don't touch Phase B's burst helper** (reuse only).
- **Don't touch `pdt_guard.py`'s SQL filter** (Phase A is durable);
  ONLY extend the constant set.
- **No magic numbers**: both windows lift from settings.
- **Edit-tool truncation discipline (HARD).** Splice pattern.
  `wc -l + ast.parse` post-edit verification mandatory.
- **Tests use `_test`-suffixed DB.**

## Out of scope

- Equity book (Phases A+B+C cover it).
- Options reconciler.
- Manual position-recovery for trade 1810 — operator runs
  `scripts/d-fix-1810.ps1` (uncomment Path A or B based on
  Robinhood app evidence) before this brief ships, OR the first
  sweep handles it post-deploy. Either is fine.
- Other stale-detection criteria beyond the two layers.
- Re-promotion of pattern 585.
- The pattern-demote sweep wiring fix
  (`f-pattern-demote-sweep-wiring-fix`) — separate brief, can
  ship after this.

## Sequencing

1. Truncation scan on
   `app/services/trading/bracket_reconciliation_service.py` and
   `app/services/trading/pdt_guard.py` (the latter for the
   constant extension).
2. **Pre-deploy audit**: how many crypto trades currently match
   each layer's criteria? Surface in CC report. Trade 1810 is
   the known one; there may be others.
3. Migration NNN + new column.
4. Settings + module-level constants.
5. New sweep + helpers + Phase A constant extension.
6. Wire into bracket-reconciliation cycle.
7. Tests including trade 1810 replay.
8. Commit + push + CC report + mark NEXT_TASK DONE.

## Operator-side after CC ships

1. Pull + truncation scan.
2. **Pre-flight: review the CC report's audit list of trades
   that will be auto-closed by the first sweep.** If any are
   unexpected (e.g., a trade you DO have at the broker but chili
   thinks is stale), surface BEFORE deploy.
3. `docker compose up -d --force-recreate chili autotrader-worker`.
4. Watch for `[crypto_reconcile] STALE_TRADE_CLOSE` warnings.
5. Verify trade 1810 is now `status='cancelled'` (or whatever
   manual cleanup the operator did).

## Rollback plan

`git revert` the commit. New column stays (additive). Sweep is
gated on the settings flags being non-zero;
`CHILI_CRYPTO_ENTRY_FILL_WINDOW_HOURS=0` disables Layer 1,
`CHILI_CRYPTO_BROKER_ZERO_QTY_STREAK_MIN=0` disables Layer 2.

## What CC should do if it's unsure

1. **If the audit shows >5 trades that would be auto-cancelled**,
   STOP and surface — that volume suggests a deeper problem
   (e.g., a recent broker outage producing many entry-never-filled
   rows) and the operator needs to review before any sweep runs.
2. **If the bracket-reconciliation-service cycle cadence is not
   a stable ~60s** (e.g., it depends on backlog), surface and
   propose a scheduler-cron alternative.
3. **If the trade 1810 audit replay test won't run cleanly**
   (e.g., due to FK constraints similar to Phase B's R32 test),
   use `user_id=NULL` seed pattern as Phase B did.

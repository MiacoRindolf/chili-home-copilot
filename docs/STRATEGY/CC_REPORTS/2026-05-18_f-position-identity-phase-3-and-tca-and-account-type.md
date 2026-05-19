# CC_REPORT: f-position-identity-phase-3 + account_type retrofit + TCA wiring

**Session type:** Cowork-direct execution (operator said "all" to Phase 3 + TCA AND "ok" to the Coinbase account_type retrofit after the Phase 2 ship report).

## What shipped

**Five commits on `main`** (chained), totaling ~700 insertions across migrations + ORM + services + tests:

| # | Subject | What |
|---|---|---|
| 1 | Phase 3 — bracket_intents.position_id (mig 249) | 7 files / +446: shared resolver in `position_resolver.py`, mig 249, ORM update, writer patch, 10 new tests + canary extension |
| 2 | Coinbase account_type retrofit (mig 250) | 2 files / +59: mig 250 + `broker_service._resolve_account_type_for_position` updated |
| 3 | TCA writer wiring (mig 251 + auto_trader.py) | 2 files / +116: autotrader writes `tca_reference_entry_price=px`, calls `apply_tca_on_trade_fill` at INSERT, mig 251 |
| 4 | TCA backfill fix (mig 252, wrong table) | 1 file: mig 252 redoes the backfill via correct table (`trading_breakout_alerts`) |
| 5 | TCA backfill phantom-row guard (mig 253) | 1 file: mig 253 wraps the backfill with `entry_price > 0 AND quantity > 0` guards |

## Verification — live DB results

**Schema tip**: `253_tca_backfill_guard_phantom_rows` (sequence 247→248→249→250→251→252→253 all applied).

### Phase 3 (mig 249)
- `trading_bracket_intents.position_id` column + partial index + ORM field — all present ✓
- **422 of 422 bracket_intents resolved (100%)** ✓
- Reader canary held: `tests/test_position_identity_phase3.py::test_no_reader_consults_position_id_on_bracket_intents_in_app_services` PASS
- 20/20 tests pass (10 Phase 2 + 10 Phase 3)

### Coinbase account_type retrofit (mig 250)
- 53 trading_positions rows flipped from `account_type='cash'` to `'spot'` (Coinbase only)
- 125 Robinhood equity rows untouched at `'cash'` ✓
- 23 Robinhood crypto rows untouched at `'cash'` (operator uses RH crypto in cash account)
- `broker_service._resolve_account_type_for_position` updated so future Coinbase fills get `'spot'`

### TCA wiring (mig 251 / 252 / 253 + auto_trader.py)
- `tca_reference_entry_price` populated on **285 of 638 trades** (was 0/638)
- `tca_entry_slippage_bps` computed on **285 of 638 trades** (was 0/638)
- `tca_exit_slippage_bps` unchanged at 181/638 (this side was already working)
- **Average entry slippage: +102 bps** (n=285, min −866, max +2,101, sd 354)
- Going forward: `auto_trader.py:2233` writes `tca_reference_entry_price=px` AND calls `apply_tca_on_trade_fill(tr)` at INSERT, so new autotrader trades populate immediately

## The TCA finding — call this out for operator attention

**Average entry slippage of 102 bps is huge relative to the system's edge.**

Pattern 585 — the system's only proven alpha — has avg_return_pct = 1.68% (168 bps) per trade. **Entry slippage alone is consuming ~60% of that gross edge.** And we haven't measured exit slippage on the same 285 trades yet.

Specific bad actors (sample):
- ACS-USD: +900-960 bps on 3 fills (entered ~9% above reference)
- BNB-USD: +394 bps consistent on 4 fills
- XLM-USD, AAVE-USD: +92-112 bps (typical small-cap crypto)

This argues hard for:
1. **Maker-only routing on Coinbase** (memory: `f-fastpath-maker-only`). The 102 bps avg includes both market-order taker fees AND adverse fills.
2. **Tighter entry price gating in `auto_trader.py`** — refuse to fill when the market is >X bps from the decision price.
3. **Reference-price re-snap before order placement** — capture the live mid-quote at place time, not the alert's stored entry_price (which can be stale by the time the order routes).

These are follow-up briefs, not in this session's scope.

## Surprises / deviations

1. **Mig 251 joined to the wrong alerts table.** `trading_alerts` doesn't have `entry_price` (it's the generic delivery log); `trading_breakout_alerts` does. The try/except inside the migration swallowed the column-does-not-exist error and the schema_version row was still written. Mig 252 redid the join.

2. **Mig 252 also failed silently — different reason.** Trade #404 (ETH-USD phantom, entry_price=0, qty=9e-07) violates `chk_trades_entry_price_positive`. PostgreSQL re-validates ALL CHECK constraints on rows touched by UPDATE, even when the constrained column isn't being modified. The JOIN matched trade 404 via a valid breakout_alert FK, the UPDATE re-validated the row, the constraint fired, the statement aborted before any rows committed. Mig 253 wraps the backfill with `entry_price > 0 AND quantity > 0` guards. The data was successfully written manually via `scripts/d-tca-manual-backfill-v2.py` before mig 253 was authored.

3. **Resolver extracted to a shared module.** Phase 2's `_resolve_position_id_for_event` is now a thin wrapper around `app/services/trading/position_resolver.resolve_position_id`. Phase 2 tests stay green via API re-export. Phase 3's writer (`bracket_intent_writer.upsert_for_trade`) imports the shared resolver directly.

4. **Phase 3 canary needed a small allowlist update.** The Phase 2 reader canary tripped on a docstring reference in the new `position_resolver.py` module. Allowed-files set extended.

## Deferred / queued for Phase 4

- **Phase 4** — `f-position-identity-phase-4-inverse-reconcile-position-history`. Rewrites the conservative `event_count == 0` workaround at the inverse-reconcile path to consult position-level fill history. Phase 2 + 3 just gave it the foundation it needs.
- **Maker-only Coinbase routing** (`f-fastpath-maker-only` from memory). Higher priority now that TCA shows 100+ bps avg entry slippage.
- **Tighter entry-price gating** in `auto_trader.py` — refuse fills > N bps from reference. Brief to be written.
- **Reference-price re-snap at place time** — use a live mid-quote, not the alert's stored entry_price. Brief to be written.
- The watcher continues running daily.

## Rollback plan

Each commit is independently revertable. The migrations are additive + idempotent. Specific reverts:

- Phase 3 (commit ?): `git revert` removes the writer patch; column + index stay (additive)
- account_type (mig 250): `UPDATE trading_positions SET account_type='cash' WHERE broker_source='coinbase'`; also revert the `broker_service._resolve_account_type_for_position` change
- TCA (mig 251/252/253 + auto_trader.py): `UPDATE trading_trades SET tca_reference_entry_price=NULL, tca_entry_slippage_bps=NULL WHERE ...` and revert the auto_trader.py commit

## Status

NEXT_TASK to be set to Phase 4. CC report = this file. Memory + CURRENT_PLAN updated separately in the docs commit.

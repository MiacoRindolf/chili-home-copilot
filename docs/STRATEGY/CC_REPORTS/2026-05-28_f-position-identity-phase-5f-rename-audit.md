# f-position-identity-phase-5f-rename-audit

Date: 2026-05-28
Status: SHIPPED
Branch: `main`

## Executive Summary

Phase 5F produced a repeatable dependency audit for the physical rename from
`trading_trades` to `trading_management_envelopes`.

The conclusion is clear: the Phase 5E data gate is green, but the physical
rename should happen only after a deliberate dry-run branch. The runtime code
still has enough direct `trading_trades` and `Trade` references that a raw
`ALTER TABLE ... RENAME` would be too blunt.

## Tooling

New read-only audit script:

```text
scripts/d-phase5f-rename-audit.py
```

Run:

```powershell
python scripts\d-phase5f-rename-audit.py
```

The script scans `app`, `tests`, `scripts`, and `docs`, excluding noisy history
and log folders, and groups references by kind.

## Audit Results

Summary from the first run:

```text
files_with_any_hit: 784
files_with_literal_trading_trades: 432
runtime_files_with_literal_trading_trades: 35
runtime_files_with_Trade_symbol: 101

literal_trading_trades total: 1307
literal_trading_management_envelopes total: 51
Trade symbol total: 2135
trade_id total: 1632
source_trade_id total: 41
```

Runtime files with direct `trading_trades` literals include:

```text
app/migrations.py
app/models/trading.py
app/routers/admin.py
app/routers/brain.py
app/routers/trading_sub/ai.py
app/services/broker_service.py
app/services/coinbase_service.py
app/services/trading/auto_trader.py
app/services/trading/auto_trader_rules.py
app/services/trading/bracket_reconciliation_service.py
app/services/trading/management_envelopes.py
app/services/trading/options/exit_monitor.py
app/services/trading/pdt_guard.py
app/services/trading/portfolio_risk.py
app/services/trading/realized_stats_sync.py
```

The full runtime list is produced by the script.

## Recommended Rename Strategy

Use a compatibility-first physical rename:

1. Dry-run in test/staging first.
2. `ALTER TABLE trading_trades RENAME TO trading_management_envelopes`.
3. Create a simple updatable compatibility view:

   ```sql
   CREATE VIEW trading_trades AS
   SELECT * FROM trading_management_envelopes;
   ```

4. Keep the `Trade` ORM class temporarily named `Trade`, but retarget its
   physical table binding only in a dedicated dry-run branch after checking
   SQLAlchemy FK metadata.
5. Do not drop `trade_id` columns or legacy close-reason fields in this phase.
6. Run a smoke suite that exercises:
   - manual trade CRUD
   - autotrader entry creation
   - Coinbase sync create/update/close
   - Robinhood broker sync
   - bracket writer/reconciliation
   - stop engine
   - attribution endpoint with and without `phase5b_compare`

## Risk Notes

The compatibility view is likely enough for direct raw SQL reads/writes if it
is a simple `SELECT *` view over the renamed base table. The ORM layer is the
riskier part because several SQLAlchemy model FKs still point at
`trading_trades.id`. The dry-run must prove mapper configuration and flush
ordering before any production migration.

The audit also confirms the rename should not be combined with semantic
cleanup. Keep `Trade` as a Python class name until the table rename has soaked;
rename the Python class later if desired.

## Acceptance For Next Phase

Phase 5G should be a dry-run implementation branch, not an immediate production
flip:

- Test DB migration can rename and create compatibility view.
- ORM metadata configures without `NoReferencedTableError`.
- Old raw SQL against `trading_trades` still works through the view.
- New SQL against `trading_management_envelopes` works.
- Existing Phase 5E reporting compare remains clean.

## Rollback

Rename rollback is straightforward if no schema cleanup is mixed in:

```sql
DROP VIEW IF EXISTS trading_trades;
ALTER TABLE trading_management_envelopes RENAME TO trading_trades;
```

Do not drop columns, constraints, or indexes during Phase 5G.

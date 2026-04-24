# Roadmap deviation 004 — Q1.T3 unified signal contract (phase 1)

## Migration ID

- A megaprompt example referenced migration **094** for a unified signal table. This repository’s `MIGRATIONS` tail after CPCV / promotion-gate work ended at **166**. Q1.T3 ships as **`167_unified_signals_table`** in [`app/migrations.py`](../app/migrations.py).
- Run `.\scripts\verify-migration-ids.ps1` before merge.

## Physical table

- ORM-free DDL table name: **`unified_signals`** (columns mirror [`app/services/trading/contracts/signal.py`](../app/services/trading/contracts/signal.py)).

## Feature flag

- **`CHILI_UNIFIED_SIGNAL_ENABLED`** → Settings **`chili_unified_signal_enabled`** (default **false**). When false, no rows are inserted; pre-T3 behavior is unchanged.

## Rollback (manual)

```sql
DROP TABLE IF EXISTS unified_signals;
```

## Phase scope

- **In:** Pydantic contract, migration, `emit_signal_*` helpers at strategy-proposal and `BreakoutAlert` persistence sites.
- **Out (later PRs):** consumers reading `unified_signals`, removal of bespoke payloads, auto-trader / executor changes.

# Phase 5AD - ORM Alias Plan

**Date:** 2026-05-30
**Status:** SHIPPED
**Scope:** Plan/canary only; no live trading behavior changed

## Summary

Phase 5AD answers the rename question directly:

Do not introduce a separate live `ManagementEnvelope` ORM class yet, and do not broadly rename `Trade`.

The useful Phase 5 work is already in place:

- physical data lives in `trading_management_envelopes`
- `trading_trades` remains the legacy compatibility relation
- semantic read helpers use `MANAGEMENT_ENVELOPES_RELATION`
- raw live readers against `trading_trades` are gone
- high-value display loaders are on envelope helpers

The remaining `Trade` ORM class is not just a table name. It is also public API vocabulary, route vocabulary, UI vocabulary, test vocabulary, and a live writer compatibility mapper. Renaming it now would create cross-system churn without adding alpha, improving risk, or reducing a known production leak.

## Options Considered

### Option A - New `ManagementEnvelope` ORM Class Mapped to the Physical Table

Rejected for now.

This would create a second ORM model over a relation that already participates in many legacy relationships, validators, foreign keys, and writer paths. It could be made to work, but the risk/reward is poor until the public compatibility boundary is reduced further.

### Option B - `ManagementEnvelope = Trade` Alias

Deferred.

This is low-risk mechanically, but it mostly creates naming ambiguity. If used, it should begin only in the private helper/type-only group identified by Phase 5AC, not in public routes or live broker paths.

### Option C - Keep `Trade` as the Compatibility Mapper, Use Semantic Helpers for New Reads

Chosen.

This is the current architecture and it is the right one:

- app code that needs envelope semantics reads through `management_envelopes.py`
- public routes can continue saying `trade_id`
- live writers can continue using the compatibility mapper
- analyzer/canary tooling prevents new raw-reader drift

## Public Names That Stay Stable

- `/trades`
- `trade_id`
- schema names containing `Trade`
- UI labels containing `trade`
- `trading_trades` compatibility relation
- `Trade` ORM mapper, until a future compatibility migration says otherwise

## Future Rename Path, If We Still Want It

The only defensible order is:

1. Private helper/type-only group first.
2. Learning/research/reporting group second, only where semantic helper APIs already exist.
3. Risk/capital gates only with parity probes and default-off flags.
4. Live action/broker/reconcile paths only with venue-specific rollback plans.
5. Public route/schema/UI names last, and only as a product/API compatibility decision.

## What Changed

- Added an explicit Phase 5AD comment beside `LEGACY_TRADES_COMPAT_RELATION`.
- Added `tests/test_phase5ad_orm_alias_plan.py` to pin the current contract:
  - physical envelope relation is `trading_management_envelopes`
  - legacy compatibility relation is `trading_trades`
  - `Trade` remains mapped to `trading_trades`
  - `Trade.id` remains the public `trade_id` / envelope id
  - `position_id` and `decision_id` remain the bridge links

## Verification

```text
python -m py_compile app\services\trading\management_envelopes.py

TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test
DATABASE_URL=$TEST_DATABASE_URL
python -m pytest tests\test_phase5ad_orm_alias_plan.py tests\test_phase5_remaining_trade_refs.py tests\test_phase5l_reader_allowlist.py -q

python scripts\analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime
```

## Architect Verdict

Phase 5 rename pressure should stop here.

From an algo-trader perspective, this rename no longer changes decision quality, execution quality, cost quality, or risk. From a senior engineering perspective, the remaining rename is mostly compatibility churn around live paths. The correct architecture is to keep the compatibility mapper and continue adding semantic helper APIs only when a concrete reader needs one.

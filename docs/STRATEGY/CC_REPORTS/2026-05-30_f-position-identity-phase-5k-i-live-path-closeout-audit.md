# Phase 5K-I: Live-Path Closeout Audit

**Date:** 2026-05-30
**Status:** COMPLETE
**Verdict:** Phase 5K is closed. Do not do more blind reader cutovers.

## Summary

Phase 5H already completed the physical rename:

- `trading_management_envelopes` is the physical base table (`relkind='r'`).
- `trading_trades` is the legacy compatibility view (`relkind='v'`).

Phase 5K moved the highest-risk live readers from the compatibility view to the
semantic base table under narrow flags, then promoted each flag after live
parity evidence. The remaining references are no longer good candidates for
single-reader flag flips. They are compatibility contracts, writer/order/broker
paths, reconciliation paths that still intentionally speak in `trade_id`, or
small reporting/contract readers that should be handled as a design pass.

## Evidence

Fresh probes:

```text
Phase 5K-A: COMPLETE_POSITIVE
PARITY_GROUPS=6
PARITY_MISMATCHES=0
CHECK_COINBASE_CAP=OK
CHECK_PDT_DAY_TRADES=OK
CHECK_PROMOTION_REALIZED=OK
CHECK_PATTERN_QUALITY=OK
CHECK_PORTFOLIO_RISK_OPEN=OK
CHECK_POSITION_INTEGRITY_OPEN=OK

Phase 5I: COMPLETE_POSITIVE
FRESH_DECISIONS=20
FRESH_ENVELOPES=20
FRESH_CLOSES=10
FRESH_CLOSE_MISMATCHES=0
HARD_LINKAGE_ISSUES=0
MISMATCHED_ROWS=0
MISMATCHED_PNL=0.0000
```

Additional old-vs-new checks run during the closeout audit also matched:

| Check | Old rows | New rows | Result |
| --- | ---: | ---: | --- |
| 90d attribution aggregates | 32 | 32 | match |
| TCA usable-sample counts | 64 | 64 | match |
| Autotrader open-by-lane counts | 2 | 2 | match |
| Autotrader probation entries today | 0 | 0 | match |
| Bracket open-reconcile summary | 1 | 1 | match |

Recent service logs contained no `trading_trades` / `trading_management_envelopes`
relation errors, undefined-table errors, undefined-column errors, or Phase 5K
reader errors.

## Remaining Reference Classification

### Compatibility contract

The `Trade` ORM and the `trading_trades` view remain intentional compatibility
surfaces. They let legacy code continue to run while the semantic model is
gradually hardened around decisions, management envelopes, and positions.

### Live writer/order/broker/reconcile paths

These must not be converted by search-and-replace:

- `coinbase_service.py` sidecar/current-envelope repair writes.
- `bracket_reconciliation_service.py` stop adoption and bracket-intent repair.
- `auto_trader.py` option-link detach repair.
- Broker/order/stop/exit paths that still create or mutate management-envelope
  rows through the legacy `Trade` contract.

These paths are state transitions. They need explicit contract APIs, not a table
name swap.

### Live readers that still depend on trade-id semantics

The remaining live readers are coupled to envelope identity and bracket intent
identity:

- `auto_trader.py` synergy retry lookup.
- `auto_trader.py` probation-entry count.
- `auto_trader_rules.py` open-by-lane count.
- `bracket_reconciliation_service.py` open-trade/intention watchdog readers.

All of these matched old-vs-new relation checks, but their real dependency is
not the relation name. Their dependency is the still-valid contract that an
active management envelope has a stable `id` used by alerts, bracket intents,
and current trade state.

### Reporting / analytics candidates

Two small readers can move later, but they are not worth another live flag:

- `attribution_service.py` closed-pattern live stats.
- `cost_aware_gate.py` TCA usable-sample backing count.

Both matched old-vs-new checks. They should be handled by a shared envelope
reader contract in Phase 5L instead of another one-off flag.

### Migration, tests, history, scripts, docs

Remaining references in migrations, tests, historical reports, and old scripts
are expected and should remain until a dedicated compatibility-retirement pass.

## Architect/Data-Science Read

The system has crossed the important line: production data is flowing through
the renamed base table, and the old name is now a compatibility view. The live
decision inputs that can safely be compared as aggregates have all matched
through both surfaces.

The risk now is not data parity. The risk is contract confusion. A mechanical
rename would blur three different things:

1. immutable entry decisions,
2. mutable management envelopes,
3. broker-authoritative positions.

That is exactly the confusion this refactor is meant to remove. The next useful
phase should make those contracts explicit, then retire the compatibility view
only after readers and writers are using named APIs instead of raw legacy table
contracts.

## Recommendation

Close Phase 5K. Start Phase 5L as a contract-hardening pass:

1. Introduce/standardize management-envelope reader helpers for the remaining
   reporting and live-gate reads.
2. Add canaries that prevent new live code from adding raw `FROM trading_trades`
   reads.
3. Keep writer/order/broker/reconcile paths on the compatibility contract until
   they are migrated behind explicit envelope/position APIs.
4. Do not drop the `trading_trades` compatibility view yet.

This preserves live stability while moving the codebase away from the old mental
model.

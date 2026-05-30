# f-position-identity-phase-5k-live-path-cutover-brief

## Summary

Phase 5K should not start with a code cutover. The remaining
`trading_trades` references sit on live behavior boundaries: broker sync,
order management, reconciliation, stops, PDT, position integrity, capital
limits, portfolio risk, and promotion/scoring gates.

The right next move is a read-only parity probe for the live gates. That gives
us the data-science value first: prove the compatibility view and the semantic
base table produce identical inputs for capital and promotion decisions before
changing any live code.

## Current State

- `trading_management_envelopes` is the physical base table.
- `trading_trades` is the compatibility view.
- Phase 5I post-rename soak is still `COMPLETE_POSITIVE`.
- Phase 5J reader cleanup is closed; no more safe reader-only conversions
  remain.
- The Python `Trade` ORM class and `trading_trades` view remain compatibility
  contracts.

## Live-Path Matrix

### Permanent Keep

These references should stay indefinitely unless there is a separate public API
or ORM deprecation project.

| Area | Files | Reason |
|---|---|---|
| ORM/FK compatibility | `app/models/trading.py` | `Trade` remains the app's legacy envelope ORM. FKs from journal, packets, execution events, and intents still target the compatibility surface. |
| Migration history | `app/migrations.py` | Historical migrations must keep historical relation names and Phase 5H creates the compatibility view. |
| Test harness | `tests/conftest.py`, compatibility tests | Tests intentionally prove legacy callers keep working after the physical rename. |
| Rename/probe history | Phase 5F/5G/5I scripts and historical docs | They document or validate the old-vs-new transition. |
| Comments only | `app/config.py`, `broker_position_truth.py` | No runtime SQL to convert. |

### Writer Boundary

These paths should keep `Trade`/`trading_trades` until a larger envelope-writer
cutover is designed. They mutate broker-facing state or are adjacent to
mutation.

| Area | Files | Recommendation |
|---|---|---|
| Robinhood broker sync and inverse reconcile | `app/services/broker_service.py` | Keep. Future work should move behavior by responsibility, not table spelling. |
| Coinbase broker sync | `app/services/coinbase_service.py` | Keep. Similar boundary as Robinhood, with Coinbase-specific reconciliation. |
| AutoTrader placement and monitoring | `app/services/trading/auto_trader.py`, `auto_trader_rules.py` | Keep. Any cutover needs live-placement parity and rollback flags. |
| Bracket reconciliation | `app/services/trading/bracket_reconciliation_service.py` | Keep. Brackets are management-envelope state and broker-order state mixed together. |
| Exit monitors | `app/services/trading/options/exit_monitor.py` | Keep. Exit code is live capital code. |
| Coinbase orphan adoption | `app/services/trading/venue/coinbase_orphan_adopt.py` | Keep. Repair/adoption code should remain compatibility-first. |
| Repair/backfill scripts | `scripts/backfill_trade_stops.py`, `scripts/assign_patterns_to_open_trades.py`, one-off `d-*` repair scripts | Keep or retire separately. They are explicit repair tools, not analytics readers. |

### Feature-Flag Future

These are read-heavy but feed live capital, risk, promotion, or model decisions.
They are not safe for opportunistic cleanup. They are candidates for a future
flagged cutover only after a parity probe proves identical inputs.

| Area | Files | Why It Matters |
|---|---|---|
| Coinbase notional/concurrency cap | `app/services/trading/cost_aware_gate.py` | Directly blocks or allows live Coinbase orders. |
| PDT guard | `app/services/trading/pdt_guard.py` | Directly blocks equity entries. |
| Promotion/cohort/quality scoring | `pattern_cohort_promote.py`, `pattern_quality_score.py`, `alpha_portfolio_gate.py`, `net_edge_ranker.py` | Changes could promote/demote capital routes. |
| Pattern survival and training inputs | `pattern_survival/features.py`, `pattern_survival/training.py` | Changes model evidence, even if nominally read-only. |
| Portfolio risk and sizing | `portfolio_risk.py` | Direct capital sizing/risk surface. |
| Pattern/regime analytics feeding behavior | `pattern_regime_ledger.py`, `crypto/pattern_miner.py`, `execution_robustness.py` | Read-heavy but strategy-impacting. |
| Position integrity | `position_integrity.py` | Audit/repair surface; must be parity-proven before moving. |

### Dirty/Defer

These files already have unrelated local edits, so they must not be included in
a mechanical Phase 5K slice without inspecting and deliberately preserving the
current diff:

- `alpha_portfolio_gate.py`
- `net_edge_ranker.py`
- `pattern_quality_score.py`
- `pattern_regime_ledger.py`
- `pattern_survival/features.py`
- `pattern_survival/training.py`
- `portfolio_risk.py`
- `position_integrity.py`
- `scripts/analyze_trade_quality_funnel.py`

## Recommended Next Slice

Ship `f-position-identity-phase-5k-a-live-path-parity-probe`.

Scope:

1. Add a read-only probe script that compares the compatibility view against the
   semantic base table for the live-path query families:
   - Coinbase venue cap open-notional/open-count
   - PDT day-trade count
   - promotion/cohort realized aggregates
   - pattern-quality realized aggregates
   - portfolio-risk open exposure aggregates
   - position-integrity open-envelope linkage
2. Emit `COMPLETE_POSITIVE` only when every old-vs-new aggregate matches.
3. Add a small test that pins the probe as read-only and confirms it uses both
   relation names intentionally.
4. Do not change any live decision path.

This gives us a clean evidence layer: if the probe stays green for a soak
window, Phase 5K-B can choose one low-risk live gate and convert it behind a
feature flag.

## What Not To Do

- Do not drop `trading_trades`.
- Do not rename `Trade`.
- Do not search-and-replace live paths.
- Do not convert broker/order/reconcile/exit code just because the table is now
  a view.
- Do not mix this with unrelated dirty edits.

## Verification For This Brief

Commands run:

```powershell
python -m pytest tests\test_phase5j_reader_cleanup.py tests\test_phase5i_post_rename_probe.py
python scripts\d-phase5i-post-rename-soak-probe.py
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\dispatch-phase5i-post-rename-soak-probe.ps1
```

Results:

- 7 tests passed
- Direct Phase 5I probe: `COMPLETE_POSITIVE`
- Scheduled-wrapper Phase 5I probe: `COMPLETE_POSITIVE`
- `LOG_SCHEMA_ERRORS=0`

## Architect Read

The live system is now in a healthy compatibility posture. We already got the
safe semantic cleanup value in Phase 5J. Phase 5K should be evidence-first:
measure parity on the live decision surfaces, then cut over one gate at a time
only when the probe proves the data is identical.

No live trading behavior changed in this brief.

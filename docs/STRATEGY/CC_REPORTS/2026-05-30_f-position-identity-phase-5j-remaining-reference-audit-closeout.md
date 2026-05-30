# f-position-identity-phase-5j-remaining-reference-audit-closeout

## Summary

Phase 5J's remaining-reference audit is complete. After slices 1-5, there are
no more obviously safe reader-only `trading_trades` conversions left.

The remaining references are intentionally left in place because they are one of:

- compatibility contracts (`Trade` ORM, FK metadata, tests, migration history)
- live writer/order/broker/stop/reconcile paths
- live capital or promotion-decision gates where a cosmetic table-name change is
  not worth the blast radius
- historical scripts/docs/probes that explicitly describe the rename
- files already carrying unrelated local edits

`trading_trades` therefore stays as the compatibility view and the Python
`Trade` ORM class stays named `Trade`.

## Inventory

Current exact-reference inventory was generated with:

```powershell
rg -n "\btrading_trades\b" app scripts tests docs
```

The app-level runtime files still containing `trading_trades` are:

- `app/config.py`
- `app/migrations.py`
- `app/models/trading.py`
- `app/services/broker_service.py`
- `app/services/coinbase_service.py`
- `app/services/trading/alpha_portfolio_gate.py`
- `app/services/trading/auto_trader.py`
- `app/services/trading/auto_trader_rules.py`
- `app/services/trading/bracket_reconciliation_service.py`
- `app/services/trading/broker_position_truth.py`
- `app/services/trading/cost_aware_gate.py`
- `app/services/trading/crypto/pattern_miner.py`
- `app/services/trading/execution_robustness.py`
- `app/services/trading/net_edge_ranker.py`
- `app/services/trading/options/exit_monitor.py`
- `app/services/trading/options/portfolio_budget.py`
- `app/services/trading/pattern_cohort_promote.py`
- `app/services/trading/pattern_quality_score.py`
- `app/services/trading/pattern_regime_ledger.py`
- `app/services/trading/pattern_survival/features.py`
- `app/services/trading/pattern_survival/training.py`
- `app/services/trading/pdt_guard.py`
- `app/services/trading/portfolio_risk.py`
- `app/services/trading/position_integrity.py`
- `app/services/trading/venue/coinbase_orphan_adopt.py`

## Classification

KEEP:

- `app/models/trading.py`: ORM table binding, FKs, and compatibility metadata.
- `app/migrations.py`: historical migrations plus the Phase 5H compatibility
  view migration. These must preserve historical table names.
- `tests/conftest.py` and compatibility tests: intentionally prove legacy
  callers still work through the view.
- Phase 5F/5G/5I scripts and historical docs: explicitly discuss the rename,
  view, or old-vs-new parity.
- `app/config.py` and `broker_position_truth.py`: comments only.

DEFER:

- Live writer/order/reconcile/stop paths:
  `broker_service.py`, `coinbase_service.py`, `auto_trader.py`,
  `auto_trader_rules.py`, `bracket_reconciliation_service.py`,
  `options/exit_monitor.py`, `pdt_guard.py`, `position_integrity.py`,
  `venue/coinbase_orphan_adopt.py`, and repair/backfill scripts.
- Live capital or promotion-decision readers:
  `cost_aware_gate.py`, `pattern_cohort_promote.py`, `pattern_quality_score.py`,
  `alpha_portfolio_gate.py`, `pattern_survival/*`, `portfolio_risk.py`,
  `crypto/pattern_miner.py`, and `execution_robustness.py`.
  These are read-heavy, but they participate in live gating/model decisions, so
  they need an owner-reviewed behavior-neutral cutover plan, not another
  opportunistic Phase 5J slice.
- Dirty local candidates:
  `alpha_portfolio_gate.py`, `net_edge_ranker.py`,
  `pattern_quality_score.py`, `pattern_regime_ledger.py`,
  `pattern_survival/features.py`, `pattern_survival/training.py`,
  `portfolio_risk.py`, `position_integrity.py`,
  and `scripts/analyze_trade_quality_funnel.py`.

CONVERT:

- None in this pass.

## Verification

Commands run:

```powershell
python -m pytest tests\test_phase5j_reader_cleanup.py tests\test_phase5i_post_rename_probe.py
python scripts\d-phase5i-post-rename-soak-probe.py
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\dispatch-phase5i-post-rename-soak-probe.ps1
```

Results:

- Phase 5J guard tests: 6 passed
- Phase 5I probe test: passed
- Direct Phase 5I probe: `COMPLETE_POSITIVE`
- Scheduled-wrapper Phase 5I probe: `COMPLETE_POSITIVE`
- `LOG_SCHEMA_ERRORS=0`

Latest live probe values:

- `FRESH_DECISIONS=20`
- `FRESH_ENVELOPES=20`
- `FRESH_CLOSES=10`
- `FRESH_CLOSE_MISMATCHES=0`
- `HARD_LINKAGE_ISSUES=0`
- `MISMATCHED_ROWS=0`
- `MISMATCHED_PNL=0.0000`

## Architect Read

Phase 5J is done. The semantic reader cleanup has harvested the safe value:
dashboards, probes, read models, analytics helpers, and learning/reporting
readers now use `trading_management_envelopes`.

The rest is not cleanup. It is live-system contract work. Changing live
order-management, risk, promotion, or reconciliation paths purely to remove the
legacy view name would create risk without improving the model. The correct
next phase is a design-and-test brief for live path cutover, not a broader
search-and-replace.

No live trading behavior changed in this closeout.

# Phase 5O Brain Plasticity Envelope Audit

Date: 2026-05-31

## Verdict

`app/services/trading/brain_neural_mesh/plasticity.py` is a learning-mutation
path and must stay a future rename blocker. It is not passive
learning/reporting code.

The plasticity engine consumes closed management-envelope outcomes and can
write `brain_graph_edge_mutations` audit rows. When
`chili_mesh_plasticity_dry_run` is disabled, it can also mutate
`BrainGraphEdge.weight`. That makes its legacy `Trade` surface a model-weight
mutation input, not a harmless reader.

No plasticity behavior was converted in this slice.

## Behavior Boundary

The key path is `handle_trade_close_plasticity(...)`:

- Requires a closed management envelope.
- Reads `mesh_entry_correlation_id`.
- Computes risked capital from `entry_price`, `stop_loss`, and `quantity`.
- Reads `pnl`.
- Calls `apply_outcome_plasticity(...)`.
- Finds activation-path edges for the correlation id.
- Writes mutation audit rows and, when not dry-run, updates edge weights.

Any rename/conversion here needs explicit old-vs-new evidence and a narrow
behavior-preserving conversion. Bulk symbol rename would be too risky.

## Evidence Added

Added read-only probe:

```text
scripts/d-phase5o-brain-plasticity-envelope-parity-probe.py
```

The probe does not call the plasticity engine and does not mutate graph
weights. It compares closed outcome evidence through:

- `trading_trades` compatibility view
- `trading_management_envelopes` physical table

Live result:

```text
VERDICT_STATUS=COMPLETE_POSITIVE
VERDICT_REASON=4 brain plasticity outcome checks matched
BRAIN_PLASTICITY_MISMATCHES=0
closed_correlation_rows old=111 new=111
eligible_trade_ids old=111 new=111
eligible_with_path_trade_ids old=111 new=111
path_edge_counts old=111 new=111
RELATION_KINDS={'trading_management_envelopes': 'r', 'trading_trades': 'v'}
```

Added focused tests:

```text
tests/test_phase5o_brain_plasticity_probe.py
```

## Verification

- `python -m py_compile app\services\trading\brain_neural_mesh\plasticity.py scripts\d-phase5o-brain-plasticity-envelope-parity-probe.py scripts\analyze_phase5_remaining_trade_refs.py`
  passed.
- Focused tests passed:
  `tests/test_phase5o_brain_plasticity_probe.py` and
  `tests/test_phase5o_remaining_runtime_compat_map.py`
  (`6 passed`, one existing SQLAlchemy sorted-table warning).
- Analyzer stayed clean:
  `orm_trade_symbol_compat = 66`, raw reader bucket `{}`, no unexpected
  runtime readers or mutations.
- Phase 5K live-path parity remained `COMPLETE_POSITIVE`.
- Phase 5I post-rename soak remained `COMPLETE_POSITIVE`.
- Source posture remains `ALERT` because app services are still mounted from
  dirty root by a shared/external process; this slice did not restart Postgres,
  touch `.env`, refresh runtime, or change live behavior.

## Inventory Movement

```text
adapter_candidate: 7 -> 6
future_rename_blocker: 43 -> 44
orm_trade_symbol_compat: 66 unchanged
learning_research_reporting: 6 unchanged
```

## Next Recommended Slice

Audit `app/services/trading/learning.py`. It is the largest remaining adapter
candidate and almost certainly contains lifecycle, demotion, promotion, and
realized-stat behavior. Treat it as high blast-radius until proven otherwise.

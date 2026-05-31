# Phase 5O Brain Action Handlers Envelope Audit

Date: 2026-05-31

## Verdict

`app/services/trading/brain_neural_mesh/action_handlers.py` is
live-action-adjacent and must stay a future rename blocker. It is not passive
learning/reporting code.

The handler is the `nm_action_signals` dispatch authority. Critical mesh child
signals can become Telegram alerts, but only after the handler revalidates the
referenced local management envelope and broker-position truth. That local
`Trade` read therefore controls whether critical alerts are dispatched or
suppressed.

No action-handler behavior was converted in this slice.

## Behavior Boundary

The key path is `_critical_trade_broker_live(...)`:

- Reads `trade_id` from child mesh `local_state`.
- Loads the local management envelope.
- Suppresses critical dispatch if the local row is missing.
- Suppresses critical dispatch if the local row is not `open`.
- Calls `broker_stale_open_trade_snapshot(...)` to suppress stale broker
  position alerts.
- Allows the alert dispatch path only when local and broker-position truth pass.

That is an alert/action safety gate. Any rename/conversion here needs explicit
old-vs-new evidence and a narrow behavior-preserving conversion.

## Evidence Added

Added read-only probe:

```text
scripts/d-phase5o-brain-action-handlers-envelope-parity-probe.py
```

The probe does not dispatch alerts and does not call broker APIs. It compares
the local validation inputs used by `_critical_trade_broker_live(...)` through:

- `trading_trades` compatibility view
- `trading_management_envelopes` physical table

Live result:

```text
VERDICT_STATUS=COMPLETE_POSITIVE
VERDICT_REASON=6 brain action-handler local validation checks matched
MESH_CHILD_STATE_ROWS=2
BRAIN_ACTION_HANDLER_MISMATCHES=0
all_child_trade_ids old=2 new=2
critical_child_trade_ids old=0 new=0
local_trade_validation_rows old=2 new=2
missing_child_trade_ids old=0 new=0
non_open_child_trade_ids old=1 new=1
open_child_trade_ids old=1 new=1
RELATION_KINDS={'trading_management_envelopes': 'r', 'trading_trades': 'v'}
```

Added focused tests:

```text
tests/test_phase5o_brain_action_handlers_probe.py
```

## Verification

- `python -m py_compile app\services\trading\brain_neural_mesh\action_handlers.py scripts\d-phase5o-brain-action-handlers-envelope-parity-probe.py scripts\analyze_phase5_remaining_trade_refs.py`
  passed.
- Focused tests passed:
  `tests/test_phase5o_brain_action_handlers_probe.py`,
  `tests/test_broker_truth_safety.py`, and
  `tests/test_phase5o_remaining_runtime_compat_map.py`
  (`12 passed`; existing SQLAlchemy and utcnow warnings only).
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
learning_research_reporting: 7 -> 6
live_action_broker_reconcile: 19 -> 20
adapter_candidate: 8 -> 7
future_rename_blocker: 42 -> 43
orm_trade_symbol_compat: 66 unchanged
```

## Next Recommended Slice

Audit `app/services/trading/brain_neural_mesh/plasticity.py`. It is adjacent to
action handling and may mutate mesh weights or consume realized outcomes, so it
deserves the next evidence-first pass before any rename/conversion pressure.

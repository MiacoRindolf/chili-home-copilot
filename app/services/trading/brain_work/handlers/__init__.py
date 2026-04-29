"""Event handlers for brain_work_events (Phase 2 of FIX 31 endgame).

Each handler in this package subscribes to a specific event_type and replaces
a step of the legacy ``run_learning_cycle`` reconcile pass. The handler runs
when its event arrives via the durable work ledger; no monolithic cycle needed.

Wiring lives in ``../dispatcher.py`` — see ``run_brain_work_dispatch_round``.

Phase 2 ship order (smallest blast radius first):
  1. ``mine`` — reacts to ``market_snapshots_batch`` outcome events.
  2. ``cpcv_gate`` — reacts to ``backtest_completed`` events; runs CPCV
     promotion gate, sets pattern lifecycle stage.
  3. ``promote`` / ``demote`` — react to gate decisions.
  4. ``regime_ledger`` — reacts to trade close events.

Once handlers 1-4 ship and prove out in shadow, ``run_learning_cycle`` and
the FIX 31 reconcile-pass gate are deleted.
"""

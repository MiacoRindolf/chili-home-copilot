# Phase 5O Stale-Promoted Sweep Envelope Audit

Date: 2026-05-31

## Verdict

`app/services/trading/cron_jobs/stale_promoted_sweep.py` is lifecycle-sensitive
and must stay a future rename blocker. It is not a passive learning/reporting
reader.

The sweep reads the latest closed live management envelope per promoted pattern
and demotes stale promoted patterns when the realized-EV gate fails. That means
the legacy `Trade` dependency influences pattern eligibility and therefore
future trading behavior.

No sweep behavior was converted in this slice.

## What The Sweep Controls

- Selects active promoted patterns from `scan_patterns`.
- Reads `max(exit_date)` by `scan_pattern_id` from the legacy `Trade`
  compatibility view.
- Skips patterns with a close in the last 7 days.
- Re-checks stale promoted patterns through `evaluate_realized_ev(...)`.
- Mutates `lifecycle_stage` to `challenged` when the EV gate fails.

That is a lifecycle demotion path. Any rename/conversion here needs explicit
old-vs-new evidence and then a narrow behavior-preserving conversion, not a
bulk symbol rename.

## Evidence Added

Added read-only probe:

```text
scripts/d-phase5o-stale-promoted-sweep-envelope-parity-probe.py
```

The probe compares the sweep's latest-exit/stale-eligibility scope through:

- `trading_trades` compatibility view
- `trading_management_envelopes` physical table

Live result:

```text
VERDICT_STATUS=COMPLETE_POSITIVE
VERDICT_REASON=3 stale-promoted sweep checks matched
PROMOTED_PATTERN_COUNT=4
STALE_PROMOTED_MISMATCHES=0
latest_exit_by_pattern old=3 new=3
recent_pattern_ids old=2 new=2
stale_candidate_pattern_ids old=2 new=2
RELATION_KINDS={'trading_management_envelopes': 'r', 'trading_trades': 'v'}
```

Added focused tests:

```text
tests/test_phase5o_stale_promoted_sweep_probe.py
```

## Verification

- `python -m py_compile app\services\trading\cron_jobs\stale_promoted_sweep.py scripts\d-phase5o-stale-promoted-sweep-envelope-parity-probe.py scripts\analyze_phase5_remaining_trade_refs.py`
  passed.
- Focused tests passed:
  `tests/test_phase5o_stale_promoted_sweep_probe.py`,
  `tests/test_cron_stale_promoted.py`, and
  `tests/test_phase5o_remaining_runtime_compat_map.py`
  (`14 passed`, one existing SQLAlchemy sorted-table warning).
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
adapter_candidate: 9 -> 8
future_rename_blocker: 41 -> 42
orm_trade_symbol_compat: 66 unchanged
learning_research_reporting: 7 unchanged
```

## Next Recommended Slice

Audit `app/services/trading/brain_neural_mesh/action_handlers.py`. It is still
classified as `learning_research_reporting / adapter_candidate`, but the name
and location imply action-state handling. Treat it as suspicious until proven
passive.

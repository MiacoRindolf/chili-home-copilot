# Phase 5O learning.py Envelope Audit

Date: 2026-05-31

## Verdict

`app/services/trading/learning.py` is a realized-evidence writer and must stay a
future rename blocker. It is not a simple read-only adapter candidate.

The file consumes closed management-envelope evidence to reinforce pattern
insights, write close journals, aggregate corrected ScanPattern stats, and
summarize setup-vitals degradation outcomes. Those paths can affect future
promotion, demotion, pattern confidence, journaled learning, and model evidence.

No `learning.py` behavior was converted in this slice.

## Behavior Boundary

Audited legacy `Trade` usage in these surfaces:

- `get_attribution_coverage_stats(...)` counts closed rows and pattern-linked
  closed rows by user.
- `analyze_closed_trade(...)` consumes closed-envelope PnL, entry/exit prices,
  direction, indicator snapshots, scan pattern linkage, and exit reason to
  reinforce pattern insights and write journal output.
- `update_pattern_stats_from_closed_trades(...)` reads the last 180 days of
  closed, pattern-linked envelopes and writes corrected ScanPattern evidence
  plus `pattern_evidence_corrections` audit rows.
- `_vitals_history_learning_summary(...)` joins setup-vitals history to closed
  envelopes and summarizes degradation-vs-outcome behavior.

This is high-blast-radius learning state, not passive reporting. Any future
rename/conversion should be a narrow behavior-preserving helper swap with
focused parity evidence.

## Evidence Added

Added read-only probe:

```text
scripts/d-phase5o-learning-envelope-parity-probe.py
```

The probe does not call any learning writer. It compares the legacy
`trading_trades` compatibility view against the physical
`trading_management_envelopes` table across the learning evidence scopes.

Live result:

```text
VERDICT_STATUS=COMPLETE_POSITIVE
VERDICT_REASON=6 learning.py evidence checks matched
LEARNING_MISMATCHES=0
actual_trade_count_by_pattern old=39 new=39
attribution_coverage_by_user old=3 new=3
closed_trade_analysis_rows old=526 new=526
evidence_correction_closed_rows old=314 new=314
evidence_pattern_buckets old=30 new=30
setup_vitals_closed_join old=5000 new=5000
RELATION_KINDS={'trading_management_envelopes': 'r', 'trading_trades': 'v'}
```

Added focused tests:

```text
tests/test_phase5o_learning_probe.py
```

## Verification

- `python -m py_compile app\services\trading\learning.py scripts\d-phase5o-learning-envelope-parity-probe.py scripts\analyze_phase5_remaining_trade_refs.py`
  passed.
- Focused tests passed:
  `tests/test_phase5o_learning_probe.py` and
  `tests/test_phase5o_remaining_runtime_compat_map.py`
  (`6 passed`, one existing SQLAlchemy sorted-table warning).
- Analyzer stayed clean for unexpected runtime readers/mutations:
  `orm_trade_symbol_compat = 66`, no unexpected runtime readers or mutations.
  The only raw SQL reader bucket is historical migration/test compatibility.
- Phase 5K live-path parity remained `COMPLETE_POSITIVE`.
- Phase 5I post-rename soak remained `COMPLETE_POSITIVE`.
- Source posture remains `ALERT` because app services are still mounted from
  dirty root `D:\dev\chili-home-copilot` by a shared/external process. This
  slice did not restart Postgres, touch `.env`, refresh runtime, or change live
  behavior.

## Inventory Movement

```text
adapter_candidate: 6 -> 5
future_rename_blocker: 44 -> 45
orm_trade_symbol_compat: 66 unchanged
learning_research_reporting: 6 unchanged
```

## Next Recommended Slice

Audit `app/services/trading/learning_cycle_architecture.py`. It is the next
learning-adjacent adapter candidate and likely controls step-state/status around
learning-cycle execution. Treat it as behavior-bearing until proven otherwise.

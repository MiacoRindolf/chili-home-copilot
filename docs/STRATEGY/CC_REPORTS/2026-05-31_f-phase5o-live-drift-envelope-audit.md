# Phase 5O live_drift Envelope Audit

Date: 2026-05-31

## Verdict

`app/services/trading/live_drift.py` is a live-drift validation and lifecycle
path, not a private helper adapter candidate.

It reads closed live management-envelope outcomes and closed paper outcomes,
builds runtime scorecards, writes validation contracts, nudges confidence, and
can auto-challenge promoted/live patterns when drift is critical. That makes its
legacy `Trade` surface behavior-bearing.

No live-drift behavior was converted in this slice.

## Behavior Boundary

Audited legacy `Trade` usage in these surfaces:

- `aggregate_runtime_samples(...)` reads closed live rows by
  `scan_pattern_id`, `user_id`, and exit window, then counts live wins.
- `aggregate_runtime_scorecards(...)` reads the same closed live rows and feeds
  `_scorecard(...)`, which consumes return, PnL, slippage, and freshness inputs.
- `run_live_drift_refresh(...)` applies the scorecards to active repeatable-edge
  promoted/live `ScanPattern` rows.
- `apply_live_drift_v2_to_pattern(...)` can transition a promoted/live pattern
  to `challenged` when critical drift is confirmed and auto-challenge is enabled.

Any rename/conversion here must preserve scorecard inputs and lifecycle behavior.

## Evidence Added

Added read-only probe:

```text
scripts/d-phase5o-live-drift-envelope-parity-probe.py
```

The probe does not call `run_live_drift_refresh(...)` and does not write
validation contracts. It compares the live closed-envelope inputs through:

- `trading_trades` compatibility view
- `trading_management_envelopes` physical table

Live result with explicit read-only probe user `PHASE5O_LIVE_DRIFT_USER_ID=1`:

```text
VERDICT_STATUS=COMPLETE_POSITIVE
VERDICT_REASON=3 live_drift live-runtime checks matched
LIVE_DRIFT_PATTERN_COUNT=1
LIVE_DRIFT_MISMATCHES=0
live_runtime_rows old=13 new=13
live_slippage_inputs old=13 new=13
live_win_counts old=1 new=1
RELATION_KINDS={'trading_management_envelopes': 'r', 'trading_trades': 'v'}
```

The host process did not have `brain_default_user_id` populated, so the probe
uses an explicit probe-only override to exercise the read surface. This changes
no app/runtime behavior.

Added focused tests:

```text
tests/test_phase5o_live_drift_probe.py
```

## Verification

- `python -m py_compile app\services\trading\live_drift.py scripts\d-phase5o-live-drift-envelope-parity-probe.py scripts\analyze_phase5_remaining_trade_refs.py`
  passed.
- Focused tests passed:
  `tests/test_phase5o_live_drift_probe.py` and
  `tests/test_phase5o_remaining_runtime_compat_map.py`
  (`7 passed`, one existing SQLAlchemy sorted-table warning).
- Analyzer reported no unexpected runtime readers/mutations.
- Phase 5K live-path parity remained `COMPLETE_POSITIVE`.
- Phase 5I post-rename soak remained `COMPLETE_POSITIVE`.
- Source posture remains `ALERT` because app services are still mounted from
  dirty root `D:\dev\chili-home-copilot` by a shared/external process. This
  slice did not restart Postgres, touch `.env`, refresh runtime, or change live
  behavior.

## Inventory Movement

```text
adapter_candidate: 4 -> 3
future_rename_blocker: 45 -> 46
private_helper_type_only: 5 -> 4
learning_research_reporting: 5 -> 6
orm_trade_symbol_compat: 65 unchanged
```

## Next Recommended Slice

Audit `app/services/trading/autotrader_desk.py`. It is one of the final three
adapter candidates and likely sits near operator-visible trading desk status,
so treat it as private/helper only after direct source proof.

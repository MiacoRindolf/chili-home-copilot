# Phase 5O Position Plan Generator Envelope Audit

Date: 2026-05-31

## Verdict

`app/services/trading/position_plan_generator.py` is a live/risk-adjacent
position-plan advisory surface, not a private helper adapter candidate.

It directly loads open live management envelopes, enriches them with current
quote inputs, latest monitor decisions, alert trade plans, pattern metadata,
option price-domain metadata, and cached `trade_ids`, then sends a material
position context to an LLM and persists plan-cache rows. That makes its legacy
`Trade` surface behavior-bearing.

No position-plan behavior was converted in this slice.

## Behavior Boundary

Audited legacy `Trade` usage in these surfaces:

- `generate_position_plans(...)` selects open live rows by user, status, and
  positive entry price before quote lookup and LLM planning.
- `_build_position_context(...)` transforms each live row into the LLM context,
  including stop/target levels, option price domains, pattern evidence, alert
  trade-plan summaries, latest monitor decisions, and bars-held.
- `_backfill_trade_ids_on_plans(...)` maps cached plan tickers back to open
  live row ids so UI/actions retain stable `trade_id` payloads.
- `_get_cached_plans(...)`, `_get_cached_plans_by_material_signature(...)`, and
  `_persist_plans(...)` key cached plans by the current live row id set.

Any rename/conversion here must preserve live plan inputs and cache-key
semantics.

## Evidence Added

Added read-only probe:

```text
scripts/d-phase5o-position-plan-generator-envelope-parity-probe.py
```

The probe does not call the LLM and does not mutate the plan cache. It compares
the open-position plan inputs through:

- `trading_trades` compatibility view
- `trading_management_envelopes` physical table

Live result with explicit read-only probe user
`PHASE5O_POSITION_PLAN_USER_ID=1`:

```text
VERDICT_STATUS=COMPLETE_POSITIVE
VERDICT_REASON=4 position-plan checks matched
PROBE_USER_ID=1
POSITION_PLAN_MISMATCHES=0
open_plan_rows old=8 new=8
plan_cache_trade_ids old=8 new=8
plan_context_rows old=8 new=8
plan_quote_inputs old=2 new=2
RELATION_KINDS={'trading_management_envelopes': 'r', 'trading_trades': 'v'}
```

Added focused tests:

```text
tests/test_phase5o_position_plan_generator_probe.py
```

## Verification

- `python -m py_compile scripts\d-phase5o-position-plan-generator-envelope-parity-probe.py app\services\trading\position_plan_generator.py scripts\analyze_phase5_remaining_trade_refs.py`
  passed.
- JSON map validation passed.
- Focused tests passed:
  `tests/test_phase5o_position_plan_generator_probe.py` and
  `tests/test_phase5o_remaining_runtime_compat_map.py`
  (`7 passed`, one existing SQLAlchemy sorted-table warning).
- Analyzer reported no unexpected runtime readers/mutations.
- Phase 5K live-path parity remained `COMPLETE_POSITIVE`.
- Phase 5I post-rename soak remained `COMPLETE_POSITIVE`.
- Source posture remains `ALERT` because app services are still mounted from
  dirty root `D:\dev\chili-home-copilot` by a shared/external process. This
  slice did not restart Postgres, touch `.env`, refresh runtime, mutate DB, or
  change live behavior.

## Inventory Movement

```text
adapter_candidate: 1 -> 0
future_rename_blocker: 47 -> 48
private_helper_type_only: 3 -> 2
risk_capital_gate: 21 -> 22
orm_trade_symbol_compat: 64 unchanged
```

## Next Recommended Slice

Phase 5O adapter triage is complete: no unclassified adapter candidates remain.
The next step should be a Phase 5O closeout/sequencing pass that summarizes the
remaining 48 future rename blockers by risk class before any broader ORM rename
is attempted.

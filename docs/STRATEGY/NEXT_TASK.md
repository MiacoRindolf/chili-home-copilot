# NEXT_TASK: f-execution-truth-wiring

STATUS: DONE

## Goal

**Phase B of evidence-fidelity-architecture.** Wire the existing
`record_fill_observation` function (venue_truth.py) into the
fill-reconcile path. `trading_venue_truth_log` and
`trading_execution_cost_estimates` are currently empty despite 15k+
rows in `trading_execution_events`. Codex correctly identified this
as the fastest path to live-net-PnL improvement — the alpha is from
avoiding setups whose realized cost exceeds gross edge.

## Brief

`docs/STRATEGY/QUEUED/f-execution-truth-wiring.md`

Parent: `docs/STRATEGY/QUEUED/f-evidence-fidelity-architecture-2026-05-14.md`
Phase A (shipped): commit `ca1705f` — corrected_* + raw_realized_*
columns + `pattern_stats_accessor.py` helper.

## Deliverables (per brief)

1. `app/services/trading/bracket_reconciler.py` — add
   `record_fill_observation` call after each fill committed to DB
2. `app/services/trading/cost_aware_gate.py` (or wherever cost estimate
   originates) — wire `persist_cost_estimate` call
3. `tests/test_execution_truth_wiring.py` — fill-reconcile fixture
   triggers a venue_truth_log row write
4. `scripts/venue-truth-backfill.ps1` — historical backfill from
   `trading_execution_events`, operator-controlled, `-DryRun` default
5. CC_REPORT

## Hard constraints

- No changes to broker code (`venue/coinbase_spot.py`, `venue/robinhood_spot.py`)
- No autotrader main-path changes — wire point is in the reconciler
- `record_fill_observation` mode stays "shadow" at merge
- Backfill script `-DryRun` default
- TEST_DATABASE_URL must end in `_test`

## Consult gate

Source-of-truth for `expected_cost_fraction` — `bracket_intent.expected_*`
(placement time) vs `cost_aware_gate` pre-trade output? Brief assumes
the former. CC should grep + confirm.

## Why this is high-impact

Once populated, NetEdge consumes real per-broker, per-ticker, per-
time-of-day realized costs instead of static assumptions. Currently
NetEdge defaults to systematically wrong cost estimates because the
truth tables are empty.

## Phase C/D/E briefs already written (chain continues automatically after B)

- `f-triple-barrier-activation.md`
- `f-netedge-live-wiring.md`
- `f-multiple-testing-discipline.md`

# f-execution-truth-wiring (Phase B of evidence-fidelity-architecture)

> **Type:** Wire existing function into the fill-reconcile path
> **Parent:** `docs/STRATEGY/QUEUED/f-evidence-fidelity-architecture-2026-05-14.md`

## Goal

`record_fill_observation` in `app/services/trading/venue_truth.py` is
fully implemented but has zero production callers. The
`trading_venue_truth_log` and `trading_execution_cost_estimates` tables
are empty. 15,000+ rows in `trading_execution_events` give us all the
raw material to populate them — we just never connect the pipes.

## Design

### Wire point

In `bracket_reconciler.py` (or the broker-sync path that processes
fills — locate the function that turns a broker fill into a closed
`Trade` row). After each fill is reconciled:

```python
from .venue_truth import FillObservation, record_fill_observation

obs = FillObservation(
    trade_id=trade.id,
    ticker=trade.ticker,
    side=trade.direction,
    notional_usd=abs(trade.entry_price * trade.quantity),
    expected_spread_bps=bracket_intent.expected_spread_bps,
    realized_spread_bps=_compute_realized_spread_bps(fill, quote_at_send),
    expected_slippage_bps=bracket_intent.expected_slippage_bps,
    realized_slippage_bps=_compute_realized_slippage_bps(fill, quote_at_send),
    expected_cost_fraction=bracket_intent.expected_cost_fraction,
    realized_cost_fraction=_compute_realized_cost_fraction(fill),
    paper_bool=False,
)
record_fill_observation(db, obs)
```

Same hook in the paper-trade closure path (with `paper_bool=True`).

### Source-of-truth fields

| Field | Source |
|---|---|
| `expected_*` | `bracket_intent` (created at order placement; has expected cost from `cost_aware_gate`) |
| `realized_*` | `trading_execution_events` (fill records) compared against `quote_at_send` snapshot |
| `paper_bool` | True for `PaperTrade` rows, False for `Trade` rows |

### Cost estimate persistence

Parallel wire to `execution_cost_model.persist_cost_estimate` (find
the corresponding write function in `execution_cost_model.py`). Call
it before placement so we can A/B compare expected vs realized later.

## Deliverables

1. **`app/services/trading/bracket_reconciler.py`** — add the
   `record_fill_observation` call after each fill is committed to DB
2. **`app/services/trading/cost_aware_gate.py`** (or wherever the cost
   estimate originates) — wire `persist_cost_estimate` call
3. **`tests/test_execution_truth_wiring.py`** — fill-reconcile fixture
   triggers a venue_truth_log row write
4. **Mini-backfill** `scripts/venue-truth-backfill.ps1` — for past 30
   days of `trading_execution_events` rows, compute and write historical
   venue_truth observations. Optional, operator-controlled.
5. **CC_REPORT**: `docs/STRATEGY/CC_REPORTS/2026-05-14_execution-truth-wiring.md`

## Hard constraints

- No changes to broker code (`venue/coinbase_spot.py`, `venue/robinhood_spot.py`).
- No changes to autotrader main path; the wire point is in the
  reconciler that already runs post-fill.
- Backfill script is `-DryRun` default.
- record_fill_observation `mode` flag stays "shadow" at merge —
  observations get written but don't gate live trading until operator
  flips to "authoritative" (separate decision).

## Consult gate

Confirm source-of-truth for `expected_cost_fraction`: is it
`bracket_intent.expected_*` (at placement time) or `cost_aware_gate`
pre-trade output? Brief assumes the former. CC should grep and
confirm.

## Why this is high-impact

Once populated, the venue_truth_log enables:
- **NetEdge** consumes real per-broker, per-ticker, per-time-of-day
  realized costs instead of static cost assumptions
- **cost_aware_gate** auto-tunes its expected-cost estimates from
  realized history (closed-loop calibration)
- **Operator visibility:** the Phase 4 runtime tab can show
  "patterns whose realized cost > expected by >X bps" — direct alpha
  drain visibility

Codex's claim that this is the "fastest path to live-net-PnL
improvement" is correct because the alpha is from *not trading*
setups whose realized cost exceeds gross edge.

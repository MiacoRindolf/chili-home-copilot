# f-fastpath-realized-decay-conditioned-admission

STATUS: QUEUED
SLUG: fastpath-realized-decay-conditioned-admission
PROPOSED: 2026-05-17
REQUESTED_BY: architect re-eval — the cost-aware gate has structural-edge math; sharpen it to realized-edge math
PREREQUISITE_FOR: live-money fast-path activation (the gate that decides "yes this alert is worth a real trade")
COMPLEMENTARY_TO: f-fastpath-confluence-signals (improves alert quality at source), f-fastpath-extended-hold-momentum (changes the holding-period assumption the gate uses)

## TL;DR

The cost-aware admission gate (`gates.py::gate_cost_aware_admission`) rejects ~85% of post-emit_short-gate decisions with `negative_edge`. That rejection is computed from a **theoretical** edge assumption (constant per alert_type) vs the median spread. Replace it with a **realized** edge lookup per `(ticker, alert_type, score_bucket)` from the existing `fast_signal_decay` table. The gate becomes "did this bucket actually pay off historically, and by how much net of fees?" — a much sharper filter than "is the structural edge bigger than spread?"

## Why

Current cost-aware gate (paraphrasing — verify in `app/services/trading/fast_path/gates.py`):

```
expected_edge_bps = STATIC_EDGE_BY_ALERT_TYPE[alert_type]
required_edge_bps = 2 * (taker_fee_bps + median_spread_bps_for_ticker)
admit if expected_edge_bps > required_edge_bps
```

Two problems:

1. **`STATIC_EDGE_BY_ALERT_TYPE` is a guess.** It's a constant calibrated once at design time. It doesn't reflect the actual realized edge of the alert in production.

2. **No ticker conditioning on edge.** `imbalance_long` on BTC-USD has very different realized edge than on DOGE-USD; the static value treats them identically.

The `fast_signal_decay` table has been accumulating since 2026-05-01 with 1m/5m/15m/30m realized forward-returns per `(ticker, alert_type, score_bucket)`. **15+ days of empirical data sitting unused for gating.** Replace the static lookup with a realized-edge query.

## Design

### New gate function

```python
def gate_realized_decay_admission(
    db, alert, *, settings, lookback_window_h: int = 168,  # 7 days
) -> tuple[bool, str | None]:
    """Admit only if the realized historical edge for this bucket exceeds
    the realized cost (round-trip fees + median realized slippage).

    Reads fast_signal_decay for the matching bucket; rejects if:
      - bucket has < min_bucket_n samples (insufficient evidence)
      - realized_avg_5m_bps <= cost_threshold_bps
      - realized_win_rate < min_realized_win_rate (default 0.50)
    """
```

### SQL query (per alert)

```sql
SELECT
    AVG(forward_5m_bps)  AS avg_5m,
    AVG(forward_15m_bps) AS avg_15m,
    AVG(forward_30m_bps) AS avg_30m,
    COUNT(*) FILTER (WHERE forward_15m_bps > 0)::float / NULLIF(COUNT(*), 0) AS wr_15m,
    COUNT(*) AS n
FROM fast_signal_decay
WHERE ticker = :ticker
  AND alert_type = :alert_type
  AND signal_score_bucket = :bucket   -- floor(score * 10) / 10 → 0.0, 0.1, ..., 0.9
  AND alert_fired_at > NOW() - :lookback_window_h * INTERVAL '1 hour'
```

`signal_score_bucket` adds the alert-quality dimension. A `volume_breakout_long` at score 0.8 is a different population than one at 0.4; conditioning the gate on the bucket captures that.

### Admission predicate

Given the query result `(avg_5m, avg_15m, avg_30m, wr_15m, n)`:

```python
# 1. Sample-size floor
if n < settings.realized_gate_min_bucket_n:  # default 30
    return (False, f"realized_gate:insufficient_samples:{n}")

# 2. Cost threshold (matches the holding period of f-fastpath-extended-hold-momentum)
target_horizon_bps = avg_15m  # the brief sets exit_max_hold_s=1800 (30min);
                              # 15m horizon is a conservative midpoint
cost_threshold_bps = settings.realized_gate_cost_safety_multiplier * (
    settings.cost_aware_taker_fee_bps_round_trip + median_spread_bps
)
if target_horizon_bps <= cost_threshold_bps:
    return (False, f"realized_gate:edge_below_cost:{target_horizon_bps:.1f}<={cost_threshold_bps:.1f}")

# 3. Win-rate floor
if wr_15m < settings.realized_gate_min_win_rate:  # default 0.50
    return (False, f"realized_gate:win_rate_below_floor:{wr_15m:.2f}")

return (True, None)
```

### Settings (new)

```python
realized_decay_gate_enabled: bool = False        # ship dormant; flip after confluence shipped
realized_gate_lookback_window_h: int = 168       # 7 days
realized_gate_min_bucket_n: int = 30             # sample-size floor; below this -> reject
realized_gate_min_win_rate: float = 0.50         # below 50% wr at 15m -> reject
realized_gate_cost_safety_multiplier: float = 1.5  # require 1.5x cost as margin of safety
realized_gate_horizon: str = "15m"               # "5m" | "15m" | "30m" — which forward-window
```

### Gate ordering

In the gates pipeline, place this gate AFTER `gate_cost_aware_admission` (the existing structural gate stays as the cheap first-pass; the realized gate is the expensive empirical second-pass). Both must pass.

When `realized_decay_gate_enabled=False`, skip with no DB hit — backwards-compat.

## Deliverables

D1. **`app/services/trading/fast_path/gates.py`**
- New `gate_realized_decay_admission` function with the query + predicate.
- Wire into the gate pipeline after the structural cost-aware gate.

D2. **`app/services/trading/fast_path/settings.py`**
- 6 new fields above + env loads.

D3. **`app/services/trading/fast_path/scanner.py` (small)**
- Ensure every alert's `signal_score_bucket` is computed and emitted (likely already done; verify).

D4. **`tests/test_fastpath_realized_decay_gate.py`**
- Seed `fast_signal_decay` rows for a bucket with positive edge → gate admits.
- Seed with negative edge → gate rejects with `edge_below_cost`.
- Seed with fewer than `min_bucket_n` samples → reject with `insufficient_samples`.
- `realized_decay_gate_enabled=False` → no-op admit regardless of data.

D5. **`docs/RUNBOOKS/FAST_PATH_REALIZED_GATE.md`**
- How the operator inspects the gate's decisions via `fast_executions.reject_reason LIKE 'realized_gate:%'`.
- How to retune the safety multiplier and win-rate floor after observation.

## Hard constraints

- **No magic defaults when data is missing.** If the query returns NULL or n=0, the gate rejects with `realized_gate:insufficient_samples`. NO silent admit-on-no-data — that would let unproven buckets through, which is exactly what cost-aware admission was added to prevent.
- **Per-decision DB hit must be cheap.** The query is single-bucket, indexed on `(ticker, alert_type, signal_score_bucket, alert_fired_at)`. Verify the index exists; if not, add one via migration 245.
- **Idempotent in shadow mode.** When `realized_decay_gate_enabled=False`, the gate function returns `(True, None)` immediately without touching the DB.

## Acceptance

- After ship + 7-day shadow log with the gate ENABLED in observation-only mode (a separate `realized_decay_gate_shadow_log: bool = True` for the first window):
  - Probe: `SELECT decision, COUNT(*) FROM fast_executions WHERE shadow_realized_gate_decision IS NOT NULL GROUP BY 1;`
  - Expect: realized-gate rejects another 30-60% of alerts that the structural cost-aware gate currently admits.
  - Among the alerts the realized gate would admit, post-hoc check their 15m realized return should average above the cost threshold (since that's the literal admission criterion).
- Operator decides whether to flip to live by comparing the shadow-log decisions to ground-truth outcomes.

## Operator activation

1. Ship in dormant state. Verify backwards-compat — no behaviour change.
2. Flip `CHILI_FAST_PATH_REALIZED_DECAY_GATE_ENABLED=true` + `CHILI_FAST_PATH_REALIZED_DECAY_GATE_SHADOW_LOG=true` (shadow first).
3. After 7d of shadow data, examine the `realized_gate:*` reject reasons in `fast_executions`. Sanity-check by manually computing one or two buckets' realized stats.
4. Flip shadow_log=false → realized gate enforces in production.

## Open question (note for plan-gate consult)

The brief assumes the existing `fast_signal_decay` schema has columns `forward_5m_bps`, `forward_15m_bps`, `forward_30m_bps`, `signal_score_bucket`. CC should verify against the live schema at plan time. If those columns don't exist as named, the brief needs schema work (migration 245+) BEFORE the gate can be implemented. The earlier probe showed `fast_signal_decay` had ~10+ entries per (ticker, alert_type) combo — verify what columns those rows have before coding the gate.

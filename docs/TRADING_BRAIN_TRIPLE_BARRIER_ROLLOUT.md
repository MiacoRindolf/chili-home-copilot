# Phase D — Triple-barrier labels + economic promotion metric (rollout)

## What this ships

1. **Triple-barrier labeler** (`app/services/trading/triple_barrier.py`) — pure
   math. Given an entry close, a sequence of forward OHLCV bars, and a
   `TripleBarrierConfig(tp_pct, sl_pct, max_bars, side)`, returns a
   `TripleBarrierLabel(label ∈ {-1, 0, +1}, barrier_hit ∈ {tp, sl, timeout,
   missing_data}, realized_return_pct, exit_bar_idx, entry_close, tp_price,
   sl_price)`. ATR-scaled variant `compute_label_atr(...)` available.

   Tie-break rule: if a single bar breaches **both** barriers, we label the
   outcome as SL. This is deliberately pessimistic — it penalises signals that
   live in very noisy intra-bar ranges rather than letting ambiguous bars
   inflate win-rate estimates.

2. **Label store** (`trading_triple_barrier_labels`, migration 131).
   UNIQUE `(ticker, label_date, side, tp_pct, sl_pct, max_bars)` so the
   labeler is **idempotent**. Shadow rows are marked `mode='shadow'`; future
   cutover rows will be marked `mode='authoritative'`.

3. **Labeler service** (`app/services/trading/triple_barrier_labeler.py`):

   * `label_single(db, *, ticker, label_date, entry_close, future_bars,
     side='long', cfg=None, snapshot_id=None, mode_override=None)` — pure +
     DB writer used directly by tests and the batch path.
   * `label_snapshots(db, *, limit=200, side='long', cfg=None,
     mode_override=None, min_lookback_days=10)` — picks recent
     `MarketSnapshot` rows at least `min_lookback_days` old, fetches forward
     bars via `market_data.fetch_ohlcv`, labels them, upserts.
   * `label_summary(db, lookback_hours=24)` — aggregation used by the
     diagnostics endpoint.

4. **Economic promotion metric**
   (`app/services/trading/promotion_metric.py`) — pure. Composite

   ```text
   economic_score = expected_pnl_per_trade - brier_penalty * oos_brier_score
   ```

   `compare_economic(active, shadow, *, min_improvement, max_brier_regression,
   brier_penalty)` returns an `EconomicComparison` dataclass with `better`,
   `reason ∈ {economic_improvement, insufficient_improvement,
   brier_regression, missing_metric}`, and the component deltas. A Brier
   regression beyond tolerance rejects even when expected PnL improves.

   Phase D does **not** auto-promote — `ModelRegistry.check_shadow_vs_active`
   signature is untouched. Cutover is gated on
   `brain_promotion_metric_mode = 'economic'` and a separate freeze.

5. **Brier + expected-PnL enrolment in `pattern_ml.train()`** — safe additive
   change to the `reg.register(...)` metrics dict:
   `oos_brier_score`, `oos_log_loss`, `expected_pnl_oos_pct`. No consumer
   reads these yet; the Phase J / economic cutover will.

## Config

All defaults are safe:

| Setting                                  | Default   | Meaning                                           |
| ---------------------------------------- | --------- | ------------------------------------------------- |
| `BRAIN_TRIPLE_BARRIER_MODE`              | `off`     | `off` / `shadow` / `authoritative`                |
| `BRAIN_TRIPLE_BARRIER_TP_PCT`            | `0.015`   | Take-profit barrier as fraction of entry          |
| `BRAIN_TRIPLE_BARRIER_SL_PCT`            | `0.010`   | Stop-loss barrier as fraction of entry            |
| `BRAIN_TRIPLE_BARRIER_MAX_BARS`          | `5`       | Time barrier (bars)                               |
| `BRAIN_TRIPLE_BARRIER_OPS_LOG_ENABLED`   | `true`    | Emit `[triple_barrier_ops]` one-liner per write   |
| `BRAIN_PROMOTION_METRIC_MODE`            | `accuracy`| `accuracy` / `shadow` / `economic`                |

## Rollout ladder

```
off  ──▶  shadow  ──▶  authoritative   (+ BRAIN_PROMOTION_METRIC_MODE=economic)
```

**Forward**

1. Apply migration 131 (`trading_triple_barrier_labels`).
2. Set `BRAIN_TRIPLE_BARRIER_MODE=shadow`, keep
   `BRAIN_PROMOTION_METRIC_MODE=accuracy`.
3. Recreate the chili service so the new env is loaded.
4. Run the labeler (manually or via scheduler) against recent snapshots.
5. Confirm `/api/trading/brain/triple-barrier/diagnostics` reports non-zero
   `labels_total` and a sensible `by_barrier` distribution.
6. Confirm no `[triple_barrier_ops] mode=authoritative` lines appear.

**Cutover to economic promotion** (future phase, not in Phase D):

1. Verify OOS Brier and `expected_pnl_oos_pct` are populated in the registry
   for the last N trained models.
2. Set `BRAIN_PROMOTION_METRIC_MODE=shadow` — compute the economic
   decision alongside, log the delta.
3. Only after N days of green shadow deltas, flip to `economic`.

**Rollback**

* `BRAIN_TRIPLE_BARRIER_MODE=off` — labeler no-ops, ops log silences.
* `BRAIN_PROMOTION_METRIC_MODE=accuracy` — reverts promotion decisions to
  legacy single-key behaviour. Labels already written remain.
* Destructive rollback (if needed): `DROP TABLE trading_triple_barrier_labels;`.

## Observability

### Ops log format

Prefix `[triple_barrier_ops]`. Two events:

```
[triple_barrier_ops] event=label_write mode=shadow ticker=AAPL label_date=2026-01-15 side=long
  tp_pct=0.015 sl_pct=0.01 max_bars=5 label=1 barrier_hit=tp exit_bar_idx=2
  realized_return_pct=0.015 snapshot_id=12345 inserted=true

[triple_barrier_ops] event=run_summary mode=shadow labels_total=200
  labels_tp=60 labels_sl=80 labels_timeout=40 labels_missing=20
  written=150 skipped_existing=30 errors=0
```

### Diagnostics endpoint

`GET /api/trading/brain/triple-barrier/diagnostics?lookback_hours=24`

Returns:

```json
{
  "ok": true,
  "triple_barrier": {
    "ok": true,
    "mode": "shadow",
    "lookback_hours": 24,
    "labels_total": 150,
    "tickers_distinct": 25,
    "by_barrier": {"tp": 60, "sl": 80, "timeout": 10, "missing_data": 0},
    "label_distribution": {"+1": 60, "-1": 80, "0": 10},
    "last_label_at": "2026-04-16T20:00:00Z",
    "tp_pct_cfg": 0.015,
    "sl_pct_cfg": 0.01,
    "max_bars_cfg": 5
  }
}
```

Shape is frozen for this phase; downstream tooling depends on it.

## Mandatory release blocker

Before any deploy, the following PowerShell **must exit 0** against the
container logs:

```powershell
docker compose logs chili --since 30m 2>&1 |
  .\scripts\check_triple_barrier_release_blocker.ps1
```

Optional additional gate enforced by CI / pre-deploy checklist:

```powershell
curl -sk https://localhost:8000/api/trading/brain/triple-barrier/diagnostics -o tb.json
.\scripts\check_triple_barrier_release_blocker.ps1 -DiagnosticsJson .\tb.json -MinLabels 10
```

Release blocker rules:

1. Any line containing **both** `[triple_barrier_ops]` and
   `mode=authoritative` → **exit 1** (shadow was supposed to be in force).
2. With `-DiagnosticsJson` + `-MinLabels N`: `labels_total < N` → **exit 1**.
3. With `-DiagnosticsJson`: `labels_total > 0` but `tp + sl + timeout == 0`
   → **exit 1** (distribution looks bogus).

## PIT safety

The triple-barrier label is computed from bars strictly **after** the
labeling date (`snapshot_date + 1 .. snapshot_date + max_bars`). The
labeler never reads the current bar or any future-dated feature. The PIT
audit (Phase C) continues to enforce that DSL conditions don't reference
future fields — triple-barrier labels are training targets, never
condition fields.

## Known limitations

* One global pair of barriers per config; no per-asset-class or
  per-volatility-regime tuning yet. Phase F / H will add per-ticker ATR.
* The labeler uses the **1d** interval. Intraday triple-barrier support is
  deferred.
* Missing-data rows (empty forward bars) are persisted with
  `barrier_hit='missing_data'` so re-runs don't reattempt them (idempotency
  via the unique key). If upstream market_data later backfills, delete
  missing-data rows for the affected ticker/date range to force re-labeling.
* `expected_pnl_oos_pct` in the registry is a simple `E[p * r]` proxy (no
  cost subtraction, no NetEdgeRanker consumption yet). Phase D is only the
  **plumbing** for economic promotion; the authoritative scoring path is
  reserved for a later phase.

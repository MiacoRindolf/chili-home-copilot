# Trading Brain - NetEdgeRanker Rollout (Phase E)

## Why

Before this phase, expected edge was heuristic: `compute_expectancy_edges` in
`expectancy_service.py` composes viability + OOS hints + static penalties. That
is fine as a guardrail but not as the **decision surface** for continuous
profitability. Phase E introduces a single calibrated net-edge score:

```
expected_net_pnl = calibrated_prob * payoff
                 - (1 - calibrated_prob) * loss_per_unit
                 - spread_cost - slippage_cost - fees_cost
                 - miss_prob_cost - partial_fill_cost
```

Implemented in `app/services/trading/net_edge_ranker.py`. Calibration uses
per-regime isotonic regression fit over realized outcomes from
`trading_trades` and `trading_paper_trades` (Phase D triple-barrier labels
will improve this without changing the public interface).

## Rollout ladder (same discipline as prediction mirror)

Single flag: `brain_net_edge_ranker_mode` (Pydantic setting in `app/config.py`).

| Mode            | Computes score? | Writes DB row? | Writes ops log? | Gates trading? |
|-----------------|-----------------|----------------|-----------------|----------------|
| `off` (default) | no              | no             | no              | no             |
| `shadow`        | yes             | yes            | yes             | no             |
| `compare`       | yes             | yes            | yes             | no             |
| `authoritative` | yes             | yes            | yes             | yes (future)   |

**Rule:** `mode != "authoritative"` MUST NOT gate any entry, exit, sizing, or
promotion. Callers that want to consume the ranker's output for a decision
must explicitly gate on `net_edge_ranker.mode_is_authoritative()`.

### Forward order

1. `off` - merge (current default). No behavior change in prod.
2. `shadow` - turn on in staging, validate `[net_edge_ops]` appears, rows land
   in `trading_net_edge_scores`, diagnostics endpoint returns non-empty.
3. `compare` - same as shadow; ops log `read=compare_ok|compare_disagree` lets
   us measure disagreement vs the current heuristic over days.
4. `authoritative` - only after Phase D (triple-barrier) and Phase F (venue
   truth) land and divergence panel (Phase K) is green. Separate plan.

### Rollback

`brain_net_edge_ranker_mode=off`, then
`docker compose up -d --force-recreate chili` (per `chili-docker-validation-rollout.mdc`
`.env` changes need force-recreate). Tables remain but zero new writes.

## Observability

### One-line ops log

Prefix: `[net_edge_ops]`. Fields (fixed order, fixed enums):

```
[net_edge_ops] mode=<off|shadow|compare|authoritative>
               read=<na|shadow|compare_ok|compare_disagree|authoritative|cold_start|error>
               decision_id=<<=24 chars>> pattern_id=<int|none>
               asset_class=<stock|crypto|none> regime=<string|none>
               net_edge=<float6|none> heuristic_score=<float6|none>
               disagree=<true|false> sample_pct=<float3>
```

Gated by `brain_net_edge_ops_log_enabled` (default on in active modes).

### Diagnostics endpoint

`GET /api/trading/brain/net-edge/diagnostics?lookback_hours=24`

Returns frozen shape:

```json
{
  "ok": true,
  "net_edge_ranker": {
    "ok": true,
    "mode": "shadow",
    "lookback_hours": 24,
    "sample_count": 123,
    "disagreement_rate": 0.17,
    "per_regime": [
      {"regime": "risk_on", "sample_count": 80,
       "disagreement_rate": 0.12, "avg_net_edge": 0.00045},
      {"regime": "risk_off", "sample_count": 43,
       "disagreement_rate": 0.26, "avg_net_edge": -0.00012}
    ],
    "last_calibration": {
      "version_id": "netedge_stock_risk_on_1713368200",
      "method": "isotonic",
      "sample_count": 420,
      "regime": "risk_on",
      "asset_class": "stock",
      "brier_score": 0.218,
      "fitted_at": "2026-04-16T08:30:00",
      "is_active": true
    }
  }
}
```

## Release blocker (MANDATORY)

**Any log line with `[net_edge_ops]` and `mode=authoritative` while
`brain_net_edge_ranker_mode != "authoritative"` blocks release.**

Check:

```powershell
(docker compose logs chili --since 30m 2>&1 |
  Select-String "\[net_edge_ops\]") |
  Where-Object { $_.Line -match "mode=authoritative" }
```

Empty output - pass this gate.

Scripted:

```powershell
.\scripts\check_net_edge_ranker_release_blocker.ps1
# or
Get-Content chili.log | .\scripts\check_net_edge_ranker_release_blocker.ps1
# or
.\scripts\check_net_edge_ranker_release_blocker.ps1 -Path .\chili.log
```

Exit 0 = clean. Exit 1 = blocker present.

## Verification gates (per phase)

Applies at each mode transition:

1. `pytest tests/test_net_edge_ranker.py -v` green.
2. `pytest tests/test_scan_status_brain_runtime.py tests/test_indicator_parity.py -v` green (no regression in frozen contracts).
3. Migration `127_net_edge_ranker` applied cleanly against a fresh Postgres.
4. With `mode=off`: zero `[net_edge_ops]` log lines, zero rows in
   `trading_net_edge_scores`.
5. With `mode=shadow` in staging for 30 minutes of paper activity: non-zero
   row count, reasonable `disagreement_rate` (<50%), no `read=error` spam.
6. Release-blocker grep above returns empty.

## Forbidden (until a future phase opens it)

- `NetEdgeRanker.score()` output driving sizing, promotion, or entry.
- Reading `expected_net_pnl` in abstain/floor logic in `portfolio_allocator`.
- Widening `[net_edge_ops]` field list or re-ordering / renaming enums.
- Consuming the diagnostics endpoint shape mutably in the UI.

## Follow-up (not in this phase)

- Phase D triple-barrier labels feed `_load_training_pairs` with honest
  win/loss signals; calibrator interface stays the same.
- Phase F venue-truth replaces the placeholder `miss_prob` and
  `partial_fill` constants with data-driven per-venue distributions.
- Phase H wires `mode_is_authoritative()` into `PositionSizer` for
  quarter-Kelly from `calibrated_prob * payoff`.
- Phase K paper-vs-live divergence panel reads from
  `trading_net_edge_scores` for calibration-error drift alerts.

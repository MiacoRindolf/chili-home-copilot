# Trading Brain - Canonical PositionSizer + portfolio optimizer rollout

Phase H ships a single, Kelly-aware, cost-corrected, portfolio-aware
position sizer that runs **shadow only** next to every legacy sizing
call-site. It does not alter the quantity placed on any live or paper
trade; it records a parallel proposal and the basis-point divergence
from the legacy size. This substrate is what Phase H.2 and Phase I
build authoritative and risk-dial-aware sizing on top of.

Phase H is **strictly shadow-only**. Phase H.2 (covariance allocator,
correlation *capping of live sizes*, authoritative cutover) is
deferred; its own plan must explicitly freeze the authority contracts
and expected-PnL / Brier gates before any legacy sizer is retired.

## Shipped in Phase H

* Migration `134_position_sizer_log`:
  * `trading_position_sizer_log` - append-only append-per-proposal log
    with the full Kelly / caps / legacy / divergence snapshot and a
    JSONB `payload_json` for arbitrary context.
  * Indexes on `proposal_id`, `(source, observed_at)`,
    `(ticker, observed_at)`.
* ORM model `PositionSizerLog` in `app/models/trading.py`.
* Pure modules:
  * `app/services/trading/position_sizer_model.py::compute_proposal`
    is the canonical Kelly-derived sizer. It applies cost symmetrically
    (half to winning branch, half to losing branch), enforces a single
    per-ticker cap, a correlation-bucket cap, a portfolio-total cap, a
    `kelly_scale` dampener, and a hard `max_risk_pct` risk cap.
    `compute_proposal_id` is a deterministic SHA-derived UUID so the
    same inputs always produce the same `proposal_id`.
  * `app/services/trading/correlation_budget.py` - DB-aware module for
    computing `CorrelationBudget` (per-bucket open notional + cap) and
    `PortfolioBudget` (total deployed + ticker-specific open notional).
    `bucket_for` reuses `portfolio_allocator._symbol_asset_family` and
    the Phase F sector map so equities and crypto bucket consistently
    across the brain.
* DB writer:
  * `position_sizer_writer.write_proposal` - defensive, mode-gated,
    emits the ops log, persists exactly one row, and computes
    `divergence_bps` against the legacy sizing when the call-site
    supplies one.
  * `position_sizer_writer.proposals_summary` - frozen diagnostic
    shape: `mode, lookback_hours, proposals_total, by_source,
    by_divergence_bucket, mean_divergence_bps, p90_divergence_bps,
    cap_trigger_counts, latest_proposal`.
* Ops log: `[position_sizer_ops]` with `event=proposal`, carrying
  `mode`, `source`, `ticker`, `direction`, `legacy_notional`,
  `proposed_notional`, `divergence_bps`, cap flags, and the Kelly
  fraction pair.
* Emitter layer: `position_sizer_emitter.emit_shadow_proposal` is the
  single entry point for all shadow logging. It is:
  * **Defensive** - every exception is swallowed so legacy sizing is
    never disturbed.
  * **Short-circuit** on `mode=off`.
  * **NetEdgeRanker-aware** - accepts the ranker's score if caller has
    one; otherwise falls back to geometric inputs
    (`payoff = (target-entry)/entry`, `loss = (entry-stop)/entry`).
* Call-sites wired (four, all logging the **legacy** size):
  * `alerts.generate_strategy_proposals` (scan emission).
  * `alerts.create_proposal_from_pick` (UI / API proposal).
  * `alerts._execute_proposal` (auto-execute path).
  * `portfolio_risk.size_position_kelly` (Kelly sizing library call).
  * `portfolio_risk.size_with_drawdown_scaling` (drawdown-scaled Kelly -
    recorded **after** DD scaling so the legacy size reflects the
    final number; the inner `size_position_kelly` call is **not**
    re-instrumented, to avoid double-counting).
  * `paper_trading.auto_open` (paper / simulated execution - also
    records any existing `NetEdgeScore` pulled from the Phase E
    ranker hook).
* Diagnostics endpoint: `GET /api/trading/brain/position-sizer/diagnostics`
  returns the frozen `proposals_summary` shape.
* Config flags (`app/config.py` + `.env`):
  * `BRAIN_POSITION_SIZER_MODE=shadow`
  * `BRAIN_POSITION_SIZER_OPS_LOG_ENABLED=true`
  * `BRAIN_POSITION_SIZER_EQUITY_BUCKET_CAP_PCT=15.0`
  * `BRAIN_POSITION_SIZER_CRYPTO_BUCKET_CAP_PCT=10.0`
  * `BRAIN_POSITION_SIZER_SINGLE_TICKER_CAP_PCT=7.5`
  * `BRAIN_POSITION_SIZER_KELLY_SCALE=0.25`
  * `BRAIN_POSITION_SIZER_MAX_RISK_PCT=2.0`
* Tests - **50 Phase H tests green**:
  * `tests/test_position_sizer_model.py` (19 pure tests).
  * `tests/test_correlation_budget.py` (15 DB tests).
  * `tests/test_position_sizer_writer.py` (8 DB tests).
  * `tests/test_position_sizer_emitter.py` (6 DB tests).
  * Plus `tests/test_scan_status_brain_runtime.py` (2) to prove the
    frozen `scan_status.brain_runtime` contract is **not extended**.
* Docker soak: `scripts/phase_h_soak.py` verifies migration 134,
  shadow-mode settings, that the emitter writes a row in shadow mode
  and is a no-op in off mode, that `proposal_id` is deterministic,
  that crypto tickers route to `asset_class=crypto`, and that the
  summary shape is frozen.
* Release blocker: `scripts/check_position_sizer_release_blocker.ps1`
  fails on any `[position_sizer_ops] event=proposal mode=authoritative`
  log line (Phase H.2 is deferred) and supports optional
  `-MinProposals` and `-MaxMeanDivergenceBps` diagnostic gates.

## Rollout ladder

Phase H stays on `shadow` across all environments. Phase H.2 will
extend this ladder; do **not** skip steps.

| Step | `BRAIN_POSITION_SIZER_MODE` | Legacy sizer authoritative? | Writes `trading_position_sizer_log`? | Replaces legacy quantity? |
|------|------------------------------|-----------------------------|--------------------------------------|---------------------------|
| Off  | `off`                        | Yes                         | No                                   | No                        |
| Shadow | `shadow`                   | Yes                         | Yes (`mode=shadow`)                  | **No**                    |
| Compare | `compare` *(reserved)*    | Yes                         | Yes (`mode=compare`)                 | **No**                    |
| Authoritative | `authoritative`     | **No** *(Phase H.2)*        | Yes (`mode=authoritative`)           | Yes *(Phase H.2)*         |

Phase H ships `off` → `shadow`. Do not flip to `authoritative` in
Phase H; the release-blocker script will exit non-zero and no
call-site currently reads `PositionSizerOutput` back into the legacy
quantity.

## Rollback

Flip `BRAIN_POSITION_SIZER_MODE=off` in `.env` and
`docker compose up -d --force-recreate chili brain-worker scheduler-worker`.
No code rollback required: shadow never affects the trade quantity
placed by any legacy sizer. Existing `trading_position_sizer_log`
rows are data-only and can stay; they are useful historical
observations for Phase H.2 calibration.

## Mandatory release blockers

1. `.\scripts\check_position_sizer_release_blocker.ps1` on the last
   30 minutes of `chili` + `brain-worker` + `scheduler-worker` logs
   must exit 0.
2. `scripts/phase_h_soak.py` must pass inside the `chili` container.
3. `tests/test_scan_status_brain_runtime.py` must stay green - the
   frozen `scan_status.brain_runtime` contract is **not** extended in
   Phase H.
4. `BRAIN_POSITION_SIZER_MODE` visible as `shadow` inside the running
   `chili` container
   (`docker compose exec chili env | Select-String BRAIN_POSITION_SIZER_MODE`).
5. `/api/trading/brain/position-sizer/diagnostics` returns
   `"mode":"shadow"` and the frozen shape
   (`mode, lookback_hours, proposals_total, by_source,
   by_divergence_bucket, mean_divergence_bps, p90_divergence_bps,
   cap_trigger_counts, latest_proposal`).

## Monitoring

* `[position_sizer_ops] event=proposal mode=shadow` should appear on
  every legacy sizing call (scan emission, auto-execute, Kelly
  library call, DD-scaled Kelly, paper auto-open).
* The `divergence_bps` distribution is the primary Phase H health
  signal. Large, persistent divergence in one direction suggests the
  legacy sizer is mis-scaling relative to Kelly+caps and is a
  candidate for Phase H.2 authoritative cutover per source.
* `cap_trigger_counts.correlation_cap` and
  `cap_trigger_counts.notional_cap` indicate how often the Phase H
  sizer would have clipped an over-concentrated trade. These are the
  gates Phase H.2 promises to enforce authoritatively.
* The diagnostics endpoint is safe for read-only observation
  dashboards; it does not trigger new proposals.

## Non-goals (for Phase H)

* Replacing any legacy sizer quantity on a live or paper trade. That
  is **Phase H.2**.
* Covariance-based portfolio allocation (full rebalance under a risk
  budget). **Phase H.2 / Phase I.**
* Regime-conditioned capital re-weighting or a user-facing risk dial.
  That is **Phase I**.
* Rewiring the NetEdgeRanker call path. Still the Phase E contract.

## Known gaps to cover in Phase H.2

* Authoritative quantity adoption at the four wired call-sites, with a
  kill-switch and governance approval per `governance.py`.
* Covariance allocator over open + proposed positions (not just bucket
  caps). Expected to share the `CorrelationBudget` substrate.
* Per-source authoritative toggles so sources can be promoted
  independently (e.g. `paper_trading` first, `alerts` last).
* Feedback from Phase F venue-truth telemetry into the cost fraction
  used by `compute_proposal` (currently inferred from NetEdgeScore +
  fallback; Phase H.2 should plug in `cost_model`'s per-ticker
  breakdown).
* Diagnostic attribution of divergence to **which** cap fired (so we
  can distinguish "legacy sized too aggressively" from "Kelly wants
  more but correlation budget clipped it").

Phase H is observability + the data substrate for the future
authoritative cutover, and nothing else.

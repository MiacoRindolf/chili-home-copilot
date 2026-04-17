# Trading Brain - Phase I - Risk Dial + Weekly Capital Re-weighting Rollout

**Status:** shadow-ready (I.1 complete). Authoritative cutover (I.2) is
**not opened**; any attempt to move either knob to `authoritative` is blocked
by release-blocker scripts and by the explicit refusal inside
`capital_reweight_service.run_sweep`.

## Summary

Phase I ships two shadow-only capabilities on top of the Phase H canonical
position sizer:

1. **Risk Dial** - a scalar multiplier in `[0, ceiling]` (default ceiling
   1.5) resolved from market regime and user drawdown. Persisted to
   `trading_risk_dial_state` and attached to every position-sizer proposal
   as `trading_position_sizer_log.risk_dial_multiplier`. Does **not**
   change sized notional in I.1 - purely observational for drift /
   sensitivity measurement.

2. **Weekly Capital Re-weighter** - an APScheduler job (Sun 18:30 server
   time by default) that produces an inverse-volatility allocation across
   observed per-ticker buckets from `trading_paper_trades`. Persisted to
   `trading_capital_reweight_log` with per-bucket drift vs. current
   notional. Does **not** rebalance in I.1 - the job is a proposal
   generator only.

## Scope (I.1)

- Log-only observability and proposal generation.
- No live trade mutations, no broker calls, no changes to sized notional,
  no changes to the Phase H position sizer's canonical output beyond
  **recording the active dial** alongside each proposal.

## Forbidden changes

- Using `risk_dial_multiplier` to modify sized notional anywhere in the
  hot path (sizer, alerts, paper trading, portfolio risk, stop engine).
- Executing any `trading_capital_reweight_log` proposal against the broker
  or paper layer.
- Setting `BRAIN_RISK_DIAL_MODE=authoritative` or
  `BRAIN_CAPITAL_REWEIGHT_MODE=authoritative` in any environment before
  Phase I.2 is explicitly opened.

## Rollout ladder

I.1 (this ships):

1. Deploy image with migration `135_risk_dial_capital_reweight` applied
   (creates `trading_risk_dial_state`, `trading_capital_reweight_log`, and
   adds `trading_position_sizer_log.risk_dial_multiplier`).
2. Set `BRAIN_RISK_DIAL_MODE=shadow` and
   `BRAIN_CAPITAL_REWEIGHT_MODE=shadow` in `.env`.
3. Recreate `chili`, `brain-worker`, and `scheduler-worker` with
   `docker compose up -d --force-recreate`.
4. Verify:
   - `GET /api/trading/brain/risk-dial/diagnostics` returns
     `mode: "shadow"`.
   - `GET /api/trading/brain/capital-reweight/diagnostics` returns
     `mode: "shadow"`.
   - `scripts/check_risk_dial_release_blocker.ps1` passes (no
     `mode=authoritative` lines in ops logs).
   - `scripts/check_capital_reweight_release_blocker.ps1` passes (no
     `event=sweep_persisted mode=authoritative` lines).
   - `scripts/phase_i_soak.py` (in-container) reports
     `ALL CHECKS PASSED`.
5. Monitor `dial_events_total`, `by_regime`, `by_dial_bucket`,
   `override_rejected_count` on the risk-dial diagnostics, and
   `sweeps_total`, `single_bucket_cap_trigger_count`,
   `concentration_cap_trigger_count`, `mean_mean_drift_bps` on the
   capital-reweight diagnostics over a multi-week window before opening
   Phase I.2.

I.2 (not opened):

- Reopen requires: named consumer(s) for the dial multiplier (e.g. apply
  it to sized notional inside `position_sizer_writer` behind a
  separate flag), named rebalance path for sweep proposals,
  explicit authority contract, drift/kill-switch criteria, mandatory
  re-run of soak + release-blocker scripts in staging.

## Rollback

At any time during shadow:

1. Set `BRAIN_RISK_DIAL_MODE=off` and `BRAIN_CAPITAL_REWEIGHT_MODE=off`
   in `.env`.
2. Recreate `chili`, `brain-worker`, `scheduler-worker`.
3. `resolve_dial` becomes a no-op (returns `None`, writes no rows).
4. The weekly scheduler hook exits early on mode gating.
5. `position_sizer_writer` stops populating
   `trading_position_sizer_log.risk_dial_multiplier` (column is
   nullable, no schema change required).
6. Existing rows in `trading_risk_dial_state` and
   `trading_capital_reweight_log` are left intact for post-mortem.

## Mandatory release blockers

Both scripts must exit **0** against a rolling log window before **any**
subsequent change is merged. They will exit **1** if a forbidden line is
observed.

```powershell
docker compose logs chili brain-worker scheduler-worker --since 30m |
  .\scripts\check_risk_dial_release_blocker.ps1

docker compose logs chili brain-worker scheduler-worker --since 30m |
  .\scripts\check_capital_reweight_release_blocker.ps1
```

The risk-dial blocker flags any ops log line containing
`event=dial_persisted` with `mode=authoritative`. The capital-reweight
blocker flags any line with `event=sweep_persisted mode=authoritative`
(and passes `event=sweep_refused_authoritative` as expected behavior).

## Monitoring

During shadow, watch:

- **Risk dial**
  - `dial_events_total` rising in line with regime changes / login
    activity.
  - `mean_dial_value` concentrated near configured defaults for the
    current regime bucket.
  - `override_rejected_count` == 0 in healthy operation (a spike
    indicates UI / API misuse sending out-of-range overrides).
  - `capped_at_ceiling_count` == 0 unless a deliberate user override is
    in flight.

- **Capital re-weighter**
  - `sweeps_total` increments weekly (Sun 18:30) when `chili` has
    any open paper positions; otherwise the hook is a no-op and
    **no** row is expected.
  - `single_bucket_cap_trigger_count` proportional to how often
    inverse-vol weights concentrate into a single bucket.
  - `mean_mean_drift_bps` and `p90_p90_drift_bps` should stabilize
    after a few weekly cycles - large sustained drift indicates
    missing buckets or bad volatility input.

## Non-goals (explicit)

- Integrating the dial into sized notional. Deferred to I.2.
- Executing sweep proposals against any broker/paper layer. Deferred to
  I.2.
- Covariance-matrix allocator. The `CovMatrixProvider` hook is in place
  but defaults to inverse-volatility. Swapping in a covariance solver is
  a separate, future phase.
- Applying the dial to `stop_engine`, `portfolio_risk.kelly`, or
  `alerts` call-sites. Deferred to I.2.

## Known gaps / I.2 checklist

1. Dial multiplier is recorded but **not consumed** by any sized notional
   path. Closing this gap requires a new flag
   (`BRAIN_RISK_DIAL_APPLIES_TO_SIZER_MODE=shadow|authoritative`) and
   dedicated shadow-vs-live comparison logging, **not** a
   `BRAIN_RISK_DIAL_MODE=authoritative` promotion.
2. Weekly sweep derives buckets from `trading_paper_trades` only (live
   positions are not yet mapped into buckets). If/when I.2 uses sweep
   proposals to rebalance live notional, bucket derivation must be
   extended to cover the live portfolio.
3. `capital_reweight` ignores target assets that have no open
   paper position at sweep time - the sweep proposes reallocations
   among *current* buckets rather than bootstrapping new buckets.
4. Regime input comes from the caller, not from a canonical
   regime-classifier service. I.2 should wire in
   `regime.classify_regime()` directly so all dial emissions share one
   regime truth.

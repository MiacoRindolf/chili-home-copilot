# NEXT_TASK: f-adaptive-cpcv-gate

STATUS: PENDING

## Goal

**Phase 2 of the adaptive-promotion-architecture initiative.** Replace
the hardcoded CPCV gate thresholds with empirical, sample-size-aware
ones (Bayesian shrinkage + lower-CI percentiles + Pareto frontier +
portfolio marginal Sharpe). Behind a feature flag, default off,
byte-identical when flag is off.

## Why this is next (and not waiting on Phase 1b soak)

Phase 1b prod flip happened 2026-05-11T17:19:45Z and is verified
functional:
- 20 `breakout_alert_resolved` rows born `status='pending'` (write path
  using flag=True)
- 1 `market_snapshots_batch` row in `processing` state (dispatcher
  claiming outcome-kind rows)
- `[brain_work:mine] ev_id=4335 starting mine` log line — first
  Phase 2 handler ever firing against production traffic

The 24h soak gate I originally wrote into the Phase 2 brief was
defensive, not architectural. Phase 2 lives in a NEW module
(`app/services/trading/cpcv_adaptive_gate.py`) behind its OWN flag
(`chili_cpcv_adaptive_gate_enabled`). It has zero runtime dependency on
Phase 1b's behavior. The two can ship in parallel.

The "soak" framing properly applies to Phase 1c-large (backfilling
1055 backtest_completed + 2659 breakout_alert_resolved rows where
handler throughput under burst load could surface issues), not to a
flag-off-default new module.

## Brief

`docs/STRATEGY/QUEUED/f-adaptive-cpcv-gate.md`

Parent architectural brief:
`docs/STRATEGY/QUEUED/f-adaptive-promotion-architecture-2026-05-11.md`

Phase 0 memo: `docs/AUDITS/2026-05-11_cpcv_gate_coverage.md`
Phase 1a memo: `docs/AUDITS/2026-05-11_dispatcher_silence.md`
Phase 1b CC_REPORT: `docs/STRATEGY/CC_REPORTS/2026-05-11_brain-event-kind-unify.md`

## Deliverables

1. **app/services/trading/cpcv_adaptive_gate.py** — new module wrapping
   `promotion_gate.promotion_gate_passes` with adaptive logic. Behind
   `chili_cpcv_adaptive_gate_enabled` flag (default False).
2. **app/config.py** — 4 new pydantic Settings fields with semantic
   defaults (5% / 90% / 0.0 bps).
3. **app/migrations.py** — migration 239 creates `cpcv_adaptive_eval_log`
   table for shadow-log + post-hoc analysis. Additive only.
4. **tests/test_cpcv_adaptive_gate.py** — flag-off parity, shrinkage
   math, empirical-percentile threshold, Pareto frontier, portfolio
   marginal, shadow-log write.
5. **Wiring point** in `promotion_gate.finalize_promotion_with_cpcv`
   (line 933) — single call site for the wrapper.
6. **docs/runbooks/CPCV_ADAPTIVE_GATE.md** — operator runbook.
7. **docs/STRATEGY/CC_REPORTS/2026-05-11_adaptive-cpcv-gate.md**

## Hard constraints

- Flag defaults `False`. Merge produces zero behavior change.
- No changes to `promotion_gate.promotion_gate_passes` itself —
  preserve as-is for byte-identical legacy path.
- The wrapper is the SINGLE call site — don't sprinkle adaptive logic
  across files.
- Shadow-log table is additive (new table + index, no column adds to
  scan_patterns).
- No autotrader / venue / broker touched.
- All numbers introduced are operator-policy parameters with semantic
  meaning, not arbitrary thresholds. Document the "policy not magic"
  framing in the runbook.

## Consult gate (3 operator-policy defaults)

Brief assumes 5% target pool / 90% CI / 0.0 bps marginal Sharpe.
CC should surface these in plan-gate consult so operator can confirm
or tune before tests are written.

## Parallel work being queued

- **Phase 1c-small** (`f-brain-event-kind-backfill.md` first wave) —
  paper_trade_closed (1 row) + live_trade_closed (4 rows) +
  broker_fill_closed (131 rows). Smallest blast radius, ships
  alongside Phase 2.
- **Phase 1c-large** (deferred ~24h after Phase 1b cohesive flip at
  17:19:45Z, i.e. ~2026-05-12T17:19Z) — backtest_completed (1055) +
  breakout_alert_resolved (2659). These benefit from observing
  organic handler cadence first.
- **Phase 3** (composite quality event-driven) — independent additive.
  Queued after Phase 2 lands.
- **Phase 4** (UI runtime tab surfacing) — independent. Queued
  whenever the watcher picks it up.

## Side-shipped earlier today

- Phase 0 audit (`738a72d`).
- Phase 1a audit (`4c1e46e`).
- Phase 1b architectural fix (`2e9365c`).
- Phase 1b prod flag flip verified functional at 17:19:45Z.
- Watcher truncation fix (`e13c7d9`) — operator override.
- Supervisor parameterization (`f71fdf1`) — `-Mode session` added.

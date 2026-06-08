# CC_REPORT: f-momentum-profit-giveback-halt

Operator-direct task (the active `NEXT_TASK.md` is the unrelated, still-PENDING
phase-5i soak watcher). Adds the verified Ross Cameron profit-giveback session
halt — the upside mirror of the momentum lane's equity-relative daily-loss cap.

## What shipped

- **Commit `fa0e5cd`** (squash-merged to `main` as **`46a159b`**, PR #511):
  `feat(momentum-lane): profit-giveback session halt (Ross 50%-giveback rule)`.
- **8 files**, +262 / −3:
  - `app/config.py` — one new knob `chili_momentum_profit_giveback_fraction`
    (default `0.5`, `0` disables; alias `CHILI_MOMENTUM_PROFIT_GIVEBACK_FRACTION`).
  - `app/services/trading/momentum_neural/risk_evaluator.py` — `_running_peak_and_total`
    (pure high-water-mark math), `_daily_realized_pnl_peak_and_current` (one-query
    peak+current from `momentum_automation_outcomes`), `evaluate_profit_giveback_halt`
    (decision), and a `profit_giveback` block-check in
    `evaluate_proposed_momentum_automation` (authoritative, honored by
    `begin_live_arm`/`confirm_live_arm`).
  - `app/services/trading/momentum_neural/auto_arm.py` — Guard 5 cheap early-out
    (`skipped="profit_giveback"`), mirroring Guard 4.
  - `app/services/trading/momentum_neural/automation_query.py` — `_compute_lane_status`
    surfaces `halt_reason="profit_giveback"` + `peak_pnl_usd` + `giveback_fraction`
    (daily-loss cap takes precedence).
  - `app/templates/trading/_tab_monitor.html` — distinct amber 🛡️ "locked in green"
    banner (vs the red daily-loss banner from #491).
  - `docs/DESIGN/MOMENTUM_LANE.md` — new §3.5 (session-level risk: daily-loss cap +
    profit-giveback halt).
  - `tests/test_momentum_auto_arm.py` (+Guard 5 cases) and new
    `tests/test_momentum_profit_giveback.py`.
- **No migration.** Peak is the high-water mark of cumulative realized PnL computed
  live from existing outcome rows over the SAME `date.today()` window as the
  daily-loss cap — stateless, resets together at 00:00 UTC.

## Design decisions (no-magic-numbers)

- **ONE documented knob** = the giveback fraction (0.5 — "easier to remember half
  than 40%"). The **activation threshold is equity-relative with no second magic
  number**: it reuses the equity-relative daily-loss-cap magnitude. Rationale: a
  green day worth protecting is, by symmetry, one that exceeds the day's max
  tolerable red.
- **Halt condition:** `peak >= activation AND current <= peak*(1 - fraction)`, only
  for `m == "live"` (paper/other = warn). Daily-loss cap is checked first and wins
  if both trip.

## Verification

- **Tests: 80 pass, 0 fail.** New + Guard 5 suites (40) and the regression set
  (`test_momentum_risk_phase6`, `test_momentum_automation_api`,
  `test_equity_relative_notional` — 40) on `chili_test`. No test asserts on the
  risk-check set/count, so the added check is non-breaking. Real-DB test seeds
  outcomes and confirms peak excludes the prior day (the reset).
- **Live `chili` DB (read-only, pre-deploy):** `_daily_realized_pnl_peak_and_current`
  agreed with the existing `_daily_realized_pnl` (−$136.07 that day); peak 0 →
  `armed=False`, `halted=False` (correctly no halt on a red day).
- **CI:** PR #511 `test` check passed (19m10s); merged squash, CLEAN.
- **Deployed to live containers** (per-sha image `chili-app:main-clean-46a159b`,
  raw `docker run`, env/mounts/network preserved via `docker inspect`, deduped
  last-wins env keeps `DATABASE_URL=…@postgres:5432`):
  - `chili-clean-recovery-scheduler` (enforcement) — recreated; clean startup, **0
    tracebacks**; `momentum_auto_arm_live` job fired `phase=ok` (Guard 5 in the live
    path); exactly one scheduler running (no double-arm).
  - `chili-clean-recovery-web` (Monitor surfacing) — recreated; `healthz` 200, **0
    tracebacks**; deployed `_compute_lane_status` returns the new fields and shows the
    **equity-relative** cap live (`max_daily_loss_usd=122.44` ⇒ Coinbase equity
    ≈ $2,449 × 0.05; activation reuses this). Crossed into a new UTC day during
    deploy: `daily_pnl_usd=0.0`, `resets_at_utc=2026-06-09` — the **daily reset**
    observed live (the prior −$136 rolled off).
  - Old containers renamed `*-pre-giveback`, stopped, `--restart no` (rollback).

## Surprises / deviations

- **Activation = full daily-loss-cap magnitude (default 5% of equity ⇒ ~$122).** On
  this account the giveback halt only arms after a fairly strong green day. Chosen to
  honor "ONE knob"; flagged below for tuning.
- The momentum lane's **live runner gating** is unchanged — Guard 5 enforces whenever
  arming is attempted; whether the runner is on/off is a separate, pre-existing flag.

## Deferred

- Did **not** add the fixed-$ daily-profit-GOAL quit rule — explicitly REFUTED in the
  2026-06-07 research; only the 50%-giveback halt is confirmed.
- Did **not** persist peak to a `trading_risk_state` table (no such model exists and
  compute-from-outcomes is stateless + self-resetting). 
- Old stopped `chili-clean-recovery-scheduler-prem*` containers (deploy cruft,
  unrelated) left as-is.

## Open questions for Cowork

1. **Activation threshold tuning.** Should it stay the full equity-relative
   daily-loss-cap magnitude, or a fraction of it (arms sooner, more Ross-like
   intraday cadence)? Decide after a soak shows how often it arms. (A fraction would
   be a second knob — currently avoided by design.)
2. **Peak basis.** Currently REALIZED PnL only (mirrors the daily-loss cap). Ross's
   own rule includes open-position marks. With lane concurrency = 1 and realized-only
   accounting, realized peak is a faithful proxy — confirm that's the intended basis.

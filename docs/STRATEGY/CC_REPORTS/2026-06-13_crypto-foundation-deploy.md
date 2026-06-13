# Crypto-live foundation — built, deployed, soak armed (2026-06-13)

Implements the crypto-live plan (`docs/STRATEGY/CC_REPORTS/2026-06-13_crypto-live-plan.md`)
through the weekend-soak boundary. Six batches shipped + merged to main, deployed
on the scheduler, gates verified on live candidate data.

## Shipped (all merged to main, deployed on `chili-app:main-clean-3d3710a`)

| PR | Batch | What |
|----|-------|------|
| [#684](https://github.com/MiacoRindolf/chili-home-copilot/pull/684) | C1 / A2 | **Fee truth.** Paper crypto charges the venue's real round-trip bps (was ~1/7th); live books Coinbase `total_fees` into the economic ledger + nets it out of realized PnL (was hardcoded `fee=0.0`). |
| [#685](https://github.com/MiacoRindolf/chili-home-copilot/pull/685) | C2 / A1 | **Liquidity floor.** Adaptive turnover gate (24h $-vol ≥ $1.44M ≈ $1k/min + optional spread probe ≤50bps), no ticker whitelist; per-name notional cap = ½ minute of turnover, wired into live sizing where the equity dvol ceiling fails open for crypto. Applies to paper too. |
| [#686](https://github.com/MiacoRindolf/chili-home-copilot/pull/686) | C3a / A5 | **UTC clock.** New crypto arms only in 05:00–10:00 + 12:00–21:00 UTC (0/21 earned in the dead band); gates live pick + paper shadow so the soak measures productive-window behavior. |
| [#687](https://github.com/MiacoRindolf/chili-home-copilot/pull/687) | C3b / A4 | **Per-class geometry.** Crypto 3:1 / 0.5 scale-out vs equity 2:1 / 0.33, via `class_aware_reward_risk(symbol)` + `scale_out_fraction(symbol=)` overrides (`-USD` only; equity untouched). Threaded through live, paper, replay. |
| [#688](https://github.com/MiacoRindolf/chili-home-copilot/pull/688) | C3c / A3 | **Maker paper fee.** Paper charges the MAKER round-trip (50bps) when `maker_only_enabled`, so the gate measures the cost structure live will run with. |
| [#689](https://github.com/MiacoRindolf/chili-home-copilot/pull/689) | C4 / A7.4 | **Plumbing.** Paper-draft dedup keys on execution_family (alpaca twin no longer collapses into the coinbase primary); venue derived from family (no more `venue="coinbase"` mislabel). |

## Deploy

- Built `chili-app:main-clean-3d3710a`, recreated `chili-clean-recovery-scheduler`
  (rm+run, `--env-file _sched_261364e.env`). DB ping OK (`chili`, 36,059 viability rows).
- Scheduler healthy on the new image: daily prescreen (2,249 candidates), Ross
  universe scan, momentum live + paper runners firing on cadence, no errors.
- Env already crypto-ready: `MAKER_ONLY_ENABLED=true`, `PAPER_RUNNER_ENABLED=1`,
  fees 153/50, **crypto live arm OFF** (gate must pass first), `crypto_only=0`
  (weekend = crypto-only de facto since equities are closed). New override knobs
  use config defaults (crypto RR 3.0, scale 0.5, qv floor $1.44M, spread 50, clock on).
- ⚠️ The shared sched env has a duplicate `DATABASE_URL` (lines 26 + 298) — both
  point to `postgres:5432/chili`, so redundant, not divergent (no DB-loss risk).

## Gates verified on LIVE candidate data

Liquidity floor on the current fresh crypto universe: **8 pass** (TRUMP, ORCA,
PEPE, SEI, VVV, WLFI, XPL, USELESS; per-name caps $556–$4,057) / **5 blocked**
below floor (BERA, JUPITER, PROS, THQ, XTZ). The toxic names the old scorer
ranked high — CHECK-USD ($24k/24h, RVOL 34), T-USD ($43k/24h) — are now excluded.

## Validation gate scorecard (instrument: `scripts/_crypto_gate_status.py`)

Six criteria, ALL must pass before `CHILI_MOMENTUM_CRYPTO_LIVE_ARM_ENABLED=1`:
n ≥ 25 RT · net ≥ +0.10R post-fee · first-target ≥ 40% · maxDD ≤ 3R · 0 zero-fee
RT · (maker fill ≥ 60% — LIVE-only, n/a in paper).

**Pre-fix baseline** (week to 06-13): n=4, −0.083R, −$7.84, first-target 75%,
DD 2.45R, 0 zero-fee RT. Binding constraints: **volume (4 vs 25) and expectancy**.

## What the soak proves / doesn't

- **Proves in paper:** post-(maker)-fee expectancy, first-target rate, drawdown,
  fee-booking integrity — on the executable-only universe, in productive windows.
- **NOT measurable in paper:** maker fill rate (a post-only limit's fill prob).
  That criterion only resolves once live; the paper soak is necessary, not
  sufficient, for the flip.
- **Throughput risk:** the entry gate is a tight funnel (pullback-break setup
  completion), so n≥25 over the weekend is not guaranteed on a ~8-name universe.
  If n tracks low by Sunday, the lever is scan/spawn breadth or a measured gate
  relaxation — NOT a blind crank now.

## Live flip (deferred, gated)

The live post-only order placement (TTL 300s cancel, no taker fallback, kill RFQ)
only EXECUTES once `CRYPTO_LIVE_ARM_ENABLED=1`, which happens ONLY after the
6-criteria gate passes. It is therefore off the soak's critical path and will be
built before the flip. Live risk box at flip: $25/trade, $75 daily, 2 concurrent,
≤10 entries/day, whitelist only, auto-disarm.

Next: monitor through the weekend; first crypto arms expected at the 05:00 UTC
window open.

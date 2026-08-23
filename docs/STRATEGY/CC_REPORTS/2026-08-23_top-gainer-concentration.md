# CC_REPORT: top-gainer-concentration

Operator-directed task (chat brief, 2026-08-23): implement the Ross top-2-gainer
concentration doctrine on the momentum arm slots. (`NEXT_TASK.md` still carries a stale
July task — left untouched; this run executed the direct chat brief.)

## What shipped

- **`chili_momentum_top_gainer_concentration_n` (default 3, LIVE+ON; 0 = kill-switch)** —
  the ONE documented knob. Outside PREMARKET, a NEW equity live arm must be one of the
  day's top-N market-wide %-gainers.
- Enforcement at the **live-pick stage** (`_live_armable` in
  `app/services/trading/momentum_neural/auto_arm.py`) — the same seam as the A2
  late-window gate, so paper shadow arms, exits, stops, and open-position management are
  untouched by construction. Mirrored in the **rank-displacement newcomer walk** so the
  lane never evicts a watcher for a newcomer the arm stage would then block
  (reap-then-blocked churn).
- **Membership sources** (zero new network, fail-open on unknown): (1) the full-market
  snapshot already fetched for Ross-universe proof, ranked by change-pct; (2) the
  candidate board's per-row scanner signals aggregated ACROSS rows; (3) the persisted
  batch-level `top_market_gainers` meta (top-5 membership).
- **Concentration, NOT starvation** (CLRO lesson / #1036): board TOP-2 (hoisted leader +
  displaced armed-first — the existing exemption primitive) always pass; STRUCTURAL
  bypass for a name whose own persisted Ross/5-Pillars evidence clears the tick-scalp
  shape gate AND viability ≥ board p90 (the A1 adaptive-percentile primitive). Bare
  rel_vol never bypasses.
- Telemetry: `out["top_gainer_concentration"]` (n/source/top), skip counters
  `top_gainer_concentration_skipped` and `..._displacement_skipped`, info log per skip.
- Docs: `docs/DESIGN/MOMENTUM_LANE.md` §12. Tests:
  `tests/test_top_gainer_concentration.py` (17 tests).

Files touched: `app/config.py`, `app/services/trading/momentum_neural/auto_arm.py`,
`tests/test_top_gainer_concentration.py`, `docs/DESIGN/MOMENTUM_LANE.md`, this report.
Migrations: none.

## Verification

- **17/17 new tests pass**; affected-area subset (10 files: auto_arm, rank displacement,
  paper shadow, ignition bridge, leader rotation/cooldown machinery, timeofday) —
  **174 passed, 2 failed**, and both failures reproduce on the UNTOUCHED base 25bb51d
  (git-stash verified): `test_market_closed_equity_skipped` and
  `test_displaces_worst_inert_for_better_newcomer` are pre-existing, not from this change.
- **Decision-level A/B in place of replay** (replay_v3 cannot exercise this change:
  `run_auto_arm_pass` hard-skips historical decisions via
  `loss_guard_history_coverage_unavailable`, so the arm gate never runs in replay).
  Read-only replay of the gate decision over ALL 1,093 live equity arms of 08-17→08-21
  against each arm's own persisted at-arm-time evidence:
  - Every one of the 1,093 was `cancelled_pre_entry` churn — **zero realized-P&L cost to
    blocking any of them**.
  - Outside premarket (693 arms): **109 hard-blocked (16%)** on even the most
    conservative membership bound (batch top-5); **255 more** pass only through the
    structural-p90 bypass (truth between 16% and 53% churn removal). Blocked list is the
    doctrine's churn class: VOGX/GDC/PMI/NINE/SLE/XRP-treasury proxies…
  - **Leader starvation: zero.** Exactly 1 batch-#1 proxy (BRNX) was membership-blocked,
    and the shipped board TOP-2 exemption passes it.
  - Live full passes rank via the market snapshot at true N=3 — stricter than the A/B's
    top-5 lower bound.

## Surprises / deviations

- **`persistence._row_execution_readiness` subsets `ross_signals` to each row's own
  symbol.** My first fallback ranked "the batch" off one row's dict — in production that
  degenerates to a 1-name set (accidental leader-only lock). Fixed to aggregate each
  row's OWN signal across the board, with a ≥2-movers floor and a regression test
  (`test_single_row_board_never_yields_one_name_lock`).
- `momentum_automation_outcomes` realized P&L has been NULL since 07-13 for live equity
  (everything terminal is `cancelled_pre_entry`) — a P&L-split A/B was impossible; the
  churn-split A/B above is the honest substitute.

## Deferred

- Full replay A/B: not meaningful for arm-stage gates (see Verification). If Cowork wants
  arm-decision replay coverage, that needs the batch-scheduler-driver replay port
  (deferred item of #1100) plus a recorded loss-guard history receipt.
- The premarket scope is fixed (gate outside premarket only) rather than a second knob —
  per the one-knob constraint; flipping to all-day is a 1-line change if the doctrine
  hardens.

## Open questions for Cowork

- The structural-p90 bypass admitted 255/693 candidates to its first condition last week
  (shape evidence is common); the p90-viability AND is what keeps it tight. If live
  telemetry shows the bypass leaking churn, tighten to "shape evidence AND top-rank
  (#1)" or drop the bypass.
- `momentum_automation_outcomes.realized_pnl_usd` dead since 07-13 — worth its own task;
  it blinds every outcome-based A/B, not just this one.

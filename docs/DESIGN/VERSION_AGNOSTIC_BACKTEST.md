# Version-Agnostic Backtest — record data faithfully, replay ANY version, diff the trades

**Date:** 2026-06-28 · **Author:** Claude (Opus 4.8) · **Origin:** operator correction — *"di ko sinasabi na iimprove yung replay by checking ng past live trades... ang gusto ko iimprove mo yung replay or yung pagrecord ng data so kahit anong version ng system is pwede mareplay sa data and pinapakita yung difference ng trades kapag ibang version na ng system."*

The replay must become a **version-agnostic backtest**: record the market DATA once, replay ANY version of the system against it, and show the **trade DIFFERENCE** between versions. NOT a live-mirror (reading past live fills can't evaluate a new version's different entries).

Audit + design: workflow `wz8pcnlm0` (3-pillar audit → synthesis).

---

## The audit verdict

**`replay_v2.py` REIMPLEMENTS ~700 lines of orchestration** (the `day_grid` loop `:1238-1648`, `manage_open` `:1021-1234`, `asof_rank`/`_full_pipeline_rank`, all gate sequencing) — it never calls `auto_arm.run_auto_arm_pass` or `live_runner.run_live_runner_batch`. It DELEGATES the pure leaf math (entry triggers `momentum_pullback_trigger`/`halt_resume_dip_trigger`, `compute_risk_first_quantity`, `stop_target_prices`, all the exit ratchets, `score_universe`) — those are genuinely version-faithful. But every **stateful loop** that wires them is a parallel re-implementation that has been hand-patched repeatedly (G1/G2, T1.1-T1.3, fidelity_v2, recorded-fills). **So the replay is NOT version-agnostic today** — a new version that changes arming/slots/gate-order/the exit contract/the de-risk wiring diverges and must be hand-patched to match.

**Data recording (Pillar 2):**
- QUOTES/L1 `momentum_nbbo_spread_tape` — GOOD backbone (4M rows, 801-name universe @60s + ~366 names @~5s, 30d/5d retention). Keep.
- **PRINTS `iqfeed_trade_ticks` — THE BIGGEST GAP:** armed/live-only coverage (06-25: 88 print-syms vs 671 NBBO-universe = **87% of candidates have NO prints**), 3d retention (too short), starts ~09:00 ET (no premarket). A new version that picks a different name has nothing to fill against.
- L2 `iqfeed_depth_snapshots` — adequate, same coverage gap.
- OHLCV — fetched live at replay-time (the Massive-502 weekend nondeterminism).

**Prints-based fill model (Pillar 3):** the quote-walk over-fills (BEEM 29/34 vs live 1/34) because quotes can't see executions. The **print tape** can: an order fills iff the prints show executions THROUGH the limit with cumulative size > queue_ahead (not just a touch); `fill_vwap` replaces the hand-tuned `min(limit,max(bid,mid))` with a MEASURED price. Adaptive (review_latency = median of recorded submitted-minus-detected events; no magic). Version-agnostic — nothing reads `momentum_fill_outcomes`, so any version's different `(symbol,limit,qty,t0)` is evaluable against the immutable recorded prints.

---

## The architecture to make replay drive REAL code (version-faithful)

Invert the dependency: the **real** orchestration must take its I/O by parameter behind injected interfaces, then the replay supplies only those. Two interfaces:
1. **`MarketEnv`** (the only replay-specific data code): `quote_at(sym,ts)`, `prices_between(...)`, `prints_between(...)`, `l2_at(...)`, `ohlcv_asof(...)`, `clock`. Live = the real feeds; replay = the recorded tables.
2. **`FillExecutor`**: live = the broker; replay = the prints-fill model.

Then `run_auto_arm_pass` / the `run_live_runner_batch` entry+management loops are refactored to take `(now, env, fill_executor, risk_state)` — the SAME function body runs live and in replay. The leaf math is already shared; only the loop's I/O is parameterized.

---

## Roadmap (ordered, each shippable + reversible)

| Step | What | Effort | Risk |
|---|---|---|---|
| **0 · Recording unlock** ⭐URGENT | rewire the iqfeed-bridge watch-set (`iqfeed_trade_bridge.py:97-109`) from `trading_automation_sessions` → the NBBO universe (801 names) + raise print retention 3d→30d. **History must accrue — every day delayed loses coverage.** | ½ day | host-daemon restart + ⚠️ IQFeed watch-limit (confirm the plan's symbol cap) |
| **1 · Flag-set diff** ✅ BUILT | `scripts/_replay_version_diff.py` — run the existing replay twice (flag-set A vs B) over the same day, diff the ledgers. Works WITHOUT the refactor: identical orchestration → the reimplementation cancels in the DIFF. Pinned basis so only the flags differ. | done | zero (new script) |
| **2 · Prints fill model** | `TradeTape` + `prints_fill_decision` behind a flag; faithful version-agnostic fills. | 2-3d | flag |
| **3 · OHLCV snapshot** | record as-of OHLCV at decision time + a replay reader; kills the live-fetch (Massive-502) nondeterminism. | 1-2d | flag |
| **4 · THE refactor** | invert the live orchestration behind `MarketEnv`/`FillExecutor` → real-code replay → enables **two-SHA diffs** (any version, not just flags). | large | flag / parallel |

**STEP 1 is the smallest thing that answers "what does this change do?"** — it works now for the common case (the operator flips a flag) because both runs share orchestration + a pinned basis, so the diff is purely the flag effect. Verified 06-24: `ENGINE_ON 1 vs 0` = zero trade diff (the slot cap rarely binds); `FIDELITY_V2 0 vs 1` = A-only LIFE, ΔrunR −2.12.

---

## The version-diff harness (STEP 1, the deliverable)

`scripts/_replay_version_diff.py YYYY-MM-DD --a '<flagspec A>' --b '<flagspec B>' [--basis USD]`. Each flagspec is a comma-separated `KEY=VAL` env list. Runs the replay as two isolated subprocesses (env = flagspec + a pinned `CHILI_REPLAY_EQUITY_BASIS_USD` so sizing differences come only from the flags), then diffs:
- **NAME-SET** A-only / B-only / both (the selection delta)
- **SHARED names** entry_ts/px, exit px, qty, run_r (ΔrunR), why deltas
- **AGGREGATE** trades/wins/run-R/$ side-by-side ($ labeled relative-only, run-R the trustworthy signal)
- a one-line **VERDICT**

---

## The honest ceiling

Irreducible even with prints: queue position (we infer `queue_ahead` from L2/L1 size-at-touch — the real FIFO position is unobservable), hidden/iceberg liquidity, exact RH agentic review latency. The print tape converts these from **unmodeled** (the quote-walk assumed touch=fill) to **bounded with a confidence band**: a through-print is direct evidence shares traded at/through the limit; cumulating real size against an estimated queue gives a defensible fill/partial/cancel with a measured price (~1-3% of broker avg on validated fills). Trades resting on degraded data (quote_fallback / zero-queue) are flagged so the operator knows which diffs rest on prints vs estimates.

**Framing:** the replay becomes a **version-diff backtest** — "B differs from A by these trades", with fill-meta attribution (a divergence is traceable to fill-resolution vs decision-logic). The recorded-truth consumer (`21cd64c`, default-OFF) is demoted to an optional "what live actually did" reference lane.

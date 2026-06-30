# Momentum Lane: from SLOTS to a continuous risk-bounded ENGINE

**Date:** 2026-06-28 · **Author:** Claude (Opus 4.8) · **Branch:** `chili/momentum-defensive-veto-bundle` (deployed `cd01f0b`)
**Origin:** operator open-minded redesign — *"baka di naman slots ang dapat para sa ganitong algo trading... or dapat ba gawan ng engine"* + *"yung S1 and S4 bottlenecks din... i-redesign ng mas matino."*

Two principal-level design workflows (paradigm: `w282z0eg9`; S1+S4 latency: `w8cdgqdsc`), each a map → multi-design judge-panel → synthesis. This doc is the unified verdict + the safe migration.

---

## 0. The verdict

**The "slot" is the wrong abstraction. It is a human-derived constraint** — Ross watches 1–2 names because he is human. A machine's structural edge is **breadth** (evaluate the *whole* mover field, all hours), and the slot model both **throttles that edge** and **manufactures the contention/starvation/stuck-session pain.**

Judge panel score /70: slots-redesigned **51** · risk-budget engine **57** · actor/reactor **59** · **best-fit engine 60.**

The decisive findings, verified in the deployed code:
- There is **no single `MAX_SLOTS`**. The live cap is `adaptive_max_concurrent_live_sessions` (`risk_policy.py:386`), derived purely from a **simultaneous-open-RISK budget** — but it is charged against *every* non-terminal session, **including zero-risk watchers** (`live_fsm.py:44`, the code's own comment at `auto_arm.py:1549`: *"every pre-fill-or-held state against one cap... is exactly why the lane never fanned past ~5–15 watchers"*). Charging a $0-risk watcher against a risk budget is the inherited human habit.
- **The real-constraint machinery already fully bounds a no-slots engine:** risk-first sizing (`risk_policy.py:1098`), equity-relative notional/loss caps (`:354/:364/:374`), `liquidity_capped_notional` (`:1272`), the 3× combined-multiplier clamp (`live_runner.py:6957`), `aggregate_open_risk_usd` over holding states (`live_fsm.py:64`), the per-broker daily-loss breaker (#727), the drawdown breaker, the kill-switch.
- The dollar-risk budget is **already a `block`-severity check** (`risk_evaluator.py:1022`, `chili_momentum_max_aggregate_risk_pct_of_equity` default 0.03) — but it lives in the *evaluator* (advisory), not at the **atomic fill boundary** (`live_runner.py:7295` still checks a **count**, `effective_position_cap`), and it uses a **flat** per-trade estimate, not the candidate's actual `(entry−stop)×qty`.
- The decouple-watchers admission is **half-built behind a flag** — `chili_momentum_decouple_watching_enabled` (`config.py:3343`, default False, *"do not flip until the atomic fill-cap + fill-burst test land"*).

**So the engine is NOT greenfield.** It is: *decouple zero-risk watchers from the risk budget → promote the existing dollar-budget check to the atomic boundary → make it shape-aware → add one new safety component (a rail rate governor).*

**Why it kills the pain by construction:** a broker-flat **stuck** session (sid 9331, the FCUV spin) holds **zero** `aggregate_open_risk_usd` → it *physically cannot* block admission. Starvation/stuck/orphan are dissolved, not patched. (The duplicate-fill root fix, shipped `cd01f0b`, is step 0 of this — it makes the stuck state impossible at the source.)

**The one axis where the slot count was accidentally right:** a count is a free, crude rate-limiter. Delete it and the genuinely-new risk is **execution flooding** — N admitted names hitting the broker at once → 429 blow-out (the failure `project_crypto_live` already hit). **The rail rate governor is therefore the single load-bearing new component** — it is shared by both the engine (admission) and S4 (placement), and is the riskiest thing to validate.

---

## 1. S1 — ingest → select (the event-driven feeder)

**Bottleneck:** a cold new mover gets no viability row until the next **~300s batch** tick (`_run_equity_viability_refresh_job`, `_cvr_secs = max(60, 600/2) = 300`, `trading_scheduler.py:6043`) — so it cannot arm for up to ~300s, even though the data already flows (Massive ~60s + IQFeed tick daemons ~1s, now mirrored into `momentum_nbbo_spread_tape` via d473331).

**Finding:** the event scorer is **already built** — `ignition_loop.py` (`IgnitionScoringLoop`) subscribes the uncapped universe to the price bus, fires on a move-floor cross, and scores ONE symbol straight into `momentum_symbol_viability` via the same `run_momentum_neural_tick` path, `freshness_ts=now`, in ~seconds. **But it is dead on the live image:** `start_ignition_loop()` (`ignition_loop.py:431`) early-returns unless `chili_autopilot_price_bus_enabled` is True — and that flag **defaults False** (`config.py:2281`). The price-bus gate vetoes the (default-True) `chili_momentum_ws_ignition_enabled`.

**Redesign — two fail-independent triggers, same idempotent upsert:**
- **Trigger A (price bus):** wake `ignition_loop`, but replace its `move_pct`-only gate with `_ross_threshold_crossed()` = RVOL≥floor **OR** gap≥floor **OR** move%≥floor, within price-band 1–20 + float-eligible (reuse `tracker._rvol` + the nbbo bounds `nbbo_tape.py:42`). Basis-complete: catches a flat-day volume spike (the SKYQ case), not just names already up X%.
- **Trigger B (tape-delta DB poll — NEW, price-bus-INDEPENDENT, the live winner today):** a fast job reading only tape rows with `observed_at > in-process high-water mark` (incremental delta, not a full rescan), applying `_ross_threshold_crossed`, scoring crossers. Adaptive cadence `clamp(p50_tape_inter-row_gap, 5, 15)`. This is the fallback when the price bus is down (the live state). With the IQFeed mirror feeding tape at ~1s, the poll catches an igniter within one cadence.

Incremental scoring keeps percentile context by scoring the igniter against a small cached field snapshot (the last batch's `{symbol: signal}` dict). The 300s batch demotes to a slow backstop. **Target: new explosive mover `live_eligible` in ~5–15s, not ~300s.**

---

## 2. S2–S3 — arm → admit (the engine core)

Replace the slot count with **continuous risk-bounded admission**:
- **Watchers are free** — flip `chili_momentum_decouple_watching_enabled`; a watcher holds zero capital/risk and no longer counts against the budget. The field can fan to the whole universe.
- **Admission = a dollar/risk budget at the atomic fill boundary** — promote the existing `risk_evaluator.py:1022` block-check into the advisory-locked fill path (replacing the `effective_position_cap` count at `live_runner.py:7295`), and make it **shape-aware** (the candidate's real `(entry−stop)×qty`, so 10 tight-stop scalps ≠ 10 full-size trades). Keep `effective_position_cap` as an explicit **misconfig backstop**.
- **The rail rate governor** (the new safety component) bounds fills/min within the broker's real limit — adaptive token bucket (widen-on-success, halve-on-429), no fixed RPS.

---

## 3. S4 — order → fill (the fast executor)

**Latency bug:** fill-confirm AND repeg are coupled to the **external tick cadence** — a pending-entry session only advances on a WS tick (`_EVENT_TICK_MIN_SPACING_S=2.0`) or the 15s batch. So confirm = 2s best / 15s worst, and a repeg-able runaway can pass the spread ceiling before the next tick.

**Four kill-switched levers, all through the existing guarded adopt/cancel/orphan contract (no new write path):**
- **A. Inline micro-repeg** — keep every bound (ceiling `live_runner.py:2219`, risk-first re-size `:6276`, max-repeg counter, equity gate) but run the repegs *within the same tick*, adaptive inter-repeg delay `min(rail RTT, expected_move_bar_fraction)`, re-reading the live ask each iter, aggressiveness scaling `repeg_index/max_repegs`. "3 repegs over 6–45s" → "3 repegs over ~3 RTTs."
- **B. Fast ack-poll** — decouple fill-confirm from the tick; poll `get_order` at the measured RTT widening geometrically.
- **C. Rate-limited / parallel placement** — the rail governor (shared with §2) so multi-admission does not collide or 429.
- **D. Idempotent** — done (`cd01f0b`).

---

## 4. The new critical chain

```
pattern crosses Ross threshold on the tape
  → tape row (IQFeed→tape bridge d473331)            ~1s
  → live_eligible (S1 event-select)                  ~5–15s   [WAS up to 300s]
  → admitted (engine, continuous, risk-budget)       ~0–15s   (arm-pass cadence, not freshness)
  → order submitted (S4 governed place)              ~1 RTT
  → fill confirmed (S4 fast-poll; repeg ~3 RTTs)     ~1–2 RTTs [WAS 2–15s]
```

**The slowest hop is no longer the 300s select.** It is now a tie between the S1 tape-delta poll (~5–15s) and the arm-pass cadence (~up to 15s) — whichever is configured slower. Everything downstream collapses to a few rail-bound RTTs. The *next* optimization target after this lands is the arm-pass cadence.

---

## 5. Migration — ordered, kill-switched, parity-tested, instantly reversible

Fallback at every step = the deployed 300s batch (S1) + tick-coupled order path (S4) + slot count (engine).

- **Phase 0 — instrument (zero behavior change):** add `ignite_to_viability_ms` + `place→confirm_ms` logging on both the batch and the dead ignition path. Establish the ~300s / 2–15s baselines. *(Duplicate-fill root fix already shipped as the lifecycle step-0.)*
- **Phase 1 — S1 trigger B in SHADOW:** `_run_tape_delta_ignite_job` + `_ross_threshold_crossed` + shared `IgniteDedup` behind `chili_momentum_event_select_primary_enabled` / `chili_momentum_tape_delta_ignite_enabled`; shadow only LOGS would-ignite names + the lead-time, no write. Parity: flag off = byte-identical. Verify shadow ignites lead the batch and pick the same names.
- **Phase 2 — S1 trigger B PRIMARY:** flip writes on (idempotent upsert with the batch). Keep the 300s batch as backstop. Monitor dup-rows (must stay 0), idle-in-transaction (short-lived rollback-in-finally session, `max_instances=1`), first ignite→arm latency. Revert = one flag.
- **Phase 3 — S1 trigger A (price bus) ON:** `CHILI_AUTOPILOT_PRICE_BUS_ENABLED=1` to wake `ignition_loop` with the tightened gate, in parallel with B.
- **Phase 4 — Engine admission:** flip `decouple_watching` (watchers free) → promote the shape-aware dollar-budget to the atomic boundary behind a flag in shadow (log would-admit vs the count) → flip primary, `effective_position_cap` as misconfig backstop.
- **Phase 5 — Rail governor + S4 inline repeg / fast-poll:** ship the adaptive rate governor (shared), then the S4 levers, each behind its own flag.

**Adaptive knobs (no magic):** S1 cadence `clamp(p50_tape_gap, 5, 15)` (the one documented floor = 5s); ignite predicate reuses the existing Ross floors; S4 inter-repeg `min(rail RTT, expected_move_bar_fraction)`; governor refill/burst discovered adaptively. Every phase off ⇒ byte-identical to deployed.

---

## 6. The riskiest assumption (validate first, cheaply)

**The MCP rail's real per-account rate limit.** S4's `get_order` fast-polls spend the *same* undocumented budget as order *places* (`get_order` is a LIST endpoint, `robinhood_mcp.py:382`). If the limit is tight, fast-polling under multi-admission could **starve actual placement** — the exact execution-flooding risk the paradigm judge flagged. **The rail governor is the load-bearing safety component for both the engine and S4.** Validate the rail's real limit (measured RTT + 429 onset under a controlled burst) BEFORE flipping engine-admission primary or S4 parallel-placement. Until then, the slot count stays as the free rate-limiter (Phases 0–3 don't touch it).

**Recommended sequencing vs Monday:** Phases 0–1 are zero-behavior-change and safe anytime. Hold the behavior-changing flips (Phase 2+) until **after** Monday's open gives a clean live baseline — so each change is measured before/after (evolve-not-devolve), not muddied by the open. Stage 0 (prove expectancy: catch a follow-through winner) remains the gating business question; this engine is the *throughput/latency* substrate that lets a proven edge scale, not a substitute for the edge.

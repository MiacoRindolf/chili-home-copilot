# Replay v3 — Live-FSM Simulator

> **Status:** DESIGN (multi-day tracked initiative). No code yet.
> **Author:** CHILI Code (investigation 2026-06-29).
> **Scope:** Drive CHILI's *real* momentum entry/exit decision FSM against historical recorded
> data, through a mock broker, on a simulated clock — so entry-path fixes (the UPC `live_eligible`
> recency-grace being the motivating case) can be PROVEN offline before they touch real money.
> **Related:** [[project_replay_lab]] · [[project_momentum_engine]] · `docs/DESIGN/MOMENTUM_LANE.md`
> · `docs/DESIGN/MOMENTUM_ENGINE.md` · `tests/test_entry_feature_parity.py`

---

## 1. Goal + the gap

### 1.1 What we have today (two instruments, each blind to half the problem)

**(A) Replay v2 — `app/services/trading/momentum_neural/replay_v2.py`** (entry CLI
`scripts/_replay_v2.py`). A **tape-replay BACKTEST**: it steps a simulated 1-minute clock
(`pd.date_range(..., freq="1min")`, `replay_v2.py:1112`; main loop `for now in day_grid:`
`replay_v2.py:1558`) over the recorded NBBO tape and trade prints, ranks/arms candidates,
fires the *shared pure trigger functions*, and models fills from the tape.

What it does well:
- Real recorded data: `momentum_nbbo_spread_tape` (`replay_v2.py:440`), `iqfeed_trade_ticks`
  (`replay_v2.py:574`), live arm spans from `trading_automation_sessions` (`replay_v2.py:1041`),
  recorded broker fills from `momentum_fill_outcomes` (`replay_v2.py:355`).
- Calls the SHARED pure triggers — `momentum_pullback_trigger` (`replay_v2.py:1705`),
  `halt_resume_dip_trigger` (`replay_v2.py:1689`) — and the SHARED exit math (cushion trail,
  OFI lock, sell-into-strength ladder, pyramid, max-loss circuit; `replay_v2.py:1397-1544`).
- A genuine fill model with a confidence band (quote-touch + prints-fill + fidelity-v2 day band).

**The hard wall: Replay v2 NEVER touches the live FSM.** Grep-confirmed absent from
`replay_v2.py`: `evaluate_proposed_momentum_automation`, `tick_live_session`,
`runner_boundary_risk_ok`, `begin_live_arm` / `confirm_live_arm`, and any read of the
`live_eligible` gate / `live_eligible_at_utc` anchor / the recency-grace. It re-implements the
arm→enter decision *inline* over the tape. So it **cannot exercise — and could not prove —**
any fix that lives inside the live FSM's risk gate. The UPC recency-grace
(`risk_evaluator.py:349-389`, consumed at `risk_evaluator.py:841-891`) is exactly such a fix.

**(B) The PAPER runner — `paper_runner.py:tick_paper_session(db, session_id, quote_fn=None)`
(`paper_runner.py:550`).** Drives a full session FSM with real fill simulation
(`paper_execution.long_entry_fill_price` `paper_execution.py:122`,
`long_exit_fill_price` `paper_execution.py:127`). But it reads `datetime.utcnow()` directly
on every tick (`paper_runner.py:97`) — **REAL TIME ONLY. It cannot replay a past day.** And
it is the *paper* FSM (`paper_fsm.py`), disjoint from the *live* FSM (`live_fsm.py`) — it does
not call `runner_boundary_risk_ok` / the live `evaluate_proposed_momentum_automation` gate, so
even in real time it isn't the live decision path.

### 1.2 The gap, in one line

> There is **no instrument that runs the LIVE FSM's actual decision path against HISTORICAL data.**
> Tape-replay has the data + a clock but not the FSM; the paper runner has the FSM but no
> historical clock — and neither runs the *live* gate where the entry fixes live.

### 1.3 What Replay v3 uniquely enables

Merge the two: **drive the real `live_runner.tick_live_session` FSM** (the exact function the
scheduler + event loop call in prod) forward on a **simulated clock**, feeding it **replayed
tape/viability/eligibility** through a **mock broker** that implements the `VenueAdapter` protocol.

Concretely, Replay v3 can do the one thing v2 cannot:

- Replay 2026-06-29 (the real UPC +500% day), arm UPC exactly as live did, and answer:
  **grace OFF → does UPC stay blocked at the entry instant? grace ON → does UPC enter?** —
  the A/B that proves the recency-grace fix on the real move, deterministically, offline.
- More generally: prove ANY entry-gate / exit-management change is net-positive or behavior-
  preserving by running the *real* FSM, satisfying [[feedback_evolve_not_devolve]] (prove every
  change net-positive/neutral with a parity-test + measure before/after + per-sha rollback).

---

## 2. Architecture — the four components

```
                      ┌───────────────────────────────────────────────────────────┐
                      │                    REPLAY v3 DRIVER                         │
                      │  (new: momentum_neural/replay_v3.py)                        │
                      │                                                             │
   recorded DB        │   simulated clock ──► for t in event_grid(date):           │
   ┌──────────────┐   │      │                   ├─ EligibilityReplayer.apply(t)   │
   │ nbbo tape    │──►│  ┌───▼────────┐          │   (write live_eligible @ t)     │
   │ iqfeed ticks │──►│  │ SimClock   │          ├─ MockBroker.advance(t)          │
   │ fast_orderbk │──►│  │ (frozen _  │          │   (BBO/fills as-of t)           │
   │ depth snaps  │──►│  │  utcnow)   │          └─ tick_live_session(db, sid,      │
   │ viability TS │   │  └────────────┘                adapter_factory=MockBroker) │
   │ live sessions│   │                                       │  ◄── REAL FSM      │
   │ momentum_    │   │   ┌──────────────────┐                ▼                    │
   │  fill_outcomes│  │   │ ParityHarness    │ ◄── decision trace vs live record   │
   └──────────────┘   │   └──────────────────┘                                     │
                      └───────────────────────────────────────────────────────────┘
```

### 2.1 (a) MOCK BROKER adapter — drop-in `VenueAdapter`

**Seam already exists.** `tick_live_session(db, session_id, *, adapter_factory=None)`
(`live_runner.py:5000-5005`) takes an injectable `adapter_factory`; absent it resolves the real
one via `resolve_live_spot_adapter_factory(ef)` (`live_runner.py:5029`). **Replay v3 passes a
`MockBrokerAdapter` factory — zero fork of the runner.**

The mock implements the `VenueAdapter` Protocol (`venue/protocol.py:133-183`). The runner only
calls a small subset per tick:
- `is_enabled()` → True (`live_runner.py:5046`)
- `get_best_bid_ask(product_id)` → `(NormalizedTicker, FreshnessMeta)` — the BBO **as-of the
  simulated clock**, reconstructed from `momentum_nbbo_spread_tape` / `iqfeed_trade_ticks`
  (`live_runner.py:5187`). Freshness stamped at `SimClock.now()` so the runner's stale-quote /
  freshness checks see a "live" quote.
- `place_market_order` / `place_limit_order_gtc` (`protocol.py:150-172`) → simulated accept +
  fill, REUSING the pure paper fill math: entry crosses the ask + adverse slippage
  (`paper_execution.long_entry_fill_price` `paper_execution.py:122`), exit crosses the bid
  (`long_exit_fill_price` `paper_execution.py:127`), fees via `roundtrip_fee_usd`
  (`paper_execution.py:132`). Returns the same result shape the runner already consumes
  (`_order_result` style dict; see `robinhood_mcp.py:763-839`).
- `get_order(order_id)` → resolve the resting order to filled/partial/cancelled deterministically
  from the tape at the fill instant (the runner polls this for ack-timeout resolution).
- `cancel_order(order_id)` → always accept (`protocol.py:173`).

**Realistic reject/edge cases the mock MUST model** (these are the exact failure paths the live
runner branches on, so a faithful mock must reproduce them):
- **`no_bbo`**: when the tape has no quote at/near `t`, return `(None, …)` from
  `get_best_bid_ask` → the runner emits `live_blocked_by_risk reason=no_bbo` and (for a
  persistently quoteless armed name) `_decline_terminal(reason="no_bbo")` → `live_cancelled`
  (`live_runner.py:5188-5197`). This is the RVMDW/warrant-class path.
- **`401 / needs_reauth` / venue-disconnect**: optional injected fault to exercise
  `_venue_broker_connected` fail-open (`live_runner.py:5055-5056`) and the `needs_reauth` order
  result (`robinhood_mcp.py:794`). Off by default (deterministic), on for fault-injection runs.
- **partial fill**: return `filled_size < base_size` so the runner's partial-exit/partial-entry
  bookkeeping is exercised (mirrors prints-fill PARTIAL in v2, `replay_v2.py` prints model).
- **ack timeout**: don't fill the resting entry within the configured window → the runner
  cancels + re-watches (the `live_pending_entry → watching_live` edge, `live_fsm.py:123`).

> **Reuse, not rebuild:** the fill PRICE math is the *paper* lane's pure functions
> (`paper_execution.py:112-174`), already proven and unit-tested. The mock is a thin
> `VenueAdapter` shell (~150 LOC) that wraps them + a tape-keyed order book. It does NOT
> re-derive fill logic.

### 2.2 (b) FSM DRIVER — step the REAL `live_runner` forward on a sim clock

The driver (`replay_v3.py`, new) owns the loop:

1. Build the **event grid** for the date. Unlike v2's fixed 1-min grid, prefer the **union of
   recorded `observed_at` timestamps** across the day's tape so a sub-minute TOCTOU flicker
   (UPC) is hit at true tick granularity (v2's 1-min grid would *step over* a 3-second flicker).
2. Seed sessions: for each live-armed name in `trading_automation_sessions` for the date
   (`replay_v2.py:1041` read pattern), reconstruct an `armed_pending_runner` / `queued_live`
   session row in the **replay DB** (a `_test` / `_replay` DB — never prod) with the *recorded*
   `risk_snapshot_json` (including the `live_eligible_at_utc` anchor `confirm_live_arm` stamped,
   `operator_actions.py:594`). Or, for counterfactual modes, run `begin_live_arm` +
   `confirm_live_arm` (`operator_actions.py:318/487`) under the sim clock to mint the anchor.
3. For each `t` in the grid, in order:
   - `EligibilityReplayer.apply(t)` (§2.3) — make the DB's viability row reflect the
     `live_eligible` state as-of `t`.
   - `MockBroker.advance(t)` — set the broker's BBO/fill clock to `t`.
   - `SimClock.set(t)` — freeze the runner's clock at `t` (§4.1).
   - `tick_live_session(db, sid, adapter_factory=mock_factory)` for each runnable session.
4. Collect the per-tick decision trace (the runner's emitted events via
   `append_trading_automation_event`, already persisted to `trading_automation_events`).

**The FSM is NOT reimplemented.** `tick_live_session` runs verbatim: it reads viability
(`live_runner.py:5168`), computes the spread/quote gate, calls `runner_boundary_risk_ok(...,
apply_eligibility_grace=True)` (`live_runner.py:2450-2482`) which calls the REAL
`evaluate_proposed_momentum_automation` with the recency-grace evidence
(`live_runner.py:2470-2481`), fires the shared triggers, places via the mock, manages exits.

### 2.3 (c) VIABILITY / ELIGIBILITY REPLAYER — reproduce the flicker

**The single hardest data problem.** `MomentumSymbolViability` has a UNIQUE(symbol, variant_id)
constraint (`models/trading.py:1565-1566`) and `live_eligible` is a single mutable column
(`models/trading.py:1577`) — **it is a current-state snapshot, NOT a time-series.** There is no
`momentum_viability_history` table. So the eligibility timeline that produced the UPC flicker is
**not directly recorded** as a column history.

Reconstruction strategy, in priority order:

1. **Forward-momentum / OFI is ALREADY replay-native (the linchpin).** The recency-grace's
   forward-momentum leg (`risk_evaluator._live_eligible_recency_grace_active`, evidence supplied
   by `live_runner._live_forward_momentum` `live_runner.py:2418-2447`) reads
   `pipeline._live_flow_slope(symbol, db, as_of=…)` — and **both** `_live_flow_slope`
   (`pipeline.py:671-698`, "same `as_of` replay semantics") and `_live_ofi_microprice`
   (`pipeline.py:212-271`, "`as_of` reads L2 AS-OF a historical instant … the durable table is
   the sole source") **already accept an `as_of` parameter** that reads `iqfeed_trade_ticks` /
   `fast_orderbook` / `iqfeed_depth_snapshots` as-of a past instant. So the *forward-momentum*
   evidence of the grace is faithfully reconstructible from recorded data with NO new schema —
   the replayer just needs the runner's read to thread `as_of = SimClock.now()` (see §6 refactor).
2. **`live_eligible` state at `t`:** three sources, best available wins:
   - **(preferred) recompute** from recorded inputs by running the *viability scorer* as-of `t`
     (the same scorer `replay_v2.py` `full_pipeline` mode invokes via `score_universe`), writing
     the resulting `live_eligible` to the DB row before the tick. This regenerates the true
     flicker because it uses the same scoring logic over the recorded tape.
   - **(fallback) event-derived**: mine `trading_automation_events` for the date — the runner
     logs `live_eligible` reads and `live_blocked_by_risk` / `live_eligible` check details in the
     boundary-risk `checks` (the grace `detail` dict `risk_evaluator.py:870`). Stitch a step
     function of eligibility over time from those emitted traces.
   - **(degenerate) two-state**: eligible during the recorded arm/confirm span, then driven by
     the recomputed scorer at the entry window — enough to reproduce a "eligible-at-confirm,
     flicker-False-at-entry" TOCTOU even if intermediate transitions are coarse.
3. The replayer WRITES the chosen `live_eligible` + `freshness_ts` onto the single viability row
   immediately before each tick, so the unchanged `tick_live_session` viability read
   (`live_runner.py:5168`) and the gate (`risk_evaluator.py:841`) see the as-of-`t` state.

> **Why this faithfully reproduces UPC:** at confirm, the row was `live_eligible=True` and the
> anchor `live_eligible_at_utc` was stamped (`operator_actions.py:594`). At the entry instant the
> replayer flips `live_eligible=False` (the flicker) while the `as_of` OFI read shows forward
> momentum True. That is *exactly* the input pair the grace branch keys on
> (`risk_evaluator.py:855-872`). Grace OFF → block; grace ON → warn (enter). Reproduced.

### 2.4 (d) PARITY HARNESS — sim decision path == live decision path

Discipline from `tests/test_entry_feature_parity.py`: feed identical input to both paths, assert
equal output at each step.

- **Recorded-session parity:** pick a real recorded live session (entry candidate detected →
  submitted → filled → exited, reconstructable from `trading_automation_events` +
  `momentum_fill_outcomes`). Replay that session through Replay v3. Assert the v3 decision trace
  matches the recorded live trace **at each FSM transition**: same state sequence
  (`watching_live → live_entry_candidate → live_pending_entry → live_entered → … → live_exited`,
  the legal edges in `live_fsm.py:114-163`), same gate verdicts (allowed/blocked + reason),
  same fill side/qty within tolerance.
- **Determinism check:** run the same date twice → byte-identical decision trace (no wall-clock,
  no network, fixed RNG if any). A diff = a hidden real-time/global dependency to quarantine (§7).
- **Grace-flag invariance (the v2 discipline):** with the grace flag OFF, Replay v3's decision
  trace on a non-flickering session must be **identical** to the flag-OFF live trace — proving
  the harness itself adds no drift (mirrors the "flag-off byte-identical" property of the live
  code, `risk_evaluator.py:377-378`).

---

## 3. Critical design constraints

### 3.1 The simulated clock (replace the runner's `now()` reads)

The runner's clock chokepoint is the module-level **`live_runner._utcnow()`** (`live_runner.py:141`):
```python
def _utcnow() -> datetime:
    return datetime.utcnow()
```
The vast majority of time reads route through it (`_utcnow()` at lines 346, 778, 985, 1426, …,
9442 — dozens of call sites). **Replay v3 controls time by controlling this one function.**

Recommended mechanism (cleanest, least invasive — see §3.4):
- Introduce a process-global, thread-local-safe **sim-clock override** read by `_utcnow()`:
  ```python
  _SIM_NOW: contextvars.ContextVar[datetime | None] = ContextVar("_sim_now", default=None)
  def _utcnow() -> datetime:
      v = _SIM_NOW.get()
      return v if v is not None else datetime.utcnow()
  ```
  Flag-/var-gated: `_SIM_NOW` unset in prod → `datetime.utcnow()` byte-identical. The driver sets
  it per tick. This is a **2-line, behavior-preserving** change to live code.

**Residual direct-clock reads to also thread or freeze** (they bypass `_utcnow()`):
- `datetime.now(timezone.utc)` / `datetime.now(ZoneInfo("America/New_York"))` at
  `live_runner.py:2992, 3320, 3886, 4922, 5383, 6332`. Each must be converted to read the sim
  clock on the replay path (or, where it's a tz-presentation read like the ET wall-clock window
  check, fed the sim instant). Catalog them in P0; convert the entry-path-relevant ones first.
- The `as_of` parameter on `_live_flow_slope` / `_live_ofi_microprice` (`pipeline.py:675, 213`)
  must be passed `SimClock.now()` from the runner's grace read (`live_runner.py:2469`) — a small
  thread-through (§6). Today the runner calls them WITHOUT `as_of` (live default).
- `VenueAdapter` freshness: the mock stamps `FreshnessMeta(retrieved_at_utc=SimClock.now())`
  (`protocol.py:20-34`) so `is_fresh_enough` (`protocol.py:37`) compares sim-to-sim.

### 3.2 Deterministic + no real broker / no network

- All market reads come from the **mock broker** (BBO/fills) or **recorded DB tables** (tape,
  ticks, depth, viability, sessions). No `fetch_ohlcv_df` / Massive / Polygon / RH-MCP network
  calls on the replay path. **`fetch_ohlcv_df` is the one heavy external read inside
  `tick_live_session`** (`live_runner.py:5213, 5586, 6154, …` — ~10 call sites for 15m ATR /
  expected-move / triggers). Replay v3 must **inject a recorded-OHLCV provider** (build 15m bars
  from the tape, or snapshot the day's bars once and serve as-of). This is the second-largest
  refactor after the clock (§6).
- No RNG, or a seeded one. Verified determinism via the parity harness double-run (§2.4).
- Runs against a **dedicated replay DB** (`chili_replay` or a `_test`-suffixed DB), NEVER prod —
  the driver WRITES session rows + viability flips. Honors Hard Rule 4 (`_test`-suffixed,
  `conftest.py` guard) for the test harness; the multi-day batch runner uses `chili_replay`
  refreshed from `chili_staging` (prod-shaped, see `docs/STAGING_DATABASE.md`).

### 3.3 REUSE over rebuild (the anti-divergence principle)

Replay v2's original sin: it FORKED the decision logic (inline arm/enter over tape), so it drifts
from live and can't prove live-only fixes. **Replay v3's non-negotiable invariant:** it WRAPS
`tick_live_session` and the live gate; it never re-implements them. If a behavior needs to be in
the sim, it must be reachable by calling the real function — otherwise it's out of scope. This is
why the FSM driver (§2.2) is thin and the mock broker (§2.1) only supplies *inputs*.

### 3.4 How to inject the mock broker + sim clock without forking `live_runner`

Three options considered:

| Mechanism | Mock broker | Sim clock | Verdict |
|---|---|---|---|
| **A. Dependency injection** | `adapter_factory=` already a param (`live_runner.py:5004`) ✅ | needs a `now_fn=` param threaded through dozens of call sites ✗ (huge surface) | broker: YES |
| **B. Process-global var (ContextVar)** | n/a | `_utcnow()` reads `_SIM_NOW` (§3.1) — 1 chokepoint, ~6 stragglers ✅ | clock: YES |
| **C. Monkeypatch in the test/driver** | patch `resolve_live_spot_adapter_factory` | patch `_utcnow` | fallback for stragglers + `fetch_ohlcv_df` until injected |

**Recommendation:** **broker via existing DI (A)** + **clock via ContextVar override (B)** +
**OHLCV via an injected provider seam (a new optional `ohlcv_provider=` param or a ContextVar
mirror of the clock pattern)**. Monkeypatch (C) only as the P0 scaffolding shortcut for the
straggler `datetime.now(...)` reads and `fetch_ohlcv_df`, to be replaced by clean seams in P1/P5.
This keeps live-code edits to: 1 ContextVar in `_utcnow`, ~6 straggler conversions, 1 OHLCV
provider seam, 1 `as_of` thread-through on the grace read. All flag/var-gated → **flag-off
byte-identical**, satisfying [[feedback_no_dark_flags]]'s spirit by being default-inert in prod
while the *replay harness* turns it on.

---

## 4. Phased build plan (P0..P5)

Each phase is independently shippable + testable. Effort in ideal-engineering-days.

### P0 — Sim-clock + mock-broker scaffolding · **~1.5 d**
- Add `_SIM_NOW` ContextVar override to `live_runner._utcnow()` (§3.1); prove flag-off
  byte-identical (existing live tests still pass).
- New `momentum_neural/replay_mock_broker.py`: `MockBrokerAdapter` implementing `VenueAdapter`
  (`protocol.py:133-183`), BBO from a tape dict, fills via the pure
  `paper_execution.long_entry_fill_price` / `long_exit_fill_price` / `roundtrip_fee_usd`.
- Unit-test the mock in isolation: place→fill, no_bbo→None, partial, cancel, ack-timeout.
- **Ship gate:** mock passes its unit tests; `_utcnow` override is inert when unset.

### P1 — Drive the FSM for ONE armed session end-to-end · **~2.5 d**
- New `momentum_neural/replay_v3.py` driver: seed one `queued_live` session in the replay DB
  from a recorded arm (with the `live_eligible_at_utc` anchor), build an event grid, step
  `tick_live_session(db, sid, adapter_factory=mock)` across it.
- Inject the recorded-OHLCV provider (build 15m bars from tape, serve as-of) — the
  `fetch_ohlcv_df` replacement (§3.2).
- Convert the entry-path straggler `datetime.now(...)` reads to the sim clock (§3.1).
- **Ship gate:** one session walks `watching_live → … → live_exited` with mock fills and a
  coherent PnL; no network calls (assert via a network guard); deterministic double-run.

### P2 — Viability / eligibility replayer (reproduce a flicker) · **~2 d**
- New `momentum_neural/replay_eligibility.py`: write `live_eligible` + `freshness_ts` as-of `t`
  (recompute via the scorer; fall back to event-derived; §2.3).
- Thread `as_of=SimClock.now()` into the runner's grace forward-momentum read (`live_runner.py:2469`
  → `pipeline._live_flow_slope(as_of=…)`).
- **Ship gate:** a synthetic flicker (eligible→False→eligible inside the grace window) is faithfully
  reproduced — the grace `detail` dict (`risk_evaluator.py:870`) shows
  `recent_eligible_within_window=True` at the flicker tick.

### P3 — Parity harness vs a real recorded session · **~2.5 d**
- New `tests/test_replay_v3_live_fsm_parity.py`: reconstruct a real recorded live session's
  trace, replay it, assert FSM-transition-level parity (state sequence + gate verdicts + fill
  side/qty within tolerance) — the `test_entry_feature_parity.py` discipline.
- Add the determinism + grace-flag-invariance assertions (§2.4).
- **Ship gate:** parity passes on ≥1 recorded session; double-run byte-identical.

### P4 — The UPC ACCEPTANCE TEST · **~1.5 d**
- New `tests/test_replay_v3_upc_recency_grace.py` (or a CLI run): replay 2026-06-29, seed UPC's
  recorded arm, reproduce its `live_eligible` flicker at the entry instant (P2), with forward
  momentum present (recorded OFI shows it).
- Assert: **grace OFF → UPC blocked at entry (`live_eligible` check `ok=False severity=block`);
  grace ON → UPC enters (`ok=True severity=warn`, fills via mock).** The exact A/B v2 cannot run.
- **Ship gate:** the A/B produces the two opposite outcomes deterministically — the proof artifact.

### P5 — Batch / day replay + metrics · **~3 d**
- Day-level driver: seed ALL recorded live-armed names for a date, run the grid, emit
  aggregate metrics (entries, fills, win/loss, run-R, $ band) comparable to v2's output so the
  two engines can be cross-checked.
- Clean up the OHLCV + clock seams (replace any P0 monkeypatch with the injected providers).
- Optional: a `/trading/replay?engine=v3` surface + a version-diff harness (v3 grace-on vs
  grace-off across many days) mirroring the v2 `--json` ledger-diff (`scripts/_replay_v2.py:38-44`).
- **Ship gate:** a full day replays end-to-end; v3 vs v2 aggregate sanity-cross-checks; the
  grace A/B runs across a batch of days.

**Total: ~13 ideal-days** (P0–P4 ≈ 10 d gets the UPC proof; P5 adds the batch instrument.)

---

## 5. THE ACCEPTANCE TEST — proving the UPC recency-grace

**The thing Replay v2 structurally cannot do**, spelled out:

**Setup (date = 2026-06-29):**
1. Replay DB seeded from recorded data: UPC's `momentum_nbbo_spread_tape` (~1.10M rows that day),
   `iqfeed_trade_ticks` (~1.09M rows), `fast_orderbook` / depth snapshots, and the recorded
   `trading_automation_sessions` arm for UPC (with the confirm-time `live_eligible_at_utc` anchor,
   `operator_actions.py:594`).
2. The EligibilityReplayer (P2) reconstructs UPC's `live_eligible` timeline: True at confirm,
   flickering False at the entry instant, while the as-of OFI read (`_live_flow_slope(as_of=t)`,
   `pipeline.py:671`) shows forward momentum True.

**Run A — grace OFF** (`policy.live_eligible_recency_grace_enabled = False`):
- At the entry tick, `tick_live_session` → `runner_boundary_risk_ok(apply_eligibility_grace=True)`
  → `evaluate_proposed_momentum_automation`. Because the flag is off,
  `_live_eligible_recency_grace_active` returns `(False, …)` immediately
  (`risk_evaluator.py:377-378`), the `live_eligible` check is `ok=False severity=block`
  (`risk_evaluator.py:874-882`), `allowed=False`. **UPC does NOT enter** → terminalizes
  `live_cancelled` (`live_runner.py:5195` no_bbo path or the clean pre-entry decline). PnL = $0.

**Run B — grace ON** (`live_eligible_recency_grace_enabled = True`, window covers arm→entry age):
- Same tick. The anchor parses + is in-window AND forward momentum is True →
  `_live_eligible_recency_grace_active` returns `(True, …)` (`risk_evaluator.py:383-389`), the
  `live_eligible` check is **downgraded to `ok=True severity=warn`** (`risk_evaluator.py:860-872`),
  `allowed=True`. **UPC ENTERS**, the mock broker fills it from the recorded ask, and the shared
  exit math rides the +500% move → a large positive run-R / $.

**Proof:** the two runs differ ONLY in the grace flag and produce **opposite entry outcomes** on
the real recorded move — deterministically, offline, with no real broker. That is the load-bearing
evidence the fix works, which Replay v2 could never produce (it never reaches the grace branch).
Honors [[feedback_fix_dont_defer_surfaced_issues]] (prove the fix, don't re-list it) and
[[feedback_overfit_default_live]] (the A/B *is* the live-test-on-the-real-system, run offline).

---

## 6. REUSE MAP — what each component wraps + minimal live-code edits

### What Replay v3 REUSES (does not re-implement)

| Component | Reuses (cite) |
|---|---|
| Mock broker | `VenueAdapter` Protocol `venue/protocol.py:133-183`; pure fill math `paper_execution.long_entry_fill_price` `:122`, `long_exit_fill_price` `:127`, `roundtrip_fee_usd` `:132`, `build_synthetic_quote` `:112`; order-result shape `robinhood_mcp.py:763-839` |
| FSM driver | **`live_runner.tick_live_session` `:5000`** (verbatim, via `adapter_factory=` `:5004`); `runner_boundary_risk_ok` `:2450`; `evaluate_proposed_momentum_automation` `risk_evaluator.py:593`; shared triggers `momentum_pullback_trigger` / `halt_resume_dip_trigger` (entry_gates); arm flow `begin_live_arm`/`confirm_live_arm` `operator_actions.py:318/487` |
| Eligibility replayer | `pipeline._live_flow_slope(as_of=)` `:671` + `_live_ofi_microprice(as_of=)` `:212` (**already replay-native**); the grace evidence builders `_arm_time_live_eligible_anchor` `live_runner.py:2392` + `_live_forward_momentum` `:2418`; the grace decision `_live_eligible_recency_grace_active` `risk_evaluator.py:349`; recorded reads from `trading_automation_sessions` (`replay_v2.py:1041`) + `momentum_fill_outcomes` (`replay_v2.py:355`) |
| Parity harness | `tests/test_entry_feature_parity.py` discipline; the legal-edge set `live_fsm.py:114-163`; recorded trace from `trading_automation_events` |
| Data layer | tape schema `migrations.py:20437-20447`; ticks schema `iqfeed_trade_bridge.py:68-76`; v2's load helpers `Tape`/`TradeTape` (`replay_v2.py:440/574`) can be lifted as-is |

### Minimal live-code edits required (all flag/var-gated, behavior-preserving)

1. **`live_runner._utcnow()` `:141`** — read a `_SIM_NOW` ContextVar; default → `datetime.utcnow()`
   (byte-identical when unset). *[~2 lines]*
2. **~6 straggler `datetime.now(...)` reads** in `live_runner` (`:2992, 3320, 3886, 4922, 5383,
   6332`) — route the entry-path-relevant ones through the sim clock on the replay path. *[small]*
3. **OHLCV provider seam** — make `tick_live_session`'s `fetch_ohlcv_df` calls go through an
   injectable provider (param or ContextVar) so the replay serves recorded bars. *[moderate; the
   one nontrivial refactor]*
4. **`as_of` thread-through** — pass `SimClock.now()` into the grace forward-momentum read at
   `live_runner.py:2469` (`_live_flow_slope(as_of=…)`). The `as_of` plumbing already exists
   downstream. *[~3 lines]*

No change to the FSM edges, the gate logic, the grace math, or the fill math — Replay v3 only
adds *seams*, it does not alter *decisions*. This is the explicit guard against repeating v2's
fork-and-drift failure.

---

## 7. Risks + mitigations

| # | Risk | Mitigation |
|---|---|---|
| R1 | **No `live_eligible` time-series** (single-column snapshot, `models/trading.py:1577`). The flicker isn't directly recorded. | §2.3 three-tier reconstruction; the *forward-momentum* leg is already replay-native via `as_of` (`pipeline.py:671/212`), so only the eligibility step-function is approximated — and even the degenerate two-state reproduces the UPC TOCTOU. **Optional future:** add a lightweight `momentum_viability_history` append table (new migration) to record `(symbol, variant_id, live_eligible, ts)` going forward → perfect fidelity for future incidents. |
| R2 | **Hidden real-time / global deps in the runner** (the ~6 straggler `datetime.now`, in-process OFI ring `microstructure.get_book_buffer` `pipeline.py:258`, price-bus singletons `live_runner_loop.py`). | The OFI ring is already skipped under `as_of` (table-only, `pipeline.py:255-271`) — good. Catalog stragglers in P0; the determinism double-run (§2.4) FLAGS any uncaught real-time dep as a trace diff to quarantine. Replay never starts the price-bus/event loop — it calls `tick_live_session` directly (the loop is only a dispatch hint, `live_runner_loop.py:1-23`). |
| R3 | **`fetch_ohlcv_df` network dependency** inside the tick (~10 call sites). | P1 injects a recorded-OHLCV provider built from tape; a network guard in the harness asserts zero external calls. Largest single refactor — sized into P1/P5. |
| R4 | **Fill-model realism vs live** (the mock is the *paper* fill math; live RH-MCP fills differ — partials, queue, 4xx). | The mock supports partial/no_bbo/ack-timeout/401 fault injection (§2.1); the parity harness (P3) calibrates the mock against *recorded* `momentum_fill_outcomes` (broker truth). Report a fill-fidelity band like v2's `day_pnl_band`, not a point estimate. |
| R5 | **Replay/live divergence creep** (the v2 disease). | The REUSE invariant (§3.3) + the grace-flag-invariance parity assertion (§2.4) + flag-off-byte-identical seams (§6) structurally prevent a forked decision path. If a behavior can't be reached by calling the real function, it's out of scope. |
| R6 | **DB write safety** (driver seeds sessions + flips viability). | Dedicated `chili_replay`/`_test` DB only; honors Hard Rule 4. Never the prod `chili` DB. The harness uses the `conftest.py` truncate guard. |
| R7 | **Clock-skew / tz correctness** (tape `observed_at` is TIMESTAMPTZ aware; runner mixes naive + aware, e.g. `nbbo_tape.py:96`). | Normalize all sim instants to naive-UTC at the seam (the codebase's dominant convention); the `as_of` readers already do `.replace(tzinfo=None)` (`pipeline.py:238`). Cover in P1 with an explicit tz-normalization helper in the driver. |

---

## 8. Recommended FIRST phase

**Build P0 first** (sim-clock ContextVar + standalone `MockBrokerAdapter`, ~1.5 d). It is the
smallest independently-testable unit, de-risks the two hardest mechanisms (clock injection +
the `VenueAdapter` mock) in isolation, and is provably inert in prod (flag-off byte-identical)
before any FSM wiring. Everything downstream (P1 single-session drive, the UPC proof) stands on it.

---

## 9. Open questions for the operator

1. **Eligibility fidelity:** is the §2.3 *recompute-via-scorer* reconstruction acceptable for the
   UPC proof, or do you want the R1 `momentum_viability_history` append-table added now so future
   incidents have perfect-fidelity replay? (Recommendation: ship the proof on recompute; add the
   history table as a fast-follow so the next TOCTOU is captured exactly.)
2. **Replay DB:** stand up a dedicated `chili_replay` (refreshed from `chili_staging`), or run the
   harness purely on `chili_test` fixtures? (Recommendation: `chili_replay` for the day/batch
   runner; `chili_test` for the pytest acceptance tests.)
3. **Surface:** is a CLI (`scripts/_replay_v3.py`, mirroring `_replay_v2.py`) enough for now, or do
   you want the `/trading/replay?engine=v3` web surface in P5?

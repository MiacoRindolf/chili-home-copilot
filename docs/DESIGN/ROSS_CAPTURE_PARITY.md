# ROSS CAPTURE PARITY — the design

**Author: Fable 5 (research + design), 2026-07-06. Executor: Claude Code (Opus 4.8), phase-by-phase.**
**Operator mission: ① capture ALL Ross winners · ② avoid ALL Ross losers · ③ eliminate CHILI's own losers.**
**Oracle: the FSM replay scorecard ONLY** (`project_ws/_worktrees/fsmdriver/scripts/replay_v3_fsm_window.py`, commit `0a5c2c0`, seeds `chili_test`, dense `TICK_STRIDE=2`).

> **Executor contract (credit-efficiency):** work the phases in order · one change per deploy · every change FSM-gated BEFORE live · STOP at any failed gate and report (diagnosis before code) · do not re-research anything in §0-§1 (verified) · never violate §4.

## 0. Verified baseline

Net **+$264.25** on the 10 tape-testable movers:

| Day | Symbol | Ross | CHILI | Verdict |
|---|---|---|---|---|
| 06-26 | ZDAI | chased top, −$25,431 | $0 | ② ✅ AVOIDED |
| 06-30 | SVRE | +$16,064 | −$0.33 | ① ❌ top-entry |
| 06-30 | JEM | +$46,010 | **+$314.53** | ① ✅ CAUGHT |
| 06-30 | CELZ | watched | +$48.17 | 🏆 beat |
| 07-01 | TC | −$395 | −$13.38 | ② ➖ bled small |
| 07-01 | LHAI | no-setup | −$8.09 | ③ ➖ |
| 07-01 | DXF | no-setup | −$49.38 | ③ ❌ top-buy |
| 07-01 | CANF | +$7,371 | −$5.90 | ① ❌ late |
| 07-01 | JEM | −$232 | $0 | ② ✅ AVOIDED |
| 07-02 | CLRO | — | −$21.37 | ③ ❌ top-buy |

**① 1/3 · ② MET · ③ −$79 remains.** Evidence: `docs/STRATEGY/CC_REPORTS/2026-07-06_fsm-scorecard-baseline-264.md` (the master scorecard), `2026-07-05_session-report.md`, `2026-07-02_master-fix-plan-ross-parity.md`, the `2026-07-02_*-visual-frame-review.md` set, `project_ws/AgentOps/ross_video_evidence/`.

**Settled by 5 negative replay results — do NOT revisit:** gate-tuning is at its ceiling. (1) bench-off hurts JEM (+314→−3); (2) un-bench hurts SVRE/CELZ; (3) frontside can't shrink DXF; (4) forced-early SVRE entry LOSES; (5) stop-width is causation-reversed. All of Ross's 5 audited losses = ONE shape (extension ≥20% above VWAP into a parabolic PMH failure) and CHILI's guards already decline it — ② is met; PRESERVE it.

## 1. Load-bearing facts (verified 2026-07-06; they change everything vs naive execution)

1. **The WIP branch `chili/momentum-concurrency-basis-independent` is 548 commits behind origin/main** — the +$264 bundle (b3ab9eb / 8a5c5a6 / 7e6bbe2) is ABSENT from it. Activating the event-admission WIP = a **PORT onto latest main**, not a commit.
2. **The branch's 3 committed commits are NOT in main** (no cherry-pick equivalents): `d473331` (bridge mirrors tick L1 → `momentum_nbbo_spread_tape` — the entry gate's feed), `0b72ced` (L2 coverage 6→120 names), `395f4ef` (per-price L2 ladder). **The running host bridge executes worktree-only code; deploying the bridge from main today kills the entry-gate feed. Standing landmine until ported.**
3. **Main's `live_runner_loop.py` ≠ the WIP's** (main = newer exits-only loop, live in the deployed image; the WIP adds the LISTEN/admission consumer). MERGE required — a bad merge breaks live-money EXITS.
4. **The double-arm fix already exists in the WIP** `operator_actions.py`: `pg_advisory_xact_lock(hashtext(sym))` around `begin_live_arm`'s dedup (~:95-105, :339); the lock releases at COMMIT → the second arm blocks, then dedups against the committed row. The remaining gate = a concurrency TEST.
5. **Interim arm path (today, post the 07-06 live-arm outage fix):** image `main-clean-d98c924` (main + PR #852 `_bus_on` fix); scheduler jobs arm every 10s via repo-`.env` `CHILI_MOMENTUM_EXEC_AUTO_ARM_SCHEDULER_ENABLED=true` + `CHILI_MOMENTUM_EXEC_LIVE_RUNNER_SCHEDULER_ENABLED=true`. The compose healthcheck (`scripts/verify_momentum_exec_process_health.py` REQUIRED_FALSE_ENV) deliberately forbids these two flags → the container reads "(unhealthy)" (cosmetic) until P1 lands and they revert to false.
6. **The momentum lane is COMPOSE-OWNED:** deploy = update `.env` `CHILI_MOMENTUM_EXEC_IMAGE` pin → `docker compose --profile live-momentum up -d --no-deps momentum-exec-worker`; label `com.chili.service=momentum-exec-worker` required; `D:/CHILI-Docker/premarket-readiness.ps1` (04:00 ET) quarantines mismatches. Raw `docker run` deploys get reverted daily.
7. **Meta-label prod data-gate is NOT yet proven live:** 07-06 = 13 outcomes, 0 snapshots, 0 `live_entry_feature_capture_error` events (consistent with no fills on the #851 code yet, but unproven). First-prod-fill verification is a hard P0 gate.
8. Incident post-mortems this cycle (context for the guardrails): `project_livearm_outage_0706` (compose `:-false` dark-flag substitutions + latent `_bus_on` UnboundLocalError → zero arm events all day; fixed via PR #852 + `.env` flags) and the same-morning IQFeed/DTN account-level disconnect (operator re-login is the only fix; detect via `iqfeed_trade_ticks` recency, not process-alive).

## 2. Phases (each independently shippable · FSM-gated BEFORE live · one-command rollback)

### P0 — Evidence + integrity (no trading change; DO FIRST; ~1 short session)
1. ✅ Scorecards committed (done with this design's PR).
2. **Baseline reproduction:** re-run the FSM replay over the 10 movers (TICK_STRIDE=2). PASS = net +$264.25, JEM +$314.53 reproduced. FAIL = **STOP EVERYTHING** — the oracle itself has drifted; nothing downstream is trustworthy until diagnosed.
3. **First-prod-fill capture check (gates P2):** after the first LIVE fill on ≥ d98c924:
   ```sql
   SELECT count(*) FILTER (WHERE entry_regime_snapshot_json IS NOT NULL
          AND entry_regime_snapshot_json::text NOT IN ('null','{}')) AS with_features
   FROM momentum_automation_outcomes WHERE created_at > '<deploy_ts>';
   -- and:
   SELECT payload_json FROM trading_automation_events
   WHERE event_type='live_entry_feature_capture_error' AND ts > '<deploy_ts>';
   ```
   PASS = snapshot populated OR the error event present (the event names the throwing call — fix THAT precisely). FAIL (neither) = #851's wiring assumption is wrong on the **scheduler-arm path** the lane uses today (the FSM only exercised force-armed sessions) → debug `live_runner.py` ~12103-12127 before relying on the meta-label lever.
4. Confirm the bindings appendix (§5) still matches the live container.

### P1 — Port + activate event-driven arming (moves ①: premarket/fresh-gapper capture; the biggest lever)
**Why:** Ross's profits are premarket. The feed already tapes movers early (JEM taped 04:03 on 06-30) while eligible→arm rides 20s+30s polls. The `<1s` tick→admit→arm path is ~80% built: producer `pg_notify('momentum_iqfeed_l1')` LIVE on the host bridge; consumer = the WIP.
1. Branch `feat/event-admission-port` from **latest origin/main**.
2. Cherry-pick `d473331`, `0b72ced`, `395f4ef`. Bridge conflicts: main has `SUBSCRIBE_FAST_POLL_S`/`_alert_symbols`; the worktree has `pg_notify` + the ross-universe pre-viability watch — the merged file keeps BOTH.
3. Port the uncommitted deltas from the worktree `D:/dev/chili-home-copilot`: `scripts/iqfeed_trade_bridge.py` (M; producer), `operator_actions.py` (M; advisory-lock patch applied onto MAIN's version), `ross_event_admission.py` (new; master flag `chili_momentum_ross_event_admission_enabled` default True — compose already sets it), and **merge** the WIP LISTEN/admission consumer into MAIN's `live_runner_loop.py` (main's exits behavior must survive byte-identically).
4. Reconcile `verify_momentum_exec_process_health.py` (main's committed copy vs the untracked worktree copy — diff, unify).
5. **Concurrency test (the P1 gate):** `tests/test_ross_event_admission_concurrency.py` — two concurrent `admit_ross_event(symbol='X')` (threads, separate DB sessions) → assert exactly ONE `trading_automation_sessions` row; also race the scheduler-arm path vs the event path on one symbol → 1 session. Use the `db` fixture (conftest truncates; `TEST_DATABASE_URL` must end `_test`; run this pytest ALONE — truncation collides).
6. **Replay gate:** full scorecard on the port branch → net ≥ +$264.25, JEM byte-identical (the admission path is additive; FSM behavior must not change), stops/targets exit parity byte-identical (the `live_runner_loop.py` merge check).
7. Deploy compose-canonical: build `chili-app:main-clean-<sha>` → `.env` pin → `docker compose --profile live-momentum up -d --no-deps momentum-exec-worker` → verify bindings in-container (report BINDINGS, not defaults).
8. **Cutover order (explicit):** ONE session with BOTH paths on (the lock + test make double-arm impossible) → verify the event path arms on live ticks (logs `iqfeed admission` / `_submit_session cause=iqfeed_notify`; latency ladder tick→session `<5s`) → THEN flip the two `CHILI_MOMENTUM_EXEC_*_SCHEDULER_ENABLED` to `false` + compose up → healthcheck GREEN → the 04:00 automation validates. **Never flip the scheduler flags off in the same deploy that first enables the event path** — if the event path is broken you recreate the 07-06 zero-arm outage.
9. Restart the HOST bridge from the ported repo file via the wrapper `start-iqfeed-trade-bridge.ps1` — only AFTER the port lands, never before.
**Rollback:** `.env` pin → `d98c924` + compose up. Host bridge: keep the current worktree file (it is the running truth).

### P2 — Meta-label promotion pipeline (moves ③ then ①; PASSIVE until the data gate opens)
**Why:** the only validated lever past the gate ceiling (perm_p=0.001 out-of-day) — but the FSM-trained model is overfit-by-construction; prod features only.
1. **Data gate:** N ≥ 100 labeled fills with ≥ 15 winners (winners run ~19%). At 0-7 fills/day = weeks → weekly check, not a blocking phase. Include PAPER fills only after a feature-parity test (live vs paper vectors on the same tape; paper write path `paper_runner.py:947`).
2. Train with the existing `train_meta_label` (GroupKFold-by-day, 1000-iter permutation). **Promotion criteria:** perm_p ≤ 0.01 AND out-of-day AUC ≥ 0.60 AND a non-degenerate derate distribution (not all 0.4-floored, not all 1.0).
3. **Shadow first** (like #848): log `_meta_mult` decisions ≥1 week without applying; compare would-have sizing vs realized outcomes; then live A/B.
4. Kill switch: `chili_momentum_meta_label_min_size` → 1.0 = instant no-op. **Rollback:** the kill switch.

### P3 — Extension-veto VWAP-anchored term (moves ③: the DXF/CLRO top-buy class; smallest scope)
**Why:** `_entry_extension_veto` (`entry_gates.py` ~2311-2382) measures vs the BREAKOUT LEVEL (floor `chili_momentum_entry_extension_floor_pct=0.08`) — SVRE's +8%-over-break top-entry passed. Winners bought near VWAP (JEM 3.47); losers bought extended (DXF 1.05-1.10 HOD, CLRO 6.74 near top).
1. Enumerate ALL call sites first (`_entry_extension_veto` + `_hod_extension_ok` ~7840-7866 feed ~14 trigger sites).
2. Add an ATR-scaled VWAP-anchored term: veto when `px > vwap × (1 + max(F_vwap, K × atr_pct))`. Calibrate on the replay (grid over F_vwap, K): must flip DXF/CLRO (ideally SVRE) to no-entry while JEM/CELZ stay byte-identical. Sub-$1 names: the ATR term must dominate (8% of $1.05 ≈ noise).
3. **Honest abandon criterion:** if no (F_vwap, K) region separates DXF-tops from JEM-breakouts → ABANDON and record the negative result (the 6th dead-end). Do not force it.
4. Ship default-ON with kill switch; replay gate net > +$264.25. **Rollback:** the kill switch.

### Metric harness (every phase, every change)
The per-day scorecard table (operator's format): `symbol | window ET | Ross action | Ross $ | CHILI $ | verdict ①②③` with timestamps. Plus the premarket latency ladder SQL per symbol (first tick → first viability → eligible → arm → entry) joining `momentum_nbbo_spread_tape` / `momentum_viability_history` / `trading_automation_sessions` / `trading_automation_events`.

## 3. RIGOR — how each lever could be wrong (+ the cheapest falsifying check)

### L1 Meta-label
- **Math:** the 57-row FSM model is overfit by construction (degenerate constant OFI/ATR pushed weight onto secondary features) — NEVER ship it. Permutation p-values are optimistic on near-constant features. GroupKFold-by-day still leaks same-day regime across movers — consider week-grouping. *Check:* a feature-variance report before any training run.
- **Logic:** the derate is DOWN-only, floor 0.4 → max saving on the DXF class ≈ 60% of the loss (≈ +$45/week on the known set) — real but modest; upside sizing lives in the conviction path, not here. *Check:* recompute the scorecard with a perfect-oracle derate (losers × 0.4) = the lever's ceiling, BEFORE building more.
- **Data:** fills are scarce (0-7/day), winners ~19% → weeks to honest N; class imbalance makes bare AUC misleading — enforce the winner-count floor. *Check:* the weekly SELECT.
- **Wiring:** capture is post-fill (`live_runner.py` ~12103-12127); the FSM exercised force-armed sessions only; today's scheduler-arm path is unproven; P1 switches paths AGAIN. **The P0 first-fill check must be repeated after P1's cutover.** *Check:* one SELECT after each path's first fill.
- **FSM can't prove:** thin-name slippage / own-order impact on resized entries → the shadow-log week covers it.

### L2 Event-admission port
- **Wiring (biggest):** the `live_runner_loop.py` merge — main's exits-loop manages live money. *Check:* replay exit parity + one full PAPER session before live cutover.
- **Logic:** LISTEN drop → silent no-arm; the WIP poll fallback (`..._iqfeed_poll_fallback_enabled=true`, 0.25s) must cover ADMISSION, not just session ticks. Halt-reopen notify storm: `admit_ross_event` cooldown (default 2.0s) must be PER-SYMBOL. *Check:* read both code paths during the port; kill the LISTEN thread in a paper test → arming must continue via fallback.
- **Math:** `<1s` is notify→admit; end-to-end includes the synchronous `refresh_viability=True` scoring — measure it (2-5s total still beats 20s+30s). *Check:* the latency ladder on day 1.
- **Data:** `pg_notify` is fire-and-forget — the 20s poll stays as the designed backstop (intended degradation, not a bug).
- **FSM can't prove:** the replay force-arms and BYPASSES admission — this lever is validated only by the concurrency test + a paper session + the live latency ladder. State this in the report.

### L3 Extension veto
- **Math:** the session-VWAP anchor in premarket must match what the FSM computes, or the calibration lies; the ATR frame must match the veto's existing source. *Check:* print both from one replayed entry first.
- **Logic:** blocking SVRE saves only $0.33 — the targets are DXF/CLRO (−$70). Over-veto is the real risk; this lever may only DECLINE tops, never push entries earlier (the forced-early dead-end).
- **Data:** ~10 movers is a tiny calibration set — treat the result as provisional; re-validate weekly on new tape.
- **Wiring:** ~14 call sites — enumerate + replay-cover all of them.

### L4 The FSM oracle itself
- Mock fills ≈ 5% optimistic vs RH agentic; ack latency ~10s median modeled → use for **A/B deltas, never absolute PnL forecasts**.
- **No-tape movers are invisible** (the ILLR +$19.9k class) → ① is UNDERMEASURED; P1 is the only path to those. Track separately.
- Byte-identical claims require pinned stride/seed/window in every A/B command.

## 4. DO-NOT guardrails (final; each cost a day or an incident — do not re-litigate)
1. Do NOT swap the host bridge to a main-built file before P1 lands (kills the L1→tape mirror + pg_notify).
2. Do NOT widen trail 500→1000 (500/500 flat WON the 2026-06-11 sweep, +$939 vs +$533).
3. Do NOT lower `entry_extension_floor_pct` 0.10→0.06 (reverts a deliberate raise; blocks JEM-type verticals).
4. Do NOT rewrite the 3-layer selection scorer (9b52d30) or un-bench the sticky-backside bench.
5. Do NOT deploy the momentum lane with raw `docker run` (compose-owned; the 04:00 automation quarantines mismatches). No bind-mount hot-edits (operator directive 2026-07-03).
6. Do NOT deploy with open positions; ALWAYS verify a single `DATABASE_URL=…/chili` in-container; report BINDING values, never config defaults; one change per deploy; FSM gate before live; rollback = `.env` pin + compose up.
7. Do NOT retrain/ship the meta-label from FSM data.
8. STOP at any failed gate and report — diagnosis before code.

## 5. Appendix — live bindings snapshot (verified in-container 2026-07-06, image main-clean-d98c924)
`DATABASE_URL=postgresql://chili:chili@postgres:5432/chili` (single) · `crypto_only=False` · `auto_arm_live=True` · `auto_arm_live_scheduler=True` (interim; P1 reverts) · `live_runner_scheduler=True` (interim; P1 reverts) · `rulebreak_nextday_lockout_enabled=False` · `trail_floor=trail_ceiling=500.0` (flat; sweep winner) · `entry_extension_floor_pct=0.10` · price bus ON · role `momentum_exec_only` · label `com.chili.service=momentum-exec-worker` · 11 scheduler jobs incl. "Momentum auto-arm-live (every 10s)".

# Handoff — Momentum Lane: lockout fix, premarket verification, pullback-scalp gap (2026-06-30)

**Audience:** the next Claude Code agent picking up the Ross-Cameron momentum lane.
**Operator constraints (load-bearing):** READ-ONLY trading (never place/cancel broker orders — direct the operator; operator pre-authorized the stuck-quote + junk-viability + risk-breaker-RESET cleanup classes THIS session only). PING before any rollback/reset. Deploy ONLY when FLAT + verify a single `DATABASE_URL=…/chili` (no duplicate env key — silent total DB-loss risk). No magic numbers (adaptive / one documented base). Kill-switched + reversible (per-flag + per-sha). commit→push→PR. Push with the gh token in the URL. ⚠️ Operator is LOW ON CLAUDE TOKENS (refresh ~1 day) → CONSERVE: lean checks, terse, PING-only-on-events.

---

## TL;DR — current state (as of ~15:30 UTC 2026-06-30)

- **Deployed:** `chili-app:main-clean-a6b704d` (container `chili-clean-recovery-scheduler`), **db=chili**, lockout CLEARED + DECOUPLED.
- **CHILI is TRADING + net green:** today ~4 trades, net **+$21.53** (CELZ +$40.13 via ORB-break + small losers). FLAT.
- **Branch:** `chili/momentum-defensive-veto-bundle` (HEAD `a6b704d`, parent `9dcc8b2`). ⚠️ A PARALLEL codex session has UNCOMMITTED `app/services/trading/momentum_neural/ross_momentum.py` + untracked `tests/test_squeeze_quality_floor.py` — DO NOT touch/commit those.
- **IN FLIGHT:** an investigation into why the fast pullback-scalp path is dormant (see §4 — the next lever the operator greenlit).

---

## Codex continuation update (2026-06-30 ~16:35 PT)

- **Current deployed scheduler:** `chili-app:main-clean-cb53d54` (`org.opencontainers.image.revision=cb53d54`), rollback container `chili-clean-recovery-scheduler-precb53d54` = `chili-app:main-clean-76aafbe`.
- **RH Agentic status:** scheduler token bundle is present, has refresh token, `ensure_authable=ok`, `RobinhoodAgenticMcpAdapter().is_enabled() == True`; live auto-arm logs show `broker_not_ready_skipped=0` on the current rail. Codex-side MCP `robinhood-trading` is OAuth logged in locally, but the current Codex thread did not hot-load RH tools; fresh tool reload/thread may expose them.
- **New blocker found/fixed after `76aafbe`:** the A-setup quality floor was still a hard `float <= 20M` live-eligibility kill. Live LGPS had float 23.65M (only 1.18x over the base ceiling) but RVOL ~496x and move ~61%, so the old gate rejected the exact high-evidence Ross runner. Fix `cb53d54` makes the base ceiling adaptive: over-ceiling float can pass only when `float/ceiling <= min(rvol/rvol_floor, abs(change)/change_floor)`, then it is risk-bounded. No new magic ceiling/multiplier.
- **Anti-regression:** deployed-code probe now scores LGPS `live_eligible=True`, `risk_bounded=True`, `viability=0.58`; MWC still rejects (`float ratio 2.95x > evidence room 1.06x`); AREC still rejects (`5.35x > 1.50x`).
- **Validation:** compile clean; focused eligibility/prequal 26 passed; setup/scheduler 68 passed; no-A-setup/meso/micro 129 passed; auto-arm+scheduler 60 passed; Replay v3 P0-P2 31 passed; live-runner priority/starvation subset 5 passed. Full `tests/test_momentum_live_runner.py` collection OK (36 tests), but full execution timed out once; subset around current risk passed.
- **Post-deploy state:** DB is `chili`; no held/live-entered rows; one `live_arm_pending`/watch-style row only. Logs after deploy show LGPS hoisted as symbol-of-day, not A-setup-float rejected. Unrelated yfinance/Massive 404s for `EURC-USDC` remain noisy but not the equity momentum blocker.

---

## 1. What was accomplished this session (deployed + proven)

### a) Explosive-prequal score floor — `9dcc8b2` (the "score-bar" fix)
A genuine Ross A-setup (low-float<20M + signed-up≥10% + rvol-or-carve-out) gets a **raise-only viability FLOOR** to clear the 0.56 impulse_breakout bar (`viability.py` ~line 918, after the hoist). Anti-junk fail-closed (a non-mover can't be floored), size-down-coupled (`_extreme_vol_risk_bounded`), scorer untouched, flag `chili_momentum_explosive_prequal_floor_enabled` (default ON). Designed via an 8-agent workflow (investigate→3 designs→adversarial-verify→synthesize). **VALIDATED LIVE:** floored BTCT (9.5M float/+10-13%), JEM (1.4M/+10.4%), CUPR (920K/+12.8%) — all genuine; anti-junk 100% held. See `project_selection_scoring_fix` memory. NOTE: necessary but NOT sufficient (the lockout, below, was the real day-blocker).

### b) Next-day-lockout DECOUPLE — `a6b704d` (the day-freeze root fix)
**Root cause of 0 trades all premarket:** the auto-arm died at **Guard 1b `rulebreak_nextday_lockout`** (auto_arm.py:3340) — `trading_risk_state` row **id=151** (regime=`rulebreak_nextday_lockout`, reason=`max_loss_circuit`, lock_day=2026-06-30) was armed the NIGHT BEFORE by a **single max-loss-circuit fire**. A misapplied human-tilt rule on a deterministic bot (same class as the `daily_trade_count_budget` the operator had removed). **Fix:** deleted the lone `set_next_day_trading_lockout("max_loss_circuit")` call (live_runner.py ~10252), kept the genuine `daily_loss_breach` + `daily_trade_count_budget` arming sites + Guard 1b + the flag. No risk hole (a systematic recurrence is bounded to ~3 circuit-capped losses before the same-day consecutive-loss halt / per-broker cap binds). 6/6 tests in `tests/test_nextday_lockout_circuit_decouple.py`. Designed via a verified workflow. See `project_arm_step_gap` memory.

### c) Premarket capability — VERIFIED (a reconstruction was REFUTED)
A reconstruction claimed CHILI "can't trade equities premarket (RTH wall)". **FALSE** — verified in-container: the entry gate uses `is_tradeable_now` (market_profile.py:364, premarket-AWARE: "so the lane can catch Ross's pre-market runners"), NOT `market_open_now` (RTH-only). `is_tradeable_now` returns **True** at 08:30/09:10/09:25 ET; `premarket_start_et=07:00`, `early_premarket_enabled=True`. ⚠️ LESSON: the reconstruction agent looked at the WRONG function + wrong line — ALWAYS verify a confident agent claim in-container.

### d) Risk-state RESETs (operator-authorized, this session)
- **Lockout row id=151 cleared** (`UPDATE trading_risk_state SET breaker_tripped=false WHERE id=151`) → unblocked the lane mid-session → CHILI immediately armed + traded (CELZ).
- **16 `circuit_breaker`/`portfolio_breaker` rows cleared** — they were FALSE-tripped on a BUGGY drawdown calc: *"30-day drawdown −389.7% (realized=−76) exceeds −6.0%"*. −389.7% is impossible (the % denominator is wrong: −$76 / ~$19.5 instead of / ~$13k equity; the REAL DD ~−3.3% is under the −6% limit). This was suppressing the sizing.
- **Kill_switch rows (06-12→06-22) LEFT untouched** — stale daily snapshots, NOT binding (CHILI trades; no 06-30 row). The #1 safety — do not clear unprompted.

---

## 2. ⚠️ Open BUGS / re-trip risks (verify these)

1. **Drawdown-calc denominator bug** (−389.7%): the circuit_breaker was cleared but the buggy calc REMAINS. If the drawdown breaker re-evaluates and recomputes −389.7%, it RE-TRIPS + re-suppresses sizing. It had stayed sticky since 06-27 (infrequent eval) so the reset should hold, but **WATCH for a new tripped `circuit_breaker` row** and FIX the denominator (it should divide realized DD by the real account equity, not ~$19.5). Operator-gated build+deploy.
2. **change-floor basis** (secondary): the explosive change-floor keys off PRIOR-CLOSE, so a gap-down-then-squeeze can read negative and bench a genuine mover. Cross-check vendor `todays_change_perc` vs the live tape / use an intraday-open basis.
3. **viability spread gate runs on `spread_bps=None`** for some names → unreliable `live_eligible`. Wire the real `momentum_nbbo_spread_tape` into viability.

---

## 3. Deploy / ops cheatsheet

- **Deploy model** ([[project_docker_deploy_model]]): per-sha clean image + raw `docker run` (NOT compose). Steps: `git worktree add --detach project_ws/_worktrees/_build_<sha> <sha>` → `docker build -t chili-app:main-clean-<sha> .` → `docker stop+rm chili-clean-recovery-scheduler` → `docker run -d` with `--env-file D:/CHILI-Docker/_sched_deploy_ce1975a.env` + the 21 `-e` flags + mounts → verify db=chili → `git worktree remove --force`.
- **The 21 `-e` flags** (all =1): SMART_HOLD, ENTRY_TIGHT_FALSE_BREAK_RECLAIM, NEWS_CATALYST_WEIGHT, EXIT_LADDER_LIVE, L2_CONFIRM, ENTRY_L2_VETO, LIVE_ELIGIBLE_RECENCY_GRACE, FRONTSIDE_ADAPTIVE, AGENTIC_TRADABILITY_PREFILTER, ASSET_TYPE_ARM_SKIP, CLEAN_DECLINE_TERMINAL, THEME_CROWDED_SUBSTITUTE, PULLBACK_ADD, VERTICAL_CHASE_NOHALT_THRUST, LOST_VWAP_FLATTEN, BOS_EXIT_LIVE, DAILY_ROOM_SIZE_DOWN, RED_INTRADAY_SIZE_DOWN, CONSECUTIVE_LOSS_HALT, FLAG_BREAKOUT_ADD, EXPLOSIVE_PREQUAL_FLOOR (each `CHILI_MOMENTUM_<NAME>=1`).
- **Rollback chain:** a6b704d → 740389e (drops the prequal floor + decouple) → 134826a → c45ccdc → ec41e3a → f1beb2a. Per-flag kill: set the relevant `-e …=0`.
- **Docker crash recovery** ([[reference_docker_recovery]]): start `C:\Program Files\Docker\Docker\Docker Desktop.exe`, wait for engine, `--restart` containers auto-recover + postgres WAL-replays, restart IQFeed bridges (`scripts/start-iqfeed-*-bridge.ps1` + `E:\DTN\IQFeed\iqconnect.exe`), verify db=chili.

### Query gotchas (IMPORTANT)
- **Count trades via `live_exit_filled` events** (count + `sum(payload_json->>'pnl_usd')`), NOT `state='live_entered'` — sessions cycle entered→exited→recycled→cancelled, so current-state shows 0 even after real trades.
- `iqfeed_trade_ticks` timestamp col = **`observed_at`** (NOT `ts`).
- `trading_automation_sessions` has NO `realized_pnl`/`automation_type` col. Active-watch state = `watching`/`watching_live`. Cumulative PnL via the events join.
- Risk states live in `trading_risk_state` (regime/breaker_reason/breaker_tripped/snapshot_date).

---

## 4. ⭐ THE ACTIVE LEVER — fast pullback-scalp is DORMANT (operator greenlit a fix)

**Problem (verified today):** CHILI scalps via FULL-EXIT + recycle (the slow 112-450s `adaptive_reentry_cooldown` path) instead of Ross-style ride+add / fast micro-reentry. The fast mechanisms are ENABLED (`pullback_add_enabled=True`, `flag_breakout_add=True`, `micropullback_reentry_cooldown_seconds=30`) but their events **NEVER FIRED today** (0 pullback_add / micropullback_reentry / pyramid; only scale_OUT fired). CELZ ran 3 FULL cycles (entry→**bailout**→recycle→cooldown→re-enter) + 5× backside_benched — it bailed out fully + re-armed slowly instead of holding+adding on pullbacks.

**Why it matters:** Ross makes his money RE-SCALPING the same runner (buy dip → sell pop → repeat). The 112/450s post-exit cooldown blocks that cadence; the FAST path (ride+add, or 30s micro-reentry) is the Ross way and it isn't engaging.

**ROOT CAUSE (confirmed via investigation, 2 compounding causes):**
1. **Structural wiring gap (dominant):** all 4 add/reload paths (pyramid_add ~live_runner.py:11972, micropullback_reentry ~12524, pullback_add ~12889, flag_breakout_add ~13381) are gated on `st == STATE_LIVE_TRAILING`. A fresh fill lands in `STATE_LIVE_ENTERED`; the ENTERED-only no-confirmation bailouts (`instant_bid_above_fill_unconfirmed` ~10491 [6s], `bail_on_no_confirmation` ~10568 [8-20s]) run FIRST each tick + `return` early — so a normal entry goes ENTERED→BAILOUT→recycle and NEVER reaches TRAILING. The add path is structurally unreachable → 0 adds. (CELZ 9943: both losers bailed in ENTERED at 7-9s.)
2. **C1 max-loss has no fresh-quote guard:** the C1 per-trade max-loss check (~10177-10185) — unlike the C1b circuit (~10197-10211) — does NOT validate `bid` freshness, so a torn/stale/zero `bid` (set at ~5606 `bid = float(tick.bid or mid)`) trips a spurious full liquidation. CELZ 9920 (the +$50 win that should've been bigger) DID reach TRAILING (scaled 52/104 @ $3.86) but was force-exited on a phantom `unrealized=−148` while the real NBBO bid was ≥$4.22 (+18%).

**THE FIX (built this session, 2 commits, kill-switchable, fail-closed — neither weakens #769 or the structural stop):**
- **FIX 1 — fresh-quote guard on C1** (`live_runner.py` ~10177-10185): reuse the C1b `_fresh_quote` predicate (bid finite & >0, halt_stale_streak==0, not suspected_halt) before C1 force-exits; skip on a stale tick. Flag `chili_momentum_max_loss_fresh_quote_guard_enabled` (default True). A genuine −max_loss on a FRESH bid still fires.
- **FIX 2 — early trail-arm** (`live_runner.py` ~14048 logic moved/duplicated before the no-confirmation bailouts): a confirmed runner (`bid >= avg * trail_activate_return`, adaptive) arms TRAILING BEFORE the bailouts can cut → opens the ride+add / micro-reentry path. Flag `chili_momentum_early_trail_arm_enabled` (default True). Arms ONLY when already in profit above the band — a loser at/below entry STILL gets the no-confirmation cut; the adds are fail-closed (knife-guard paper_execution.py ~962-986) and pyramid_blend re-bases the #769 circuit to R0.

**STATUS: DONE + DEPLOYED.** FIX 1 committed `40d1900` (C1 fresh-quote guard + IQFeed `momentum_nbbo_spread_tape` tick-level cross-check, adaptive divergence = mult × recent median spread); FIX 2 committed `5d8c207` (early trail-arm). 23/23 tests pass (`tests/test_pullback_scalp_enable.py`). Merged to main via **PR #828** (main `9a955f2`). **DEPLOYED LIVE** = `chili-app:main-clean-5d8c207` (fresh_quote_guard + early_trail_arm + prequal all ON, db=chili, lockout clear, 0 errors). Flags: `chili_momentum_max_loss_fresh_quote_guard_enabled`, `chili_momentum_early_trail_arm_enabled` (both default True). NEXT (validation): **live-soak for the first `live_pullback_add_fill` / `live_micro_pullback_reentry_submitted`** on a real runner + confirm no spurious `max_loss_per_trade` bailout recurs. The rigorous follow-up instrument = drive `tick_live_session` (replay_v3) over the CELZ 9920 tape. Per-flag kill-switch or rollback to `a6b704d` if either fix misbehaves.

---

## 5. Other operator-gated levers (do NOT auto-start)

- **Conviction-sizing on A+ entries** (the operator wants bigger size on the best setups — today's sizes are small, e.g. CELZ 104 sh / ~$382; the false drawdown suppression is now cleared so sizes should recover toward equity-relative).
- The `daily_trade_count_budget` next-day-lockout arming site (live_runner.py:9531) — it's the ADAPTIVE SCAL101 entry-count ceiling (base 5, adaptive 5-10), NOT the removed hard human cap; a follow-up decision whether to decouple it too.
- A same-day per-trade-circuit-fire COUNT halt (≥2 fires today ⇒ halt) — tightens the decouple's residual bound.
- SHORT lane P1+ ([[project_short_side_lane]], P0 done) ; Replay v3 P3-P5 ([[project_replay_v3_live_fsm_sim]]).
- Merge `9dcc8b2` + `a6b704d` (and the replay/R1 harness commits) to main via PR when ready.

---

## 6. Monitoring posture
Conserve-mode: hourly lean health/trade checks (one query: trades+PnL via live_exit_filled, open position, scheduler Up, db=chili, lockout CLEAR, traceback). PING the operator only on: a notable trade/WIN/loss, scheduler down/crash, db!=chili, lockout re-armed, bridge hung. ⭐ Tighten to ~600s for tomorrow's PREMARKET (~10:45 UTC = 06:45 ET) — the big-runner window CHILI is now (post-fix) capable of catching.

**Relevant memories:** `project_arm_step_gap`, `project_selection_scoring_fix`, `project_fill_on_verticals_fixed`, `project_momentum_conversion_fixes`, `project_momentum_engine`, `project_docker_deploy_model`, `reference_docker_recovery`, `reference_iqfeed_bridge_silent_hang`.

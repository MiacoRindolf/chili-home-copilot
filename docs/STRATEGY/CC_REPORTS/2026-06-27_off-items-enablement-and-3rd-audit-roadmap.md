# 2026-06-27 — Autonomous roadmap: enable the 5 OFF items (properly + unit-tested) → 3rd-pass re-audit

**Operator directive (sleeping, full autonomy):** "iimplement mo ng maayos and i-ON mo after matest mo ng maayos sa unit tests" for the 5 deliberately-OFF items; THEN "i-reaudit mo ulit yung courses against sa bagong chili and busisiin mo ulit ang gaps." Use best principal-level engineering rigor.

## Standing constraints (do NOT violate)
- READ-ONLY trading (never place/cancel broker orders — direct operator). Equities-only, agentic CASH 674153143.
- Deploy ONLY when broker FLAT + single `DATABASE_URL=...chili` (verify no duplicate). Per-git-sha image `chili-app:main-clean-<sha>` built from the etfrank working tree; raw `docker run` recreate of `chili-clean-recovery-scheduler`; env-file `D:/CHILI-Docker/_sched_env_<sha>.env`.
- Do NOT pause the lane. Each change kill-switched (default OFF=byte-identical), parity-proven, instant per-sha rollback.
- **Prove-before-enable:** an item flips ON ONLY after its unit tests pass GREEN (run `conda run -n chili-env pytest <one-file> -v` with `TEST_DATABASE_URL=...chili_test`, ONE file at a time — truncate collisions). No pytest vs live `chili`.
- One logical change at a time. commit→push→PR #822. Push with gh token in URL. `git show --stat HEAD` after every commit (git-add-abort bug). Verify `docker images <sha>` EXISTS before stop/rm.
- Honest reporting — no over-claims; if a test fails, say so + fix.

## Part A — enable the 5 OFF items (each: harden → unit-test → run → if green, ON + deploy)
1. **R1 order-path (riskiest): anticipation-starter + order-chunking** — harden dedupe/orphan-safety (no double-count, no naked leg, fail-closed-to-single, reconciler adopts every leg) + adversarial unit tests `tests/test_momentum_order_path_dedupe.py`. Flags: `chili_momentum_anticipation_starter_enabled`, `chili_momentum_order_chunking_enabled` (+ `_order_chunking_blocks`). _Workflow wlrhjucec._
2. **R2 add-into-halt** — ADD the 4 chase-guards (tape REQUIRED+fail-closed, extension veto, not-backside, structural stop) + no-chase unit tests. Flag: `chili_momentum_add_into_halt_*`.
3. **R3 green-day-graduation** — bounds unit tests (cap 2x, reverts to 1.0 on a red day, never a veto, OFF=1.0). Flag: `chili_momentum_green_day_graduation_enabled`.
4. **R4 cup-and-handle** — chase-guards + no-chase unit tests. ⚠️ a chip-spawned PARALLEL session is also hardening this (own worktree) — check for its delivery first; verify+enable if good, else harden myself. Flag: `chili_momentum_cup_and_handle_entry_enabled`.

Acceptance per item: green unit tests + 4/4 adversarial verify + flag-OFF byte-identical → enable in the env + deploy + verify in-container.

## Part B — 3rd-pass re-audit against the NEW chili
After Part A, re-audit ALL 7 Warrior courses (AS101/HVM101/PSY101/RH101/SCAL101/TOS101/SS101) against the now-much-more-complete deployed code (all 7 gap-fill clusters + the 5 enabled items). Read transcript CONTENT + DIFFERENT key-frames (not filenames/function-names — the 3x over-claim root cause). Diff vs the deployed code. Find any STILL-missed gaps; scrutinize whether the now-enabled items behave as Ross teaches. Honest calibrated verdict. Fill mechanizable gaps in verified kill-switched clusters.

## Progress log (update as I go)
- [x] R1 order-path (7edb559) — hardened stranded-leg bug; 33 tests green; anticipation+chunking(blocks=2) ON
- [x] R2 add-into-halt (19c7a32) — 4 guards + fail-closed-under-master fix; 24 tests green; ON
- [x] R3 green-day-graduation (dce8bc0) — never-entered-exclusion bugfix; 33 bounds tests green; ON
- [x] R4 cup-and-handle (90c9cd6+d82973b) — cherry-picked parallel hardening (10c7018) + 22 tests green; ON
- [~] Part B — 3rd-pass re-audit (completeness tail + coherence) — workflow wc7v2b29f RUNNING

**Part A DONE: all 5 OFF items implemented-properly + unit-tested (111 tests) + enabled.** Live img `chili-app:main-clean-d82973b` (PR #822). Started this run at dfce6a8.
Per-round shas: 7edb559→19c7a32→dce8bc0→90c9cd6→d82973b.

## Part B — 3rd-pass re-audit findings (wc7v2b29f, 23 agents)
**Honest verdict: ~85% Ross-complete + largely coherent.** The audit itself contained ≥1 false alarm caught by reading deployed code.
- **VERIFIED FALSE ALARM (no fix):** cushion(2x)×green-day(2x)=4x "exceeds the 3x ceiling" — REFUTED: live_runner.py:6664-6667 `_eff_max_loss = min(base*ALL_16_mults, base*3.0)` — both are inside the hard min() clamp, so the product is hard-capped at 3x. Risk ceiling holds; enabling green-day did NOT breach it.
- **VERIFIED FALSE ALARM:** "pullback_break_confirmation skips a guard" — refuted by the synthesizer reading code (primary triggers carry backside/front_side/red-vol/explosive + extension/flow/L2/tape).
- **2 HIGH-leverage GAPS to BUILD (the operator's own flagged levers):**
  1. **Catalyst-driven conviction SIZE multiplier** — the "#1 profitability lever". Data feed + viability tilt exist, but no mechanical `conviction_size_factor` on the SIZING path. Build a bounded catalyst-grade size mult, composed UNDER the 3x ceiling (auto-clamped), kill-switched + tested.
  2. **Regime-aware PRE-arm move-exhaustion abandon** — the 2026-06-24 PLSM 19→10 chase. A preventative "move-is-done, sit-flat" gate keyed to tape-coldness / leader-crashing / viability-regression BEFORE arming (only a post-exit reactive cooldown exists). Risk-reducing veto, kill-switched + tested.
- **MEDIUM punch-list (pre-existing, note only):** vwap_reclaim flat magic-number defaults (min_below_bars=2 — not vol-scaled; aligns w/ the no-magic preference); vwap/wick_reclaim lighter-guarded than primary triggers; daily-loss recovery auto-clear races the arm kill-switch (1-2 tick lag); hard-stop-vs-trail priority on micro-pullbacks.
- **LOW tail (note only):** vol-band algo-shutdown detector, tape volume-price divergence/absorption, jackknife-candle disqualify, stair-step trend state machine, MM-bid-ladder structure signals. PSY101/RH101 residual = human-discipline, correctly NOT mechanized.

### Part B build plan (each: build → unit-test → run → if green, ON + deploy)
- [x] B1 pre-arm move-exhaustion abandon (bbcd224) — 20 tests green; ON
- [x] B2 catalyst-conviction size multiplier (a0b1102) — 28 tests green (caught+fixed a falsy-zero bug); ON

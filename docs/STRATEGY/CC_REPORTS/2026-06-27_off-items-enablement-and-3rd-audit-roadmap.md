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
- [ ] R1 order-path — workflow wlrhjucec running → run tests → enable
- [ ] R2 add-into-halt
- [ ] R3 green-day-graduation
- [ ] R4 cup-and-handle (coordinate w/ parallel session)
- [ ] Part B — 3rd-pass re-audit + fill

Live img at start of this run: `chili-app:main-clean-dfce6a8` (PR #822, branch chili/momentum-defensive-veto-bundle).

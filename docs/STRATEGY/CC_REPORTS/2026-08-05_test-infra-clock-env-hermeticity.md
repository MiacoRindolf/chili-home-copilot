# 2026-08-05 — Nine failing tests on main: zero product bugs, and the two silent measurement faults behind them

**PRs:** [#989](https://github.com/MiacoRindolf/chili-home-copilot/pull/989) (L12, merged → `c1715ed`) · [#990](https://github.com/MiacoRindolf/chili-home-copilot/pull/990) (test-infra, open)

**Operator direction (verbatim sequence):** "sige, i-merge mo na" → "sige, imbestigahan mo yung 8 pre-existing failures" → "sige, gawin mo lahat, simulan mo sa .env isolation".

---

## 1. What was asked, and what actually turned out to be true

The session began by merging L12 (origin-based price band). Three things surfaced during that merge that changed the picture, and each is worth recording because each was a *measurement* fault rather than a code fault.

### 1.1 L12 was incomplete, and not at the site that mattered

The band is enforced at **three** sites, not one:

| # | Site | Role | v1 | v2 |
|---|---|---|---|---|
| 1 | `ross_smallcap_profile_evidence` | per-symbol evidence gate | ✅ | ✅ |
| 2 | `build_equity_universe` | "decides WHICH names make the pool" | ❌ | ✅ |
| 3 | `symbols_within_profile_price_band` | live-arm class gate (`auto_arm._enforce_ross_price_band`) | ❌ | ✅ |

Site #1 is only consulted *after* a name is already in the pool. The v1 that was about to be merged therefore had **no effect on the motivating case**. Proven by two regression guards that fail on `8ab9d97` and pass on the fix.

**Rule recorded:** when changing a band/gate/threshold, `grep` every enforcement site before claiming it is fixed.

### 1.2 The AMIX premise was wrong

The original claim — "AMIX was ejected from the universe at \$20 and never re-scored" — was derived from `momentum_symbol_viability`, which is an **upsert** table. The append-only `momentum_viability_history` shows the opposite: **1,800 rows on 08-04, 12:25→23:56, `live_eligible=t` continuously from 12:25:45 to 20:29:45**, including the \$20 crossing (19:30 bucket: +502%, still eligible).

The \$20 ceiling's real bite is at the **arm** gate, not at scoring — `_enforce_ross_price_band`'s own docstring notes the broad-brain scoring path is *not* price-screened. So site #3 is the one that matters, and only v2 fixes it.

**Rule recorded (third occurrence):** an upsert table is not history. Look for the `*_history` table before asserting "stopped" or "disappeared".

### 1.3 The lane has never produced a fill

`alpaca_paper_fill_activities` = **0 rows**. `alpaca_paper_cycle_settlements` = **0 rows**. `trading_trades` last entry = **2026-06-24**.

AMIX was never going to trade on 08-04 regardless of the price band. The ceiling is a real defect; it was not the binding constraint.

---

## 2. The nine failing tests

Nine (not eight — `candle_quality` was a `conda run` NO-RESULT artifact, confirmed separately). Nine independent analysts plus direct empirical testing: **9/9 DEFECTIVE_TEST, high confidence, zero REAL_PRODUCT_BUG.**

A controlled A/B over the union of every failure across three sweeps — pre-L12 vs merged main, serial — was **identical in both arms**. L12 caused none of them.

### Class A — wall-clock dependent (5)

`_premarket_tickbreak_confirmed(..., now=now)` requires an ATR thrust buffer in premarket (the CUPR false-pop guard) and not in RTH. Tests that poke a level by \$0.01 without passing `now` read the **real clock**:

```
now=PREMARKET (08:00 ET) -> ok=False  premarket_tickbreak_unconfirmed
now=RTH       (11:00 ET) -> ok=True   pullback_break_tick_ok
```

**Production is correct.** An AST audit of all 14 call sites: 6 pass `live_price`, and **all 6 also pass `now`** — including `replay_v2` and `counterfactual_replay`. **No replay-fidelity bug; golden-window numbers are unaffected.**

This also explains the "+4 new failures" on PR #989 vs main: the PR's CI ran at 05:14 ET (premarket), main's at 00:35 ET (closed). Five of the six "extra" failures were exactly this set — no code change involved.

### Class B — local `.env` leak (1)

See §3.1.

### Class C — superseded contracts / defective fixtures (3)

- **wide spread** — the hard 12/25bps disqualify was deliberately removed 2026-06-25 (`ef96a15`); it was silently blocking every squeeze (1,495 blocks in one day; ILLR 38-91bps, FCUV 70-87bps). The real ceiling is `chili_momentum_live_eligible_max_spread_bps`, default **300bps**. Not a safety regression.
- **ross tilt** — superseded by `no_signal_derank_enabled` (default `True`), which intentionally ranks no-signal names *below* scored movers.
- **pyramid guard #4** — the non-Alpaca account-identity fence fires ~1,900 lines before the guard under test and masks it.

---

## 3. The four fixes (PR #990)

### 3.1 `.env` isolation under pytest — `app/config.py`

`Settings()` declares `env_file=".env"` with **no pytest exemption**; `Settings(_env_file=None)` is reached only via the captured-PAPER isolation marker. The local suite therefore measures the operator's desktop config while CI — which has no `.env`, since it is gitignored — measures code defaults.

**41 policy flags in `.env` contradict their code defaults**, including `CHILI_MOMENTUM_AUTO_ARM_CRYPTO_ONLY=false` (default `True`), `AUTO_ARM_LIVE_ENABLED=false`, `CHASE_CAP_LEADER_BYPASS_ENABLED=false`.

Measured over 16 files / 332 tests, serial:

| | failed | passed |
|---|---|---|
| before (reads `.env`) | 23 | 309 |
| after (isolated) | **6** | **326** |

**17 fixed, 0 broken.** All 17 were in `test_momentum_auto_arm.py` — the entire auto-arm suite was broken locally and green in CI.

Real env vars are unaffected (`os.environ` is still read, so conftest's `TEST_DATABASE_URL`/`DATABASE_URL` still bind). Production is untouched — gated on `CHILI_PYTEST`. **CI behaviour is unchanged**, since CI never had a `.env`.

> This invalidates the standing practice of treating "local changed-area green" as the trustworthy merge signal while CI is red. Local and CI were not measuring the same configuration.

### 3.2 Viability shim defaults — `viability.py`

`from_runtime` uses `getattr(source, name, default)`, so its own `defaults` dict governs any attribute absent from the source. Four disagreed with `app/config.py`, **all permissive**:

```
live_eligible_max_spread_bps   0.0    -> 300.0   (0.0 = NO toxic ceiling at all)
risk_max_spread_bps_abs_cap    1500.0 -> 300.0
a_setup_quality_floor_enabled  False  -> True
no_signal_derank_enabled       False  -> True
```

Dormant — every real caller passes full `settings` — but fail-OPEN on a live-money gate. New `test_viability_shim_defaults.py` compares the **whole** shim against `Settings`.

Rotated the four frozen-oracle roots in `test_captured_viability_adapter.py`. **Values rotation, not economic**: viability 0.5665 and every eligibility flag unchanged, and those assertions precede the hashes so they fail first if economics ever move.

### 3.3 Four superseded contracts

No assertion was simply deleted:

- **wide spread** — split into derate-but-eligible **plus a new test** proving the 300bps toxic ceiling still disqualifies. Without the second, removing the first would have left the gate unguarded.
- **ross tilt** — **both** contracts now pinned (derank ON and OFF), so the flag's real effect is watched rather than implied. Measured: OFF → NOSCORE 0.58 > COLD 0.49; ON → 0.48 < 0.49.
- **liquidity-bias** — the old assertion became impossible when the 3-layer scorer (`9b52d30`, PLSM #15 compression fix) normalised the final score within the batch: with two names the lower is always exactly 0.0. Now measures the **gap**, which is what it means.
- **pyramid** — exercises the **real** account-identity fence (frozen identity in 3 sessions + `get_account_identity_truth` on the adapter) rather than monkeypatching it, so the fence stays covered. 39/39.

### 3.4 Clock hermeticity

`now` anchored across 5 files. `test_deep_reclaim_entry`'s own fixture was **premarket** (12:00Z) while asserting a \$0.02 poke fires — self-contradictory; moved to 09:35 ET, still inside the deep-reclaim 10:30 ET morning cutoff (`entry_gates.py:1518`).

New **`tests/test_entry_gate_clock_hermeticity.py`** — an AST guard rejecting any test that passes `live_price` without `now`. It **immediately caught four more call sites** missed by hand (`e1_backside` ×3, plus a second site in `candle_quality`). Includes a self-test proving the detector actually catches a violation.

The CUPR guard itself already has 8 hermetic tests in `test_premarket_tickbreak_confirm.py` — no coverage was lost.

---

## 4. Verification, and its limits

Each group green locally: 332-test A/B subset, 55 viability, 39 pyramid, 113 entry-gate.

**The full local suite was not run.** It reached 7% after one hour (~14h projected) and was cancelled. CI is the correct oracle here — 45 minutes, and it has never had a `.env`. Baseline on main: **322 failed / 14,826 passed**. The bar is no new failures.

### Process faults committed and recorded

- **`TaskStop` does not kill the pytest child.** A "cancelled" run kept truncating `chili_test` under the next sweep, producing three mutually inconsistent failure sets (6 / 4 / 9). Every sweep comparison made before this was discovered is void. Check `pg_stat_activity` and `Get-CimInstance Win32_Process` before any sweep; kill by PID.
- **`conda run` is unreliable with `::` arguments** (`NotImplementedError` in `wrap_subprocess_call`), producing a silent NO-RESULT that reads as a failure. Use the env's `python.exe -m pytest` directly for single tests.

---

## 5. State and what is actually next

- PR #990 open, CI running.
- `docs/STRATEGY/NEXT_TASK.md` still reads `STATUS: PENDING` for `ross-capture-P1a-entry-snapshot-durability` (07-06). **Left untouched** — this session was operator-directed conversationally and did not do that task; marking it DONE would be false.
- Nine pre-existing failures explained; the remaining CI redness is dominated by the captured-paper/Alpaca lane (~114 of 319 failures across `test_build_captured_paper_activation_authority`, `test_adaptive_risk_reservation`, `test_alpaca_governed_place_bbo`, and siblings) — Codex's lane.

**None of this moves us closer to a first fill.** It makes the suite trustworthy, which it was not: local "green" measured the operator's desktop, and several tests measured the hour they ran. The binding constraint remains the execution lane with zero fills.

---

# Addendum — the rest of 2026-08-05: the zero-fill gap, located

**Further PRs:** [#991](https://github.com/MiacoRindolf/chili-home-copilot/pull/991) (clear pytest-isolation error, → `487be94`) · [#992](https://github.com/MiacoRindolf/chili-home-copilot/pull/992) (chain probe + admission census, → `fbfa7f2`)

**Operator direction:** "sige, ayusin mo na yung execution lane" → "tanggalin mo yung hangganan, ayusin mo na" (the four Codex-owned files were opened to me).

## 6. Where the chain actually stops

Not liveness. Not strategy. **Eligible → arm.**

`one-shot-r102` ran **51 minutes of RTH** (16:22–17:13 UTC): 3,027 capture events, **550 selection events, 18 symbols**. It reached selection and stopped — arm 0, entry 0, order 0, fill 0.

In the same window `momentum_viability_history` shows `paper_eligible=t` AND `live_eligible=t` for YXT (460 scores), BJDX, JLHL, ASTC, INLF, SHPH, AMWL, OESX — several with **no `blocked_reason` at all**. And `trading_automation_sessions` for the day: **0**; last row **2026-07-13**.

Eligible candidates for 90 minutes; nothing armed.

### Two premises of mine that were wrong

- **"The service wasn't alive."** I inspected one run-subdir, saw 8 bootstrap events, and nearly wrote that off. The probe scans every subdir: 3,027 events. Manual inspection of a sharded store is not sampling — it is guessing.
- **"`admit_ross_event` arms the captured-paper lane."** It does not. `_admit_iqfeed_symbol` (`live_runner_loop.py:2176`) branches on `_captured_paper_scope` to the sealed `CapturedPaperInitialAdmissionController.admit` and returns before `:2269`; that call is the ordinary-lane tail. Five independent tracers all returned `blocks_arming = no` on my five hypothesised gates — the blocker was not among them.

### What the tracers did establish

`start_captured_paper_live_runner_loop` **did** start. `captured_paper_runtime_env.py:461-477` force-installs `LIVE_RUNNER_ENABLED` / `LIVE_RUNNER_LOOP_ENABLED` / `AUTOPILOT_PRICE_BUS_ENABLED` = `true` into `os.environ` before the first `app` import, and sets `CHILI_CAPTURED_PAPER_CONFIG_ISOLATED=true` so pydantic never reads the `.env`. The desktop `.env` says `CHILI_MOMENTUM_LIVE_RUNNER_ENABLED=false` — overridden. **I nearly "fixed" a gate that was already open.**

`CHILI_SCHEDULER_ROLE=rnd_only` is likewise deliberate (`docs/DESIGN/SCHEDULER_SPLIT.md`) and not the cause.

## 7. The real blocker: the admission path cannot be observed

Four silent-loss paths in `_admit_iqfeed_symbol`:

1. rejection with **no reason string** → never logged (the guard is `... and rejection_reason`)
2. rate-limited log with **no counts** → 3,000 rejections look like 1
3. non-`Mapping` result → silent `return None`
4. exception → `_log.debug`, invisible wherever root WARNING is the default

The fourth is the same shape as the bug that silently skipped an entire scale-out step earlier in the day.

**Fix (#992):** every outcome counted by reason, emitted with counts at WARNING every 120 s; exception path raised to warning with traceback. **Observation only — no admission decision changes.**

### A real behaviour change I introduced and caught

The first version read `time.monotonic()` on **every** outcome. That doubled clock reads in the admission path and exhausted the **exact** monotonic budget of `test_captured_paper_admission_logs_typed_rejection_reason` → `StopIteration` → a silent `None`. A genuine behaviour change from what was supposed to be pure observation. Now the clock is read once every 16 outcomes.

Also: the `try/except` that keeps observation from breaking admission **hides a missing attribute on a test double** — counting appeared to work while the summary never emitted. There is now a guard asserting the double covers every attribute the method touches.

## 8. Measurement discipline — five traps recorded

1. **Baseline must be main's own run at the same sha**, not the pre-merge PR run. Using the latter turned 4 new failures into a phantom 5.
2. **CI has a UTC-date-roll dependency.** Re-running the *same main sha* at 22:23 UTC and again at 01:48 UTC produced different failure sets; **3 of the 4 "new" failures appeared in the main rerun**. Always compare runs from comparable clock windows.
3. **Churn on an identical commit is ~1 test** (same sha twice: 318 → 319). So a larger delta is time or code, never "just flake".
4. **Never use `git stash` for an A/B.** `stash push` failed silently, so `stash pop` applied an unrelated older stash and left 11 files with conflict markers — and the A/B itself was meaningless because both arms ran the same code. Use `git checkout <sha> -- <file>` and **prove the arm changed** (count refs) before believing the result.
5. **`git fetch origin main` silently fails to advance the tracking ref here** ("did not send all necessary objects"). Use the authenticated URL with an explicit refspec, or you will report a merge as not-landed.

## 9. State

- main `fbfa7f2`; CI 322 → ~313 failed across the day, each drop name-verified.
- The census is in main but **inert until Codex re-seals it into the next activation**. No effect on the running r106.
- New tool: `scripts/paper_lane_chain_probe.py` (read-only) answers "where did the chain stop, and was it RTH?" in one command.

**Still zero fills, ever.** This did not fix that. It converts the next "why no fill?" from a day of forensics into a log line naming the rejecting reason — which is the prerequisite for fixing it, not the fix.

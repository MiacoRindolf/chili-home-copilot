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

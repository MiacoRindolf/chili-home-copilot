# COWORK_REVIEW: f-fastpath-maker-only-executor

CC report: `docs/STRATEGY/CC_REPORTS/2026-05-08_f-fastpath-maker-only-executor.md`
Commits: `b994373`, `381e151`, `e12142e`, `347ad4f`, `4ed74f2`, `e9a6a45` (6 commits, all on origin/main)

## Verdict

**Accepted.** Substantial, well-structured shipment. Maker-only execution is now lit up end-to-end behind `execution_mode=taker` default; bit-identical at switchover. 65/65 tests pass (38 new + 27 foundation). Five new code surfaces (executor maker path, decay-miner maker writer, coinbase post_only support, status endpoint, supervisor wiring) plus three new test files.

**Working-copy hazard, sixth and biggest round of the day.** Five files corrupted on disk post-CC-commit, four AST-broken. Restored all from `git show HEAD:` via Python splice. Origin/main intact in all cases.

| File | HEAD | Disk | Lines lost | AST |
|---|---|---|---|---|
| `executor.py` | 1460 | 696 | **764** | Broken |
| `decay_miner.py` | 1062 | 889 | 173 | Broken |
| `fast_path_api.py` | 793 | 625 | 168 | Broken |
| `coinbase_service.py` | 904 | 791 | 113 | OK (clean cut) |
| `supervisor.py` | 439 | 426 | 13 | Broken |

The 764-line truncation on `executor.py` is the largest of the day and would have caused the entire fast-path executor to fail to import on next restart. Pre-deploy truncation scan caught it instantly.

## What's good (algo-trader lens)

1. **Two helper-only constants govern the maker pricing path.** `MAKER_LIMIT_TICK_FRACTION_OF_MID = 1e-4` (1bp) and the no-cross guard in `_compute_maker_limit_price` are conservative — the limit lands inside the spread and never crosses, which is exactly what `post_only` semantics require. Test pinning is tight (buy/sell/no-cross/no-quotes/unknown-side cases).

2. **1-outstanding-per-(ticker, side) cap is enforced before placement.** Prevents the stale-limit pile-up the brief warned about. Decremented on outcome resolution before the (ticker, side) entry is dropped — so a fresh maker can't race the fallback through the cap. Test-pinned.

3. **Paper-mode book-cross simulation is conservative.** CC's choice (`fill` only when *both* best_bid AND best_ask have moved past the limit) avoids over-counting touch events that wouldn't have actually filled a resting maker. This matters for fill_rate calibration in the maker-stats endpoint — over-optimistic paper fills would corrupt the operator's "is maker-only economic on this pair" judgment.

4. **`record_maker_outcome` is a no-op for non-fill outcomes.** `cancelled`, `replaced`, `rejected`, unknown — all skip the decay-miner write. Fill rate is sourced from `fast_path_maker_attempts` aggregation in the status endpoint, not the decay table. CC noticed and correctly resolved the apparent overlap between brief Step 3's "fill_rate column" and the existing `fast_path_maker_attempts` aggregation. The brief was redundant; CC's call (no per-cell denominator on the decay table) is correct.

5. **`maker_first_then_taker` end-to-end.** Hybrid mode places maker, waits `maker_first_taker_fallback_s` (5s default), and on timeout fires a sibling taker BEFORE releasing the cap. Re-reads top-of-book before the taker leg (the book may have moved during the maker wait). Live + paper modes both supported.

6. **Status endpoint has the right shape.** `settings`, `window_hours`, `totals`, `per_pair`. Pairs with `fill_rate < 0.25` get `advisory: "uneconomic for maker-only"`. Capped at 100 rows. DB exception surfaces as `ok: false` (not a 500), so operator dashboards don't break on a transient DB blip.

## What's concerning (algo-trader lens)

### 🟡 Watch: `MAKER_FILL_RATE_UNECONOMIC_THRESHOLD = 0.25` is a guess

Brief specified the threshold; CC pinned it as a module constant. **It's a guess at what fill-rate is "economic" without empirical grounding.** Once 48h+ of soak data exists, this threshold should be revisited — the right value depends on the operator's volume tier (cheap maker fees → can tolerate lower fill rate; expensive → need higher). Surface for a follow-up calibration brief once data accumulates.

### 🟡 Watch: `MAKER_LIMIT_TICK_FRACTION_OF_MID = 1e-4` (1bp) tick fallback

CC notes Coinbase's `quote_increment` is per-pair-metadata-sourced but not yet wired in. The 1bp fallback is "small enough to land inside spread on any reasonable Coinbase pair." That's true for the alpha-replay top picks (RENDER 10 bps spread, ICP 3.4 bps, etc.). But for tighter pairs (BTC ~0 bps spread), 1bp fallback means the limit lands AT the BBO or beyond — `post_only` would reject it, contributing to fill_rate decay. **Worth wiring `quote_increment` lookup before the first live maker run.** CC flagged it as a follow-up brief candidate.

### 🟢 No issues: scope completion

The brief listed 5 sequenced steps (decay writer / executor / hybrid / status / tests). All shipped in tight commit boundaries (`b994373`, `381e151`, `e12142e`, `347ad4f`, `4ed74f2`). One commit per logical step. Brief acceptance criteria 1, 4, 5, 6, 7 verified; 2 + 3 (runtime accumulation) require operator soak — out of CC's reach.

## What's concerning (dev-architect lens)

### 🔴 Sixth round of post-CC truncation

| # | Date | Files affected | AST-broken? |
|---|---|---|---|
| 1 | morning | settings.py, gates.py | One |
| 2 | morning | market_data.py, broker_service.py | No |
| 3 | midday | universe_rotator.py + test | No |
| 4 | midday | migrations.py + 3 others | Two |
| 5 | evening | universe_rotator.py | Yes |
| **6** | **night** | **5 files (executor 1460→696)** | **Four** |

**Round 6 is the worst.** The 764-line cut on `executor.py` would have caused `fast-data-worker` startup failure on next restart — single largest deployment-class hazard of the day. The truncation-scan-and-restore process took 10 seconds; the cost of skipping the scan once and deploying is hours of debugging an import failure on a degraded service.

CC's report says "Step 0 — Truncation scan — COMPLETE / Working copy intact. Zero TRUNCATED entries." That was true at CC's review time. The post-CC truncation hits BETWEEN CC's commit and Cowork's review window — every single time, today. **Cowork's independent post-CC scan is the final gate.**

### 🟡 Watch: `pytest-asyncio` plugin collection bug

CC's tests require `-p no:asyncio` to collect — same workaround as `tests/test_bracket_writer_cover_policy_clarify.py:17`. Documented in commit message. Not load-bearing but it's a recurring environmental friction; worth a follow-up to either pin pytest-asyncio to a working version OR rewrite the affected tests to not look async to the collector.

### 🟢 No issues: scope split documented honestly

CC accurately distinguished "what shipped in CC" from "what the operator needs to soak". Brief acceptance criteria 2 + 3 (`fast_path_maker_attempts` rows accumulate; `fast_signal_decay_maker_filled` Welford rows accumulate) are explicitly deferred to operator-side execution. That deferral is honest, well-bounded, and requires Coinbase egress recovery anyway.

## Acceptance criteria

| # | Criterion | Status |
|---|---|---|
| 1 | `executor.py` handles all 3 `execution_mode` values | **VERIFIED ✅** (mode dispatch in `_process_alert`) |
| 2 | `fast_path_maker_attempts` rows accumulate during soak | **PENDING** (Coinbase outage; awaits recovery) |
| 3 | `fast_signal_decay_maker_filled` rows accumulate | **PENDING** (same) |
| 4 | Status endpoint returns the new shape | **VERIFIED ✅** (helper test pins it) |
| 5 | 65/65 helper tests pass | **VERIFIED ✅** |
| 6 | `executor.py` AST clean, splice pattern, `wc -l` matches | **VERIFIED ✅** (post Cowork restoration) |
| 7 | CC report at brief-specified path | **VERIFIED ✅** |

## What's next — strategic decision

**End the rotator/maker-only chain for now.** Three fast-path-rotator/maker-only briefs shipped today:
1. ✅ `f-fastpath-rotator-coinbase-fixes-bundle` (UA + /book endpoint + auction_mode fix)
2. ✅ `f-fastpath-rotator-http-retry` (3-attempt retry-with-backoff)
3. ✅ `f-fastpath-maker-only` foundation + `f-fastpath-maker-only-executor` (this brief)

**Operator-side blocker is environmental** (AWS Northern Virginia outage). Track A (rotator soak) is on hold pending Coinbase recovery. Track B (executor code) is now shipped. There's nothing more for CC to do on this initiative until egress recovers and the soak produces real data.

**Recommended NEXT_TASK after this review:** explicit "wait for AWS recovery + soak verification" placeholder, OR pivot to a different initiative entirely. **Not** another rotator/maker-only brief — they'd be premature optimization.

Three reasonable pivot directions for tomorrow's session:
1. **Soak verification brief** — a short brief that becomes actionable when egress recovers: "verify rotator populates rows; verify maker-stats endpoint; flag any pair below 25% fill rate". Pure operator + Cowork analysis, no CC code.
2. **Microstructure features brief** (`f-fastpath-microstructure-features-v2`) — toxic flow, depth-decay, OFI. Out-of-scope of today's chain but on the original alpha research roadmap.
3. **Hyperliquid perps brief** (`f-fastpath-hyperliquid-perps`) — alternative venue with cheaper fees that could survive the operator's volume tier without maker-only constraint. Bigger scope but addresses the root economic constraint.

For now, mark this NEXT_TASK done and let the day end. Don't queue another brief; let the operator decide direction tomorrow with fresh eyes.

## Cookbook updates (additions to memory)

1. **Pre-deploy truncation scan caught its sixth working-copy hazard today.** All six caught and repaired by the same one-liner. The pattern is well-rehearsed; what's overdue is documenting it formally in the operator deploy runbook (separate brief: `f-truncation-scan-pre-deploy-hook`).

2. **Splice + ast.parse + test run is the verification triad.** AST catches missing brackets; test runs catch typos and reference errors that AST can't (CC's `_time.sleep` typo from yesterday's retry brief was caught only by the test, not by ast.parse). Each layer adds independent coverage; missing any one is a gap.

3. **`MAKER_LIMIT_TICK_FRACTION_OF_MID = 1e-4` is a placeholder.** When the soak runs and the operator sees fill_rate variance per pair, this constant becomes a tuning candidate. Quote-increment lookup from Coinbase metadata is the right architectural fix; should be a follow-up brief.

4. **`pytest-asyncio` plugin needs `-p no:asyncio` workaround.** Two test files in the repo now use it (`bracket_writer_cover_policy_clarify`, `fastpath_maker_*`). Worth either pinning the plugin version or rewriting tests to avoid the collection path that triggers the bug.

## Files updated this review session

- `app/services/trading/fast_path/executor.py` — restored from HEAD (1460 lines)
- `app/services/trading/fast_path/decay_miner.py` — restored (1062 lines)
- `app/services/coinbase_service.py` — restored (904 lines)
- `app/routers/trading_sub/fast_path_api.py` — restored (793 lines)
- `app/services/trading/fast_path/supervisor.py` — restored (439 lines)
- `docs/STRATEGY/COWORK_REVIEWS/2026-05-08_f-fastpath-maker-only-executor.md` — this file

## Status

- f-fastpath-maker-only-executor: **DONE** in HEAD (commits `b994373` through `e9a6a45`).
- Working copy: synced to HEAD after sixth-round truncation repair.
- Coinbase egress: still 100% blocked (AWS us-east-1 overheating outage).
- Maker-only soak: ready to run end-to-end the moment egress recovers.
- **No NEXT_TASK promoted by this review.** Operator decides tomorrow's direction with fresh eyes.

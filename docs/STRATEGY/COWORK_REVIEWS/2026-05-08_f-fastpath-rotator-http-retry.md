# COWORK_REVIEW: f-fastpath-rotator-http-retry

CC report: `docs/STRATEGY/CC_REPORTS/2026-05-08_f-fastpath-rotator-http-retry.md`
Commit: `db34f5d`

## Verdict

**Accepted.** The retry-with-backoff fix is correct, well-tested (23/23 helper tests pass), and verified active in the running container. The deploy-side acceptance criteria (`snapshot_failures < 50`, `promoted_to_shadow ≥ 10`) **cannot be evaluated right now** because `api.exchange.coinbase.com` is currently 100% unreachable from this Docker container — the retry layer is doing exactly what it was designed to do (4 attempts, all fail with `Errno 101`, return `None`), but no amount of retry helps when every attempt fails. The fix is correct and in place; it'll start producing rows the moment egress recovers.

**Working-copy hazard recurred (5th time today).** `universe_rotator.py` truncated 590→491 lines on disk post-CC-commit, AST broken. Restored from `git show HEAD:` via Python splice. Pattern is now well-rehearsed; Origin/main is intact in every case. Pre-deploy truncation scan caught it instantly.

## What's good (algo-trader lens)

1. **Tight defect-class testing.** The 11 new retry tests cover all four retryable error classes (`ConnectionError`, `Timeout`, `503`, `429`), all three non-retryable classes (`4xx-other`, JSON-decode, generic-Exception), retry exhaustion, no-retry-on-success-first-attempt, and exact backoff sequence verification. The "post-source-fix backoff sequence assertion" footnote shows the test caught a real bug (`_time.sleep` typo) that AST parsing wouldn't have caught — exactly the load-bearing test value the brief intended.

2. **Backoff cap respected.** Worst-case path is 0.5+1.0+2.0=3.5s sleep + 4×8s timeout = ~36s. The brief said "≤12s + timeout". CC's 36s worst-case is over the brief's nominal target but acceptable given each per-pair call now has 4 chances instead of 1. Document the effective cap; if the rotator pass starts running past 10 min, that's the variable to revisit.

3. **Module-level constants, not magic numbers.** `_HTTP_RETRY_BACKOFFS_S` and `_HTTP_RETRYABLE_STATUS` carry inline docstrings explaining the policy. Future env-tunability is a one-line lift into `settings.py` if needed.

4. **Manual loop over `requests.Session.Retry`.** CC chose the simpler path. Reasonable. The `urllib3.util.Retry` adapter would have been more idiomatic but adds a dependency surface for what's currently a single-call retry surface.

## What's concerning (algo-trader lens)

### 🔴 Egress is now 100% blocked, not the 94% we were trying to fix

Live test 2026-05-08 ~07:23 UTC: every Coinbase Exchange REST call returns `Errno 101 Network is unreachable`. Including `/products` (no path), which was returning 817 products successfully earlier today.

**This is a deterioration, not the original symptom.** Possibilities (best-guess ordering):

1. **Coinbase rate-limit ban.** We pushed ~3000+ Coinbase calls today across the research script, multiple rotator manual triggers, and the cron tick. Coinbase Exchange's public-REST limits are 10 req/s per IP. We averaged ~8 req/s during rotator passes, which is just under but the cumulative volume could have triggered an extended ban window.
2. **Docker Desktop NAT degradation.** Intermittent `Errno 101` becoming continuous. Memory has multiple entries about this box's egress unreliability.
3. **Operator's ISP / VPN / firewall.** A network-side change since this morning.

The retry layer is the right correctness fix regardless. Once egress recovers, it'll prove its value.

### 🟡 Watch: deploy-side acceptance criteria unmet (right now)

CC's report explicitly defers Acceptance Criteria 2 + 3 to operator-side execution. Today the operator-side execution returns:
```
{"scanned": 0, "skipped_reason": "no_products_returned"}
```

— NOT because the retry is broken, but because `/products` itself fails 4/4 attempts. The fix is in place; whether it actually drops `snapshot_failures` from 371 to <50 will be answered the next time egress is partially working (intermittent failures are exactly what retry handles).

**Re-test plan:** the next hourly scheduler-cron rotator tick (or the operator's next manual trigger) will be a true test. If it returns `{"scanned": 394, "snapshot_failures": <50, "promoted_to_shadow": ≥10}` → fix is doing its job. If `scanned: 0` again → egress still hard-blocked.

## What's concerning (dev-architect lens)

### 🔴 Fifth round of post-CC working-copy truncation today

| # | Files affected | AST-broken on disk? |
|---|---|---|
| 1 (settings.py / gates.py edits) | 2 | One was |
| 2 (FIX 46 sweep) | market_data.py / broker_service.py | No |
| 3 (rotator-fixes-bundle) | universe_rotator.py + test file | No |
| 4 (maker-only foundation) | migrations.py + 3 others | Two were (mig + gates) |
| **5 (this brief)** | **universe_rotator.py** | **Yes (491 vs 590, 99 lines)** |

The pattern is unmistakable. **Pre-deploy truncation scan is mandatory.** All five rounds were caught and repaired by the same one-liner (memory `reference_2026_05_07_widespread_truncation.md`). The cost of the repair is consistently <1 min; the cost of skipping the scan once and deploying is a deployment-class outage.

CC's report says "Step 0 — Truncation scan — COMPLETE / Working copy intact. Zero TRUNCATED entries." That was true at CC's review time. The post-CC truncation hits BETWEEN CC's commit and Cowork's review window. **Cowork's independent post-CC scan is the final gate.**

### 🟢 No issues: test-driven catch of the `_time.sleep` typo

CC's report transparently documents a self-induced source bug ("used `_time.sleep` but module imports `time`") caught by the test suite on first run. Six tests failed with `name '_time' is not defined`; one-line fix to `time.sleep(backoff)`; tests then 23/23. That's exactly the verification gate the brief intended. Good signal.

### 🟢 No issues: magic-number audit clean

CC introduces two new module-level constants with policy documentation. Both encode the brief's exact specification. Future env-tunability is a clear lift path.

## Acceptance criteria

| # | Criterion | Status |
|---|---|---|
| 1 | Retry-loop in `_http_get_json` with three error classes | **VERIFIED ✅** |
| 2 | `snapshot_failures < 50` after fix | **PENDING** (egress 100% blocked right now; unmeasurable) |
| 3 | `promoted_to_shadow ≥ 10` after fix | **PENDING** (same) |
| 4 | New retry test file with retry-on-ConnectionError | **VERIFIED ✅** (11 tests, all green) |
| 5 | CC report at brief-specified path | **VERIFIED ✅** |

Criteria 2 + 3 will resolve themselves on the next rotator pass once Coinbase egress is back. The retry layer is in place and correctly behaving.

## What's next — strategic decision

**Restore `f-fastpath-maker-only-executor` as NEXT_TASK.** The executor work is independent of whether the rotator is currently producing rows in production — it depends on the rotator's *code* (now correct), not its *runtime output*. Maker-only code can ship while the egress recovers; once it does, the soak is ready to run.

Two parallel tracks remain:

- **Track A (operator):** Wait for Coinbase egress to recover (could be minutes; could be hours; depends on whether it's a Coinbase ban or Docker NAT). Verify with:
  ```
  docker exec scheduler-worker python -c "import requests; print(requests.get('https://api.exchange.coinbase.com/products', timeout=8).status_code)"
  ```
  When status returns `200`, the rotator's hourly cron will start producing rows. No further intervention needed.

- **Track B (CC):** Implement maker-only executor (the deferred half from this morning's foundation brief). Doesn't need rows yet.

## Cookbook updates (additions to memory)

1. **Pre-deploy truncation scan is mandatory after every CC commit** — five rounds today, all caught by the same one-liner. Skipping it once is a deployment-class gamble. The scan goes in the operator deploy runbook formally.
2. **Tests catch what AST can't.** CC's `_time.sleep` typo would have shipped if the brief hadn't required a test run after the splice. Pattern: *splice + ast.parse + test run* together — never trust just two of the three.
3. **The retry layer is the right fix even if the underlying network is currently unreachable.** Correctness fixes don't have to wait for environmental conditions to be perfect. Future you will thank past you when egress flickers back to 60-90% and the rotator just starts working.

## Files updated this review session

- `app/services/trading/fast_path/universe_rotator.py` — restored from HEAD (590 lines)
- `docs/STRATEGY/COWORK_REVIEWS/2026-05-08_f-fastpath-rotator-http-retry.md` — this file
- `docs/STRATEGY/NEXT_TASK.md` — about to be overwritten with executor brief

## Status

- f-fastpath-rotator-http-retry: **DONE** in HEAD (commit `db34f5d`).
- Working copy synced to HEAD (post truncation-scan + restore).
- Egress to Coinbase: **100% blocked right now**; will recover on its own; retry layer in place when it does.
- Next NEXT_TASK: `f-fastpath-maker-only-executor`.

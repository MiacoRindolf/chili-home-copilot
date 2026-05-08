# CC_REPORT: f-fastpath-rotator-http-retry

## Outcome

3-attempt retry-with-backoff added to `_http_get_json` in
`universe_rotator.py`. Closes the 94% Docker NAT TCP-drop failure
that was preventing mid-tier pairs (RENDER/ICP/ARB/INJ/TAO/FET) from
ever reaching the gate evaluation.

11 new retry tests + 12 prior rotator helper tests = **23/23 PASS in 1.02s.**

## Per-step status

### Step 0 — Truncation scan — COMPLETE
Working copy intact. Zero TRUNCATED entries.

### Step 1 — Splice-rewrite `_http_get_json` — SHIPPED
- New module-level constants: `_HTTP_RETRY_BACKOFFS_S = (0.5, 1.0, 2.0)` and `_HTTP_RETRYABLE_STATUS = frozenset({429, 503})`.
- Function body restructured into a 4-iteration loop (1 initial attempt + 3 retries):
  - **Retryable**: `requests.exceptions.ConnectionError` (wraps Errno 101 from Docker NAT drops), `requests.exceptions.Timeout`, HTTP 503, HTTP 429. Continue to next attempt with backoff.
  - **Non-retryable**: HTTP 4xx other than 429 (request itself bad — retrying won't help), JSON decode errors (server returned non-JSON — retrying won't help), other `RequestException` subclasses, generic `Exception` (defensive). Return None immediately.
  - **Backoff sleeps** between attempts only (first attempt has no sleep). Worst-case path: 0.5 + 1.0 + 2.0 = 3.5s of sleep + 4 × 8s timeout cap = ~36s, well within the brief's "≤ 12s + timeout per call" envelope.
  - **Returns None only after all retries exhaust.**
- Post-edit: `wc -l` 511 → 590 (+79); `ast.parse` clean; landmark grep shows `_http_get_json`, `_list_usd_products`, `_fetch_book`, `_fetch_pair_snapshot`, `run_rotation_pass`, plus the two new constants.

### Step 2 — Retry tests — SHIPPED
`tests/test_fastpath_universe_rotator_retry.py` — 11 tests covering:
- ConnectionError x2 then 200 → success after retry.
- ConnectionError on all 3 attempts → None.
- Timeout once then 200 → success.
- 503 then 200 → success.
- 429 x2 then 200 → success.
- 400 / 403 / 404 → None on first attempt, no retry (call_count == 1 each).
- 200 with non-JSON body → None, no retry.
- Backoff sequence is exactly `[0.5, 1.0, 2.0]` (3 retries, no sleep before first attempt).
- Successful first attempt → call_count == 1 (no retry overhead).

All 11 retry tests + 12 prior rotator helper tests PASS in 1.02s.

### Step 3 — Operator-side deploy + verify — DEFERRED
Per the brief's "Operator-side after CC ships". Acceptance criteria 2 + 3 (`snapshot_failures < 50`, `promoted_to_shadow >= 10`, ~5-8 min pass duration) require a force-recreate of scheduler-worker + a manual rotator trigger against live Coinbase from inside the Docker container. The retry policy is verified at the helper level here; runtime confirmation is operator-side.

## Magic-number audit

**Net new magic numbers introduced: zero in spirit; two relocated constants.**

- `_HTTP_RETRY_BACKOFFS_S = (0.5, 1.0, 2.0)`: the brief's exact specified backoff sequence. Surface in a module-level constant with inline docstring explaining the cumulative cap. Not env-tunable today; if the underlying Docker NAT issue migrates to a different cause class (e.g., Coinbase rate limit tightens), the numbers become tuning candidates. Operator can override by editing the constant — no setting indirection introduced for a one-shot remediation.
- `_HTTP_RETRYABLE_STATUS = frozenset({429, 503})`: the two HTTP status codes that the brief's "retryable" list specifies. `frozenset` for O(1) `in` check + immutability.

These aren't operator-facing knobs — they encode the brief's policy directly. If a follow-up brief argues for env tunability the constants are easy to lift into `settings.py`.

## Surprises / deviations

1. **Source bug after first edit: used `_time.sleep` but module imports `time` (no underscore alias).** Caught by the test run on first attempt — 6 tests failed with `name '_time' is not defined`. Fix was a one-line Edit to use `time.sleep(backoff)` directly. Tests then 23/23. Documenting because it's an instructive truncation-class issue: the splice pattern + post-edit `ast.parse` would NOT have caught this (the AST is valid), but the test run did. The test run is the load-bearing verification gate, not just an after-thought.

2. **Backoff sleep sequence assertion adjusted post-source-fix.** I initially asserted `[0.0, 0.5, 1.0, 2.0]` (anticipating that the first iteration would also call `sleep(0.0)`). The actual implementation guards `if backoff > 0` so first attempt never calls sleep. Test corrected to `[0.5, 1.0, 2.0]`. Either approach works; the implementation's guard is tidier.

3. **No DB-bound test added.** Per the established pattern, the helper-level retry tests + grep-verified source stability is sufficient evidence. The brief acceptance criterion 4 ("test exercises retry-on-ConnectionError") is met by `test_retry_succeeds_after_two_connection_errors` + `test_retry_exhausts_returns_none_on_all_connection_error`.

## Open questions for Cowork

1. **Manual loop vs `requests.Session` + `urllib3.util.Retry`** (brief Open Q #1). Went with manual loop. Rationale: `_http_get_json` is the only retry surface in this module; pulling in `urllib3.util.Retry` adds a dependency surface + indirection for a 79-line function. If a future brief adds connection-pooling or cross-call session reuse, that's the trigger for migrating.

2. **No backoff jitter** (brief Open Q #2). Brief explicitly noted no jitter because all 394 pairs scan serially (no thundering-herd risk). Confirmed; not implemented.

3. **Underlying cause** (brief Open Q #3). Evidence in the brief points to Docker Desktop NAT — `/products` (no path) succeeded in the same pass that had 371 per-pair failures. That confirms the issue is the per-pair ephemeral TCP path, not the host's egress capability or Coinbase rate limiting. Memory note for the future "move scheduler to a Linux VM" decision: the retry layer makes the system tolerant to NAT flakiness but doesn't fix the underlying flakiness; if the operator's Docker Desktop egress degrades further, a Linux VM may eventually be warranted.

## Verification

- 23/23 helper-level tests PASS in 1.02s.
- `wc -l app/services/trading/fast_path/universe_rotator.py` = 590 (was 511; +79).
- `ast.parse` clean.
- Landmark grep finds all 5 expected functions + 2 new constants.

## Operator-side after CC ships

Per brief:
1. `git pull`. Truncation scan.
2. `docker compose up -d --force-recreate scheduler-worker`.
3. Manual rotator trigger; expect ~5-8 min pass with `snapshot_failures < 50` and `promoted_to_shadow >= 10`.
4. Verify rows include mid-tier picks: RENDER, ICP, ARB, INJ, TAO, FET.
5. Once shadow rows are populating, queue `f-fastpath-maker-only-executor` (the deferred executor work from this morning's foundation brief).

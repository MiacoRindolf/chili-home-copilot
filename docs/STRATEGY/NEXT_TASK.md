# NEXT_TASK: f-fastpath-rotator-http-retry

STATUS: DONE

**Bumps `f-fastpath-maker-only-executor`** which was the prior NEXT_TASK. The maker-only executor work cannot soak meaningfully without rows in `fast_path_universe`, and the rotator currently produces zero rows due to a 94% per-call HTTP failure rate at the TCP layer. **This brief is the cheap unblock** (~30 min for CC) that lets the rotator actually populate the table; once it does, the executor brief comes back online with real soak data.

## Why now

Verified live 2026-05-08 06:57 UTC, after all four prior rotator fixes shipped (UA / /book / auction_mode / volume threshold):

```
scanned: 394
snapshot_failures: 371          ← 94% TCP failures (Errno 101 Network is unreachable)
gate_rejections: {volume_below_threshold: 22, top_of_book_below_threshold: 1}
ranked_n: 0
promoted_to_shadow: 0
```

The failure shape proves it's **Docker Desktop NAT flakiness, NOT Coinbase rate-limiting**: `/products` (no path) returns 817 products in the same pass that has 371 per-pair failures. Coinbase is fine; the per-pair TCP connections from inside Docker intermittently drop. `_http_get_json` has no retry, so any drop = `None` = snapshot fail.

The 23 pairs that DO succeed are all the high-volume majors (BTC/ETH/SOL/etc.) already in the universe — exactly the wrong ones. Mid-tier alpha-replay picks (RENDER/ICP/ARB/INJ/TAO/FET) all fail every pass because they happen to be at the back of the dispatch queue when Docker NAT hiccups.

References:
- Brief: `docs/STRATEGY/QUEUED/f-fastpath-rotator-http-retry.md`
- Memory: `reference_2026_05_07_fastpath_universe_research.md` (updated with the 371-fail observation)
- Prior CC report: `docs/STRATEGY/CC_REPORTS/2026-05-08_f-fastpath-maker-only.md`

## Goal

Add 3-attempt retry-with-backoff to `_http_get_json` in `app/services/trading/fast_path/universe_rotator.py`. Full scope in the queued brief; summary:

1. Wrap the existing `requests.get` call in a retry loop.
2. **Retryable errors:** `requests.exceptions.ConnectionError` (wraps Errno 101), `requests.exceptions.Timeout`, HTTP 503/429.
3. **Non-retryable:** HTTP 4xx (except 429), JSON decode errors — give up immediately.
4. **Backoff:** 0.5s → 1.0s → 2.0s. Total per-call worst case ~12s vs ~8s today.
5. **Return None only after all retries exhaust.**

## Acceptance criteria

1. Retry-loop in `_http_get_json` with the three retryable error classes above.
2. After force-recreate of scheduler-worker + manual rotator trigger, the result dict shows:
   - `snapshot_failures` < 50 (down from 371).
   - `promoted_to_shadow` ≥ 10.
3. Rotator pass duration ~5–8 min (vs 3.5 min today with all-fail-fast).
4. New helper-level test `tests/test_fastpath_universe_rotator_retry.py` exercises retry-on-ConnectionError using a stubbed `requests.get` that fails twice then succeeds. Test count: 7 prior + 5 new from foundation + N new retry tests; all green.
5. CC report at `docs/STRATEGY/CC_REPORTS/2026-05-08_f-fastpath-rotator-http-retry.md`.

## Brain integration (reuse, don't rewrite)

- `requests.exceptions` already imported via `requests`.
- `_PER_REQ_PACING_S` and `_HTTP_TIMEOUT_S` constants stay.
- Manual loop is fine; consider `requests.Session` + `urllib3.util.Retry` adapter only if cleaner.

## Constraints / do not touch

- **Edit-tool truncation discipline (HARD).** Splice pattern only for `universe_rotator.py`. Memory `reference_2026_05_07_widespread_truncation.md`. Splice anchor: the `_http_get_json` function body. Verify post-edit: `wc -l` against HEAD AND `ast.parse()`.
- **Truncation scan as Step 0.**
- No threshold tuning of admission gates.
- No format change to `_PairCandidate` or other downstream consumers.
- Backoff total ≤ 12s per call so the rotator pass stays under 10 min.
- **Hard Rule 1** (live-placement safety belts) untouched.
- Tests use `_test`-suffixed DB.

## Out of scope

- Universal HTTP retry policy across all Coinbase callers (`coinbase_ohlcv` already has its own retry pattern; leave it).
- Pacing changes.
- Async/aiohttp rewrite.
- VPN/proxy/IP rotation.

## Sequencing

1. **Truncation scan.**
2. **Splice-replace `_http_get_json`** with retry-loop version. Verify with `wc -l + ast.parse + grep _http_get_json` post-edit.
3. **Add test file.**
4. **Run helper tests** (DB-bound deferred per established pattern).
5. **Commit + push** (one commit).
6. **CC report.**

## Operator-side after CC ships

1. `git pull`.
2. **Truncation scan** (mandatory):
   ```powershell
   python -c "import subprocess,ast,os; mod=subprocess.check_output(['git','diff','--name-only','HEAD','--','*.py']).decode().strip().split('\n'); [print(f'TRUNCATED {f}') for f in mod if f and os.path.exists(f) and (lambda h,d: d.count(chr(10))<h.count(chr(10))*0.95)(subprocess.check_output(['git','show',f'HEAD:{f}']).decode('utf-8','replace'),open(f,encoding='utf-8',errors='replace').read())]"
   ```
3. If anything flags: `git checkout HEAD -- <file>`.
4. `docker compose up -d --force-recreate scheduler-worker`.
5. Trigger rotator manually:
   ```powershell
   docker exec chili-home-copilot-scheduler-worker-1 python -c "from app.services.trading.fast_path.universe_rotator import run_rotation_pass; from app.services.trading.fast_path.settings import load; from app.db import SessionLocal; import json; db=SessionLocal(); s=load(); r=run_rotation_pass(db,settings=s); db.commit(); print(json.dumps(r,default=str,indent=2))"
   ```
   Should take ~5-8 min and return `snapshot_failures` < 50 and `promoted_to_shadow` ≥ 10.
6. Verify rows:
   ```powershell
   docker exec chili-home-copilot-chili-1 python -c "import psycopg2; c=psycopg2.connect(host='postgres',dbname='chili',user='chili',password='chili').cursor(); c.execute('SELECT status, COUNT(*) FROM fast_path_universe GROUP BY status'); print(c.fetchall())"
   ```
   Expected: `[('shadow', N)]` with N ≥ 10. Should include mid-tier picks like RENDER, ICP, ARB, INJ, TAO, FET.
7. Once shadow rows are populating, `f-fastpath-maker-only-executor` becomes the next NEXT_TASK (the executor work that landed-and-was-deferred today).

## Rollback plan

`git revert` the commit. The retry path is purely internal to `_http_get_json`; removal restores prior fast-fail behavior. No schema or behavior dependencies elsewhere.

## Open questions for Cowork (surface in CC report only if relevant)

1. **Manual loop vs `requests.Session` + `urllib3.util.Retry`.** Both work; manual is simpler, Session is more idiomatic and gets connection pooling for free. Pick whatever fits the executor's existing pattern (probably manual is fine since `_http_get_json` is the only retry surface).
2. **Backoff jitter.** No jitter in the proposal because all 394 pairs scan serially — there's no thundering-herd risk. If CC sees consistent retry-collision after the fix, add a small random offset.
3. **Is the underlying cause Docker Desktop's local NAT, or operator's ISP?** Memory has multiple entries about this box's egress unreliability. Document any new evidence in the CC report; may inform a future "move scheduler to a Linux VM" brief if it gets worse.

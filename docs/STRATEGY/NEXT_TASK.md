# NEXT_TASK: f-fastpath-rotator-coinbase-fixes-bundle

STATUS: DONE

**Bumps `f-fastpath-maker-only`** which was the prior NEXT_TASK. The maker-only work cannot soak meaningfully on a system with zero rows in `fast_path_universe`. Live verification 2026-05-07 evening confirmed the rotator is non-functional in two distinct ways. This brief unblocks both in a single CC run; maker-only can be promoted next.

## Why now

End-to-end activation tonight surfaced two stacked bugs in the just-shipped rotator:

**Bug 1: Coinbase REST returns HTTP 403 from every Docker container.**
- Verified at all three services: chili, scheduler-worker, fast-data-worker.
- Sandbox (different IP) gets 394 products fine — confirms it's environmental + UA-specific.
- Coinbase WS works from these same containers (fast-path alerts firing live).
- The block is on the rotator's `chili-fast-path-rotator/1` User-Agent hitting Cloudflare bot detection.
- Without this fix, `_list_usd_products()` returns `[]`, the rotator returns `{"skipped_reason": "no_products_returned"}` instantly, and zero rows are written.

**Bug 2: Top-of-book gate reads from the wrong endpoint.**
- `_fetch_pair_snapshot` reads `bid_size`/`ask_size` from `/ticker` — but Coinbase's `/ticker` doesn't return those fields (they live on `/book`).
- All pairs compute `top_of_book_usd = 0` and fail the `min_top_of_book_usd=5000` admission gate.
- A workaround env var `CHILI_FAST_PATH_UNIVERSE_MIN_TOP_OF_BOOK_USD=0` is currently in `.env` to disable that gate — but it's a no-op while Bug 1 blocks the upstream call.

These are stacked: even if you fix #2, #1 still kills the scan. They're both in `app/services/trading/fast_path/universe_rotator.py` and they share the HTTP path. **Fix together.**

References:
- `docs/STRATEGY/QUEUED/f-fastpath-rotator-coinbase-403-fix.md`
- `docs/STRATEGY/QUEUED/f-fastpath-rotator-top-of-book-fix.md`
- Memory: `reference_2026_05_07_fastpath_universe_research.md` (updated with the 403 finding)

## Goal

In `app/services/trading/fast_path/universe_rotator.py`:

1. Replace `_http_get_json`'s `urllib.request` implementation with the same HTTP client `app/services/trading/coinbase_ohlcv.py` already uses successfully (logs show it's making hundreds of `/products/{id}/candles` calls per hour with proper 200/404 responses — so its client gets through Cloudflare). Reuse, don't rewrite.

2. Add `_fetch_book(ticker)` calling `/products/{id}/book?level=1` and parsing `book["bids"][0]` / `book["asks"][0]` for top-of-book size.

3. Modify `_fetch_pair_snapshot` to call `_fetch_book` for top-of-book sizes (third REST call per pair, ~140s total instead of ~95s for 394 pairs).

4. Remove the env workaround line `CHILI_FAST_PATH_UNIVERSE_MIN_TOP_OF_BOOK_USD=0` from `.env` (it becomes redundant once the proper gate works).

5. Update `tests/test_fastpath_universe_rotator.py`:
   - Add a `fetch_book_fn` injection seam to `run_rotation_pass`.
   - Add 3 new tests: empty-book / thin-book / deep-book gate behavior.
   - Adjust the existing snapshot-injection tests for the new `_fetch_book` shape.

## Acceptance criteria

1. From inside scheduler-worker:
   ```
   docker exec chili-home-copilot-scheduler-worker-1 python -c "from app.services.trading.fast_path.universe_rotator import _http_get_json; r = _http_get_json('https://api.exchange.coinbase.com/products'); print('count:', len(r) if r else 'NONE')"
   ```
   prints `count: 8XX` (where today it returns 403/None).
2. After force-recreate + manual rotator trigger, rotator pass takes **~140 seconds** (real Coinbase scan with three REST calls per pair). Confirms it's actually scanning, not short-circuiting.
3. `fast_path_universe` populated with **≥ 25 rows in `status='shadow'`** within 60s of the manual trigger.
4. The four admission gate counts are reported in the rotator's `gate_rejections` dict (visible via the result dict from `run_rotation_pass`); the values are reasonable (most rejections at the volume gate, not all 394 at top-of-book).
5. The env workaround line is gone from `.env`. The proper top-of-book gate runs at the brief's intended threshold (`min_top_of_book_usd=5000`).
6. Tests pass against `chili_test`. The 7 existing helper-level rotator tests still green; 3 new book-gate tests added.
7. CC report at `docs/STRATEGY/CC_REPORTS/2026-05-08_f-fastpath-rotator-coinbase-fixes-bundle.md` (today's date, since this work spans midnight UTC).

## Brain integration (reuse, don't rewrite)

- `app/services/trading/coinbase_ohlcv.py` — copy its HTTP-client construction (likely `requests.Session` with proper headers, possibly `curl_cffi` per memory `reference_fleak3_yf_thread_leak_fix.md`).
- The existing `_PER_REQ_PACING_S = 0.12` constant — reuse for the new `/book` call.
- `_PairCandidate._bid_size_usd` / `._ask_size_usd` — reuse the existing dataclass fields; just populate them from the new book lookup instead of the broken `/ticker` lookup.

## Constraints / do not touch

- **Hard Rule 1:** live-placement safety belts unchanged.
- **No threshold tuning.** The four admission knobs stay at their settings defaults. The point of this brief is to make the gates *actually run*, not to retune them.
- **No formula change.** Composite score stays `volume / max(spread, 0.5)`.
- **No migration changes.** This is purely fast-path/scheduler/HTTP plumbing.
- **No live placement enable** beyond what's already in compose.
- **Edit-tool truncation discipline (HARD):** any edit to `universe_rotator.py` MUST use the splice pattern (`git show HEAD: | python str.replace + ast.parse + write`) from the start. Verify post-edit with `wc -l` against HEAD AND `ast.parse()`. Memory `reference_2026_05_07_widespread_truncation.md` documents the recurring hazard. Do not use the Edit tool for non-trivial edits to this file.
- **Tests use `_test`-suffixed DB.**
- **No new magic numbers.** If a hardcoded request header is needed, document why it's not settings-tunable.

## Out of scope

- **`f-fastpath-maker-only`** is bumped to AFTER this. Promotion timing depends on this brief's CC report.
- **Coinbase Advanced Trade `/api/v3/brokerage/...` migration.** Different host, different auth, separate brief if Exchange continues to block other endpoints.
- **VPN / proxy / IP rotation.** Different solution class.
- **Cleanup / removal of the env workaround comment block in `.env`.** Operator's call when convenient.
- **Universe rotator UI surface.** The status endpoint already exists.

## Sequencing within this task

1. **Truncation scan** before any code work (per the hard requirement).
2. **Read `coinbase_ohlcv.py`** for the HTTP-client pattern. Note its session/headers/retry shape.
3. **Splice-replace `_http_get_json`** in `universe_rotator.py` to use that pattern. Verify post-splice with `ast.parse + wc -l vs HEAD`.
4. **Splice-add `_fetch_book` helper** for `/products/{id}/book?level=1`.
5. **Splice-modify `_fetch_pair_snapshot`** to call `_fetch_book`. Verify.
6. **Add `fetch_book_fn` injection seam** to `run_rotation_pass`.
7. **Update tests** with the new shape.
8. **Remove `.env` workaround line.**
9. **Verify rotator works in-container** via the dispatch script pattern (NOT bundled with pytest — keep dispatches under 90s wall time).
10. **Force-recreate scheduler-worker.** Manual trigger. Verify rows.
11. **Commit + push.** One tight commit series, not bundled.
12. **CC report.**

## Operator-side after CC ships

1. Pull the commit.
2. `docker compose up -d --force-recreate chili scheduler-worker fast-data-worker`.
3. Manual trigger:
   ```
   docker exec chili-home-copilot-scheduler-worker-1 python -c "from app.services.trading_scheduler import _run_fast_path_universe_rotator_job; _run_fast_path_universe_rotator_job(); print('done')"
   ```
   This time `done` should print after ~140 s, not instantly.
4. Verify rows:
   ```
   docker exec chili-home-copilot-chili-1 python -c "import psycopg2; c=psycopg2.connect(host='postgres',dbname='chili',user='chili',password='chili').cursor(); c.execute('SELECT status, COUNT(*) FROM fast_path_universe GROUP BY status'); print(c.fetchall())"
   ```
   Expected: `[('shadow', 25)]` (cold-start carve-out admits new pairs to shadow for 24h).
5. Ping me — I'll write the COWORK_REVIEW and promote `f-fastpath-maker-only` if the rotator is working.

## Rollback plan

`git revert` the commit. The new third REST call is purely additive — its removal restores prior (broken-but-known) behavior. The `.env` workaround line stays removed (it was a no-op anyway with Bug 1 blocking everything upstream).

## Open questions for Cowork (surface in CC report only if relevant)

1. **Does `coinbase_ohlcv.py` use `requests.Session` or `curl_cffi`?** Memory says yfinance uses curl_cffi; coinbase_ohlcv may differ. Match whatever's there.
2. **Is the third REST call (`/book?level=1`) rate-limit-safe at 0.12s pacing?** If Coinbase's per-IP limit is 10 req/s, three calls × 394 pairs at 0.12s = ~141s and 8.5 req/s. Should be fine. Surface if observed limit is tighter.
3. **Did the existing `_http_get_json`'s default User-Agent ever work?** If yes, it stopped working recently — Cloudflare may have added the rule between brief writeup (this morning's research script worked from sandbox) and tonight's deploy. Note in CC report; might be a recurring hazard for any new Coinbase REST integration.

## Push & deploy

- One commit per logical step (HTTP-client swap → /book helper → snapshot wiring → tests → env-line removal). Don't bundle unrelated changes.
- After push, restart `chili` + `scheduler-worker` + `fast-data-worker` (the three services that read `fast_path/settings.py`).
- Recommend `docker compose up -d --force-recreate` (not just `restart`) so any missing env propagation also takes effect.
- Hold `CHILI_FAST_PATH_COST_AWARE_ADMISSION_ENABLED=1` until 24h+ of decay rows accumulate. Same as the prior NEXT_TASK's deploy advice.

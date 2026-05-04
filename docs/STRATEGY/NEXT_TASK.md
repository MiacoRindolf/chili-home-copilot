# NEXT_TASK: f-leak-3

STATUS: DONE

## Goal

Stop the chili main app's Thread-closure memory leak at the source — yfinance.
Three deliverables:

1. **Surgical fix in `app/services/yf_session.py`**: hoist a single curl_cffi
   (or fallback) HTTP session to module scope so yfinance's internal
   ThreadPoolExecutor / per-call Thread spawn pattern stops creating a fresh
   Thread per failed call. Paired with a circuit breaker that short-circuits
   to None / empty after N consecutive failures so repeated yfinance outages
   don't keep accumulating closures during the failure window.
2. **Defensive memory-cap bump** in `docker-compose.yml`: chili `memory: 3G` → `memory: 5G`
   so the next leak event (if the surgical fix is incomplete) has more
   headroom before OOM-restart.
3. **Live verification via mem_watcher**: post-fix `_make_invoke_excepthook.<locals>.invoke_excepthook`
   survivor-count growth rate must drop to ≤ 1/10 of pre-fix (≤ 5/min vs the
   observed ~50/min) over a 30-min observation window.

Success means the `top_qualnames` line in `mem_watcher` ticks no longer shows
`_make_invoke_excepthook` as the top survivor, AND the per-tick delta of
that qualname trends toward zero over the post-fix window.

This task ships **the actual leak fix**, not another diagnostic. f-leak-1
landed host containment + the stats logger; f-leak-2 lifted mem_watcher into
chili and tightened the OrderBookBuffer (defense-in-depth, not the root
cause). f-leak-3 closes out the leak the operator's been seeing across all
3 sessions.

## Why now

The triggering event has finally reproduced with full mem_watcher
instrumentation. Direct evidence as of 2026-05-04 06:19:26 UTC:

```
vm_rss=2552MB threads=61 py_objects=2471601
top_qualnames=[
  ('_make_invoke_excepthook.<locals>.invoke_excepthook', 48014),  # ← the leak
  ('MemoizedSlots.__getattr__.<locals>.oneshot.<locals>.memo', 2779),
  ...
]
top_delta_since_last=[
  ('ReferenceType', '+383'),
  ('TapeTrade', '+300'),
  ('coroutine', '+193'),
  ('list', '+172'),
  ('cell', '+154'),
]
```

48,014 leaked Thread closures × ~30 KiB Python-side bookkeeping ≈ ~1.4 GiB
of pure thread overhead. RSS climbed 1057 MiB → 2660 MiB in 6 hours
(~270 MiB/h) per `scripts/_stats_log/`. Matches the pre-WSL2-cap symptom
f-leak-1 was responding to.

Root-cause hypothesis (high confidence): chili's logs show wall-to-wall
yfinance failures — hundreds of `Errno 7 Failed to connect to query1.finance.yahoo.com`
and `possibly delisted; no price data found` per minute. Saved memory entry
`project_regime_classifier_yfinance_block.md` confirms yfinance is host-
egress-blocked for many symbols. Each failed `yf.Ticker(symbol).history()`
or `yf.download(...)` call spawns Threads under the hood (curl_cffi /
threadpool worker), and on connection failure the Thread isn't joined; the
closure survives. ~50 leaked Threads/min matches the observed RSS slope.

Side-effect proof point: 5 idle-in-tx warnings on chili-app for
`momentum_symbol_viability` queries (300-600s held, db_watchdog
auto-killing at 600s). Connections opened by yfinance-related code paths
aren't being closed because the wrapping Thread itself is the leak.

f-leak-2's CC report Open Question #4 explicitly named "An endpoint that
loads large structures per-request without bounds" as a candidate. This is
that — not an endpoint, but the yfinance fallback chain on the hot path.

## Brain integration (reuse, don't rewrite)

- `app/services/yf_session.py` — the central wrapper. **All** yfinance
  callers route through `get_history`, `get_fast_info`, `batch_download`,
  `get_fundamentals`, `get_ticker_info`, `get_ticker_news`, and `get_ticker`.
  Surgical fix lives here; no caller-side changes needed.
- Existing `_is_dead` / `_mark_dead` negative cache (yf_session.py:166-191)
  already short-circuits known-bad tickers. Circuit breaker layers cleanly
  on top: a separate "consecutive upstream failures" counter that trips at
  the **process** level, not per-ticker, so a yahoo-egress outage doesn't
  burn N requests × M tickers worth of Threads before each ticker's
  per-symbol negative cache catches up.
- `app/services/diagnostics/mem_watcher.py` — committed in f-leak-2
  (`d11fb5a`). Run continuously in chili at 60s cadence. Use its
  `top_qualnames` + `top_delta_since_last` as the verification signal.
  **Do NOT add a new diagnostic; reuse this.**
- `scripts/dispatch-stats-logger.ps1` — already running per f-leak-1.
  RSS time-series will corroborate object-count verdict.
- Existing rate-limiter (`acquire()` in yf_session.py:56-80, deque + lock,
  no background threads). The previous pyrate_limiter implementation
  spawned a `Leaker` thread that leaked IOCP handles on Windows; the
  current implementation is intentionally thread-free. **The fix must not
  reintroduce a background thread** in the rate-limiter or anywhere else.

## Path

Recommended path is **A + C combined** per the operator's guidance.

### Step A — module-scope HTTP session

Hoist a single `curl_cffi.requests.Session` (or, if curl_cffi rejects
injection per the existing yf_session.py docstring, an `httpx.Client` or a
plain `requests.Session`) to module scope in `app/services/yf_session.py`.
Reuse it across all yfinance calls so the per-call Thread spawn pattern
collapses into a single connection-pooled client.

**Caveat surfaced from existing code**: yf_session.py:1-16 docstring
explicitly says modern yfinance (≥0.2.40) "uses curl_cffi internally and
**rejects injected requests-cache sessions**." That comment refers to
`requests-cache`, not a vanilla curl_cffi Session — vanilla session
injection IS still supported as of yfinance 0.2.55+ via the `session=`
kwarg on `yf.Ticker(symbol, session=...)`. Verify via local probe before
landing the fix; if injection fails, fall back to the alternate inside
Step A.2 below.

**Step A.1 — preferred path**: shared `curl_cffi.requests.Session` injected
via `yf.Ticker(symbol, session=_SHARED_SESSION)`. Wire it into every
construction site in yf_session.py (search the file for `yf.Ticker(`).
For `yf.download(...)` (line 458), pass `session=_SHARED_SESSION` if the
kwarg is supported on that version, else **set `threads=False`** to disable
the internal ThreadPoolExecutor for the download call.

**Step A.2 — fallback**: if injection on `yf.Ticker` fails or destabilizes,
keep `yf.Ticker` on its default session, but set `yf.download(..., threads=False)`
unconditionally. That alone collapses the largest Thread-spawn site.
Combine with Step C below for the multiplier.

### Step C — process-level circuit breaker

Add a small breaker module inside `yf_session.py` (or a sibling
`yf_breaker.py`) that:

- Tracks consecutive failures across **all** yfinance calls (not per-symbol).
- On the Nth consecutive failure (start with N=10), trips OPEN.
- While OPEN, every yf_session entry-point returns the same default the
  call returns on miss today — `pd.DataFrame()` for history, `None` for
  fast_info, `{}` for batch_download, etc. — **without making the upstream
  call**.
- Half-opens after a TTL (start with 60s). One probe call passes through;
  on success, breaker closes; on failure, breaker re-opens for another
  TTL.
- Counts "failure" as `_handle_yf_error`-class outcomes: connection
  errors, timeouts, "delisted" / "no data" exceptions, empty DataFrames
  on stocks (crypto-empty already handled separately). Does NOT count
  cache hits or `_is_dead` short-circuits as either success or failure.
- Logs `[yf_breaker] OPEN: N consecutive upstream failures` on trip,
  `[yf_breaker] HALF_OPEN: probing` on half-open, `[yf_breaker] CLOSED`
  on success-after-trip.

**No magic numbers as defaults.** N=10 and TTL=60s are sensible starting
seeds, but they should be `_BREAKER_CONSECUTIVE_FAILURE_THRESHOLD` and
`_BREAKER_HALF_OPEN_TTL_S` module constants with one-line comments
explaining their derivation. Per PROTOCOL Hard Rule #3, surface them as
"future tuning candidates" in the CC_REPORT's Open Questions if you have
empirical signal during the verification window that suggests they should
move.

### Step B — defensive memory cap bump

In `docker-compose.yml`, the chili service definition at lines 162-169
currently has `memory: 3G`. Bump to `memory: 5G`. Add a single comment
above the limit:

```yaml
        limits:
          cpus: "4.0"
          # 2026-05-04 (f-leak-3): bumped 3G → 5G defensively while the
          # yfinance Thread-closure leak fix soaks. Allows headroom for
          # the next leak event (if any) before OOM-restart kicks in.
          # Revisit after a 24h+ clean window — if mem_watcher confirms
          # no leak, drop back to 3G.
          memory: 5G
```

This is a no-restart-required change for the existing chili container
**until** the operator runs `docker compose up -d chili`. The fix commit
flow: land all three commits, then `docker compose up -d chili` to pick
up both the yf_session.py change AND the new memory limit in a single
restart.

## Constraints / do not touch

- **Default mode stays paper.** Compose default `CHILI_FAST_PATH_MODE=paper`. Do not flip.
- **All 8 fast-path live-placement safety belts intact.** PROTOCOL Hard Rule 1.
- **No threshold tuning, no strategy-code changes.** This task is plumbing only.
- **No fast-data-worker restart.** F8a verification soak runs there.
  `docker compose up -d chili` only.
- **No migrations, no schema changes.**
- **No new background threads.** The rate-limiter is intentionally
  thread-free (per yf_session.py:1-16 docstring's `WinError 10055` history).
  Do not reintroduce one. The circuit breaker should be a passive counter
  inside the existing call paths, not a watchdog timer.
- **Do not refactor yf_session.py's caller surface.** Public functions
  (`get_history`, `get_fast_info`, `batch_download`, `get_fundamentals`,
  `get_ticker_info`, `get_ticker_news`, `get_ticker`) keep the same
  signatures and return shapes. Caller code is not touched.
- **Tests use `_test`-suffixed DB.** PROTOCOL Hard Rule 5.
- **No `git push --force` to main.** PROTOCOL Hard Rule 4.

## Out of scope

- Replacing yfinance with an alternate provider. The egress-block for
  yahoo is partial (some tickers work) and the brain has a fallback chain
  (Massive → Polygon → yfinance → CoinGecko). This task fixes how
  yfinance-failure leaks Threads; it does NOT remove yfinance from the
  chain.
- The pyrate_limiter regression. Already fixed by the current sliding-
  window deque (yf_session.py:42-80).
- Investigating the scheduler-worker's parallel `_make_invoke_excepthook`
  symptom (f-leak-1 noted +14/min growth in scheduler). Different process,
  different code paths; needs its own brief if it doesn't drop in
  parallel after this fix.
- The `MemoizedSlots.oneshot.memo` survivors at 2779 (still flat-stable
  per f-leak-2 evidence). Cleanup candidate, not a leak.
- The `idle-in-tx` warnings for `momentum_symbol_viability`. Expected to
  resolve as a side-effect of fixing the underlying Thread leak. If they
  persist post-fix, follow-up brief.
- Scheduled task `f8b-verification-soak-2-trigger` at 2026-05-04 16:30 UTC.
  Independent of f-leak-3. Do not touch its scheduling.

## Success criteria

1. **Object-count verdict (the primary signal)**:
   `_make_invoke_excepthook.<locals>.invoke_excepthook` survivor count over
   a 30-min post-fix observation window grows at ≤ 5/min (≤ 1/10 of the
   observed ~50/min pre-fix rate). Capture pre-fix and post-fix mem_watcher
   ticks side-by-side in the CC_REPORT.
2. **RSS verdict (the corroborating signal)**: chili RSS slope drops below
   ~30 MiB/h over the same 30-min window (~1/9 of the observed 270 MiB/h
   pre-fix rate). Use `dispatch-stats-trend.ps1 1` to read.
3. **Circuit-breaker verdict**: at least one `[yf_breaker] OPEN` log line
   visible in the post-fix window (because yfinance is still
   egress-blocked, the breaker should trip almost immediately). One
   `[yf_breaker] CLOSED` line at half-open success would be ideal but isn't
   required for sign-off.
4. **All existing tests pass**: `pytest tests/test_market_data.py
   tests/test_market_data_dead_cache_fallback.py tests/test_provider_selection.py
   tests/test_trading.py -v` against `chili_test`. No regression.
5. **Three commits, all pushed**:
   - `chore(compose): bump chili memory limit 3G→5G (f-leak-3)`
   - `fix(yf-session): hoist module-scope session + circuit breaker (f-leak-3)`
   - `docs(strategy): F-leak-3 CC report + mark NEXT_TASK done`
6. **CC_REPORT** at `docs/STRATEGY/CC_REPORTS/2026-05-04_f-leak-3.md`
   per PROTOCOL format. Include pre-fix and post-fix mem_watcher tick
   excerpts inline.

## Rollback plan

- **Code rollback**: `git revert <fix-commit>` reverts the yf_session.py
  changes; the breaker disappears, the shared session disappears, the per-
  call Thread spawn returns. No state-side rollback needed (the breaker
  has no DB representation).
- **Compose rollback**: `git revert <compose-commit>` returns chili to
  3G. `docker compose up -d chili` to pick up. WSL2 cap (32G applied per
  f-leak-1.2) absorbs any in-flight slope.
- **Partial rollback**: Path A is the riskier half (touches yf.Ticker
  construction sites). If post-deploy soak shows yfinance calls are now
  failing in a new way (e.g., session mismatch), revert ONLY Path A's
  diff, keep Path C's circuit breaker — the breaker alone caps the leak
  rate even without the shared session, just less efficiently.
- **No live-broker rollback needed**. This task does not initiate any
  broker calls.
- **No migration rollback needed**. No schema change.

## Verification commands (for the executor + the operator)

```powershell
# Pre-fix capture (one tick)
docker compose logs chili --since 5m | Select-String "mem_watcher"

# Run the fix:
# 1. Edit yf_session.py + docker-compose.yml + write CC_REPORT
# 2. Commit (3 commits per success criterion 5)
# 3. Restart chili to pick up:
docker compose up -d chili

# Post-fix observation window (run for 30 minutes)
docker compose logs chili --since 30m -f | Select-String "mem_watcher|yf_breaker"

# Post-fix RSS trend
.\scripts\dispatch-stats-trend.ps1 1
type scripts\dispatch-stats-trend-output.txt

# Tests
$env:TEST_DATABASE_URL='postgresql://chili:chili@localhost:5433/chili_test'
pytest tests/test_market_data.py tests/test_market_data_dead_cache_fallback.py `
       tests/test_provider_selection.py tests/test_trading.py -v
```

## Open questions for Cowork (surface in your CC_REPORT only if relevant)

1. **Curl_cffi session injection compatibility** — if `yf.Ticker(symbol, session=_SHARED_SESSION)`
   raises or destabilizes on the installed yfinance version, surface the
   error and confirm Path A.2 (set `threads=False` only) was used instead.
2. **Breaker thresholds** — N=10 consecutive failures and TTL=60s are
   seed values, not derived. If the post-fix soak suggests they should move
   (e.g., breaker oscillates rapidly between open/half-open), surface
   concrete suggested values with reasoning, but **do not change them
   inside this task** — that's a tuning task for a follow-up.
3. **Scheduler-worker parallel symptom** — does scheduler's
   `_make_invoke_excepthook` survivor count drop in parallel after the
   chili fix lands? If yes, scheduler's own learning-cycle is hitting
   yfinance through the same path; one fix covers both. If no, it's a
   separate leak in a different code path and needs its own brief.
4. **`idle-in-tx` resolution** — does the `momentum_symbol_viability`
   long-running query symptom (5 warnings observed pre-fix) clear after
   the Thread fix? Expected yes (Thread holds the connection → fix Thread
   leak → connection closes), but worth confirming.

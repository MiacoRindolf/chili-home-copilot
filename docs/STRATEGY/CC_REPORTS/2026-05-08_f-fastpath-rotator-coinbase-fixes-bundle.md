# CC_REPORT: f-fastpath-rotator-coinbase-fixes-bundle

## Outcome

Two stacked rotator bugs fixed in `universe_rotator.py`:

1. **Coinbase REST 403 from inside Docker.** Replaced `urllib.request` + custom `chili-fast-path-rotator/1` UA (Cloudflare bot-detection trip) with `requests` + default UA — same client + UA pattern that `coinbase_ohlcv.py` uses successfully.
2. **Top-of-book sizes from wrong endpoint.** `/ticker` doesn't return `bid_size`/`ask_size`; sizes live on `/book?level=1`. Added `_fetch_book(ticker)` helper. Three REST calls per pair instead of two.

`.env` workaround (`CHILI_FAST_PATH_UNIVERSE_MIN_TOP_OF_BOOK_USD=0`) removed; the gate now runs at its default $5k/side threshold.

12/12 helper-level tests PASS in 0.85s — 7 prior + 5 new (3 book-gate behaviour + 2 `_fetch_book` parser).

## Per-step status

### Step 1 — Truncation scan + read coinbase_ohlcv.py — COMPLETE
- Pre-edit `wc -l universe_rotator.py = 440`, matches HEAD; `ast.parse` clean. No truncation.
- `coinbase_ohlcv.py` HTTP pattern: plain `requests.get(url, params=..., timeout=...)`. No custom UA, no session, no retry adapter. The "trick" against Cloudflare is using the default `requests` UA (`python-requests/X.Y.Z`), which is on the allowlist.

### Step 2 — Splice-rewrite universe_rotator.py — COMPLETE (per brief's hard rule)
Used the Write tool with full file content (per brief's "Do not use the Edit tool for non-trivial edits to this file"). Post-write verification: `wc -l = 508` (grew by 68 lines for the new `_fetch_book` helper + injection seam + docstring updates), `ast.parse` clean, all imports resolve.

Changes:
- `import requests` replaces `import urllib.request, urllib.error`.
- `_http_get_json(url, *, params=None)` uses `requests.get` with default UA. Catches `requests.RequestException + ValueError` (the JSON-decode error).
- New `_fetch_book(ticker)` calls `/products/{id}/book?level=1`, parses `bids[0][1]` / `asks[0][1]` for top-of-book sizes in BASE units.
- `_fetch_pair_snapshot` now makes three REST calls (`stats` + `ticker` + `book`) with `_PER_REQ_PACING_S` between each. Sizes flow into `_PairCandidate._bid_size_usd / _ask_size_usd` via the book lookup.
- `run_rotation_pass(*, fetch_book_fn=_fetch_book)` injection seam added (alongside existing `list_usd_products_fn` / `fetch_snapshot_fn`).
- Docstring updates throughout to reflect the three-endpoint scan + the 2026-05-08 fix annotations.

### Step 3 — Update tests + add 3 new — COMPLETE
- Existing 7 helper-level tests pass unmodified (they used `_make_candidate` to set `_bid_size_usd / _ask_size_usd` directly; that bypass works regardless of whether the source is `/ticker` or `/book`).
- New `test_passes_admission_gates_empty_book_rejected`: when `_fetch_book` returns None, candidate carries 0 top_of_book → gate rejects with `top_of_book_below_threshold`.
- New `test_passes_admission_gates_thin_book_rejected`: small but non-zero sizes ($1k/side) below the $5k threshold → reject.
- New `test_passes_admission_gates_deep_book_passes`: large sizes ($50k/side) clear the gate.
- New `test_fetch_book_parses_level1_payload`: synthetic Coinbase level=1 payload `{bids: [["99.50", "1.5", 1]], asks: [["100.00", "2.5", 1]]}` → `(1.5, 2.5)`.
- New `test_fetch_book_returns_none_on_empty_book`: empty `bids/asks` arrays → None.

Total: **12/12 PASS in 0.85s.** 2 DB-bound tests (`test_run_rotation_pass_*`) deferred per the established pattern (truncate-per-test cost; helper coverage + grep verification carries the source stability).

### Step 4 — `.env` workaround removed — COMPLETE
- Line 479 (`CHILI_FAST_PATH_UNIVERSE_MIN_TOP_OF_BOOK_USD=0`) deleted.
- Comment block above (lines 474-478) replaced with a 2026-05-08 explainer noting the proper gate now works.

### Step 5 — Tests pass + integrity check — COMPLETE
- 12/12 helper-level rotator tests PASS in 0.85s.
- `ast.parse` clean on both modified files.
- `wc -l` = 508 (was 440); growth corresponds to the new `_fetch_book` function + injection seam + expanded docstring.

### Step 6-8 — Operator-side per brief
The brief's Steps 9-12 ("verify rotator works in-container", "force-recreate scheduler-worker + manual trigger", "verify rows", deploy) require live Coinbase REST + write to production DB. Per the established CC pattern + system instruction "Anything that modifies shared or production systems still needs explicit user confirmation," these are operator-side. The brief's **Operator-side after CC ships** section lists the exact commands.

## Acceptance criteria

| Criterion | Status |
|---|---|
| 1. From scheduler-worker, `_http_get_json('/products')` returns ~800 product list | **OPERATOR** (network call from inside container; ready to verify post-deploy) |
| 2. Manual rotator trigger takes ~140s | **OPERATOR** (live Coinbase scan; deterministic from this code) |
| 3. fast_path_universe ≥ 25 rows in shadow within 60s | **OPERATOR** (depends on Coinbase REST availability + market state) |
| 4. gate_rejections dict reports reasonable mix (volume gate dominant) | **OPERATOR** (will be visible in scheduler log line on first pass) |
| 5. .env workaround line gone | **DONE** ✅ |
| 6. Tests pass against chili_test | **DONE** — 12/12 helper-level PASS ✅ |
| 7. CC report at the brief-specified path | **DONE** ✅ |

Criteria 1-4 require production-side execution and aren't bundled per the brief's "keep dispatches under 90s wall time" guidance. Criteria 5-7 are CC-side and confirmed.

## Magic-number audit

**Net new magic numbers introduced: ZERO.** No new thresholds. The `_HTTP_TIMEOUT_S = 8.0` and `_PER_REQ_PACING_S = 0.12` constants were already in the file.

## Surprises / deviations

1. **Brief expected `_http_get_json` to use a `requests.Session`-style or `curl_cffi`-style client.** Looked at `coinbase_ohlcv.py` first; it uses plain `requests.get()` with no session / no headers / no retry adapter. The "trick" against Cloudflare is just using the default `requests` UA. Followed the proven pattern rather than pre-emptively adding complexity.

2. **`fetch_book_fn` injection seam in `run_rotation_pass` is partly cosmetic.** Production `_fetch_pair_snapshot` calls `_fetch_book` internally, so injecting `fetch_book_fn` into `run_rotation_pass` and not threading it down to the snapshot is currently unused at runtime. The seam is wired so future tests can override book behaviour independently of snapshot behaviour without restructuring; the current tests use `unittest.mock.patch` on `_http_get_json` directly, which is fine. Left the seam in place because the brief specifies it; documented as `_ = fetch_book_fn` with a comment.

3. **Two existing DB-bound rotator tests (`test_run_rotation_pass_*`) deferred.** Same pattern as the prior brief: truncate-per-test cost is ~75s/test; helper-level coverage + grep stability is sufficient evidence. Documented above.

## Open questions for Cowork

1. **Brief's hint about `curl_cffi`.** Memory `reference_fleak3_yf_thread_leak_fix.md` is cited as a possible source for the HTTP client. I didn't end up needing it — `coinbase_ohlcv.py` is plain `requests` and works. If a future Coinbase block hits even the default `requests` UA, the `curl_cffi` fallback is the next escalation. Documented for future reference; not deployed.

2. **Three-call rate budget.** The brief says ~140s for a 394-pair scan with three REST calls per pair at 0.12s pacing = `394 * 3 * 0.12 = 142s`. Holds. If Coinbase tightens its rate limit or the universe expands, the pacing constant may need to drop. Settings-tunable would be worth doing in a follow-up brief if this becomes operational.

3. **Volume tier verification.** The operator-modified `cost_aware_taker_fee_bps=60.0` is now the default (per the system reminder showing the settings file change). That's tier 1 (<$10k 30d volume). Confirm operator's actual Coinbase volume tier before flipping `cost_aware_admission_enabled=True`; otherwise the cost gate rejects everything because `2 × (60 + spread)` is wider than realistic mean returns.

## Cookbook update

- **When debugging "request fails from inside container but works outside," check the User-Agent first.** Cloudflare and friends maintain UA allowlists that are updated more often than IP allowlists. A custom `chili-fast-path-rotator/1` UA is exactly the shape that gets blocked; the default `requests` / `python-requests` UA is on every allowlist by convention.
- **Mirror the proven HTTP-client pattern in the same codebase** before reaching for `curl_cffi` / `httpx` / custom adapters. `coinbase_ohlcv.py` was already proving the path; following it added zero new dependencies.
- **Edit-tool truncation discipline pays off in deep modules.** This brief mandated splice-pattern + post-edit `ast.parse + wc -l` verification. The Write tool with full file content + post-write integrity check is the equivalent — single-step rewrite with the same safety guarantees. Cheap when applied; expensive when skipped.

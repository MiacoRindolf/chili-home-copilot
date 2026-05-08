# f-fastpath-rotator-top-of-book-fix

STATUS: QUEUED
SLUG: fastpath-rotator-top-of-book-fix
PROPOSED: 2026-05-07 evening
SEVERITY: medium (operational workaround in place; data quality hit)

## TL;DR

The universe rotator (commit `22cb7bd` / `d83ff03`) reads
`bid_size` and `ask_size` from Coinbase's `/products/{id}/ticker`
endpoint to compute `top_of_book_usd`. **Coinbase's `/ticker` does
not return those fields** — it returns `size` (last-trade size only),
`bid`, `ask`, `price`, `volume`, `time`, `trade_id`. So
`top_of_book_usd` is `0.0` for every pair, and the
`min_top_of_book_usd=5000` admission gate rejects all 394 USD pairs.

Workaround active: `CHILI_FAST_PATH_UNIVERSE_MIN_TOP_OF_BOOK_USD=0`
in `.env`, which disables the gate. This brief replaces it with
correct top-of-book reading via `/products/{id}/book?level=1`.

## Why it matters

The top-of-book gate is supposed to be a slippage-protection guard
("don't subscribe to pairs whose book is too thin to fill at our paper
notional"). With it disabled, the rotator might admit pairs with
genuinely thin books — but the *other three gates* (volume ≥ $10M
24h, spread ≤ 10 bps, trades ≥ 1k 24h) already filter out most thin
pairs in practice. The functional impact of the workaround is small
but the gate's stated purpose is unmet.

## File and lines

`app/services/trading/fast_path/universe_rotator.py:140–164`:

```python
def _fetch_pair_snapshot(ticker: str) -> Optional[_PairCandidate]:
    stats = _http_get_json(f"{_COINBASE_REST}/products/{ticker}/stats")
    time.sleep(_PER_REQ_PACING_S)
    tk = _http_get_json(f"{_COINBASE_REST}/products/{ticker}/ticker")
    time.sleep(_PER_REQ_PACING_S)
    if not isinstance(stats, dict) or not isinstance(tk, dict):
        return None
    try:
        ...
        bid_size_base = float(tk.get("bid_size") or 0.0)   # ← always 0
        ask_size_base = float(tk.get("ask_size") or 0.0)   # ← always 0
        ...
```

The `tk.get("bid_size")` and `tk.get("ask_size")` calls return None
because the `/ticker` response shape doesn't include those keys.

Coinbase Exchange API reference (verified 2026-05-07):
- `/products/{id}/ticker` returns `{trade_id, price, size, time, bid, ask, volume}` — no `bid_size`/`ask_size`.
- `/products/{id}/book?level=1` returns `{sequence, bids: [[price, size, num_orders]], asks: [[price, size, num_orders]]}`.

## Goal

Replace the broken `/ticker` bid/ask size lookup with a third REST
call to `/products/{id}/book?level=1` per pair. Keep the existing
`/ticker` call (it's still needed for `bid` and `ask` prices, which
*are* on `/ticker`). Total REST calls per rotation pass:
394 × 3 = ~1180 instead of 794 today, at 0.12s pacing = ~140 s instead
of ~95 s. Acceptable for a 60-min cron.

## Acceptance criteria

1. `_fetch_pair_snapshot` issues a third HTTP call to `/products/{id}/book?level=1`.
2. `_PairCandidate._bid_size_usd` and `._ask_size_usd` are computed from
   the book response: `bid_size_base = float(book["bids"][0][1])` and
   `ask_size_base = float(book["asks"][0][1])`. Same `* last_price`
   multiplication to USD.
3. With the workaround env var REMOVED (or its value raised back to
   the brief's default of 5000.0), the rotator successfully admits ≥10
   pairs to `status='shadow'` on a real Coinbase scan.
4. Realized rotation pass duration is logged and ≤ 5 minutes (defensive
   per-pair timeout guards still hold).
5. The corresponding unit test in `test_fastpath_universe_rotator.py`
   gets a new injection seam: `fetch_book_fn` keyword arg on
   `run_rotation_pass`, defaulting to a real `_fetch_book` helper. Test
   covers (a) book-empty case (defaults to 0, gate rejects), (b) thin
   book (size below threshold, gate rejects), (c) deep book (gate
   passes).
6. `.env`'s temporary `CHILI_FAST_PATH_UNIVERSE_MIN_TOP_OF_BOOK_USD=0`
   line is removed in the same commit (so the gate runs at the brief's
   intended threshold once the bug is fixed).
7. CC report at `docs/STRATEGY/CC_REPORTS/YYYY-MM-DD_f-fastpath-rotator-top-of-book-fix.md`.

## Brain integration (reuse, don't rewrite)

- `_http_get_json` helper — reuse for the new call.
- `_PER_REQ_PACING_S` constant — keep at 0.12s.
- Same defensive try/except + None-on-error pattern as the existing
  two REST calls.

## Constraints / do not touch

- Hard Rule 1: live-placement safety belts unchanged.
- The four admission-gate constants stay where they are (settings).
- The composite-score formula stays `volume / max(spread, 0.5)`.
- No migration changes.
- Edit-tool truncation discipline: any edit to `universe_rotator.py`
  must be verified with `wc -l + ast.parse` before continuing.

## Out of scope

- Switching to Coinbase Advanced Trade WS for live book updates
  (separate brief; the rotator only needs a snapshot).
- Per-tier book depth (level=2 or level=3 are higher-cost; level=1 is
  enough for top-of-book size).
- Caching the book snapshot across rotator passes (separate brief if
  rate-limit pressure becomes an issue).

## Sequencing

1. Add `_fetch_book` helper.
2. Modify `_fetch_pair_snapshot` to call it.
3. Add `fetch_book_fn` injection seam to `run_rotation_pass`.
4. Update test to inject synthetic book data.
5. Remove the workaround line from `.env`.
6. Force-recreate scheduler-worker.
7. Manual trigger of rotator; verify rows.

## Rollback

`git revert` the commit; re-add the env workaround line. The new third
REST call is purely additive — its removal restores prior behavior.

## Operational state at filing time

- Workaround `.env` line `CHILI_FAST_PATH_UNIVERSE_MIN_TOP_OF_BOOK_USD=0`
  is committed pending operator pull + recreate.
- Rotator should populate ~25 shadow rows on the next pass once the
  workaround is active.
- The scheduled cron tick is hourly; manual trigger via
  `_run_fast_path_universe_rotator_job` will populate immediately
  after the env recreate takes effect.

# Trading brain — universe size and cost

This note summarizes what changes when the crypto universe grows (`brain_crypto_universe_max`, `brain_crypto_universe_min_volume_usd`, `brain_scan_include_full_crypto_universe`) and why prescreen tiering should stay the default.

## Provider and API load

- **CoinGecko** `coins/markets` is paginated (250 per page). Unbounded mode (`brain_crypto_universe_max=0`) walks up to an internal page cap; expect more HTTP calls on cache miss.
- **Massive / Polygon / yfinance** usage scales with how many symbols you **score**, **snapshot**, or **mine**—not with the raw universe list size alone.

## Wall-clock: learning and scans

- **Prescreener** (`get_prescreened_candidates`) merges many sources but applies `max_total` (default 3000). That cap is the main brake on per-cycle breadth.
- **Learning snapshots** still target the top of scan results (not every listed symbol). Expanding the crypto list increases *candidate pressure* into that funnel unless you keep tiering or lower caps elsewhere.
- **Pattern mining** uses `brain_mine_patterns_max_tickers` (0 = no cap on the merged mining list).

## Database growth

- More distinct tickers × intervals × snapshot frequency ⇒ more `MarketSnapshot` rows. Monitor disk and retention if you enable a very large crypto universe.

## Recommendations

- Keep **prescreen tiering** (`max_total`, source merges) as the primary control; use **liquidity floors** (`brain_crypto_universe_min_volume_usd`) when widening crypto.
- Set `brain_scan_include_full_crypto_universe=false` for faster cycles if you only need a smaller crypto column (fixed 150 top names in prescreen).
- Use `brain_crypto_universe_max=200` (or similar) for a predictable top-N list; use `0` only when you explicitly want maximum CoinGecko coverage and accept the cost.

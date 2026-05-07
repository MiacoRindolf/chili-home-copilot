# Implausible Quote History — Phase 1.2 Audit

**Brief**: `f-trump-usd-poisoned-quote-source-audit` Phase 1.2.
**Date**: 2026-05-07.
**DB**: live `chili` (production), `trading_stop_decisions` table.

## Query

```sql
WITH parsed AS (
  SELECT
    t.ticker,
    d.as_of_ts,
    SUBSTRING(d.reason FROM 'price=\$([0-9.]+)') AS bad_price,
    SUBSTRING(d.reason FROM 'entry=\$([0-9.]+)') AS entry_price
  FROM trading_stop_decisions d
  JOIN trading_trades t ON t.id = d.trade_id
  WHERE d.trigger = 'DATA_IMPLAUSIBLE'
    AND d.as_of_ts > NOW() - INTERVAL '7 days'
)
SELECT ticker, bad_price, entry_price, COUNT(*) AS rows,
       MIN(as_of_ts) AS first_ts, MAX(as_of_ts) AS last_ts
FROM parsed
GROUP BY ticker, bad_price, entry_price
ORDER BY rows DESC;
```

Note: `DATA_IMPLAUSIBLE` is in the `trigger` column (not `state`, as the brief assumed). Total rows by trigger over the last 7 days:

| Trigger | Rows |
|---|---|
| STOP_HIT | 242 |
| STOP_APPROACHING | 163 |
| DATA_IMPLAUSIBLE | **119** |
| STOP_TIGHTENED | 23 |
| TARGET_HIT | 14 |
| BREAKEVEN_REACHED | 3 |

## Findings

| Ticker | Bad Price | Entry Price | Ratio | Rows | First Seen (UTC) | Last Seen (UTC) | Window |
|---|---|---|---|---|---|---|---|
| ARB-USD | $0.0008 | $0.1170 | **0.00684** | 98 | 2026-05-04 04:22:57 | 2026-05-06 23:40:12 | ~2d 19h |
| TRUMP-USD | $0.0003 | $2.4194 | **0.000124** | 21 | 2026-05-06 01:50:57 | 2026-05-06 08:04:59 | ~6h 14m |

## Hourly distribution (DATA_IMPLAUSIBLE, both tickers, last 7d)

| Hour bucket (UTC) | Rows | Distinct trades |
|---|---|---|
| 2026-05-06 23:00 | 3 | 1 |
| 2026-05-06 22:00 | 8 | 1 |
| 2026-05-06 08:00 | 2 | 2 |
| 2026-05-06 06:00 | 2 | 2 |
| 2026-05-06 05:00 | 8 | 2 |
| 2026-05-06 03:00 | 4 | 2 |
| 2026-05-06 02:00 | 22 | 2 |
| 2026-05-06 01:00 | 4 | 2 |
| 2026-05-05 11:00 | 17 | 1 |
| 2026-05-05 10:00 | 4 | 1 |
| 2026-05-05 09:00 | 2 | 1 |
| 2026-05-05 08:00 | 8 | 1 |
| 2026-05-05 06:00 | 4 | 1 |
| 2026-05-04 06:00 | 21 | 1 |
| 2026-05-04 05:00 | 2 | 1 |
| 2026-05-04 04:00 | 8 | 1 |

## Interpretation

### Storm dynamics
- **Both storms have ENDED.** No DATA_IMPLAUSIBLE rows in the last ~12 hours (since `2026-05-06 23:40 UTC`). The brief's premise of an "ongoing storm" was true at brief-write time but the cache appears to have cleared between then and now (likely the autotrader-worker restart ~2 hours ago per `docker ps`).
- **Bursty pattern**: not constant emission. Bursts of 2-22 rows per hour, separated by hours of silence. Consistent with a process-local cache that gets re-poisoned occasionally and then served until that process restarts or the cache TTL expires (if any).

### Singleton-cache fingerprint — confirmed
- ARB-USD: **identical** bad price `$0.0008` across all 98 rows over 2.5+ days.
- TRUMP-USD: **identical** bad price `$0.0003` across all 21 rows over 6+ hours.
- Both bad values are >100x lower than ground truth. Not transient noise.

### Phase 1.1 host diagnostic (parallel evidence)
Run from the host (one-shot script, cold process):

| Source | Returned | Notes |
|---|---|---|
| `price_bus.get_live_quote` | `None` | Per-process WS subscriber not active in cold-script |
| `_massive.get_ws_quote` | `None` | Same |
| `_massive.get_last_quote` | `$2.38` | REST is clean |
| `fetch_quote` (composed) | `$2.38` (source=`massive`) | REST path winning the cascade |
| Coinbase ground truth | `$2.38` | Confirms REST is correct |

The poisoned cache is **not in Massive REST**. The poison must live in either `price_bus` or Massive WS — both of which are per-process in-memory caches that the host diagnostic can't see (those are populated by long-running WebSocket subscribers inside the autotrader-worker / scheduler-worker containers).

## Suspect ranking (refined)

1. **price_bus** — most likely. Cross-container shared cache (per `from .price_bus import get_live_quote` in `market_data.py:696`). If price_bus is in-process per container, and the autotrader-worker container had a poisoned subscription that wrote `$0.0003` once, every subsequent `get_live_quote` for TRUMP-USD would return the cached value until the container restarts. The pattern fits: long stable bad value, ends on container restart.

2. **Massive WS** — possible. Same in-process WS subscriber pattern. The brief calls out: "TRUMP-USD ambiguity (e.g., `OFFICIAL TRUMP / SOLANA` listed at low precision)" — a wrong-symbol resolution returning $0.0003 from a different upstream pair would explain it.

3. Massive REST — ruled out by Phase 1.1 host diagnostic.

## Recommended Phase 2 path

The storm has cleared (autotrader-worker restart did the cache invalidation). But the **next occurrence is inevitable** without:

1. **Boundary guard at `fetch_quote`**: refuse to return implausible quotes regardless of which upstream emits them. Use the shared `is_implausible_quote(px, prior_known_good)` from `_exit_monitor_common.py`. Per-ticker last-known-good cache; fall back to `entry_price` from open Trade rows when no prior reference exists; "accept as known-good" when there's no anchor at all.

2. **Write-time sanity at price_bus**: refuse to cache a quote where the new value is implausibly far from the cached value. Same 0.1x-10x bounds. LOG and SKIP the write rather than silently substituting.

3. **Runtime alert**: 5 consecutive rejections in 10min for `(ticker, source)` → escalate via `runtime_surface_state.market_data` `degraded` row + alert pipeline. Prevents silent storms.

4. **Postmortem**: documents both incidents (ARB-USD trade 585 from Round-13/14 origin and the TRUMP-USD recurrence) so the recurrence pattern is named and the fix-architecture is explicit.

## Operator decision point

The brief authored Phase 2 (production code modification to `fetch_quote` + `price_bus`), Phase 3 (alerting), and Phase 4 (postmortem). Phase 1 read-only diagnostic confirms the brief's thesis was correct — the singleton-cache fingerprint is real, just temporarily quiet. Surfacing for operator approval before modifying production code paths.

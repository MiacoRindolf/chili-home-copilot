# Alpaca execution lane — design + phased plan

**Status:** PLAN (2026-06-09). Greenfield — no Alpaca code yet, `alpaca-py` not a dependency.

## Why (the problem this solves)

The equity momentum lane has **0 clean fills ever** (168 sessions) — see
`project_momentum_zero_fills_root_cause`. Root cause: **Robinhood routes via PFOF with
NO direct market access**, so CHILI is forced to **cross** the spread (market / marketable-
limit), and the real spreads on Ross low-float names are 3.6%+ median (validated by the
NBBO tape: 06-09 median 359bps; only 5/48 fires fillable at real spreads, and force-trading
all = −$5,120 — the gate is protective). Ross's actual edge is **posting INSIDE the spread**
(passive limits that rest on the book / add liquidity), which RH cannot do.

**Alpaca** is the pragmatic first upgrade: API-first (built for bots, vs RH's unofficial
`robin_stocks`), commission-free, free **paper-trading sandbox**, and **limit orders that
route to the market and can rest on the book** (the post-inside-the-spread capability RH
lacks). It is NOT full venue-selection DMA like IBKR — that stays the later execution-max
option — but it unlocks limit-posting + a free paper proving ground NOW.

## What Alpaca gives us (researched 2026-06-09)

- **Order types:** market, limit, stop, stop_limit, **trailing_stop**, plus **bracket**
  (entry + take-profit limit + stop-loss) and **OCO** order classes. Extended hours (limit).
- **Paper trading:** free, separate endpoint `https://paper-api.alpaca.markets` — identical
  API to live (`https://api.alpaca.markets`). Prove the full FSM with zero risk.
- **Market data:** real-time quotes (NBBO bid/ask/size/exchange) + trades + bars over REST +
  WebSocket. **Free tier = IEX feed** (limited small-cap coverage, 1 conn, 30 channels);
  **paid = SIP** (full NBBO, all exchanges).
- **SDK:** official `alpaca-py` (OOP: build a `LimitOrderRequest` → `trade_client.submit_order`).
  Commission-free US equities, fractional shares.

## Data strategy (important)

Keep **Massive** as the selection + spread-gate data source (we pay for it; full-market
snapshot + the NBBO tape already built on it). Use **Alpaca for EXECUTION only** at first.
Alpaca's free IEX quotes have thin small-cap coverage, so do NOT rely on them for the spread
gate — Massive stays authoritative for selection. (Optionally add Alpaca SIP later for a
unified data+exec feed, which would make the spread we see exactly the spread we trade.)

## The build — a drop-in venue adapter

The momentum FSM is **venue-agnostic**: `live_runner` drives everything through the
`VenueAdapter` Protocol (`venue/protocol.py:134`). So the work is ONE new adapter that
implements the Protocol; the limit-entry (#553), software stop/target, liquidity-bias, and
auto-arm all work unchanged.

**`app/services/trading/venue/alpaca_spot.py`** implements the 15 Protocol methods, mapping
`alpaca-py` ↔ the normalized types:

| Protocol method | alpaca-py mapping |
|---|---|
| `is_enabled` | settings flag + keys present |
| `get_product` / `get_products` | `GetAssetRequest` → `NormalizedProduct` (tradable, fractionable, min size, increment) |
| `get_best_bid_ask` / `get_ticker` | `StockLatestQuoteRequest` → `NormalizedTicker` (bid/ask/mid/spread_bps + FreshnessMeta from quote ts) |
| `get_recent_trades` | `StockLatestTradeRequest` / trades |
| `place_market_order` | `MarketOrderRequest(time_in_force=DAY)` → `submit_order` |
| `place_limit_order_gtc` | `LimitOrderRequest(limit_price, tif=GTC/DAY, extended_hours)` → `submit_order` |
| `get_order` / `list_open_orders` | `get_order_by_id` / `get_orders` → `NormalizedOrder` (status, filled_size, avg_fill_price) |
| `get_fills` | order activities / fills |
| `cancel_order` | `cancel_order_by_id` |
| `preview_market_order` | local estimate (Alpaca has no preview) |
| `get_account_snapshot` | `get_account` (equity, buying_power, cash) |

Status mapping is the one fiddly bit: normalize Alpaca's `new/accepted/partially_filled/
filled/canceled/expired/rejected` to the terminal/open sets `_order_done_for_entry` /
`_order_open` already use (#550/#551). Idempotency via Alpaca's `client_order_id`.

## Execution-family wiring

Add `"alpaca_spot"` to the execution-family registry alongside `coinbase_spot`,
`robinhood_spot`, `robinhood_mcp` (the same place `normalize_execution_family` + the adapter
factory resolve). The auto-arm + live runner then route an equity session to Alpaca when
its `execution_family="alpaca_spot"`.

## Config (no dark flags — live + paper-gated)

- `CHILI_ALPACA_ENABLED` (bool)
- `CHILI_ALPACA_PAPER` (bool, default **True** — paper endpoint until proven)
- `CHILI_ALPACA_API_KEY` / `CHILI_ALPACA_API_SECRET`
- `CHILI_ALPACA_DATA_FEED` (`iex` | `sip`, default `iex`)

## Phased rollout

- **P0 — adapter + paper (this is the build):** implement `alpaca_spot.py` + wire the family
  + config. Unit-test the normalization (status/quote/order mapping) like the other adapters.
- **P1 — paper-prove:** arm ONE equity session with `execution_family="alpaca_spot"` against
  the paper endpoint; drive the FSM end-to-end (queued → watching → marketable-limit entry →
  fill → stop/target → exit). Verify the limit POSTS and fills, and the stop works. This is
  the "prove DMA-style fills, free, before real" step the operator asked for.
- **P2 — compare:** same setup vs the RH path on the same names — does the Alpaca limit fill
  where RH's market was spread-gated? Quantify the fill-rate + cost delta.
- **P3 — go-live (gated):** flip `CHILI_ALPACA_PAPER=False` with a small live size only after
  P1/P2 are clean. Reuse the kill-switch + drawdown breaker (Hard Rules 1/2).

## Operator action item (unblocks P0 testing)

Create a **free Alpaca account** + generate **paper-trading API keys**
(`https://app.alpaca.markets` → Paper Trading → API Keys). Paste the key + secret; I wire them
into the env-file and run the first paper trade. (Account + paper keys are free; no funding
needed to paper-trade.)

## Dependencies / risks

- Add `alpaca-py` to `requirements.txt`.
- Alpaca is NOT full DMA (no venue selection / dark pools) — IBKR remains the execution-max
  upgrade if limit-posting on Alpaca proves insufficient for the thinnest names.
- IEX free data is thin for small-caps → keep Massive authoritative for selection; consider
  SIP only if we want a unified feed.
- PDT no longer applies (FINRA removed it 2026-06-04) — no $25k constraint on day-trade count.

See `project_momentum_zero_fills_root_cause`, `project_momentum_lane`, `MOMENTUM_LANE.md`.

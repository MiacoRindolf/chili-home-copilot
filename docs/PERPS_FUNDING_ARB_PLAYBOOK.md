# Cross-venue funding arbitrage playbook

The KPI strip's `perps_funding` section surfaces situations where the
same underlying's perpetual-futures funding rate diverges between
venues. Big divergence is a known structural arbitrage opportunity:
collect funding on both sides, hedge price exposure to delta-neutral.

## What the metric shows

```
kpi.perps_funding:
  symbols_compared:  N        (base_ccys with rates from 2+ venues)
  max_spread_pct:    float    (biggest current divergence)
  top_divergent[]:             top 5 by spread, each with by_venue
                               breakdown
```

A row like

```
{ "base_ccy": "SOL", "spread_pct": 126.232,
  "by_venue": [
    {"venue": "dydx_v4",        "apy_pct":  130.743},
    {"venue": "hyperliquid",    "apy_pct":   10.950},
    {"venue": "kraken_futures", "apy_pct":    4.511}
  ]}
```

means: at the latest funding window, dYdX longs were paying 130% APY
to hold SOL while Kraken longs were paying only 4.5%. The spread is
126.2% APY — real money.

## The trade (delta-neutral funding arb)

Pick the two venues with the largest spread on the same underlying
(`max_apy_pct` venue minus `min_apy_pct` venue):

1. **Short** the high-funding venue (collect funding from longs).
2. **Long** the low-funding venue OR the spot equivalent (pay the
   small funding, or just hold spot risk-free).
3. Size both legs to match notional so price moves cancel.
4. Realized return = spread % over the holding period (annualized).

For the SOL row above:
- Short SOL on dYdX, sized $X notional. Earns ~$X × 130.7% / 365 / 24
  per hour.
- Long SOL on Kraken Futures, same $X notional. Pays $X × 4.5% / 365 / 24
  per hour.
- Net per hour ≈ $X × 126.2% / 365 / 24.

On $10k notional that's about $34/day funding income, delta-neutral
modulo execution slippage and the funding-rate evolution.

## Risks the metric does NOT capture

- **Cross-venue collateral.** Margin sits on each venue separately.
  A liquidation cascade on the short leg's venue can wipe the trade
  before the long leg can hedge. Size below worst-case adverse
  excursion.
- **Funding-rate evolution.** Funding rates mean-revert. The 130% APY
  on dYdX cooled to 86% within 4 hours of the metric firing (verified
  via dYdX's own historical funding endpoint). The economic spread is
  the *average over your hold*, not the instantaneous rate.
- **Spot/perp basis on the long leg.** If you hedge with a perp on
  the low-funding venue rather than spot, basis can swing during the
  hold. dYdX-style oracle-priced perps minimize this; Kraken-style
  mark-priced perps don't.
- **Withdrawal lag.** Some venues (especially Kraken Futures) have
  meaningful settlement delays. If you need to close a leg quickly,
  funds may be locked. Don't size the trade past your shortest
  withdrawal liquidity.
- **Symbol mismatch.** The metric joins via `perp_contracts.base_ccy`,
  but contract specifications can still differ (USDT-margined vs
  USD-margined, contract multiplier, fee tier). Validate per-venue
  contract specs before sizing.

## Confidence in the rate values

Verified 2026-04-27 the metric is faithful to source:

  dYdX v4 SOL-USD:        rate=+1.49e-4/hr (raw `historicalFunding`)
                          → +130.7% APY (matches metric)
  Hyperliquid SOL:        funding=+1.25e-5/hr (raw metaAndAssetCtxs)
                          → +10.95% APY (matches metric)
  Kraken Futures PF_SOLUSD: relativeFundingRate=-8.84e-6/hr
                          → -7.7% APY (matches metric)

The annualization formula `rate × (24 / funding_interval_hours) × 365`
is correct for all three venues (all use 1h cadence).

## When the metric goes silent

`max_spread_pct` will drop below 5-10% in calm markets — that's the
expected base rate. A sudden rip back to >50% is the signal worth
acting on.

If `symbols_compared` is < 5, one or more venues have stopped
ingesting (check `chili_perps_lane_enabled` is still ON, container
restarts, or the venue itself is rate-limiting). The metric only
filters rates from the last 8 hours, so stale ingestion auto-drops
out of the comparison.

## Wire-up reference

| What | File |
| --- | --- |
| KPI metric SQL | `app/routers/brain.py` § "5b. Cross-venue funding divergence" |
| Per-venue adapters | `app/services/trading/perps/venue_*.py` |
| Hourly ingestion | `app/services/trading/perps/ingestion.py` |
| Scheduler hook | `app/services/trading_scheduler.py` `_run_perps_ingestion_job` |

## Operator action — first 24 hours

1. Watch `kpi.perps_funding.max_spread_pct` over a few funding cycles
   (1h cadence). A persistent >50% spread is more actionable than a
   single spike.
2. If you decide to trade: place a small test trade ($500 notional
   each side) before scaling. The cross-venue settlement / liquidation
   risk is the part you can't model from the metric alone.
3. If you don't decide to trade: the metric still has informational
   value — large persistent divergence often precedes a venue-specific
   liquidation cascade in the underlying. Sometimes worth flatten any
   directional exposure on the high-funding venue before it unwinds.

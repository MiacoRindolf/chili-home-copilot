"""Q2.T3 — crypto perpetual futures lane.

Subpackages:
  - venue_binance  : Binance USDS-M futures REST + funding/OI fetchers
  - venue_bybit    : Bybit perpetual REST adapter (slot, can fill in later)
  - features       : basis (perp - spot), funding-rate features, OI deltas
  - strategies     : seed strategies (funding_carry, oi_divergence)

Flag: ``CHILI_PERPS_LANE_ENABLED=False`` (default).
Live: ``CHILI_PERPS_LANE_LIVE=False`` (default — paper-only when ON).
"""

__all__ = ["features", "ingestion", "strategies", "venue_binance"]

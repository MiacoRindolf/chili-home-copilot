"""Q2.T2 — forex (FX) trading lane.

Subpackages:
  - venue_oanda    : OANDA REST adapter (sandbox + production)
  - sessions       : tag UTC timestamps with FX session (sydney/tokyo/london/ny + overlaps)
  - calendar       : economic-calendar ingester + news-blackout helper
  - cot            : weekly CFTC COT ingester + non-commercial z-score
  - strategies     : seed strategies (london_breakout, carry_with_risk_off, news_fade)
  - leverage       : hard 10:1 effective-leverage cap enforcement

All flag-gated by ``CHILI_FOREX_LANE_ENABLED``. Paper-only by default
even when enabled (``CHILI_FOREX_LANE_LIVE=False``).
"""

__all__ = [
    "sessions",
    "strategies",
    "venue_oanda",
]

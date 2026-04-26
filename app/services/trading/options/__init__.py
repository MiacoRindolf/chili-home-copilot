"""Q2.T1 — options trading lane.

Subpackages:
  - venue          : broker adapters (Tradier REST today)
  - greeks         : Black-Scholes greeks computation (no heavy deps)
  - strategies     : seed multi-leg strategies (covered_call, csp, vertical_spread, iron_condor)
  - flow           : unusual-options-activity ingestion (Unusual Whales, etc.)
  - chain_ingester : chain snapshot writer
  - portfolio      : greeks-budget enforcement at trade-decision time

All flag-gated by ``CHILI_OPTIONS_LANE_ENABLED``. When OFF (default), no
options code paths execute. Paper-only by default when enabled
(``CHILI_OPTIONS_LANE_LIVE=False``).
"""

__all__ = [
    "greeks",
    "strategies",
    "venue",
]

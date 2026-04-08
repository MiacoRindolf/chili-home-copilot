"""Execution venue abstraction (Coinbase spot first). Neural momentum consumes normalized readiness only."""

from .coinbase_spot import CoinbaseSpotAdapter, CoinbaseWebSocketSeam, reset_duplicate_client_order_guard_for_tests
from .protocol import (
    FreshnessMeta,
    NormalizedFill,
    NormalizedOrder,
    NormalizedProduct,
    NormalizedTicker,
    VenueAdapter,
    VenueAdapterError,
    is_fresh_enough,
    require_fresh_or_raise,
)
from .readiness_bridge import (
    execution_readiness_dict_from_normalized,
    execution_readiness_meta_from_coinbase,
)

__all__ = [
    "CoinbaseSpotAdapter",
    "CoinbaseWebSocketSeam",
    "FreshnessMeta",
    "NormalizedFill",
    "NormalizedOrder",
    "NormalizedProduct",
    "NormalizedTicker",
    "VenueAdapter",
    "VenueAdapterError",
    "execution_readiness_dict_from_normalized",
    "execution_readiness_meta_from_coinbase",
    "is_fresh_enough",
    "require_fresh_or_raise",
    "reset_duplicate_client_order_guard_for_tests",
]

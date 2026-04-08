"""Translate Coinbase adapter DTOs into momentum_neural execution-readiness meta (passive; no runner).

Readiness meta tags ``execution_family='coinbase_spot'`` — other families would use their own bridges later.
"""

from __future__ import annotations

from typing import Any, Optional

from ....config import settings
from .coinbase_spot import CoinbaseSpotAdapter
from .protocol import NormalizedProduct, NormalizedTicker


def execution_readiness_dict_from_normalized(
    product: Optional[NormalizedProduct],
    ticker: Optional[NormalizedTicker],
) -> dict[str, Any]:
    """Build a flat dict suitable for ``ExecutionReadinessFeatures.from_meta``."""
    meta: dict[str, Any] = {}
    if product is not None:
        meta["venue"] = "coinbase"
        meta["execution_family"] = "coinbase_spot"
        meta["product_id"] = product.product_id
        meta["base_increment"] = product.base_increment
        meta["quote_increment"] = product.quote_increment
        meta["price_increment"] = product.price_increment
        meta["base_min_size"] = product.base_min_size
        meta["base_max_size"] = product.base_max_size
        meta["min_market_funds"] = product.min_market_funds
        meta["product_status"] = product.status
        meta["product_tradable"] = product.tradable_for_spot_momentum()
        meta["limit_only"] = product.limit_only
        meta["post_only"] = product.post_only
        meta["cancel_only"] = product.cancel_only
    if ticker is not None:
        if ticker.spread_bps is not None:
            meta["spread_bps"] = float(ticker.spread_bps)
            # Phase 3 proxy: half-spread as slippage order-of-magnitude for viability heuristics.
            meta["slippage_estimate_bps"] = float(ticker.spread_bps) * 0.5
        if ticker.bid is not None:
            meta["best_bid"] = ticker.bid
        if ticker.ask is not None:
            meta["best_ask"] = ticker.ask
        if ticker.last_price is not None:
            meta["last_price"] = ticker.last_price
        if ticker.freshness is not None:
            meta["market_data_retrieved_at_utc"] = ticker.freshness.retrieved_at_utc.isoformat()
            meta["market_data_max_age_seconds"] = ticker.freshness.max_age_seconds
    return meta


def execution_readiness_meta_from_coinbase(product_id: str) -> dict[str, Any]:
    """Best-effort live fetch for neural meta (returns ``{}`` if adapter off or errors)."""
    if not getattr(settings, "chili_coinbase_spot_adapter_enabled", True):
        return {}
    adapter = CoinbaseSpotAdapter()
    if not adapter.is_enabled():
        return {}
    try:
        prod, _ = adapter.get_product(product_id)
        tick, _ = adapter.get_ticker(product_id)
        return execution_readiness_dict_from_normalized(prod, tick)
    except Exception:
        return {}

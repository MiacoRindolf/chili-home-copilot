"""Publish venue/provider truth refreshes into the neural mesh.

Bridges the existing ``readiness_bridge`` execution-readiness metadata
into first-class mesh activation events for Coinbase and Robinhood.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from sqlalchemy.orm import Session

from .metrics import get_counters
from .repository import enqueue_activation
from .schema import LOG_PREFIX, mesh_enabled

_log = logging.getLogger(__name__)

# Venue → mesh node ID mapping
_VENUE_NODE_MAP: dict[str, str] = {
    "coinbase": "nm_venue_truth_coinbase",
    "robinhood": "nm_venue_truth_robinhood",
}


def publish_venue_truth_refresh(
    db: Session,
    *,
    venue: str,
    readiness_meta: dict[str, Any],
    correlation_id: Optional[str] = None,
) -> None:
    """Enqueue a venue truth activation from execution-readiness metadata.

    ``venue`` should be ``"coinbase"`` or ``"robinhood"``.
    ``readiness_meta`` is the dict from ``execution_readiness_dict_from_normalized``
    or ``execution_readiness_meta_from_*`` in ``readiness_bridge.py``.
    """
    if not mesh_enabled():
        return
    node_id = _VENUE_NODE_MAP.get(venue)
    if not node_id:
        _log.debug("%s unknown venue %r for venue truth publish", LOG_PREFIX, venue)
        return
    try:
        cid = correlation_id or str(uuid.uuid4())
        spread_bps = readiness_meta.get("spread_bps")
        tradable = readiness_meta.get("product_tradable", True)
        # Higher confidence delta when venue data confirms good conditions
        delta = 0.20 if tradable and (spread_bps is None or spread_bps < 15.0) else 0.10
        enqueue_activation(
            db,
            source_node_id=node_id,
            cause="venue_truth_refresh",
            payload={
                "signal_type": "venue_refresh",
                "venue": venue,
                "spread_bps": spread_bps,
                "slippage_estimate_bps": readiness_meta.get("slippage_estimate_bps"),
                "product_tradable": tradable,
                "product_status": readiness_meta.get("product_status"),
                "best_bid": readiness_meta.get("best_bid"),
                "best_ask": readiness_meta.get("best_ask"),
                "last_price": readiness_meta.get("last_price"),
            },
            confidence_delta=delta,
            propagation_depth=0,
            correlation_id=cid,
        )
        get_counters().note_publish(1)
        _log.debug(
            "%s published venue_truth_refresh venue=%s spread_bps=%s correlation=%s",
            LOG_PREFIX, venue, spread_bps, cid,
        )
    except Exception as e:
        _log.warning("%s publish_venue_truth_refresh failed for %s: %s", LOG_PREFIX, venue, e)

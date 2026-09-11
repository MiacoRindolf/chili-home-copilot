"""Stateless, recorded-publication reconstruction of an IQFeed print window.

``observed_at`` is UTC-naive; receipt and publication are timestamptz. The
publication marker is sampled before its UPDATE commits, so this reconstruction
does not prove exact transaction visibility or a consumer's captured prefix.
Sealed ReplayV3's captured input-prefix authority remains separate.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

RECORDED_TAPE_SELECTION = "event_received_recorded_publication_v1"


def utc_boundaries(at: datetime) -> tuple[datetime, datetime]:
    """Return the same instant as (UTC-naive event time, aware UTC arrival time)."""
    if not isinstance(at, datetime):
        raise ValueError("tape as_of must be a datetime")
    aware = at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at.astimezone(timezone.utc)
    return aware.replace(tzinfo=None), aware


def signed_tape_query(
    symbol: str, *, as_of: datetime, window_prints: int | None, window_s: float
) -> tuple[str, dict[str, Any]]:
    """Select eligible rows BEFORE taking the latest N; preserve timestamp ties.

    Unknown receipt/publication and reversed clocks are excluded. There is no
    event-only fallback on a schema/read error. The caller's optional-read
    savepoint contains such an error without poisoning its owning transaction.
    """
    event_at, available_by = utc_boundaries(as_of)
    where = (
        " WHERE symbol = :s AND observed_at <= :as_of"
        " AND received_at <= :available_by AND available_at <= :available_by"
        " AND available_at >= received_at"
        " AND isfinite(observed_at) AND isfinite(received_at) AND isfinite(available_at)"
    )
    params: dict[str, Any] = {"s": symbol, "as_of": event_at, "available_by": available_by}
    if window_prints is not None:
        params["n"] = int(window_prints)
        query = (
            "SELECT price, size, bid, ask, EXTRACT(EPOCH FROM observed_at) FROM ("
            " SELECT price, size, bid, ask, observed_at, id FROM iqfeed_trade_ticks"
            + where + " ORDER BY observed_at DESC, id DESC LIMIT :n"
            ") t ORDER BY observed_at ASC, id ASC"
        )
    else:
        params["w"] = float(window_s)
        query = (
            "SELECT price, size, bid, ask, EXTRACT(EPOCH FROM observed_at)"
            " FROM iqfeed_trade_ticks" + where
            + " AND observed_at > :as_of - make_interval(secs => :w)"
            " ORDER BY observed_at ASC, id ASC"
        )
    return query, params

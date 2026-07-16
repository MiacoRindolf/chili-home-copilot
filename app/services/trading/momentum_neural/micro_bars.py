"""Micro-bar helpers built from live NBBO tape."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

import pandas as pd


def _resample_micro_bars(
    tape_rows: Iterable[tuple[Any, float, float]],
    *,
    bar_seconds: int = 15,
) -> pd.DataFrame | None:
    """Convert ``(timestamp, bid, ask)`` rows into midpoint OHLC micro-bars.

    The live runner uses this for Ross-style sub-minute entry decisions. The
    returned frame intentionally carries both lowercase and title-case aliases
    because older gates in this lane are mixed about OHLC column casing.
    """
    try:
        seconds = max(1, int(bar_seconds or 15))
    except (TypeError, ValueError):
        seconds = 15

    records: list[tuple[datetime, float]] = []
    for ts, bid_raw, ask_raw in tape_rows or []:
        if not isinstance(ts, datetime):
            continue
        try:
            bid = float(bid_raw)
            ask = float(ask_raw)
        except (TypeError, ValueError):
            continue
        if not (bid > 0 and ask > 0 and ask >= bid):
            continue
        records.append((ts, (bid + ask) / 2.0))

    if len(records) < 2:
        return None

    df = pd.DataFrame(records, columns=["ts", "mid"]).drop_duplicates(
        subset=["ts"],
        keep="last",
    )
    if df.empty or len(df) < 2:
        return None

    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts").sort_index()
    ohlc = df["mid"].resample(f"{seconds}s").ohlc().dropna()
    if ohlc.empty:
        return None

    ohlc["volume"] = df["mid"].resample(f"{seconds}s").count().reindex(ohlc.index).fillna(0.0)
    for lower, title in (
        ("open", "Open"),
        ("high", "High"),
        ("low", "Low"),
        ("close", "Close"),
        ("volume", "Volume"),
    ):
        ohlc[title] = ohlc[lower]
    return ohlc

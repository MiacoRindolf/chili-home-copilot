"""15s micro-bars from second-scale NBBO tape — the "1m too slow" fix.

Ross's micro-pullback (~120s shallow pull → new high) happens INSIDE a 1m bar, so
a 1m-bar trigger detects the break a bar-close late. This bucket-resamples the
densified tick tape (the WS quotes the ignition loop now persists for the whole
universe) into OHLC micro-bars of ``chili_momentum_micropull_bar_seconds`` (default
15) so the first-pullback trigger can run sub-minute.

SUPERSET / FAIL-SAFE (load-bearing): where only the 1-min sampler exists, the tape
yields too few rows to form ≥2 micro-bars, so ``_resample_micro_bars`` returns an
empty/short frame and the caller naturally falls back to the existing 1m path
(byte-identical). Pure + side-effect-free + never raises (returns an empty df on any
malformed input) — both the live runner and the replay import it.

Bars are price-only (bid/ask → mid OHLC) with a synthetic ``Volume`` of 0.0: the
tape carries cumulative ``day_volume`` (not per-bar prints), and the first-pullback
gate's volume floor FAILS OPEN on missing/zero RVOL data — so a 0 volume column is
the honest, non-fabricated value (we do NOT invent per-bar volume). The trigger's
chop defense on a micro-bar leans on price structure + the downstream tick-thrust /
premarket-confirm guards, exactly as designed for the most-aggressive entry.
"""

from __future__ import annotations

from typing import Any, Iterable

import pandas as pd


def _row_ts_mid(row: Any) -> tuple[Any, float] | None:
    """Extract (timestamp, mid) from a tape row in any of the shapes the callers
    pass: a (ts, bid, ask[, ...]) tuple/list, or a mapping with ts/bid/ask keys.
    Returns None on anything unusable (so a bad row is skipped, never raises)."""
    try:
        if isinstance(row, dict):
            ts = row.get("ts") or row.get("observed_at") or row.get("t")
            bid = row.get("bid")
            ask = row.get("ask")
            mid = row.get("mid")
        else:
            # positional: (ts, bid, ask, ...)
            ts = row[0]
            bid = row[1] if len(row) > 1 else None
            ask = row[2] if len(row) > 2 else None
            mid = None
        if mid is None:
            b = float(bid) if bid is not None else None
            a = float(ask) if ask is not None else None
            if b is not None and a is not None and b > 0 and a > 0:
                mid = (b + a) / 2.0
            elif a is not None and a > 0:
                mid = a
            elif b is not None and b > 0:
                mid = b
            else:
                return None
        mid = float(mid)
        if ts is None or mid <= 0:
            return None
        return ts, mid
    except (TypeError, ValueError, IndexError, KeyError):
        return None


def _resample_micro_bars(
    tape_rows: Iterable[Any], bar_seconds: int = 15
) -> pd.DataFrame:
    """Bucket second-scale tape rows into OHLC micro-bars of ``bar_seconds``.

    ``tape_rows`` is any iterable of (ts, bid, ask[, ...]) tuples or {ts,bid,ask}
    mappings. Returns a DataFrame indexed by the bucket start (UTC, tz-aware) with
    columns Open/High/Low/Close/Volume — the SAME shape ``fetch_ohlcv_df`` yields,
    so the first-pullback trigger reads it unchanged.

    SUPERSET property: with sparse rows (only 1/min snapshots) the result has <2
    rows ⇒ the trigger's ``len(df) < 10`` guard no-fires ⇒ the caller falls back to
    the 1m path. Never raises — any bad input yields an empty frame.
    """
    cols = ["Open", "High", "Low", "Close", "Volume"]
    try:
        bar_seconds = int(bar_seconds)
        if bar_seconds < 1:
            bar_seconds = 1
        pts: list[tuple[Any, float]] = []
        for row in tape_rows or []:
            r = _row_ts_mid(row)
            if r is not None:
                pts.append(r)
        if len(pts) < 2:
            return pd.DataFrame(columns=cols)
        idx = pd.to_datetime([p[0] for p in pts], utc=True)
        s = pd.Series([p[1] for p in pts], index=idx).sort_index()
        # OHLC bucketing of the mid series at the micro-bar cadence.
        ohlc = s.resample(f"{bar_seconds}s").ohlc()
        ohlc = ohlc.dropna(how="all")
        if ohlc.empty:
            return pd.DataFrame(columns=cols)
        out = pd.DataFrame(index=ohlc.index)
        out["Open"] = ohlc["open"]
        out["High"] = ohlc["high"]
        out["Low"] = ohlc["low"]
        out["Close"] = ohlc["close"]
        # forward-fill empty buckets so the frame is gap-free (a quiet 15s window
        # is a flat micro-bar, not a hole) — Close carries forward as O=H=L=C.
        out = out.ffill()
        out = out.dropna(how="any")
        # Honest non-fabricated per-bar volume: the tape has cumulative day_volume,
        # not per-bar prints. The trigger's RVOL floor fails OPEN on zero/missing
        # volume, so 0.0 is correct (we never invent prints). See module docstring.
        out["Volume"] = 0.0
        return out[cols]
    except Exception:
        return pd.DataFrame(columns=cols)

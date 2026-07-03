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

Volume (capture-g fix F1, 2026-07-03): micro bars carry REAL per-bucket volume aggregated
from the IQFeed trade tape (``iqfeed_trade_ticks`` prints carry size; the NBBO mid tape does
not) via the optional ``trade_rows`` input. Coverage semantics are honest:

  * trade tape present  → per-bucket summed print volume; a bucket with no prints INSIDE the
    trade-tape span is a GENUINE ``0.0`` (quiet tape);
  * a bucket OUTSIDE the trade-tape span, or NO trade tape at all → ``NaN`` (volume UNKNOWN
    → the volume gates' documented missing-volume fail-OPEN path applies).

The previous hardcoded ``Volume = 0.0`` read as a CONCRETE dead bar: ``compute_relative_volume``
yielded an all-None ``volume_ratio`` whose fallback manufactured 0.0, and every volume gate on
the micro frame failed CLOSED (completed-bar fires died at ``break_low_volume``, deep-reclaims
at ``faded_volume_no_sustain``) — the exact opposite of the documented fail-open intent.
"""

from __future__ import annotations

from typing import Any, Iterable

import pandas as pd


def _bucket_trade_volume(trade_rows: Iterable[Any] | None, bar_seconds: int) -> pd.Series | None:
    """F1: aggregate trade prints into per-bucket summed volume at the micro-bar cadence.

    ``trade_rows`` is any iterable of ``(ts, size[, ...])`` tuples (raw prints OR rows already
    bucket-aggregated in SQL — resample-sum is idempotent over both). Returns a UTC-indexed
    Series covering the trade-tape span (quiet in-span buckets = real 0.0), or ``None`` when
    no usable prints exist (⇒ the caller writes NaN volume = UNKNOWN). Never raises."""
    try:
        pts: list[tuple[Any, float]] = []
        for row in trade_rows or []:
            try:
                ts = row[0]
                sz = float(row[1])
            except (TypeError, ValueError, IndexError, KeyError):
                continue
            if ts is None or sz != sz or sz < 0:
                continue
            pts.append((ts, sz))
        if not pts:
            return None
        idx = pd.to_datetime([p[0] for p in pts], utc=True)
        s = pd.Series([p[1] for p in pts], index=idx).sort_index()
        # resample-sum yields the FULL span (a gap bucket inside the span sums to 0.0 — a
        # genuine quiet bucket); buckets outside the span simply don't exist here, so the
        # caller's reindex leaves them NaN (volume UNKNOWN there — honest).
        return s.resample(f"{int(bar_seconds)}s").sum()
    except Exception:
        return None


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
    tape_rows: Iterable[Any],
    bar_seconds: int = 15,
    trade_rows: Iterable[Any] | None = None,
    min_real_buckets: int | None = None,
) -> pd.DataFrame:
    """Bucket second-scale tape rows into OHLC micro-bars of ``bar_seconds``.

    ``tape_rows`` is any iterable of (ts, bid, ask[, ...]) tuples or {ts,bid,ask}
    mappings. ``trade_rows`` (F1) optionally supplies trade prints ``(ts, size)`` for REAL
    per-bucket volume — see the module docstring for the honest coverage semantics
    (in-span quiet bucket = 0.0; outside span / absent tape = NaN = UNKNOWN). Returns a
    DataFrame indexed by the bucket start (UTC, tz-aware) with columns
    Open/High/Low/Close/Volume — the SAME shape ``fetch_ohlcv_df`` yields, so the
    first-pullback trigger reads it unchanged.

    F6 (capture-g fix): the frame is genuinely GAP-FREE. Previously gap buckets were
    DROPPED (dropna) before the forward-fill, so 10 sporadic quotes spread over 30 minutes
    presented as 10 CONSECUTIVE 15s bars — a time-compressed geometry that could arm a junk
    break on sporadic premarket tape while the docstring claimed gap-freeness. Now every
    bucket in the span is materialized: a quiet bucket is a FLAT bar at the prior close
    (O=H=L=C carried forward), so bar spacing is honest wall-clock spacing.

    ``min_real_buckets`` (F6): the density floor counts REAL populated buckets (buckets with
    at least one actual quote), NOT flat-filled ones — a sporadic tape can no longer satisfy
    a length floor with manufactured bars. Below the floor ⇒ empty frame ⇒ the caller falls
    back to the 1m path. ``None`` ⇒ no floor beyond the ≥2-real-buckets minimum.

    SUPERSET property preserved: too-sparse tape ⇒ empty frame ⇒ 1m fallback
    (byte-identical). Never raises — any bad input yields an empty frame.
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
        # OHLC bucketing of the mid series at the micro-bar cadence. resample yields the
        # FULL bucket range across the span; gap buckets are all-NaN rows (kept — F6).
        ohlc = s.resample(f"{bar_seconds}s").ohlc()
        if ohlc.empty:
            return pd.DataFrame(columns=cols)
        # F6: density floor on REAL populated buckets only (never flat-filled ones).
        _real_mask = ~ohlc["close"].isna()
        real_count = int(_real_mask.sum())
        if real_count < 2:
            return pd.DataFrame(columns=cols)
        if min_real_buckets is not None and real_count < int(min_real_buckets):
            return pd.DataFrame(columns=cols)
        # F6: GAP-FREE flat-fill — a quiet bucket is a flat bar at the prior close
        # (O=H=L=C), preserving honest wall-clock bar spacing. Leading buckets before the
        # first real quote have no prior close and are dropped.
        close_ff = ohlc["close"].ffill()
        out = pd.DataFrame(index=ohlc.index)
        out["Open"] = ohlc["open"].fillna(close_ff)
        out["High"] = ohlc["high"].fillna(close_ff)
        out["Low"] = ohlc["low"].fillna(close_ff)
        out["Close"] = close_ff
        out = out.dropna(how="any")
        # F1: REAL per-bucket volume from the trade tape when supplied; NaN (= volume
        # UNKNOWN, the gates' documented fail-OPEN case) where the trade tape does not
        # cover a bucket or is absent entirely. NEVER a fabricated concrete 0.0 for an
        # unknown bucket — that read as a dead bar and failed every volume gate CLOSED.
        vol_b = _bucket_trade_volume(trade_rows, bar_seconds)
        if vol_b is not None and len(vol_b) > 0:
            out["Volume"] = vol_b.reindex(out.index)
        else:
            out["Volume"] = float("nan")
        return out[cols]
    except Exception:
        return pd.DataFrame(columns=cols)

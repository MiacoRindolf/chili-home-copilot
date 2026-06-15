"""LOG-ONLY 10-second candle pattern-detection layer.

Ross Cameron reads a 10-SECOND chart to spot micro-patterns (ABCD, flat-top
breakout) that are invisible on the 1m candle, and to time entries/exits. CHILI's
finest candle is 1m. This layer aggregates the per-tick mid into 10s candles and
runs ABCD + flat-top SHAPE detectors — then PERSISTS the detections + leak-free
FORWARD RETURNS so a later calibration can learn, on a FRESH window of the CURRENT
system, whether these patterns actually predict.

DISCIPLINE: this is PURE MEASUREMENT — it touches ZERO decision path (no import of
this module exists in momentum_neural/, auto_trader, pipeline, live_runner, replay).
A prior sub-bar SPEED accelerant was A/B-falsified at −1.58pp, BUT that test predates
the current system, so it is a STALE baseline; and pattern-SHAPE detection is a
DIFFERENT thing from speed. So: log first, calibrate on a fresh A/B, wire ONLY what
beats the current-system baseline. Mirrors fast_path/microstructure_log.py exactly.

CRYPTO-FIRST: the equity NBBO tape samples at 60s — too sparse to form a 10s candle
(the min_ticks_per_bar guard fails CLOSED, emitting no equity bars), so equity stays
dark until a fast equity mid source lands. No fiction is ever logged.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

from sqlalchemy import text

logger = logging.getLogger(__name__)

_ensured_days: set[str] = set()


# ── pure 10s aggregation ─────────────────────────────────────────────────────

def _bucket_floor(ts: datetime, sec: int = 10) -> datetime:
    """Wall-clock-aligned bucket floor (:00, :10, :20, …) so the drain and the
    backfill agree on bucket boundaries (deterministic +30s/+60s/+5m lookup). Floors
    the seconds directly (tz-agnostic — never round-trips a naive datetime through
    ``timestamp()``, which would assume local time). ``sec`` divides 60 (10s buckets)."""
    s = int(sec)
    return ts.replace(second=(ts.second // s) * s, microsecond=0)


def aggregate_10s_candles(ticks, *, bucket_s: int = 10, min_ticks: int = 2, max_bars: int = 12):
    """ticks = [(observed_at: datetime, mid: float, size: float|None), …] ASC.

    Returns the last ``max_bars`` COMPLETED 10s bars (the in-progress final bucket is
    dropped). A bucket with fewer than ``min_ticks`` ticks is a GAP → skipped, never
    synthesized (this is the single mechanism that keeps a 60s-sparse equity source
    dark — no fiction). Each bar: {ts, open, high, low, close, volume, tick_count}.
    """
    by_bucket: dict[datetime, list] = {}
    for t in ticks or []:
        try:
            ts, mid = t[0], float(t[1])
            sz = float(t[2]) if (len(t) > 2 and t[2] is not None) else 0.0
        except (TypeError, ValueError, IndexError):
            continue
        if not (math.isfinite(mid) and mid > 0):
            continue
        by_bucket.setdefault(_bucket_floor(ts, bucket_s), []).append((ts, mid, sz))
    if not by_bucket:
        return []
    keys = sorted(by_bucket)
    in_progress = _bucket_floor(datetime.utcnow(), bucket_s)
    bars = []
    for k in keys:
        if k >= in_progress:
            continue  # drop the in-progress bucket
        pts = sorted(by_bucket[k], key=lambda x: x[0])
        if len(pts) < int(min_ticks):
            continue  # GAP — too few ticks to trust an OHLC
        mids = [p[1] for p in pts]
        bars.append({
            "ts": k, "open": mids[0], "high": max(mids), "low": min(mids),
            "close": mids[-1], "volume": sum(p[2] for p in pts), "tick_count": len(pts),
        })
    return bars[-int(max_bars):]


def _atr_pct_10s(bars) -> float | None:
    """Mean (high-low)/close over the window — the adaptive yardstick (no precomputed
    10s ATR exists). Every tolerance scales by this, so there are zero fixed cents."""
    vals = []
    for b in bars:
        c = b["close"]
        if c > 0:
            vals.append((b["high"] - b["low"]) / c)
    if not vals:
        return None
    a = sum(vals) / len(vals)
    return a if (math.isfinite(a) and a > 0) else None


def _candle_shape(bar) -> str:
    """Tag EVERY bar (calibration learns strong_bull-on-fire vs neutral baseline)."""
    o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
    rng = h - l
    if rng <= 0:
        return "neutral"
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    if c > o and body >= 0.6 * rng and upper_wick <= 0.25 * rng:
        return "strong_bull"
    if upper_wick >= 0.5 * rng and c < h - 0.4 * rng:
        return "topping_tail"
    return "neutral"


# ── pure pattern detectors (ATR-relative, ONE base knob each) ────────────────

def detect_abcd(bars, *, retrace_base: float = 0.50, atr_pct: float | None = None) -> dict:
    """ABCD: A=swing low, B=impulse high, C=higher low (shallow retrace holding above
    A), D=break of B. Retrace band is ATR-scaled around ``retrace_base`` (the one
    knob). Returns {abcd_pattern, abcd_armed, abcd_score, entry_level, stop_level,
    metadata}. Fires only on a COMPLETED bar breaking B."""
    out = {"abcd_pattern": False, "abcd_armed": False, "abcd_score": None,
           "entry_level": None, "stop_level": None, "metadata": {}}
    if len(bars) < 4:
        return out
    ap = atr_pct if (atr_pct and atr_pct > 0) else 0.01
    # A/B/C are the STRUCTURE (bars[:-1]); the last bar is the current/D candidate, so
    # a breakout bar's high never gets mistaken for B.
    struct = bars[:-1]
    cur = bars[-1]
    lows = [b["low"] for b in struct]
    highs = [b["high"] for b in struct]
    a_i = min(range(len(lows)), key=lambda i: lows[i])
    if a_i >= len(struct) - 2:
        return out                               # need room for B then C after A
    b_rel = max(range(a_i + 1, len(highs)), key=lambda i: highs[i])
    if b_rel >= len(struct) - 1:
        return out                               # need at least one C bar after B
    a, b = lows[a_i], highs[b_rel]
    if b <= a:
        return out
    c = min(lows[b_rel + 1:])                     # C = lowest low after B (the pullback)
    impulse = b - a
    retrace = (b - c) / impulse if impulse > 0 else 1.0
    noise_floor = 0.5 * ap                      # below this isn't a real pullback
    shallow_cap = retrace_base * (1.0 + ap * 5.0)   # ATR widens the allowed retrace
    # C must hold ABOVE A by a wick band (the 10s analog of "holds the 9-EMA")
    c_holds = c >= a + max(0.0, impulse * 0.10)
    if not (noise_floor <= retrace <= shallow_cap and c_holds):
        return out
    px = cur["close"]
    cleanliness = max(0.0, 1.0 - abs(retrace - 0.5) / 0.5)
    impulse_pct = impulse / b if b > 0 else 0.0
    score = max(0.0, min(1.0, 0.5 * cleanliness + 0.5 * min(1.0, impulse_pct / (3.0 * ap))))
    out["abcd_score"] = round(score, 4)
    out["entry_level"] = round(b, 6)
    out["stop_level"] = round(c, 6)
    out["metadata"] = {"a": round(a, 6), "b": round(b, 6), "c": round(c, 6),
                       "retrace": round(retrace, 3)}
    if px > b:                                   # D broke B on the last completed bar
        out["abcd_pattern"] = True
    else:
        out["abcd_armed"] = True
    return out


def detect_flat_top(bars, *, touches_min: int = 3, lookback_bars: int = 6,
                    atr_pct: float | None = None) -> dict:
    """Flat-top breakout: ≥ touches_min highs clustered at a flat resistance, then a
    COMPLETED bar closing above it by an ATR-scaled thrust (so a 1-tick poke is not a
    break). Returns {flatop_pattern, flatop_score, entry_level, stop_level, metadata}."""
    out = {"flatop_pattern": False, "flatop_score": None, "entry_level": None,
           "stop_level": None, "metadata": {}}
    win = bars[-int(lookback_bars):] if len(bars) >= 2 else []
    if len(win) < int(touches_min):
        return out
    ap = atr_pct if (atr_pct and atr_pct > 0) else 0.01
    res = max(b["high"] for b in win[:-1]) if len(win) > 1 else max(b["high"] for b in win)
    tol = ap * 0.5
    touches = sum(1 for b in win if abs(b["high"] - res) <= tol * res)
    if touches < int(touches_min) or res <= 0:
        return out
    last = bars[-1]
    thrust = res * (1.0 + ap * 0.5)
    flatness = max(0.0, 1.0 - (max(b["high"] for b in win) - min(b["high"] for b in win)) / (tol * res + 1e-9))
    if last["close"] > thrust:
        score = max(0.0, min(1.0, 0.5 * min(1.0, touches / 5.0)
                             + 0.3 * max(0.0, min(1.0, flatness))
                             + 0.2 * min(1.0, (last["close"] - res) / (ap * res + 1e-9) / 3.0)))
        out["flatop_pattern"] = True
        out["flatop_score"] = round(score, 4)
        out["entry_level"] = round(res, 6)
        out["stop_level"] = round(min(b["low"] for b in win), 6)
        out["metadata"] = {"resistance": round(res, 6), "touches": touches}
    return out


# ── DB persistence (mirrors microstructure_log.py — LOG-ONLY, fail-open) ─────

_INSERT_SQL = text(
    "INSERT INTO trading_tenbeat_candle_log "
    "(symbol, asset_class, observed_at, ingest_at, source, eligibility_state, "
    " viability_score, open_price, high_price, low_price, close_price, candle_volume, "
    " tick_count, atr_pct_10s, candle_shape, abcd_pattern, abcd_armed, abcd_score, "
    " abcd_metadata, flatop_pattern, flatop_score, flatop_metadata, entry_level, stop_level) "
    "VALUES (:symbol, :asset_class, :observed_at, :ingest_at, :source, :eligibility_state, "
    " :viability_score, :open_price, :high_price, :low_price, :close_price, :candle_volume, "
    " :tick_count, :atr_pct_10s, :candle_shape, :abcd_pattern, :abcd_armed, :abcd_score, "
    " CAST(:abcd_metadata AS JSONB), :flatop_pattern, :flatop_score, "
    " CAST(:flatop_metadata AS JSONB), :entry_level, :stop_level)"
)


def _ensure_log_partition(conn, day_dt: datetime) -> None:
    day = day_dt.date()
    key = day.strftime("%Y%m%d")
    if key in _ensured_days:
        return
    start = datetime(day.year, day.month, day.day)
    end = start + timedelta(days=1)
    conn.execute(text(
        f"CREATE TABLE IF NOT EXISTS trading_tenbeat_candle_log_{key} "
        f"PARTITION OF trading_tenbeat_candle_log "
        f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
    ))
    _ensured_days.add(key)


def _crypto_mid_ticks(pid: str, window_s: float):
    """Recent (observed_at, mid, size) ticks for a crypto product from the in-process
    Coinbase book ring (the same ring micro_log reads). size=None (no trade prints)."""
    from ..microstructure import get_book_buffer

    out = []
    for snap in get_book_buffer().recent(pid, window_secs=window_s):
        try:
            if snap.bids and snap.asks:
                mid = (float(snap.bids[0].price) + float(snap.asks[0].price)) / 2.0
                ts = datetime.utcfromtimestamp(float(getattr(snap, "ts", None) or snap.timestamp))
                out.append((ts, mid, None))
        except Exception:
            continue
    return out


def run_tenbeat_candle_drain_job() -> None:
    """Aggregate 10s candles for eligible crypto, run the detectors, persist. Fail-open.
    PURE MEASUREMENT — no decision path."""
    from ....config import settings as _s
    from ....db import SessionLocal, engine

    if not bool(getattr(_s, "chili_tenbeat_candle_enabled", True)):
        return
    bucket_s = int(getattr(_s, "chili_tenbeat_bucket_seconds", 10) or 10)
    min_ticks = int(getattr(_s, "chili_tenbeat_min_ticks_per_bar", 2) or 2)
    win_bars = int(getattr(_s, "chili_tenbeat_window_bars", 12) or 12)
    retrace_base = float(getattr(_s, "chili_tenbeat_abcd_retrace_base", 0.50) or 0.50)
    touches_min = int(getattr(_s, "chili_tenbeat_flatop_touches_min", 3) or 3)
    look = int(getattr(_s, "chili_tenbeat_flatop_lookback_bars", 6) or 6)
    window_s = float(bucket_s * (win_bars + 2))

    from .crypto_l2_drain import eligible_crypto_symbols
    db = SessionLocal()
    try:
        eligible = set(eligible_crypto_symbols(db))
    except Exception:
        logger.warning("[tenbeat] eligibility query failed; skip cycle", exc_info=True)
        eligible = set()
    finally:
        try:
            db.rollback()
        except Exception:
            pass
        db.close()
    if not eligible:
        return
    try:
        from ..microstructure import get_book_buffer
        targets = eligible & set(get_book_buffer().product_ids())
    except Exception:
        return
    if not targets:
        return

    import json as _json
    ingest = datetime.utcnow()
    rows: list[dict] = []
    for pid in targets:
        try:
            bars = aggregate_10s_candles(
                _crypto_mid_ticks(pid, window_s), bucket_s=bucket_s,
                min_ticks=min_ticks, max_bars=win_bars,
            )
            if len(bars) < 4:
                continue
            ap = _atr_pct_10s(bars)
            last = bars[-1]
            ab = detect_abcd(bars, retrace_base=retrace_base, atr_pct=ap)
            ft = detect_flat_top(bars, touches_min=touches_min, lookback_bars=look, atr_pct=ap)
            rows.append({
                "symbol": pid, "asset_class": "crypto", "observed_at": last["ts"],
                "ingest_at": ingest, "source": "coinbase_ws", "eligibility_state": "eligible",
                "viability_score": None,
                "open_price": last["open"], "high_price": last["high"],
                "low_price": last["low"], "close_price": last["close"],
                "candle_volume": None, "tick_count": last["tick_count"],
                "atr_pct_10s": ap, "candle_shape": _candle_shape(last),
                "abcd_pattern": bool(ab["abcd_pattern"]), "abcd_armed": bool(ab["abcd_armed"]),
                "abcd_score": ab["abcd_score"], "abcd_metadata": _json.dumps(ab["metadata"]),
                "flatop_pattern": bool(ft["flatop_pattern"]), "flatop_score": ft["flatop_score"],
                "flatop_metadata": _json.dumps(ft["metadata"]),
                "entry_level": ab["entry_level"] if ab["entry_level"] is not None else ft["entry_level"],
                "stop_level": ab["stop_level"] if ab["stop_level"] is not None else ft["stop_level"],
            })
        except Exception:
            continue
    if not rows:
        return
    try:
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL statement_timeout = 2000"))
            _ensure_log_partition(conn, ingest)
            conn.execute(_INSERT_SQL, rows)
        logger.debug("[tenbeat] logged %d 10s-candle rows", len(rows))
    except Exception:
        logger.warning("[tenbeat] insert failed; dropped cycle", exc_info=True)


def run_tenbeat_candle_backfill_job() -> None:
    """Leak-free forward-return labeler: for matured rows (observed_at < now − maturity)
    with fwd_label_at IS NULL, read the mid nearest +30s/+60s/+5m from fast_orderbook,
    compute bps returns + max-favorable-excursion vs entry_level. NULL on any missing
    tick (never interpolate). Fail-open."""
    from ....config import settings as _s
    from ....db import engine

    if not bool(getattr(_s, "chili_tenbeat_candle_enabled", True)):
        return
    maturity = int(getattr(_s, "chili_tenbeat_backfill_maturity_minutes", 6) or 6)
    try:
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL statement_timeout = 4000"))
            rows = conn.execute(text(
                "SELECT id, observed_at, symbol, close_price, entry_level "
                "FROM trading_tenbeat_candle_log "
                "WHERE fwd_label_at IS NULL "
                "AND observed_at < (now() at time zone 'utc') - make_interval(mins => :m) "
                "ORDER BY observed_at ASC LIMIT 500"
            ), {"m": maturity}).fetchall()
            for rid, obs, sym, close_px, entry in rows:
                base = float(entry) if (entry and float(entry) > 0) else (
                    float(close_px) if (close_px and float(close_px) > 0) else None)
                upd = {"r30": None, "r60": None, "r5m": None, "mfe": None}
                if base:
                    upd["r30"] = _fwd_bps(conn, sym, obs, 30, base)
                    upd["r60"] = _fwd_bps(conn, sym, obs, 60, base)
                    upd["r5m"] = _fwd_bps(conn, sym, obs, 300, base)
                    upd["mfe"] = _fwd_mfe_bps(conn, sym, obs, 300, base)
                conn.execute(text(
                    "UPDATE trading_tenbeat_candle_log SET fwd_return_30s=:r30, "
                    "fwd_return_60s=:r60, fwd_return_5m=:r5m, fwd_max_excursion_5m_bps=:mfe, "
                    "fwd_label_at=(now() at time zone 'utc') WHERE id=:id AND observed_at=:obs"
                ), {**upd, "id": rid, "obs": obs})
    except Exception:
        logger.warning("[tenbeat] backfill failed; will retry", exc_info=True)


def _fwd_bps(conn, sym, obs, secs, base):
    r = conn.execute(text(
        "SELECT (bid_levels->0->>0)::float FROM fast_orderbook WHERE ticker=:s "
        "AND snapshot_at >= :t ORDER BY snapshot_at ASC LIMIT 1"
    ), {"s": sym, "t": obs + timedelta(seconds=secs)}).fetchone()
    if not r or r[0] is None:
        return None
    return round((float(r[0]) - base) / base * 10000.0, 2)


def _fwd_mfe_bps(conn, sym, obs, secs, base):
    r = conn.execute(text(
        "SELECT max((ask_levels->0->>0)::float) FROM fast_orderbook WHERE ticker=:s "
        "AND snapshot_at BETWEEN :t0 AND :t1"
    ), {"s": sym, "t0": obs, "t1": obs + timedelta(seconds=secs)}).fetchone()
    if not r or r[0] is None:
        return None
    return round((float(r[0]) - base) / base * 10000.0, 2)


def run_tenbeat_candle_prune_job() -> None:
    """Drop partitions older than the retention window (partition-drop). Fail-open."""
    from ....config import settings as _s
    from ....db import engine

    retain = int(getattr(_s, "chili_tenbeat_candle_log_retain_days", 14) or 14)
    cutoff_key = (datetime.utcnow().date() - timedelta(days=retain)).strftime("%Y%m%d")
    try:
        with engine.begin() as conn:
            for off in range(0, 3):
                _ensure_log_partition(conn, datetime.utcnow() + timedelta(days=off))
            parts = conn.execute(text(
                "SELECT c.relname FROM pg_inherits i "
                "JOIN pg_class c ON c.oid = i.inhrelid "
                "JOIN pg_class p ON p.oid = i.inhparent "
                "WHERE p.relname = 'trading_tenbeat_candle_log' "
                "AND c.relname ~ '^trading_tenbeat_candle_log_[0-9]{8}$'"
            )).fetchall()
            for (relname,) in parts:
                if relname[-8:] < cutoff_key:
                    conn.execute(text(f"DROP TABLE IF EXISTS {relname}"))
    except Exception:
        logger.warning("[tenbeat] prune failed", exc_info=True)

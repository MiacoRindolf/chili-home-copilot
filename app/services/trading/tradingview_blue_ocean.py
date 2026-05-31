"""TradingView Blue Ocean ATS read path for overnight US equity data.

Robinhood Legend uses Blue Ocean ATS as one of its stock/ETF market-data
sources during overnight trading. The classic Robinhood quote endpoint can lag
that venue by days, so the Robinhood venue adapter uses this module only as a
broker-scoped fallback when its own quote payload is stale.
"""
from __future__ import annotations

import json
import logging
import random
import string
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

from ...config import settings

logger = logging.getLogger(__name__)

_TV_WS_URL = "wss://data.tradingview.com/socket.io/websocket?from=chart%2F"
_CACHE: "OrderedDict[tuple[str, str, int], tuple[float, list[dict[str, Any]]]]" = OrderedDict()
_CACHE_MAX = 1_024


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def _session(prefix: str) -> str:
    suffix = "".join(random.choice(string.ascii_lowercase) for _ in range(12))
    return f"{prefix}_{suffix}"


def _tv_interval(interval: str) -> str:
    raw = (interval or "5m").strip().lower()
    return {
        "1m": "1",
        "2m": "2",
        "5m": "5",
        "15m": "15",
        "30m": "30",
        "1h": "60",
        "60m": "60",
        "4h": "240",
        "1d": "1D",
    }.get(raw, raw[:-1] if raw.endswith("m") and raw[:-1].isdigit() else "5")


def _frame(message: dict[str, Any]) -> str:
    body = json.dumps(message, separators=(",", ":"))
    return f"~m~{len(body)}~m~{body}"


def _send(ws: Any, method: str, params: list[Any]) -> None:
    ws.send(_frame({"m": method, "p": params}))


def _cache_get(
    key: tuple[str, str, int],
    *,
    now: float,
    ttl: float,
) -> list[dict[str, Any]] | None:
    cached = _CACHE.get(key)
    if not cached:
        return None
    ts, bars = cached
    if (now - ts) <= ttl:
        _CACHE.move_to_end(key)
        return list(bars)
    _CACHE.pop(key, None)
    return None


def _cache_set(
    key: tuple[str, str, int],
    bars: list[dict[str, Any]],
    *,
    now: float,
    ttl: float,
) -> None:
    _CACHE.pop(key, None)
    _CACHE[key] = (now, bars)
    _prune_cache(now=now, ttl=ttl)


def _prune_cache(*, now: float, ttl: float) -> None:
    cutoff = now - ttl
    while _CACHE:
        oldest = next(iter(_CACHE))
        ts = _CACHE[oldest][0]
        if ts >= cutoff:
            break
        _CACHE.pop(oldest, None)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)


def _parse_frames(raw: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    idx = 0
    while idx < len(raw):
        marker = raw.find("~m~", idx)
        if marker < 0:
            break
        len_start = marker + 3
        len_end = raw.find("~m~", len_start)
        if len_end < 0:
            break
        try:
            size = int(raw[len_start:len_end])
        except ValueError:
            idx = len_end + 3
            continue
        body_start = len_end + 3
        body = raw[body_start:body_start + size]
        idx = body_start + size
        try:
            msg = json.loads(body)
        except json.JSONDecodeError:
            continue
        if isinstance(msg, dict):
            out.append(msg)
    return out


def fetch_boats_ohlcv(
    ticker: str,
    *,
    interval: str = "5m",
    bars: int = 150,
    timeout_sec: float | None = None,
) -> list[dict[str, Any]]:
    """Return recent OHLCV bars for ``BOATS:<ticker>`` from TradingView."""
    symbol = (ticker or "").strip().upper()
    if not symbol:
        return []
    tv_int = _tv_interval(interval)
    n = max(1, min(int(bars or 150), 500))
    ttl = max(1.0, float(getattr(settings, "chili_robinhood_legend_quote_cache_seconds", 10)))
    cache_key = (symbol, tv_int, n)
    now = time.monotonic()
    cached = _cache_get(cache_key, now=now, ttl=ttl)
    if cached is not None:
        return cached

    try:
        import websocket
    except Exception:
        logger.debug("[boats] websocket-client unavailable", exc_info=True)
        return []

    timeout = float(timeout_sec or getattr(settings, "chili_robinhood_legend_quote_timeout_seconds", 8.0))
    ws = None
    try:
        ws = websocket.create_connection(
            _TV_WS_URL,
            timeout=timeout,
            origin="https://www.tradingview.com",
            header=["User-Agent: Mozilla/5.0"],
        )
        chart_session = _session("cs")
        quote_session = _session("qs")
        symbol_payload = json.dumps(
            {
                "symbol": f"BOATS:{symbol}",
                "adjustment": "splits",
                "session": "extended",
            },
            separators=(",", ":"),
        )
        _send(ws, "set_auth_token", ["unauthorized_user_token"])
        _send(ws, "chart_create_session", [chart_session, ""])
        _send(ws, "quote_create_session", [quote_session])
        _send(ws, "resolve_symbol", [chart_session, "symbol_1", "=" + symbol_payload])
        _send(ws, "create_series", [chart_session, "s1", "s1", "symbol_1", tv_int, n])

        deadline = time.monotonic() + timeout
        parsed: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            raw = ws.recv()
            for msg in _parse_frames(raw):
                if msg.get("m") == "timescale_update":
                    parsed.extend(_bars_from_timescale_update(msg))
                elif msg.get("m") == "symbol_error":
                    logger.debug("[boats] symbol_error ticker=%s msg=%s", symbol, msg)
                    return []
            if parsed:
                break
    except Exception as exc:
        logger.debug("[boats] fetch failed ticker=%s err=%s", symbol, exc)
        return []
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    if parsed:
        _cache_set(cache_key, parsed, now=time.monotonic(), ttl=ttl)
    return list(parsed)


def _bars_from_timescale_update(msg: dict[str, Any]) -> list[dict[str, Any]]:
    payload = msg.get("p") or []
    if len(payload) < 2 or not isinstance(payload[1], dict):
        return []
    series = payload[1].get("s1") or {}
    raw_bars = series.get("s") or []
    out: list[dict[str, Any]] = []
    for bar in raw_bars:
        values = bar.get("v") if isinstance(bar, dict) else None
        if not isinstance(values, list) or len(values) < 5:
            continue
        ts = _safe_float(values[0])
        o = _safe_float(values[1])
        h = _safe_float(values[2])
        l = _safe_float(values[3])
        c = _safe_float(values[4])
        if ts is None or o is None or h is None or l is None or c is None:
            continue
        vol = _safe_float(values[5]) if len(values) > 5 else None
        out.append({
            "time": int(ts),
            "open": float(o),
            "high": float(h),
            "low": float(l),
            "close": float(c),
            "volume": float(vol or 0.0),
        })
    out.sort(key=lambda x: int(x["time"]))
    return out


def fetch_boats_quote(
    ticker: str,
    *,
    interval: str = "5m",
    bars: int = 150,
) -> dict[str, Any] | None:
    data = fetch_boats_ohlcv(ticker, interval=interval, bars=bars)
    if not data:
        return None
    last = data[-1]
    ts = int(last["time"])
    provider_dt = datetime.fromtimestamp(ts, timezone.utc)
    same_day = [b for b in data if datetime.fromtimestamp(int(b["time"]), timezone.utc).date() == provider_dt.date()]
    return {
        "ticker": (ticker or "").strip().upper(),
        "price": float(last["close"]),
        "last_price": float(last["close"]),
        "open": float(last["open"]),
        "day_high": max(float(b["high"]) for b in same_day) if same_day else float(last["high"]),
        "day_low": min(float(b["low"]) for b in same_day) if same_day else float(last["low"]),
        "volume": sum(float(b.get("volume") or 0.0) for b in same_day),
        "quote_ts": provider_dt.isoformat(),
        "provider_time_utc": provider_dt,
        "source": "tradingview_boats",
        "market_session": "blue_ocean_overnight",
    }

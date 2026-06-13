"""Crypto liquidity floor (2026-06-13 crypto-live plan, A1).

The Ross momentum scorer ranks crypto pairs on burst signals (RVOL, gap,
hurst) that are BASIS-BLIND to whether the name can actually be traded. The
2026-06-13 forensics found the lane arming names like CHECK-USD (RVOL 34!) and
T-USD (RVOL 5.2) whose 24 h $-volume was ~$24k / ~$43k — ~$20/min of turnover,
unexecutable for any real size. Those toxic fills were 81 % of the crypto
lane's losses.

This gate is the executability filter the scorer lacks. It is ADAPTIVE — no
hardcoded ticker whitelist (the operator's standing rule: derive from data, not
magic lists). A name is tradeable iff its measured turnover clears a documented
floor; the per-name size cap then scales with that turnover so we never post
more than the book can absorb (the scaling-engine liquidity ceiling).

Two floors, both single documented settings:
  - 24 h quote ($) volume >= ``chili_crypto_min_quote_volume_24h_usd``
    (default $1.44M = ~$1k/min, the plan's median-1m-$vol >= $1k floor).
  - live spread (when an adapter is supplied + the probe is enabled)
    <= ``chili_crypto_max_spread_bps`` (default 50 bps).

Per-name notional cap = ``chili_crypto_notional_vol_fraction`` (default 0.5) of
the per-minute $-volume, i.e. never take more than half a minute of turnover.

Fail-CLOSED for crypto: a name with no turnover datum is NOT proven executable,
so it is blocked (with a distinct reason so monitoring can tell a data outage
from a genuinely thin name).
"""
from __future__ import annotations

import math
from typing import Any

from app.config import settings


_MIN_PER_DAY = 1440.0


def _is_crypto(symbol: str) -> bool:
    return bool(symbol) and str(symbol).upper().endswith("-USD")


def _quote_volume_24h_for(viability_row: Any, symbol: str) -> float | None:
    """Pull the symbol's 24 h quote ($) volume from the viability snapshot.

    Every viability row embeds the whole batch's ``ross_signals`` keyed by
    symbol, so the candidate row carries its own turnover datum with no extra
    network call. Returns None when the datum is absent or unparseable.
    """
    try:
        extra = (viability_row.execution_readiness_json or {}).get("extra") or {}
        sig = (extra.get("ross_signals") or {}).get(str(symbol).upper())
        if not isinstance(sig, dict):
            return None
        qv = sig.get("quote_volume_24h")
        if qv is None:
            return None
        qv = float(qv)
    except (AttributeError, TypeError, ValueError):
        return None
    return qv if math.isfinite(qv) and qv >= 0.0 else None


def _min_quote_volume_24h_usd() -> float:
    return float(getattr(settings, "chili_crypto_min_quote_volume_24h_usd", 1_440_000.0) or 1_440_000.0)


def _max_spread_bps() -> float:
    return float(getattr(settings, "chili_crypto_max_spread_bps", 50.0) or 50.0)


def _notional_vol_fraction() -> float:
    return float(getattr(settings, "chili_crypto_notional_vol_fraction", 0.5) or 0.5)


def _spread_probe_enabled() -> bool:
    return bool(getattr(settings, "chili_crypto_liquidity_spread_probe_enabled", True))


def crypto_liquidity_ok(
    symbol: str,
    viability_row: Any,
    *,
    adapter: Any | None = None,
) -> tuple[bool, dict[str, Any], float | None]:
    """Decide whether a crypto name is liquid enough to trade, and how big.

    Returns ``(ok, detail, max_notional_usd)``. ``max_notional_usd`` is None
    when the name is blocked (no size). Non-crypto symbols always pass with no
    cap — this gate governs the crypto lane only.
    """
    if not _is_crypto(symbol):
        return True, {"liquidity_gate": "n/a_equity"}, None

    qv24h = _quote_volume_24h_for(viability_row, symbol)
    if qv24h is None:
        return False, {"liquidity_gate": "blocked", "reason": "liquidity_data_missing"}, None

    floor = _min_quote_volume_24h_usd()
    detail: dict[str, Any] = {
        "liquidity_gate": "ok",
        "quote_volume_24h_usd": round(qv24h, 2),
        "quote_volume_24h_floor": floor,
    }
    if qv24h < floor:
        detail["liquidity_gate"] = "blocked"
        detail["reason"] = "quote_volume_below_floor"
        return False, detail, None

    # Per-minute turnover -> per-name notional cap (never post more than a
    # fraction of one minute's $-volume; this is the liquidity ceiling).
    per_min_vol = qv24h / _MIN_PER_DAY
    max_notional = max(0.0, _notional_vol_fraction() * per_min_vol)
    detail["per_min_vol_usd"] = round(per_min_vol, 2)
    detail["max_notional_usd"] = round(max_notional, 2)

    # Live spread probe (optional, network-bound) — a wide book is a hidden
    # round-trip cost the $-volume floor alone won't catch.
    if adapter is not None and _spread_probe_enabled():
        spread_bps = _probe_spread_bps(adapter, symbol)
        if spread_bps is not None:
            detail["spread_bps"] = round(spread_bps, 2)
            detail["spread_bps_floor"] = _max_spread_bps()
            if spread_bps > _max_spread_bps():
                detail["liquidity_gate"] = "blocked"
                detail["reason"] = "spread_above_floor"
                return False, detail, None

    return True, detail, max_notional


def _probe_spread_bps(adapter: Any, symbol: str) -> float | None:
    """Best-effort live spread in bps via the adapter; None on any failure
    (fail-open on the probe — the $-volume floor is the primary gate)."""
    try:
        product_id = symbol if str(symbol).upper().endswith("-USD") else f"{symbol}-USD"
        tick, _ = adapter.get_best_bid_ask(product_id)
        if tick is None:
            return None
        sb = getattr(tick, "spread_bps", None)
        if sb is None:
            return None
        sb = float(sb)
        return sb if math.isfinite(sb) and sb >= 0.0 else None
    except Exception:
        return None

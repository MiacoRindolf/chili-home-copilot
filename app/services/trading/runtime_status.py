"""Operator runtime status: aggregate health and freshness across all key trading surfaces.

Provides a single read point for:
- Scanner / top-picks cache freshness
- Prediction mirror cache freshness
- Broker connectivity and session age
- Learning cycle last-run timestamp
- Market data provider health
- Circuit breaker state
- Regime freshness

All functions are read-only and safe to call on any request; heavy computation
is NOT done here — this module reads from in-process state and lightweight DB
queries only.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ── Freshness thresholds (seconds) ─────────────────────────────────────────
_STALE_SCANNER = 600        # top-picks stale after 10m
_STALE_PREDICTIONS = 1800   # predictions stale after 30m
_STALE_BROKER_SYNC = 300    # broker sync stale after 5m
_STALE_LEARNING = 3600      # learning cycle stale after 1h
_STALE_MARKET_DATA = 120    # quote cache stale after 2m
_STALE_REGIME = 900         # regime reading stale after 15m


def _age_seconds(ts: float | None) -> float | None:
    if not ts:
        return None
    return round(time.time() - ts, 1)


def _as_of(ts: float | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _surface(
    name: str,
    ts: float | None,
    stale_threshold: float,
    *,
    extra: dict[str, Any] | None = None,
    ok: bool = True,
    note: str | None = None,
) -> dict[str, Any]:
    age = _age_seconds(ts)
    is_stale = (age is None) or (age > stale_threshold)
    out: dict[str, Any] = {
        "surface": name,
        "ok": ok and not is_stale,
        "as_of": _as_of(ts),
        "age_seconds": age,
        "is_stale": is_stale,
        "stale_threshold_seconds": stale_threshold,
    }
    if note:
        out["note"] = note
    if extra:
        out.update(extra)
    return out


# ── Individual surface checks ───────────────────────────────────────────────

def scanner_status() -> dict[str, Any]:
    """Top-picks / scanner cache freshness."""
    try:
        from .scanner import get_top_picks_freshness, _top_picks_cache  # type: ignore[attr-defined]
        fresh = get_top_picks_freshness(stale_threshold_seconds=_STALE_SCANNER)
        return {
            "surface": "scanner",
            "ok": not fresh.get("is_stale", True),
            "as_of": fresh.get("as_of"),
            "age_seconds": fresh.get("age_seconds"),
            "is_stale": fresh.get("is_stale", True),
            "stale_threshold_seconds": _STALE_SCANNER,
            "cached_picks": len(_top_picks_cache.get("picks") or []),
        }
    except Exception as e:
        logger.debug("[runtime_status] scanner_status error: %s", e)
        return _surface("scanner", None, _STALE_SCANNER, ok=False, note=str(e))


def predictions_status() -> dict[str, Any]:
    """Prediction mirror (SWR) cache freshness."""
    try:
        from .learning import _prediction_cache_ts  # type: ignore[attr-defined]
        ts = _prediction_cache_ts if isinstance(_prediction_cache_ts, float) else None
        return _surface("predictions", ts, _STALE_PREDICTIONS)
    except Exception:
        # Cache ts not exposed or not yet populated — not an error
        return _surface("predictions", None, _STALE_PREDICTIONS, note="cache ts unavailable")


def broker_status() -> dict[str, Any]:
    """Broker connectivity, session age, and last-sync timestamp."""
    try:
        from .. import broker_service
        connected = broker_service.is_connected()
        session_ts = getattr(broker_service, "_session_connected_at", None)
        sync_ts = getattr(broker_service, "_last_sync_ts", None)
        age = _age_seconds(sync_ts)
        is_stale = (age is None) or (age > _STALE_BROKER_SYNC)
        return {
            "surface": "broker",
            "ok": connected and not is_stale,
            "connected": connected,
            "session_as_of": _as_of(session_ts),
            "last_sync_as_of": _as_of(sync_ts),
            "last_sync_age_seconds": age,
            "is_stale": is_stale,
            "stale_threshold_seconds": _STALE_BROKER_SYNC,
        }
    except Exception as e:
        logger.debug("[runtime_status] broker_status error: %s", e)
        return {
            "surface": "broker",
            "ok": False,
            "connected": False,
            "note": str(e),
            "is_stale": True,
            "stale_threshold_seconds": _STALE_BROKER_SYNC,
        }


def learning_status() -> dict[str, Any]:
    """Last learning cycle completion timestamp."""
    try:
        from .learning import _last_cycle_ts  # type: ignore[attr-defined]
        ts = _last_cycle_ts if isinstance(_last_cycle_ts, float) else None
        return _surface("learning", ts, _STALE_LEARNING)
    except Exception:
        return _surface("learning", None, _STALE_LEARNING, note="cycle ts unavailable")


def market_data_status() -> dict[str, Any]:
    """Market data provider freshness from cached quote state."""
    try:
        from .market_data import _quote_cache_ts, _quote_cache_provider  # type: ignore[attr-defined]
        ts = _quote_cache_ts if isinstance(_quote_cache_ts, (int, float)) else None
        provider = _quote_cache_provider if isinstance(_quote_cache_provider, str) else "unknown"
        return _surface("market_data", ts, _STALE_MARKET_DATA, extra={"provider": provider})
    except Exception:
        return _surface("market_data", None, _STALE_MARKET_DATA, note="quote cache ts unavailable")


def regime_status() -> dict[str, Any]:
    """Market regime freshness and current composite reading."""
    try:
        from .market_data import _regime_cache, _regime_cache_ts  # type: ignore[attr-defined]
        ts = _regime_cache_ts if isinstance(_regime_cache_ts, (int, float)) else None
        regime = _regime_cache or {}
        return _surface(
            "regime",
            ts,
            _STALE_REGIME,
            extra={
                "composite": regime.get("regime", "unknown"),
                "vix_regime": regime.get("vix_regime", "unknown"),
                "spy_direction": regime.get("spy_direction", "unknown"),
            },
        )
    except Exception:
        return _surface("regime", None, _STALE_REGIME, note="regime cache unavailable")


def circuit_breaker_status() -> dict[str, Any]:
    """Circuit breaker state."""
    try:
        from .portfolio_risk import get_breaker_status
        s = get_breaker_status()
        return {
            "surface": "circuit_breaker",
            "ok": not s["tripped"],
            "tripped": s["tripped"],
            "reason": s.get("reason"),
        }
    except Exception as e:
        return {
            "surface": "circuit_breaker",
            "ok": False,
            "tripped": False,
            "note": str(e),
        }


def kill_switch_status() -> dict[str, Any]:
    """Kill-switch (global halt) state."""
    try:
        from .governance import is_kill_switch_active, get_kill_switch_reason  # type: ignore[attr-defined]
        active = is_kill_switch_active()
        reason = get_kill_switch_reason() if active else None
        return {
            "surface": "kill_switch",
            "ok": not active,
            "active": active,
            "reason": reason,
        }
    except Exception:
        return {
            "surface": "kill_switch",
            "ok": True,
            "active": False,
            "note": "kill switch unavailable",
        }


# ── Aggregate view ──────────────────────────────────────────────────────────

def get_runtime_overview() -> dict[str, Any]:
    """Return a full runtime health snapshot across all surfaces.

    Suitable for the operator dashboard endpoint. Never raises — errors are
    absorbed into per-surface ``ok: false`` entries.
    """
    surfaces = [
        scanner_status(),
        predictions_status(),
        broker_status(),
        learning_status(),
        market_data_status(),
        regime_status(),
        circuit_breaker_status(),
        kill_switch_status(),
    ]
    degraded = [s["surface"] for s in surfaces if not s.get("ok", True)]
    return {
        "as_of": datetime.now(tz=timezone.utc).isoformat(),
        "healthy": len(degraded) == 0,
        "degraded_surfaces": degraded,
        "surfaces": {s["surface"]: s for s in surfaces},
    }


def get_freshness_summary() -> dict[str, Any]:
    """Lightweight freshness-only view (no connectivity checks).

    Used by the UI to show data-age banners without full health polling.
    """
    items = []
    for fn in (scanner_status, predictions_status, market_data_status, regime_status, learning_status):
        try:
            s = fn()
            items.append({
                "surface": s["surface"],
                "as_of": s.get("as_of"),
                "age_seconds": s.get("age_seconds"),
                "is_stale": s.get("is_stale", True),
            })
        except Exception:
            pass
    return {
        "as_of": datetime.now(tz=timezone.utc).isoformat(),
        "surfaces": items,
    }

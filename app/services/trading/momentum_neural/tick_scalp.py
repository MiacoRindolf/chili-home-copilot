"""Tick-level Ross first-pullback scalp helpers.

The bar-based pullback gate is still useful for slower continuation entries, but
Ross's premarket small-cap first pullbacks can be decided from scanner evidence
plus live quote/tape state before any 1m candle closes. This module keeps that
micro state isolated and side-effect free so the live runner can call it on every
quote tick.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any


TRIGGER_REASON = "tick_first_pullback_scalp"
WATCH_REASON = "tick_first_pullback_watch"
INDEPENDENT_A_PLUS_WATCH_REASON = "independent_smallcap_a_plus_watch"
ROSS_TICK_SCALP_MIN_PRICE = 1.0
ROSS_TICK_SCALP_COURSE_PRICE_FLOOR = 2.0
ROSS_TICK_SCALP_MAX_PRICE = 25.0
ROSS_DIRECT_TRADE_MIN_CHANGE_PCT = 3.0


@dataclass(frozen=True)
class TickFirstPullbackDecision:
    fire: bool
    reason: str
    state: dict[str, Any]
    debug: dict[str, Any]


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        multiplier = 1.0
        if text.endswith("%"):
            text = text[:-1]
        elif text[-1:].lower() == "m":
            text = text[:-1]
            multiplier = 1_000_000.0
        elif text[-1:].lower() == "k":
            text = text[:-1]
            multiplier = 1_000.0
        value = text
    else:
        multiplier = 1.0
    try:
        out = float(value) * multiplier
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _first_num(src: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in src:
            value = _num(src.get(key))
            if value is not None:
                return value
    return None


def _as_shares(value: float | None, *, key_hint: str = "") -> float | None:
    if value is None:
        return None
    key = key_hint.lower()
    if "float" in key or "million" in key:
        if value < 1_000.0:
            return value * 1_000_000.0
    return value


def _source_text(signal: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "scanner_source",
        "source",
        "scan",
        "strategy",
        "strategies",
        "alert_name",
        "headline",
        "news_headline",
        "catalyst",
    ):
        value = signal.get(key)
        if isinstance(value, list):
            parts.extend(str(v) for v in value if v is not None)
        elif value is not None:
            parts.append(str(value))
    return " ".join(parts).lower()


def _invalid_transcript_context(signal: dict[str, Any]) -> bool:
    signal_type = str(signal.get("signal_type") or "").strip().lower()
    source = _source_text(signal)
    if signal_type != "ross_transcript_mention" and "ross_audio_transcript" not in source:
        return False
    text = str(signal.get("transcript_text") or "").strip()
    if not text:
        return True
    try:
        from .ross_transcript_bridge import has_trading_context

        return not bool(has_trading_context(text))
    except Exception:
        return True


def _direct_ross_trade_source(signal: dict[str, Any], source: str) -> bool:
    """True only for Ross trade context, not a generic Warrior scanner mention."""
    signal_type = str(signal.get("signal_type") or "").strip().lower()
    text = " ".join(
        str(signal.get(key) or "")
        for key in (
            "source",
            "scanner_source",
            "signal_type",
            "alert_name",
            "strategy",
            "headline",
            "transcript_text",
        )
    ).lower()
    if signal_type in {
        "ross_trade",
        "ross_live_trade",
        "ross_entry",
        "ross_position",
        "ross_transcript_trade",
    }:
        return True
    if "ross_audio_transcript" in source and any(
        token in text
        for token in (
            "i'm in",
            "im in",
            "i bought",
            "i took",
            "entry",
            "scalp",
            "starter",
            "long",
            "added",
        )
    ):
        return True
    return any(
        token in text
        for token in (
            "ross live trade",
            "ross_trade",
            "ross entry",
            "ross bought",
            "ross long",
            "ross scalp",
        )
    )


def _float_shares(signal: dict[str, Any]) -> float | None:
    keyed = (
        ("float_shares", signal.get("float_shares")),
        ("share_float", signal.get("share_float")),
        ("shares_float", signal.get("shares_float")),
        ("float_millions", signal.get("float_millions")),
        ("float_m", signal.get("float_m")),
        ("float", signal.get("float")),
    )
    for key, raw in keyed:
        value = _as_shares(_num(raw), key_hint=key)
        if value is not None:
            return value
    return None


def ross_signal_for_symbol(execution_readiness_json: Any, symbol: str) -> dict[str, Any] | None:
    """Extract a symbol's Ross/Warrior scanner payload from a persisted viability row."""
    if not isinstance(execution_readiness_json, dict):
        return None
    sym = str(symbol or "").strip().upper()
    roots = [execution_readiness_json]
    extra = execution_readiness_json.get("extra")
    if isinstance(extra, dict):
        roots.insert(0, extra)
    for root in roots:
        signals = root.get("ross_signals") if isinstance(root, dict) else None
        if not isinstance(signals, dict):
            continue
        for key in (sym, sym.lower(), sym.upper()):
            sig = signals.get(key)
            if isinstance(sig, dict):
                return dict(sig)
    return None


def ross_score_for_symbol(execution_readiness_json: Any, symbol: str) -> float | None:
    if not isinstance(execution_readiness_json, dict):
        return None
    sym = str(symbol or "").strip().upper()
    roots = [execution_readiness_json]
    extra = execution_readiness_json.get("extra")
    if isinstance(extra, dict):
        roots.insert(0, extra)
    for root in roots:
        scores = root.get("ross_scores") if isinstance(root, dict) else None
        if not isinstance(scores, dict):
            continue
        for key in (sym, sym.lower(), sym.upper()):
            value = _num(scores.get(key))
            if value is not None:
                return value
    return None


def ross_tick_scalp_evidence_ok(
    signal: dict[str, Any] | None,
    *,
    min_change_pct: float = 10.0,
    min_rvol: float = 5.0,
    max_float_shares: float = 20_000_000.0,
    min_price: float = ROSS_TICK_SCALP_MIN_PRICE,
    max_price: float = ROSS_TICK_SCALP_MAX_PRICE,
) -> tuple[bool, str, dict[str, Any]]:
    """Return whether scanner evidence is strong enough for tick scalp watching.

    This deliberately does not use a fixed Ross-score floor. The gate is shaped
    around Ross's five-pillar setup: explosive percent move, high RVOL/pace, low
    float, actionable scanner/catalyst context, and a tradable price range.
    """
    if not isinstance(signal, dict) or not signal:
        return False, "no_ross_signal", {}
    if _invalid_transcript_context(signal):
        return False, "ross_transcript_context_rejected", {
            "signal_type": signal.get("signal_type"),
            "source": _source_text(signal),
        }

    price = _first_num(signal, "price", "last_price", "alert_price", "hod_price", "close")
    change_pct = _first_num(
        signal,
        "daily_change_pct",
        "todays_change_perc",
        "change_pct",
        "percent_change",
        "gap_pct",
        "pct_change",
        "change",
    )
    rvol = _first_num(
        signal,
        "rvol_pace",
        "rvol",
        "relative_volume",
        "relative_volume_daily_rate",
        "daily_rate",
        "five_min_rvol",
        "5m_rvol",
        "volume_rate",
        "vol_ratio",
        "intraday_cumulative_rvol",
    )
    float_shares = _float_shares(signal)
    source = _source_text(signal)
    source_support = any(
        token in source
        for token in (
            "5 pillar",
            "5 pillars",
            "five pillar",
            "hod",
            "high of day",
            "low float",
            "squeeze alert",
            "warrior",
            "ross",
            "news",
            "tape_delta",
            "tape delta",
            "ws_ignition",
            "ignition",
        )
    ) or bool(signal.get("daily_breaking_major"))
    direct_ross_trade = _direct_ross_trade_source(signal, source)

    debug = {
        "price": price,
        "change_pct": change_pct,
        "rvol": rvol,
        "float_shares": float_shares,
        "source_support": source_support,
        "direct_ross_trade": direct_ross_trade,
        "daily_breaking_major": bool(signal.get("daily_breaking_major")),
        "has_catalyst_text": any(k in source for k in ("news", "phase", "fda", "contract", "earnings")),
        "min_change_pct": float(min_change_pct),
        "min_rvol": float(min_rvol),
        "max_float_shares": float(max_float_shares),
    }

    if price is not None:
        if price <= 0:
            return False, "invalid_price", debug
        if price > max_price:
            return False, "price_above_scalp_range", debug
        debug["price_below_course_range"] = bool(price < ROSS_TICK_SCALP_COURSE_PRICE_FLOOR)
        if price < min_price and not source_support:
            return False, "price_below_scalp_range", debug

    if float_shares is not None and float_shares > max_float_shares:
        return False, "float_too_large", debug

    change_ok = change_pct is not None and change_pct >= min_change_pct
    rvol_ok = rvol is not None and rvol >= min_rvol
    float_ok = float_shares is None or float_shares <= max_float_shares
    catalyst_ok = bool(debug.get("has_catalyst_text"))
    direct_trade_ok = bool(debug.get("direct_ross_trade"))
    daily_break_ok = bool(debug.get("daily_breaking_major"))
    failed_pillars: list[str] = []
    if not change_ok:
        failed_pillars.append("change_pct")
    if not rvol_ok:
        failed_pillars.append("rvol")
    if not float_ok:
        failed_pillars.append("float")
    if not source_support:
        failed_pillars.append("source_support")
    if not (catalyst_ok or direct_trade_ok or daily_break_ok or source_support):
        failed_pillars.append("catalyst_or_direct_context")
    debug["pillar_pass"] = {
        "change_pct": change_ok,
        "rvol": rvol_ok,
        "float": float_ok,
        "source_support": bool(source_support),
        "catalyst_or_direct_context": bool(catalyst_ok or direct_trade_ok or daily_break_ok or source_support),
    }
    debug["failed_pillars"] = failed_pillars
    if change_ok and rvol_ok:
        return True, WATCH_REASON, debug
    if source_support and (change_ok or rvol_ok):
        debug["source_support_relaxed_missing_pillar"] = True
        return True, WATCH_REASON, debug
    if (
        direct_ross_trade
        and source_support
        and change_pct is not None
        and change_pct >= ROSS_DIRECT_TRADE_MIN_CHANGE_PCT
    ):
        debug["direct_ross_trade_relaxed_scanner_pillars"] = True
        debug["direct_ross_trade_min_change_pct"] = ROSS_DIRECT_TRADE_MIN_CHANGE_PCT
        return True, WATCH_REASON, debug
    return False, "ross_pillars_not_explosive", debug


def independent_smallcap_a_plus_evidence_ok(
    signal: dict[str, Any] | None,
    *,
    min_change_pct: float = 10.0,
    min_dollar_volume: float = 5_000_000.0,
    min_volume: float = 500_000.0,
    max_price: float = ROSS_TICK_SCALP_MAX_PRICE,
    max_float_shares: float = 30_000_000.0,
) -> tuple[bool, str, dict[str, Any]]:
    """Strict non-Ross proof for small-cap A+ watcher admission.

    Ross/audio/scanner context can be slow. This path lets a name reach the
    same tick-level playbook when live tape itself proves an A+ small-cap mover.
    It deliberately requires stronger market facts than the Ross-supported path
    and does not relax final order risk checks.
    """
    if not isinstance(signal, dict) or not signal:
        return False, "no_independent_smallcap_signal", {}

    price = _first_num(signal, "price", "last_price", "alert_price", "hod_price", "close")
    change_pct = _first_num(
        signal,
        "daily_change_pct",
        "todays_change_perc",
        "change_pct",
        "percent_change",
        "gap_pct",
        "pct_change",
        "change",
    )
    dollar_volume = _first_num(
        signal,
        "dollar_volume",
        "dollar_vol",
        "todays_dollar_volume",
        "today_dollar_volume",
        "day_dollar_volume",
        "turnover",
        "notional_volume",
    )
    volume = _first_num(
        signal,
        "volume",
        "day_volume",
        "todays_volume",
        "today_volume",
        "cumulative_volume",
        "volume_today",
        "share_volume",
        "shares_traded_today",
    )
    if dollar_volume is None and price is not None and volume is not None:
        dollar_volume = float(price) * float(volume)
    float_shares = _float_shares(signal)
    source = _source_text(signal)
    source_ok = any(
        token in source
        for token in (
            "tape_delta",
            "tape delta",
            "iqfeed",
            "nbbo",
            "ws_ignition",
            "ignition",
            "hod",
            "high of day",
            "running_up",
            "running up",
            "breakout",
            "news",
        )
    ) or bool(signal.get("daily_breaking_major"))

    debug = {
        "price": price,
        "change_pct": change_pct,
        "dollar_volume": dollar_volume,
        "volume": volume,
        "float_shares": float_shares,
        "source_support": source_ok,
        "min_change_pct": float(min_change_pct),
        "min_dollar_volume": float(min_dollar_volume),
        "min_volume": float(min_volume),
        "max_float_shares": float(max_float_shares),
    }
    if price is None or price <= 0:
        return False, "independent_smallcap_missing_price", debug
    if price > max_price:
        return False, "independent_smallcap_price_above_range", debug
    if float_shares is not None and float_shares > max_float_shares:
        return False, "independent_smallcap_float_too_large", debug
    if change_pct is None or change_pct < min_change_pct:
        return False, "independent_smallcap_change_below_floor", debug
    if dollar_volume is None or dollar_volume < min_dollar_volume:
        return False, "independent_smallcap_dollar_volume_below_floor", debug
    if volume is None or volume < min_volume:
        return False, "independent_smallcap_volume_below_floor", debug
    if not source_ok:
        return False, "independent_smallcap_source_not_tape", debug
    return True, INDEPENDENT_A_PLUS_WATCH_REASON, debug


def expected_move_bps_from_ross_signal(signal: dict[str, Any] | None) -> float | None:
    if not isinstance(signal, dict):
        return None
    change_pct = _first_num(
        signal,
        "daily_change_pct",
        "todays_change_perc",
        "change_pct",
        "percent_change",
        "gap_pct",
        "pct_change",
        "change",
    )
    rvol = _first_num(
        signal,
        "rvol_pace",
        "rvol",
        "daily_rate",
        "relative_volume_daily_rate",
        "vol_ratio",
        "intraday_cumulative_rvol",
    )
    candidates: list[float] = []
    if change_pct is not None and change_pct > 0:
        candidates.append(change_pct * 100.0)
    if rvol is not None and rvol > 0:
        candidates.append(min(2_500.0, rvol * 35.0))
    if not candidates:
        return None
    value = max(candidates)
    return value if math.isfinite(value) and value > 0 else None


def _quote_price(*, bid: float | None, ask: float | None, mid: float | None) -> float | None:
    mid_f = _num(mid)
    if mid_f is not None and mid_f > 0:
        return mid_f
    bid_f = _num(bid)
    ask_f = _num(ask)
    if bid_f is not None and ask_f is not None and bid_f > 0 and ask_f >= bid_f:
        return (bid_f + ask_f) / 2.0
    if ask_f is not None and ask_f > 0:
        return ask_f
    if bid_f is not None and bid_f > 0:
        return bid_f
    return None


def _placeability_gate_enabled() -> bool:
    """Read the STEP-B #12 flag (default True). Import-safe: any failure => ON."""
    try:
        from ....config import settings

        return bool(getattr(settings, "chili_momentum_tick_scalp_placeability_gate_enabled", True))
    except Exception:
        return True


def _tick_scalp_max_rearms_per_day(override: int | None = None) -> int:
    """Per-day rearm cap (the one documented base = 8; the override is a FLOOR the caller
    can raise). Import-safe."""
    base = 8
    try:
        from ....config import settings

        base = int(getattr(settings, "chili_momentum_tick_scalp_max_rearms_per_day", 8) or 8)
    except Exception:
        base = 8
    if override is not None:
        try:
            return max(1, int(override), base)
        except (TypeError, ValueError):
            return max(1, base)
    return max(1, base)


def evaluate_tick_first_pullback(
    *,
    symbol: str,
    signal: dict[str, Any] | None,
    state: dict[str, Any] | None,
    bid: float | None,
    ask: float | None,
    mid: float | None,
    now_utc: datetime | None = None,
    min_pullback_bps: float = 35.0,
    max_pullback_bps: float = 1_800.0,
    min_reclaim_bps: float = 8.0,
    stop_buffer_bps: float = 12.0,
    max_hold_seconds: float = 12.0,
    placeable: bool | None = None,
    max_rearms_per_day: int | None = None,
) -> TickFirstPullbackDecision:
    """Evaluate the tick-level first-pullback scalp for one quote tick.

    ONE-SHOT LATCH → PLACEABILITY-GATED (STEP-B #12): the reclaim fires once, then the
    ``fired`` latch blocks any re-buy of the same leg. With the placeability gate ON (flag
    ``chili_momentum_tick_scalp_placeability_gate_enabled``, default True) the latch is only
    CONSUMED when the caller passes ``placeable=True`` (market-hours ok + adapter healthy +
    spread passable — the caller owns those checks). A reclaim that fires while
    ``placeable=False`` REARMS instead of latching (does not set ``fired``) so the very next
    placeable tick can fire — bounded by ``max_rearms_per_day`` (the one documented base = 8,
    override is a FLOOR). Once the rearm cap is hit the latch consumes anyway to stop an
    unbounded blocked-spin. ``placeable=None`` (or the flag OFF) preserves the legacy
    consume-on-reclaim behavior for callers not participating in the gate."""
    evidence_ok, evidence_reason, evidence_debug = ross_tick_scalp_evidence_ok(signal)
    existing = dict(state or {})
    if not evidence_ok:
        existing["last_reject_reason"] = evidence_reason
        return TickFirstPullbackDecision(False, evidence_reason, existing, evidence_debug)

    price = _quote_price(bid=bid, ask=ask, mid=mid)
    if price is None or price <= 0:
        existing["last_reject_reason"] = "invalid_tick_price"
        return TickFirstPullbackDecision(False, "invalid_tick_price", existing, evidence_debug)
    if existing.get("fired"):
        existing["last_price"] = price
        return TickFirstPullbackDecision(False, "already_fired", existing, evidence_debug)

    signal_price = _first_num(signal or {}, "price", "last_price", "alert_price", "hod_price", "close")
    # FIX-19(d): anchor the pullback high to the TRUE session high (incl. premarket), not just
    # the watch-start signal price. The signal already carries a tracked session/day high — if
    # the real HOD is above the watch-start price, a pullback measured from the lower signal
    # price UNDER-reads its depth (a real deep flush looks shallow). Fold the tracked session
    # high into the running max so the depth is measured from the genuine top. Fail-open: no
    # session-high field ⇒ unchanged (byte-identical to the signal_price anchor).
    session_high = _first_num(
        signal or {}, "session_high", "premarket_high", "day_high", "hod", "hod_price", "high"
    )
    prev_high = _num(existing.get("high"))
    high = max(
        v for v in (prev_high, signal_price, session_high, price) if v is not None and v > 0
    )
    prev_phase = str(existing.get("phase") or "watching")
    prev_low = _num(existing.get("pullback_low"))
    prev_last = _num(existing.get("last_price"))
    phase = prev_phase
    pullback_low = prev_low

    if price > high:
        high = price
        phase = "thrust"
        pullback_low = None
    else:
        depth_now = ((high - price) / high) * 10_000.0 if high > 0 else 0.0
        if prev_phase == "pullback" and prev_low is not None:
            pullback_low = min(prev_low, price)
            phase = "pullback"
        elif depth_now >= max(0.0, float(min_pullback_bps)):
            pullback_low = price
            phase = "pullback"
        elif prev_phase not in ("pullback", "too_deep"):
            phase = "watching"

    now_text = (now_utc or datetime.utcnow()).isoformat()
    _prev_rearms = existing.get("rearm_count")
    try:
        _prev_rearms = int(_prev_rearms) if _prev_rearms is not None else 0
    except (TypeError, ValueError):
        _prev_rearms = 0
    next_state = {
        "symbol": str(symbol or "").upper(),
        "phase": phase,
        "high": round(float(high), 6),
        "pullback_low": None if pullback_low is None else round(float(pullback_low), 6),
        "last_price": round(float(price), 6),
        "last_update_utc": now_text,
    }
    if _prev_rearms:
        next_state["rearm_count"] = _prev_rearms  # carry the per-day rearm counter forward
    if signal_price is not None:
        next_state["signal_price"] = round(float(signal_price), 6)

    debug = dict(evidence_debug)
    debug.update(
        {
            "price": price,
            "high": high,
            "pullback_low": pullback_low,
            "prev_phase": prev_phase,
            "prev_last_price": prev_last,
            "min_pullback_bps": float(min_pullback_bps),
            "max_pullback_bps": float(max_pullback_bps),
            "min_reclaim_bps": float(min_reclaim_bps),
        }
    )

    if phase != "pullback" or pullback_low is None:
        return TickFirstPullbackDecision(False, "waiting_for_tick_pullback", next_state, debug)

    pullback_depth_bps = ((high - pullback_low) / high) * 10_000.0 if high > 0 else 0.0
    debug["pullback_depth_bps"] = round(pullback_depth_bps, 4)
    if pullback_depth_bps > max(0.0, float(max_pullback_bps)):
        next_state["phase"] = "too_deep"
        return TickFirstPullbackDecision(False, "tick_pullback_too_deep", next_state, debug)

    reclaim_level = pullback_low * (1.0 + max(0.0, float(min_reclaim_bps)) / 10_000.0)
    structural_stop = pullback_low * (1.0 - max(0.0, float(stop_buffer_bps)) / 10_000.0)
    debug.update(
        {
            "reclaim_level": round(reclaim_level, 6),
            "structural_stop_price": round(structural_stop, 6),
            "breakout_level_price": round(reclaim_level, 6),
            "max_hold_seconds": float(max_hold_seconds),
        }
    )

    had_prior_pullback = prev_phase == "pullback" and prev_low is not None
    tick_uptick = prev_last is None or price > prev_last
    if had_prior_pullback and tick_uptick and price >= reclaim_level:
        # PLACEABILITY GATE (STEP-B #12): only CONSUME the one-shot latch when an order is
        # actually placeable. A reclaim fired while blocked (dark adapter / closed clock /
        # unpassable spread) must NOT strand the trigger in `fired` — it REARMS so the next
        # placeable tick can fire, bounded by the per-day cap.
        _gate_on = _placeability_gate_enabled()
        debug["placeability_gate_enabled"] = bool(_gate_on)
        debug["placeable"] = placeable
        if not _gate_on or placeable is None or placeable is True:
            # Legacy path (gate off / caller not participating) OR confirmed placeable:
            # consume the latch and fire.
            next_state["phase"] = "fired"
            next_state["fired"] = True
            return TickFirstPullbackDecision(True, TRIGGER_REASON, next_state, debug)
        # placeable is False and the gate is on: rearm (don't latch) up to the per-day cap.
        _cap = _tick_scalp_max_rearms_per_day(max_rearms_per_day)
        _rearms = int(next_state.get("rearm_count", 0) or 0)
        debug["rearm_count"] = _rearms
        debug["max_rearms_per_day"] = _cap
        if _rearms < _cap:
            next_state["rearm_count"] = _rearms + 1
            next_state["phase"] = "pullback"  # hold the reclaim-ready state so the next placeable tick fires
            next_state["last_reject_reason"] = "tick_reclaim_not_placeable_rearmed"
            debug["rearm_count"] = _rearms + 1
            return TickFirstPullbackDecision(
                False, "tick_reclaim_not_placeable_rearmed", next_state, debug
            )
        # Cap exhausted: consume the latch anyway so a persistently-blocked name can't spin.
        next_state["phase"] = "fired"
        next_state["fired"] = True
        next_state["last_reject_reason"] = "tick_reclaim_rearm_cap_exhausted"
        return TickFirstPullbackDecision(
            False, "tick_reclaim_rearm_cap_exhausted", next_state, debug
        )

    return TickFirstPullbackDecision(False, "waiting_for_tick_reclaim", next_state, debug)

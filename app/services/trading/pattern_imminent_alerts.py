"""Imminent breakout alerts for ScanPatterns (promoted/live main channel by default).

Uses shared scoring in ``opportunity_scoring`` (composite = quality first, ETA secondary).
Stock patterns use US session gates; crypto runs 24/7. ETA is heuristic, not guaranteed.
"""
from __future__ import annotations

import json
import logging
import time as _time
from collections import Counter
from datetime import datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import desc, func
from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo

from ...config import (
    PATTERN_IMMINENT_COINBASE_SPOT_FILTER_DEFAULT_TTL_SECONDS,
    PATTERN_IMMINENT_DEFAULT_HARD_RECERT_SHADOW_LIFECYCLE_STAGES,
    PATTERN_IMMINENT_DEFAULT_HARD_RECERT_SHADOW_REASONS,
    PATTERN_IMMINENT_DEFAULT_MAX_TICKERS_PER_PATTERN,
    PATTERN_IMMINENT_DEFAULT_MISSING_INDICATOR_SAMPLE_LIMIT,
    PATTERN_IMMINENT_DEFAULT_READINESS_NEAR_MISS_LIMIT,
    PATTERN_IMMINENT_DEFAULT_SHADOW_NEAR_MISS_ADAPTIVE_ENABLED,
    PATTERN_IMMINENT_DEFAULT_SHADOW_NEAR_MISS_ADAPTIVE_MAX_PER_RUN,
    PATTERN_IMMINENT_DEFAULT_SHADOW_NEAR_MISS_ADAPTIVE_MIN_READINESS_FRACTION,
    PATTERN_IMMINENT_DEFAULT_SHADOW_NEAR_MISS_MAX_GAP,
    PATTERN_IMMINENT_DEFAULT_SHADOW_NEAR_MISS_LIFECYCLE_STAGES,
    PATTERN_IMMINENT_DEFAULT_SUPPRESSED_DIAGNOSTIC_LIMIT,
    PATTERN_IMMINENT_DEFAULT_TICKER_ROTATION_EXPLORE_TICKERS,
    PATTERN_IMMINENT_DEFAULT_TICKER_ROTATION_WINDOW_MINUTES,
    PATTERN_IMMINENT_EQUITY_SESSION_SHADOW_SIGNAL_LANE,
    PATTERN_IMMINENT_HARD_RECERT_SHADOW_SIGNAL_LANE,
    PATTERN_IMMINENT_OFFSESSION_STOCK_SHADOW_DEFAULT_ENABLED,
    PATTERN_IMMINENT_MIN_TICKER_ROTATION_WINDOW_MINUTES,
    PATTERN_IMMINENT_OPEN_POSITION_DEFLECTION_DEFAULT_ENABLED,
    PATTERN_IMMINENT_SCORE_FAILURE_DEFAULT_COOLDOWN_MINUTES,
    PATTERN_IMMINENT_SCORE_FAILURE_DEFAULT_MIN_FAILURES,
    PATTERN_IMMINENT_SCORE_DEFAULT_TIME_BUDGET_SECONDS,
    PATTERN_IMMINENT_SHADOW_POOR_EDGE_DEFAULT_LOOKBACK_HOURS,
    PATTERN_IMMINENT_SHADOW_POOR_EDGE_DEFAULT_MAX_AVG_RETURN_PCT,
    PATTERN_IMMINENT_SHADOW_POOR_EDGE_DEFAULT_MIN_REJECTS,
    settings,
)
from ...models.trading import (
    AlertHistory,
    AutoTraderRun,
    BreakoutAlert,
    ScanPattern,
    ScanResult,
    Trade,
)
from .alert_formatter import format_pattern_imminent
from .alerts import PATTERN_BREAKOUT_IMMINENT, dispatch_alert
from .market_data import DEFAULT_CRYPTO_TICKERS, DEFAULT_SCAN_TICKERS, fetch_ohlcv_df, is_crypto
from .opportunity_scoring import (
    compute_composite_score,
    eta_timeliness_score,
    evaluate_readiness_with_gates,
    feature_coverage_detail,
    overextension_penalty,
    pattern_quality_score,
    risk_reward_score,
    scan_pattern_eligible_main_imminent,
    parse_pattern_conditions,
)
from .pattern_engine import _condition_has_data, _eval_condition
from .pattern_ml import compute_condition_strength
from .portfolio import get_watchlist
from .prescreen_job import load_active_global_candidate_tickers
from .scanner import _estimate_hold_duration, _score_ticker, classify_trade_type
from .learning_predictions import _build_prediction_tickers

logger = logging.getLogger(__name__)

_HOURS_PER_BAR = {
    "1m": 1 / 60,
    "5m": 5 / 60,
    "15m": 0.25,
    "30m": 0.5,
    "1h": 1.0,
    "4h": 4.0,
    "1d": 6.5,
    "1wk": 32.5,
}
SHADOW_PROMOTED_STAGE = "shadow_promoted"
PILOT_PROMOTED_STAGE = "pilot_promoted"
PROMOTED_STAGE = "promoted"
LIVE_STAGE = "live"
POOR_EDGE_REJECT_REASON = "non_positive_expected_edge"
SECONDS_PER_MINUTE = 60.0
UNBOUNDED_SCORE_BUDGET = 10**9
UNKNOWN_LIFECYCLE_STAGE = "none"
READINESS_SKIP_BELOW_MIN = "readiness_below_min"
READINESS_SKIP_AT_OR_ABOVE_CAP = "readiness_at_or_above_cap"
STANDARD_SIGNAL_LANE = "standard"
SHADOW_NEAR_MISS_SIGNAL_LANE = "shadow_near_miss"
HARD_RECERT_SHADOW_SIGNAL_LANE = PATTERN_IMMINENT_HARD_RECERT_SHADOW_SIGNAL_LANE
EQUITY_SESSION_SHADOW_SIGNAL_LANE = (
    PATTERN_IMMINENT_EQUITY_SESSION_SHADOW_SIGNAL_LANE
)
SHADOW_OBSERVATION_SIGNAL_LANES = frozenset({
    SHADOW_NEAR_MISS_SIGNAL_LANE,
    HARD_RECERT_SHADOW_SIGNAL_LANE,
    EQUITY_SESSION_SHADOW_SIGNAL_LANE,
})
SHADOW_NEAR_MISS_SOURCE_FIXED_GAP = "fixed_gap"
SHADOW_NEAR_MISS_SOURCE_ADAPTIVE_BUFFER = "adaptive_priority_buffer"
CSV_TOKEN_SEPARATOR = ","
PORTFOLIO_GATE_RECERT_REASONS_KEY = "recert_reasons"
IMMINENT_SCORE_SKIP_PATTERN_ENGINE = True
PATTERN_ID_ROTATION_FALLBACK = 0
MIN_POSITIVE_CONFIG_INT = 1
FRACTION_UPPER_BOUND = 1.0
PERCENT_TO_FRACTION_DIVISOR = 100.0
RECENT_SWING_RESISTANCE_LOOKBACK_BARS = 20
AUTOTRADER_POSITION_DEFLECTION_VERSION = "v1"
AUTOTRADER_POSITION_DEFLECTION_OPEN_STATUS = "open"
AUTOTRADER_POSITION_DEFLECTION_WORKING_STATUS = "working"
AUTOTRADER_POSITION_DEFLECTION_STATUSES = (
    AUTOTRADER_POSITION_DEFLECTION_OPEN_STATUS,
    AUTOTRADER_POSITION_DEFLECTION_WORKING_STATUS,
)
PATTERN_IMMINENT_SCAN_ELIGIBLE_PRIORITY = 0
PATTERN_IMMINENT_SCAN_INELIGIBLE_PRIORITY = 1
PATTERN_IMMINENT_SCAN_PROMOTED_PRIORITY = 0
PATTERN_IMMINENT_SCAN_PILOT_PRIORITY = 1
PATTERN_IMMINENT_SCAN_SHADOW_PRIORITY = 2
PATTERN_IMMINENT_SCAN_DEFAULT_STAGE_PRIORITY = 9
PATTERN_IMMINENT_SCAN_RECERT_CLEAR_PRIORITY = 0
PATTERN_IMMINENT_SCAN_RECERT_DEBT_PRIORITY = 1
PATTERN_IMMINENT_SCAN_HARD_RECERT_SHADOW_PRIORITY = 3
PATTERN_IMMINENT_SCAN_STAGE_PRIORITY_BY_NAME = {
    LIVE_STAGE: PATTERN_IMMINENT_SCAN_PROMOTED_PRIORITY,
    PROMOTED_STAGE: PATTERN_IMMINENT_SCAN_PROMOTED_PRIORITY,
    PILOT_PROMOTED_STAGE: PATTERN_IMMINENT_SCAN_PILOT_PRIORITY,
    SHADOW_PROMOTED_STAGE: PATTERN_IMMINENT_SCAN_SHADOW_PRIORITY,
}
_COINBASE_SPOT_TICKER_CACHE: dict[str, Any] = {
    "expires_at": 0.0,
    "tickers": frozenset(),
}
_SCORE_FAILURE_CACHE: dict[str, dict[str, float | int]] = {}


def _non_negative_int_setting(name: str, default: int) -> int:
    try:
        return max(0, int(getattr(settings, name, default) or 0))
    except (TypeError, ValueError):
        return max(0, int(default))


def _non_negative_float_setting(name: str, default: float) -> float:
    try:
        return max(0.0, float(getattr(settings, name, default) or 0.0))
    except (TypeError, ValueError):
        return max(0.0, float(default))


def _fraction_float_setting(name: str, default: float) -> float:
    return min(FRACTION_UPPER_BOUND, _non_negative_float_setting(name, default))


def _positive_int_setting(name: str, default: int) -> int:
    try:
        return max(MIN_POSITIVE_CONFIG_INT, int(getattr(settings, name, default) or default))
    except (TypeError, ValueError):
        return max(MIN_POSITIVE_CONFIG_INT, int(default))


def _csv_stage_setting(name: str, default: str) -> frozenset[str]:
    raw = getattr(settings, name, default) or default
    if isinstance(raw, (set, frozenset, list, tuple)):
        values = raw
    else:
        values = str(raw).split(CSV_TOKEN_SEPARATOR)
    stages = {
        str(value).strip().lower()
        for value in values
        if str(value).strip()
    }
    return frozenset(stages)


def _normalized_token_set(raw: Any) -> frozenset[str]:
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        values: list[Any] = list(raw.split(CSV_TOKEN_SEPARATOR))
    elif isinstance(raw, (set, frozenset, list, tuple)):
        values = []
        for item in raw:
            if isinstance(item, str):
                values.extend(item.split(CSV_TOKEN_SEPARATOR))
            else:
                values.append(item)
    else:
        values = [raw]
    return frozenset(
        str(value).strip().lower()
        for value in values
        if str(value).strip()
    )


def _csv_token_setting(name: str, default: str) -> frozenset[str]:
    return _normalized_token_set(getattr(settings, name, default) or default)


def _recert_reason_tokens(pat: ScanPattern) -> frozenset[str]:
    reasons = set(_normalized_token_set(getattr(pat, "recert_reason", None)))
    gate_json = getattr(pat, "portfolio_gate_json", None)
    if isinstance(gate_json, str):
        try:
            gate_json = json.loads(gate_json)
        except Exception:
            gate_json = None
    if isinstance(gate_json, dict):
        reasons.update(
            _normalized_token_set(gate_json.get(PORTFOLIO_GATE_RECERT_REASONS_KEY))
        )
    return frozenset(reasons)


def _hard_recert_shadow_reasons_for_pattern(
    pat: ScanPattern,
    *,
    enabled: bool,
    lifecycle_stages: frozenset[str],
    hard_reasons: frozenset[str],
) -> frozenset[str]:
    if not enabled or not bool(getattr(pat, "recert_required", False)):
        return frozenset()
    stage = _lifecycle_stage(pat)
    promotion_status = (getattr(pat, "promotion_status", None) or "").strip().lower()
    if stage not in lifecycle_stages and promotion_status not in lifecycle_stages:
        return frozenset()
    return frozenset(_recert_reason_tokens(pat) & hard_reasons)


def _open_autotrader_position_keys(
    db: Session,
    user_id: int | None,
) -> set[tuple[int, str]]:
    """Exact pattern/ticker live positions the scanner should deflect."""
    try:
        q = db.query(Trade.scan_pattern_id, Trade.ticker).filter(
            Trade.auto_trader_version == AUTOTRADER_POSITION_DEFLECTION_VERSION,
            Trade.status.in_(AUTOTRADER_POSITION_DEFLECTION_STATUSES),
            Trade.scan_pattern_id.isnot(None),
        )
        if user_id is not None:
            q = q.filter(Trade.user_id == user_id)
        out: set[tuple[int, str]] = set()
        for pattern_id, ticker in q.all():
            try:
                pid = int(pattern_id or 0)
            except (TypeError, ValueError):
                pid = 0
            ticker_key = str(ticker or "").strip().upper()
            if pid > 0 and ticker_key:
                out.add((pid, ticker_key))
        return out
    except Exception:
        logger.debug("[pattern_imminent] open-position deflection lookup failed", exc_info=True)
        return set()


def _lifecycle_stage(pat: ScanPattern) -> str:
    return (getattr(pat, "lifecycle_stage", None) or "").strip().lower()


def _pattern_priority_quality(pat: ScanPattern) -> float:
    try:
        stored = getattr(pat, "quality_composite_score", None)
        if stored is not None:
            return float(stored)
        return float(pattern_quality_score(pat))
    except (TypeError, ValueError):
        return 0.0


def _imminent_scan_priority_key(pat: ScanPattern) -> tuple[int, int, int, float, int, int]:
    stage = _lifecycle_stage(pat)
    promotion_status = (getattr(pat, "promotion_status", None) or "").strip().lower()
    eligible_priority = (
        PATTERN_IMMINENT_SCAN_ELIGIBLE_PRIORITY
        if scan_pattern_eligible_main_imminent(pat)
        else PATTERN_IMMINENT_SCAN_INELIGIBLE_PRIORITY
    )
    stage_priority = PATTERN_IMMINENT_SCAN_STAGE_PRIORITY_BY_NAME.get(
        stage,
        (
            PATTERN_IMMINENT_SCAN_PROMOTED_PRIORITY
            if promotion_status == PROMOTED_STAGE
            else PATTERN_IMMINENT_SCAN_DEFAULT_STAGE_PRIORITY
        ),
    )
    recert_priority = (
        PATTERN_IMMINENT_SCAN_RECERT_DEBT_PRIORITY
        if bool(getattr(pat, "recert_required", False))
        else PATTERN_IMMINENT_SCAN_RECERT_CLEAR_PRIORITY
    )
    hard_recert_reasons = _hard_recert_shadow_reasons_for_pattern(
        pat,
        enabled=bool(
            getattr(settings, "pattern_imminent_hard_recert_shadow_enabled", True)
        ),
        lifecycle_stages=_csv_stage_setting(
            "pattern_imminent_hard_recert_shadow_lifecycle_stages",
            PATTERN_IMMINENT_DEFAULT_HARD_RECERT_SHADOW_LIFECYCLE_STAGES,
        ),
        hard_reasons=_csv_token_setting(
            "pattern_imminent_hard_recert_shadow_reasons",
            PATTERN_IMMINENT_DEFAULT_HARD_RECERT_SHADOW_REASONS,
        ),
    )
    if hard_recert_reasons:
        stage_priority = max(
            stage_priority,
            PATTERN_IMMINENT_SCAN_HARD_RECERT_SHADOW_PRIORITY,
        )
        recert_priority = PATTERN_IMMINENT_SCAN_HARD_RECERT_SHADOW_PRIORITY
    evidence_count = int(getattr(pat, "evidence_count", 0) or 0)
    pattern_id = _pattern_id_for_rotation(pat)
    return (
        eligible_priority,
        stage_priority,
        recert_priority,
        -_pattern_priority_quality(pat),
        -evidence_count,
        pattern_id,
    )


def _pattern_id_for_rotation(pat: ScanPattern) -> int:
    try:
        return int(getattr(pat, "id", None) or PATTERN_ID_ROTATION_FALLBACK)
    except (TypeError, ValueError):
        return PATTERN_ID_ROTATION_FALLBACK


def _rotated_ticker_cap_slice(
    tickers: list[str],
    *,
    cap: int,
    pat: ScanPattern,
    enabled: bool,
    window_minutes: int,
    explore_count: int,
    now_utc: datetime | None = None,
) -> tuple[list[str], dict[str, Any] | None]:
    if len(tickers) <= cap:
        return list(tickers), None
    if not enabled:
        return tickers[:cap], None

    window = max(PATTERN_IMMINENT_MIN_TICKER_ROTATION_WINDOW_MINUTES, int(window_minutes))
    explore = max(MIN_POSITIVE_CONFIG_INT, min(int(explore_count), cap))
    stable_count = max(0, cap - explore)
    stable = tickers[:stable_count]
    rotation_pool = tickers[stable_count:] or tickers
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    bucket_seconds = window * SECONDS_PER_MINUTE
    bucket = int(now.timestamp() // bucket_seconds)
    pattern_offset = _pattern_id_for_rotation(pat)
    start = ((bucket + pattern_offset) * explore) % len(rotation_pool)
    rotated = rotation_pool[start:] + rotation_pool[:start]
    return (stable + rotated[:explore])[:cap], {
        "pattern_id": getattr(pat, "id", None),
        "available_tickers": len(tickers),
        "cap": cap,
        "start": start,
        "stable_count": stable_count,
        "explore_count": explore,
        "rotation_pool": len(rotation_pool),
        "window_minutes": window,
    }


def us_stock_session_open(now_utc: datetime | None = None) -> bool:
    """True during Mon–Fri US/Eastern 9:30–16:00 (inclusive start, exclusive end at 16:00)."""
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    et = now.astimezone(ZoneInfo("US/Eastern"))
    if et.weekday() >= 5:
        return False
    t = et.time()
    open_t = time(9, 30)
    close_t = time(16, 0)
    return open_t <= t < close_t


def us_stock_extended_session_open(now_utc: datetime | None = None) -> bool:
    """True during Mon–Fri US/Eastern 04:00–20:00 — pre + RTH + post.

    Robinhood's 24/5 window covers ~04:00–20:00 ET for most tickers (plus
    narrower overnight sessions for a subset). This helper is intentionally
    conservative: it keeps the AutoTrader away from weekends entirely and
    gates on standard extended hours so entries/exits can attempt fills
    outside RTH when ``chili_autotrader_allow_extended_hours`` is set. The
    adapter itself decides whether market-order vs limit-order is appropriate
    and surfaces rejection as a ``sell_fail`` / ``error`` without corrupting
    position state.
    """
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    et = now.astimezone(ZoneInfo("US/Eastern"))
    if et.weekday() >= 5:
        return False
    t = et.time()
    return time(4, 0) <= t < time(20, 0)


def describe_us_session_context(now_utc: datetime | None = None) -> dict[str, Any]:
    """US equity session label for UI (premarket / regular / after_hours / closed)."""
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    et = now.astimezone(ZoneInfo("US/Eastern"))
    if et.weekday() >= 5:
        return {
            "us_session": "closed",
            "label": "US stocks: weekend (closed)",
            "equity_evaluation_active": False,
        }
    t = et.time()
    pre_open = time(4, 0)
    reg_open = time(9, 30)
    reg_close = time(16, 0)
    post_close = time(20, 0)
    if t < pre_open or t >= post_close:
        return {
            "us_session": "closed",
            "label": "US stocks: session closed",
            "equity_evaluation_active": False,
        }
    if pre_open <= t < reg_open:
        return {
            "us_session": "premarket",
            "label": "US stocks: premarket",
            "equity_evaluation_active": False,
        }
    if reg_open <= t < reg_close:
        return {
            "us_session": "regular_hours",
            "label": "US stocks: regular session",
            "equity_evaluation_active": True,
        }
    return {
        "us_session": "after_hours",
        "label": "US stocks: after hours",
        "equity_evaluation_active": False,
    }


def timeframe_to_hours_per_step(timeframe: str | None) -> float:
    tf = (timeframe or "1d").strip().lower()
    return _HOURS_PER_BAR.get(tf, 6.5)


def estimate_breakout_eta_hours(
    readiness: float,
    timeframe: str | None,
    *,
    k: float,
    max_eta_hours: float,
) -> tuple[float, float]:
    h_step = timeframe_to_hours_per_step(timeframe)
    gap = max(0.0, min(1.0, 1.0 - readiness))
    center = k * gap * h_step
    min_h = 5 / 60
    center = max(min_h, min(max_eta_hours, center))
    low = max(min_h, center * 0.5)
    high = min(max_eta_hours, center * 1.5)
    if high < low:
        low, high = high, low
    return low, high


def format_eta_range(low_h: float, high_h: float) -> str:
    if high_h < 1:
        lo_m = max(1, int(low_h * 60))
        hi_m = max(lo_m, int(high_h * 60))
        return f"~{lo_m}–{hi_m} min" if lo_m != hi_m else f"~{lo_m} min"
    if high_h < 24:
        return f"~{low_h:.1f}–{high_h:.1f} hours"
    d_lo = low_h / 24
    d_hi = high_h / 24
    return f"~{d_lo:.1f}–{d_hi:.1f} days"


def recent_swing_resistance(ticker: str) -> float | None:
    try:
        df = fetch_ohlcv_df(ticker, period="3mo", interval="1d")
        if df is None or df.empty or "High" not in df.columns:
            return None
        hi = df["High"].tail(RECENT_SWING_RESISTANCE_LOOKBACK_BARS)
        v = float(hi.max())
        return v if v > 0 else None
    except Exception:
        return None


def resistance_from_score(score: dict[str, Any]) -> float | None:
    for container in (score, score.get("indicators") or {}):
        try:
            value = container.get("resistance")
        except AttributeError:
            continue
        try:
            resistance = float(value)
        except (TypeError, ValueError):
            continue
        if resistance > 0:
            return resistance
    return None


def _flat_indicator_value_supported(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, float) and value != value:
        return False
    return isinstance(value, (bool, int, float, str))


def _copy_scalar_indicator_fields(
    flat: dict[str, Any],
    source: dict[str, Any],
) -> None:
    for key, value in source.items():
        key_s = str(key or "").strip()
        if not key_s or key_s in flat:
            continue
        if _flat_indicator_value_supported(value):
            flat[key_s] = value


def flat_indicators_from_score(
    score: dict[str, Any],
    *,
    resistance: float | None,
) -> dict[str, Any]:
    price = float(score.get("price") or score.get("entry_price") or 0)
    ind = score.get("indicators") or {}
    flat: dict[str, Any] = {"price": price}
    _copy_scalar_indicator_fields(flat, score)
    _copy_scalar_indicator_fields(flat, ind)

    rsi = ind.get("rsi")
    if rsi is not None:
        flat["rsi_14"] = float(rsi)

    for key in (
        "macd_hist",
        "adx",
        "atr",
        "ema_20",
        "ema_50",
        "ema_100",
        "stoch_k",
        "stoch_d",
    ):
        v = ind.get(key)
        if v is not None:
            flat[key] = float(v)

    macd_hist = flat.get("macd_hist")
    if macd_hist is not None:
        flat["macd_histogram"] = float(macd_hist)

    stoch_k = flat.get("stoch_k")
    if stoch_k is not None:
        flat["stochastic_k"] = float(stoch_k)
    stoch_d = flat.get("stoch_d")
    if stoch_d is not None:
        flat["stochastic_d"] = float(stoch_d)

    # R34 (2026-04-30): scanner output has 'vol_ratio' inside score['indicators']
    # AND sometimes at the top-level of score (intraday _score_ticker_intraday).
    # Promoted patterns reference indicator keys 'volume_ratio' and 'gap_pct'
    # by name in their conditions JSON (see indicator_core compute_all_from_df
    # which exposes both as canonical names). Without these aliases in flat,
    # _condition_has_data returns False for every condition that names them
    # and pattern_imminent_alerts logs every crypto candidate as
    # 'readiness_unusable / missing_indicators=[volume_ratio,gap_pct]' even
    # when egress is healthy and the scanner did populate the underlying
    # numbers. Fix: copy through with both names so condition lookup matches.
    vr = ind.get("vol_ratio")
    if vr is None:
        vr = score.get("vol_ratio")
    if vr is not None:
        flat["rel_vol"] = float(vr)
        flat["volume_ratio"] = float(vr)

    gp = ind.get("gap_pct")
    if gp is None:
        gp = score.get("gap_pct")
    if gp is not None:
        flat["gap_pct"] = float(gp)

    bb_pct = ind.get("bb_pct")
    if bb_pct is not None:
        bb_pct_f = float(bb_pct)
        flat["bb_pct_percent"] = (
            bb_pct_f
            if abs(bb_pct_f) > FRACTION_UPPER_BOUND
            else bb_pct_f * PERCENT_TO_FRACTION_DIVISOR
        )
        flat["bb_pct"] = (
            bb_pct_f / PERCENT_TO_FRACTION_DIVISOR
            if abs(bb_pct_f) > FRACTION_UPPER_BOUND
            else bb_pct_f
        )

    if resistance and price > 0:
        flat["resistance"] = float(resistance)
        flat["dist_to_resistance_pct"] = round((resistance - price) / price * 100, 4)

    return flat


def evaluate_imminent_readiness(
    conditions: list[dict[str, Any]],
    flat: dict[str, Any],
    *,
    evaluable_ratio_floor: float,
    min_evaluable_for_wide_patterns: int = 2,
) -> tuple[float | None, bool, float]:
    """Backward-compatible readiness; delegates to shared gates."""
    readiness, all_pass, ratio, _miss = evaluate_readiness_with_gates(
        conditions,
        flat,
        min_coverage_ratio=evaluable_ratio_floor,
        min_evaluable_absolute=min_evaluable_for_wide_patterns,
        allow_shortcut_two_evaluable=True,
    )
    return readiness, all_pass, ratio


def _parse_scope_tickers(pat: ScanPattern) -> list[str]:
    raw = getattr(pat, "scope_tickers", None) or ""
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(x).strip().upper() for x in data if x]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def _crypto_execution_filter_enabled() -> bool:
    return bool(getattr(settings, "pattern_imminent_filter_crypto_to_coinbase_spot", True))


def _coinbase_spot_ticker_set() -> frozenset[str]:
    """Return cached Coinbase USD spot products; empty means fail open."""
    if not _crypto_execution_filter_enabled():
        return frozenset()

    ttl_s = max(
        0,
        int(
            getattr(
                settings,
                "pattern_imminent_coinbase_spot_filter_ttl_seconds",
                PATTERN_IMMINENT_COINBASE_SPOT_FILTER_DEFAULT_TTL_SECONDS,
            )
            or PATTERN_IMMINENT_COINBASE_SPOT_FILTER_DEFAULT_TTL_SECONDS
        ),
    )
    now = _time.monotonic()
    cached = _COINBASE_SPOT_TICKER_CACHE.get("tickers")
    if (
        isinstance(cached, frozenset)
        and cached
        and now < float(_COINBASE_SPOT_TICKER_CACHE.get("expires_at") or 0.0)
    ):
        return cached

    try:
        from .venue.coinbase_spot import CoinbaseSpotAdapter

        rows = CoinbaseSpotAdapter().list_usd_spot_universe_entries()
    except Exception:
        logger.debug("[pattern_imminent] Coinbase spot universe filter unavailable", exc_info=True)
        return frozenset()

    tickers = frozenset(
        str(row.get("ticker") or "").strip().upper()
        for row in rows
        if row.get("ticker")
    )
    if tickers:
        _COINBASE_SPOT_TICKER_CACHE["tickers"] = tickers
        _COINBASE_SPOT_TICKER_CACHE["expires_at"] = now + ttl_s
    return tickers


def _filter_crypto_to_execution_universe(
    tickers: list[str],
    *,
    coinbase_spot_tickers: frozenset[str] | None = None,
) -> tuple[list[str], int, int]:
    """Drop crypto symbols that cannot graduate into Coinbase spot execution."""
    if not _crypto_execution_filter_enabled():
        return tickers, 0, 0
    spot_tickers = (
        coinbase_spot_tickers
        if coinbase_spot_tickers is not None
        else _coinbase_spot_ticker_set()
    )
    if not spot_tickers:
        return tickers, 0, 0

    kept: list[str] = []
    dropped = 0
    for ticker in tickers:
        ticker_u = str(ticker or "").strip().upper()
        if is_crypto(ticker_u) and ticker_u not in spot_tickers:
            dropped += 1
            continue
        kept.append(ticker)
    return kept, dropped, len(spot_tickers)


def _score_failure_cooldown_enabled() -> bool:
    return bool(getattr(settings, "pattern_imminent_score_failure_cooldown_enabled", True))


def _score_failure_cooldown_minutes() -> float:
    return max(
        0.0,
        _float_or_none(
            getattr(
                settings,
                "pattern_imminent_score_failure_cooldown_minutes",
                PATTERN_IMMINENT_SCORE_FAILURE_DEFAULT_COOLDOWN_MINUTES,
            )
        )
        or PATTERN_IMMINENT_SCORE_FAILURE_DEFAULT_COOLDOWN_MINUTES,
    )


def _score_failure_min_failures() -> int:
    return max(
        1,
        int(
            getattr(
                settings,
                "pattern_imminent_score_failure_min_failures",
                PATTERN_IMMINENT_SCORE_FAILURE_DEFAULT_MIN_FAILURES,
            )
            or PATTERN_IMMINENT_SCORE_FAILURE_DEFAULT_MIN_FAILURES
        ),
    )


def _score_time_budget_seconds() -> float:
    return max(
        0.0,
        _float_or_none(
            getattr(
                settings,
                "pattern_imminent_score_time_budget_seconds",
                PATTERN_IMMINENT_SCORE_DEFAULT_TIME_BUDGET_SECONDS,
            )
        )
        or PATTERN_IMMINENT_SCORE_DEFAULT_TIME_BUDGET_SECONDS,
    )


def _score_failure_cooldown_active(ticker: str) -> bool:
    if not _score_failure_cooldown_enabled():
        return False
    entry = _SCORE_FAILURE_CACHE.get(str(ticker or "").strip().upper())
    if not entry:
        return False
    now = _time.monotonic()
    cooldown_until = float(entry.get("cooldown_until") or 0.0)
    if cooldown_until <= 0.0:
        return False
    if now < cooldown_until:
        return True
    _SCORE_FAILURE_CACHE.pop(str(ticker or "").strip().upper(), None)
    return False


def _record_score_failure(ticker: str) -> None:
    if not _score_failure_cooldown_enabled():
        return
    cooldown_minutes = _score_failure_cooldown_minutes()
    if cooldown_minutes <= 0.0:
        return
    ticker_u = str(ticker or "").strip().upper()
    if not ticker_u:
        return

    now = _time.monotonic()
    entry = _SCORE_FAILURE_CACHE.get(ticker_u) or {}
    failures = int(entry.get("failures") or 0) + 1
    cooldown_until = float(entry.get("cooldown_until") or 0.0)
    if failures >= _score_failure_min_failures():
        cooldown_until = now + (cooldown_minutes * SECONDS_PER_MINUTE)
    _SCORE_FAILURE_CACHE[ticker_u] = {
        "failures": failures,
        "cooldown_until": cooldown_until,
    }


def _record_score_success(ticker: str) -> None:
    _SCORE_FAILURE_CACHE.pop(str(ticker or "").strip().upper(), None)


def build_imminent_ticker_universe(
    db: Session,
    user_id: int | None,
    cap: int,
) -> tuple[list[str], dict[str, int]]:
    """Merge watchlist, prescreen, predictions, scanner, defaults; dedupe; source counts."""
    seen: list[str] = []
    have: set[str] = set()
    counts: dict[str, int] = {}

    def add(t: str) -> None:
        t = (t or "").strip().upper()
        if not t or t in have:
            return
        have.add(t)
        seen.append(t)

    n0 = len(seen)
    try:
        for w in get_watchlist(db, user_id):
            add(getattr(w, "ticker", "") or "")
    except Exception:
        pass
    counts["watchlist"] = len(seen) - n0

    n1 = len(seen)
    if getattr(settings, "pattern_imminent_use_prescreener_universe", True):
        try:
            pre = load_active_global_candidate_tickers(db)
            m = max(0, int(getattr(settings, "pattern_imminent_max_prescreener_tickers", 80)))
            for t in pre[:m]:
                add(t)
        except Exception:
            pass
    counts["prescreener"] = len(seen) - n1

    n2 = len(seen)
    if getattr(settings, "pattern_imminent_use_predictions_universe", True):
        try:
            preds = _build_prediction_tickers(db, None)
            m = max(0, int(getattr(settings, "pattern_imminent_max_prediction_tickers", 40)))
            for t in preds[:m]:
                add(t)
        except Exception:
            pass
    counts["predictions"] = len(seen) - n2

    n3 = len(seen)
    if getattr(settings, "pattern_imminent_use_scanner_universe", True):
        try:
            m = max(0, int(getattr(settings, "pattern_imminent_max_scanner_tickers", 50)))
            q = db.query(ScanResult.ticker).order_by(desc(ScanResult.scanned_at)).limit(m)
            for (tk,) in q.all():
                add(tk or "")
        except Exception:
            pass
    counts["scanner"] = len(seen) - n3

    n4 = len(seen)
    for t in DEFAULT_SCAN_TICKERS[:35]:
        add(t)
    for t in DEFAULT_CRYPTO_TICKERS[:20]:
        add(t)
    counts["defaults"] = len(seen) - n4
    seen, dropped, spot_count = _filter_crypto_to_execution_universe(seen)
    counts["crypto_execution_filter_dropped"] = dropped
    counts["crypto_execution_filter_spot_tickers"] = spot_count

    return seen[:cap], counts


def _tickers_for_pattern(
    pat: ScanPattern,
    global_universe: list[str],
    *,
    equity_open: bool,
    allow_offsession_stock_shadow: bool = False,
) -> list[str]:
    scope = (getattr(pat, "ticker_scope", None) or "universal").strip().lower()
    ac = (getattr(pat, "asset_class", None) or "all").strip().lower()

    # 2026-04-28 audit fix: honor 'explicit_list' the same as 'ticker_specific'.
    # The ticker_scope_autotuner (scripts/brain_worker.py:496) writes
    # ticker_scope='explicit_list' when it narrows a pattern's scope to its
    # edge tickers (see ticker_scope_autotune.py:172). Before this fix, the
    # matcher silently fell into the 'else' branch and the autotuner's
    # narrowing was a no-op — which is why pattern 1052 (scope_tickers
    # ['ACMR','INFQ']) was firing alerts for ABNB/RAY-USD/DOGE-USD/ETH-USD.
    if scope in ("ticker_specific", "explicit_list"):
        scoped = _parse_scope_tickers(pat)
        cap = max(1, int(settings.pattern_imminent_scope_tickers_cap))
        scoped = scoped[:cap]
        if not scoped:
            return []
        tickers = scoped
    else:
        tickers = list(global_universe)

    out: list[str] = []
    for t in tickers:
        cr = is_crypto(t)
        if ac == "crypto" and not cr:
            continue
        if ac == "stocks" and cr:
            continue
        if ac == "stocks" and not equity_open and not allow_offsession_stock_shadow:
            continue
        if ac == "all":
            if not cr and not equity_open and not allow_offsession_stock_shadow:
                continue
        out.append(t)
    return out


def _pattern_conditions(pat: ScanPattern) -> list[dict[str, Any]]:
    return parse_pattern_conditions(pat.rules_json)


def _cooldown_active(
    db: Session,
    user_id: int | None,
    ticker: str,
    pattern_id: int,
    hours: float,
) -> bool:
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    q = (
        db.query(AlertHistory)
        .filter(
            AlertHistory.alert_type == PATTERN_BREAKOUT_IMMINENT,
            AlertHistory.ticker == ticker,
            AlertHistory.success.is_(True),
            AlertHistory.created_at >= cutoff,
        )
    )
    if user_id is not None:
        q = q.filter(AlertHistory.user_id == user_id)
    rows = q.order_by(AlertHistory.created_at.desc()).limit(25).all()
    for r in rows:
        spid = getattr(r, "scan_pattern_id", None)
        if spid is not None and int(spid) == int(pattern_id):
            return True
    return False


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _shadow_poor_edge_pattern_ids(
    db: Session,
    patterns: list[ScanPattern],
    user_id: int | None,
) -> tuple[set[int], dict[int, int]]:
    """Shadow-promoted patterns to pause because recent edge rejects dominate.

    This is scanner-slot hygiene, not a promotion/demotion decision. It only
    applies when the pattern's stored average return is already non-positive
    and AutoTrader has recently rejected it repeatedly for expected-edge math.
    """
    if not bool(
        getattr(settings, "pattern_imminent_shadow_poor_edge_cooldown_enabled", True)
    ):
        return set(), {}

    lookback_h = max(
        0.0,
        _float_or_none(
            getattr(
                settings,
                "pattern_imminent_shadow_poor_edge_lookback_hours",
                PATTERN_IMMINENT_SHADOW_POOR_EDGE_DEFAULT_LOOKBACK_HOURS,
            )
        )
        or 0.0,
    )
    if lookback_h <= 0.0:
        return set(), {}

    min_rejects = max(
        1,
        int(
            getattr(
                settings,
                "pattern_imminent_shadow_poor_edge_min_rejects",
                PATTERN_IMMINENT_SHADOW_POOR_EDGE_DEFAULT_MIN_REJECTS,
            )
            or PATTERN_IMMINENT_SHADOW_POOR_EDGE_DEFAULT_MIN_REJECTS
        ),
    )
    max_avg_return = (
        _float_or_none(
            getattr(
                settings,
                "pattern_imminent_shadow_poor_edge_max_avg_return_pct",
                PATTERN_IMMINENT_SHADOW_POOR_EDGE_DEFAULT_MAX_AVG_RETURN_PCT,
            )
        )
        or PATTERN_IMMINENT_SHADOW_POOR_EDGE_DEFAULT_MAX_AVG_RETURN_PCT
    )

    candidate_ids: list[int] = []
    for pat in patterns:
        stage = (getattr(pat, "lifecycle_stage", "") or "").strip().lower()
        if stage != SHADOW_PROMOTED_STAGE:
            continue
        avg_return = _float_or_none(getattr(pat, "avg_return_pct", None))
        if avg_return is None or avg_return > max_avg_return:
            continue
        try:
            candidate_ids.append(int(pat.id))
        except (TypeError, ValueError):
            continue

    if not candidate_ids:
        return set(), {}

    cutoff = datetime.utcnow() - timedelta(hours=lookback_h)
    q = (
        db.query(
            AutoTraderRun.scan_pattern_id,
            func.count(AutoTraderRun.id),
        )
        .filter(
            AutoTraderRun.scan_pattern_id.in_(candidate_ids),
            AutoTraderRun.reason == POOR_EDGE_REJECT_REASON,
            AutoTraderRun.created_at >= cutoff,
        )
        .group_by(AutoTraderRun.scan_pattern_id)
    )
    if user_id is not None:
        q = q.filter(AutoTraderRun.user_id == user_id)
    counts = {
        int(pattern_id): int(count)
        for pattern_id, count in q.all()
        if pattern_id is not None
    }
    return {pid for pid, count in counts.items() if count >= min_rejects}, counts


def _insert_imminent_breakout_alert(
    db: Session,
    user_id: int | None,
    pat: ScanPattern,
    ticker: str,
    score: dict[str, Any],
    flat: dict[str, Any],
    *,
    composite: float,
    score_breakdown: dict[str, float],
    readiness: float,
    coverage_ratio: float,
    eta_lo: float,
    eta_hi: float,
    signal_lane: str | None = None,
    readiness_gap_to_min: float | None = None,
    shadow_near_miss_source: str | None = None,
    hard_recert_reasons: list[str] | None = None,
) -> None:
    price = float(score.get("price") or 0)
    scorecard = {
        "composite": composite,
        "breakdown": score_breakdown,
        "readiness": readiness,
        "feature_coverage": coverage_ratio,
        "eta_hours": [eta_lo, eta_hi],
        "lifecycle_stage": getattr(pat, "lifecycle_stage", None),
        "promotion_status": getattr(pat, "promotion_status", None),
        "signal_lane": signal_lane or STANDARD_SIGNAL_LANE,
    }
    if readiness_gap_to_min is not None:
        scorecard["readiness_gap_to_min"] = readiness_gap_to_min
    if shadow_near_miss_source:
        scorecard["shadow_near_miss_source"] = shadow_near_miss_source
    if hard_recert_reasons:
        scorecard["hard_recert_reasons"] = hard_recert_reasons
    snap = {
        "flat_indicators": {k: v for k, v in flat.items() if v is not None},
        "imminent_scorecard": scorecard,
    }
    asset = "crypto" if is_crypto(ticker) else "stock"
    row = BreakoutAlert(
        ticker=ticker,
        asset_type=asset,
        alert_tier="pattern_imminent",
        score_at_alert=composite,
        indicator_snapshot=snap,
        price_at_alert=price,
        entry_price=score.get("entry_price"),
        stop_loss=score.get("stop_loss"),
        target_price=score.get("take_profit"),
        signals_snapshot={"signals": (score.get("signals") or [])[:12]},
        outcome="pending",
        user_id=user_id,
        scan_pattern_id=pat.id,
        timeframe=(pat.timeframe or "1d")[:10],
    )
    db.add(row)
    db.flush()
    try:
        from .contracts.signal_emit import emit_signal_for_breakout_alert

        emit_signal_for_breakout_alert(
            db,
            row,
            scanner="pattern_imminent",
            strategy_family=pat.name or f"pattern_{pat.id}",
            commit=False,
        )
    except Exception as _use:
        logger.debug(
            "[unified_signal] imminent breakout emit skipped: %s",
            _use,
            exc_info=True,
        )
    db.commit()


def gather_imminent_candidate_rows(
    db: Session,
    user_id: int | None,
    *,
    equity_session_open: bool | None = None,
    all_active_patterns: bool = False,
    apply_main_dispatch_filters: bool = False,
    for_opportunity_board: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Score (pattern × ticker) rows using shared composite math.

    *all_active_patterns*: when False, only promoted/live (or legacy promoted) patterns.
    *apply_main_dispatch_filters*: when True, enforce main Telegram coverage + composite floors.
    *for_opportunity_board*: when True, apply tighter universe/per-pattern/total score caps so
      the HTTP board stays within latency budgets. Does **not** change main imminent dispatch
      (call with ``for_opportunity_board=False`` there).
    """
    eq_open = equity_session_open if equity_session_open is not None else us_stock_session_open()
    max_eta = float(settings.pattern_imminent_max_eta_hours)
    min_rd = float(settings.pattern_imminent_min_readiness)
    cap_rd = float(settings.pattern_imminent_readiness_cap)
    max_tickers = int(settings.pattern_imminent_max_tickers_per_run)
    if for_opportunity_board:
        cap_u = int(getattr(settings, "opportunity_board_max_universe_cap", 80))
        max_tickers = max(1, min(max_tickers, cap_u))
    eval_floor_board = float(settings.pattern_imminent_evaluable_ratio_floor)
    min_cov_main = float(getattr(settings, "pattern_imminent_min_feature_coverage_main", 0.45))
    min_comp_main = float(getattr(settings, "pattern_imminent_min_composite_main", 0.42))
    allow_shortcut = bool(getattr(settings, "pattern_imminent_allow_evaluable_shortcut", True))
    k_eta = float(settings.pattern_imminent_eta_scale_k)
    score_time_budget_s = _score_time_budget_seconds()
    score_started_at = _time.monotonic()
    suppressed_limit = _non_negative_int_setting(
        "pattern_imminent_suppressed_diagnostic_limit",
        PATTERN_IMMINENT_DEFAULT_SUPPRESSED_DIAGNOSTIC_LIMIT,
    )
    missing_indicator_limit = _non_negative_int_setting(
        "pattern_imminent_missing_indicator_sample_limit",
        PATTERN_IMMINENT_DEFAULT_MISSING_INDICATOR_SAMPLE_LIMIT,
    )
    readiness_near_miss_limit = _non_negative_int_setting(
        "pattern_imminent_readiness_near_miss_limit",
        PATTERN_IMMINENT_DEFAULT_READINESS_NEAR_MISS_LIMIT,
    )
    shadow_near_miss_enabled = bool(
        getattr(settings, "pattern_imminent_shadow_near_miss_enabled", True)
    )
    shadow_near_miss_max_gap = _non_negative_float_setting(
        "pattern_imminent_shadow_near_miss_max_gap",
        PATTERN_IMMINENT_DEFAULT_SHADOW_NEAR_MISS_MAX_GAP,
    )
    shadow_near_miss_adaptive_enabled = bool(
        getattr(
            settings,
            "pattern_imminent_shadow_near_miss_adaptive_enabled",
            PATTERN_IMMINENT_DEFAULT_SHADOW_NEAR_MISS_ADAPTIVE_ENABLED,
        )
    )
    shadow_near_miss_adaptive_max_per_run = _non_negative_int_setting(
        "pattern_imminent_shadow_near_miss_adaptive_max_per_run",
        PATTERN_IMMINENT_DEFAULT_SHADOW_NEAR_MISS_ADAPTIVE_MAX_PER_RUN,
    )
    shadow_near_miss_adaptive_min_readiness_fraction = _fraction_float_setting(
        "pattern_imminent_shadow_near_miss_adaptive_min_readiness_fraction",
        PATTERN_IMMINENT_DEFAULT_SHADOW_NEAR_MISS_ADAPTIVE_MIN_READINESS_FRACTION,
    )
    shadow_near_miss_adaptive_min_readiness = (
        min_rd * shadow_near_miss_adaptive_min_readiness_fraction
    )
    shadow_near_miss_lifecycle_stages = _csv_stage_setting(
        "pattern_imminent_shadow_near_miss_lifecycle_stages",
        PATTERN_IMMINENT_DEFAULT_SHADOW_NEAR_MISS_LIFECYCLE_STAGES,
    )
    hard_recert_shadow_enabled = (
        bool(getattr(settings, "pattern_imminent_hard_recert_shadow_enabled", True))
        and bool(apply_main_dispatch_filters)
        and not bool(for_opportunity_board)
    )
    hard_recert_shadow_lifecycle_stages = _csv_stage_setting(
        "pattern_imminent_hard_recert_shadow_lifecycle_stages",
        PATTERN_IMMINENT_DEFAULT_HARD_RECERT_SHADOW_LIFECYCLE_STAGES,
    )
    hard_recert_shadow_reasons = _csv_token_setting(
        "pattern_imminent_hard_recert_shadow_reasons",
        PATTERN_IMMINENT_DEFAULT_HARD_RECERT_SHADOW_REASONS,
    )
    offsession_stock_shadow_enabled = (
        bool(
            getattr(
                settings,
                "pattern_imminent_offsession_stock_shadow_enabled",
                PATTERN_IMMINENT_OFFSESSION_STOCK_SHADOW_DEFAULT_ENABLED,
            )
        )
        and bool(apply_main_dispatch_filters)
        and not bool(for_opportunity_board)
    )
    ticker_rotation_enabled = (
        bool(getattr(settings, "pattern_imminent_ticker_rotation_enabled", True))
        and bool(apply_main_dispatch_filters)
        and not bool(for_opportunity_board)
    )
    ticker_rotation_window_minutes = _positive_int_setting(
        "pattern_imminent_ticker_rotation_window_minutes",
        PATTERN_IMMINENT_DEFAULT_TICKER_ROTATION_WINDOW_MINUTES,
    )
    ticker_rotation_explore_tickers = _positive_int_setting(
        "pattern_imminent_ticker_rotation_explore_tickers",
        PATTERN_IMMINENT_DEFAULT_TICKER_ROTATION_EXPLORE_TICKERS,
    )
    open_position_deflection_enabled = (
        bool(
            getattr(
                settings,
                "pattern_imminent_open_position_deflection_enabled",
                PATTERN_IMMINENT_OPEN_POSITION_DEFLECTION_DEFAULT_ENABLED,
            )
        )
        and bool(apply_main_dispatch_filters)
        and not bool(for_opportunity_board)
    )

    patterns = sorted(
        db.query(ScanPattern).filter(ScanPattern.active.is_(True)).all(),
        key=_imminent_scan_priority_key,
    )
    poor_shadow_ids, poor_shadow_counts = _shadow_poor_edge_pattern_ids(
        db,
        patterns,
        user_id,
    )
    global_uni, uni_counts = build_imminent_ticker_universe(db, user_id, max_tickers)
    coinbase_spot_tickers = _coinbase_spot_ticker_set()
    open_position_keys = (
        _open_autotrader_position_keys(db, user_id)
        if open_position_deflection_enabled
        else set()
    )

    candidates: list[dict[str, Any]] = []
    patterns_tried = 0
    tickers_scored = 0
    score_cache: dict[str, dict[str, Any] | None] = {}
    score_cooldown_keys: set[str] = set()
    score_cache_hits = 0
    score_cache_misses = 0
    skip: dict[str, int] = {
        "pattern_no_tickers": 0,
        "pattern_no_conditions": 0,
        "score_failed": 0,
        "readiness_unusable": 0,
        "all_conditions_met": 0,
        "readiness_outside_band": 0,
        READINESS_SKIP_BELOW_MIN: 0,
        READINESS_SKIP_AT_OR_ABOVE_CAP: 0,
        "eta_too_long": 0,
        "excluded_promotion_lifecycle": 0,
        "insufficient_coverage_main": 0,
        "below_composite_main": 0,
        "shadow_poor_edge_cooldown": 0,
        "crypto_execution_universe_filtered": 0,
        "score_failure_cooldown": 0,
        "open_position_deflected": 0,
    }
    excluded_lifecycle_by_stage: Counter[str] = Counter()
    suppressed: list[dict[str, Any]] = []
    readiness_band_near_misses: list[dict[str, Any]] = []
    pending_shadow_near_misses: list[dict[str, Any]] = []
    shadow_near_miss_eligible = 0
    shadow_near_miss_admitted = 0
    shadow_near_miss_gap_eligible = 0
    shadow_near_miss_gap_admitted = 0
    shadow_near_miss_adaptive_eligible = 0
    shadow_near_miss_adaptive_selected = 0
    shadow_near_miss_adaptive_admitted = 0
    shadow_near_miss_adaptive_skipped_not_selected = 0
    hard_recert_shadow_patterns = 0
    hard_recert_shadow_eligible = 0
    hard_recert_shadow_admitted = 0
    hard_recert_shadow_reason_counts: Counter[str] = Counter()
    offsession_stock_shadow_admitted = 0
    ticker_rotation_applied = 0
    ticker_rotation_total_available = 0
    ticker_rotation_samples: list[dict[str, Any]] = []
    pattern_priority_top_stage_counts: Counter[str] = Counter(
        _lifecycle_stage(pat) or UNKNOWN_LIFECYCLE_STAGE
        for pat in patterns[: max(0, suppressed_limit)]
    )

    def _append_suppressed(row: dict[str, Any]) -> None:
        if suppressed_limit <= 0 or len(suppressed) >= suppressed_limit:
            return
        suppressed.append(row)

    def _sample_missing_indicators(missing: list[str]) -> list[str]:
        if missing_indicator_limit <= 0:
            return []
        return missing[:missing_indicator_limit]

    def _append_readiness_band_miss(
        *,
        reason: str,
        ticker: str,
        pat: ScanPattern,
        readiness: float,
        coverage: float,
        missing: list[str],
        gap: float,
    ) -> None:
        if readiness_near_miss_limit <= 0:
            return
        readiness_band_near_misses.append({
            "ticker": ticker,
            "pattern_id": pat.id,
            "lifecycle_stage": (
                (getattr(pat, "lifecycle_stage", None) or UNKNOWN_LIFECYCLE_STAGE)
                .strip()
                .lower()
            ),
            "reason": reason,
            "readiness": round(float(readiness), 4),
            "coverage": round(float(coverage), 4),
            "gap": round(max(0.0, float(gap)), 4),
            "missing_indicators": _sample_missing_indicators(missing),
        })

    per_pat_cap = max(
        1,
        int(
            getattr(
                settings,
                "pattern_imminent_max_tickers_per_pattern",
                PATTERN_IMMINENT_DEFAULT_MAX_TICKERS_PER_PATTERN,
            )
            or PATTERN_IMMINENT_DEFAULT_MAX_TICKERS_PER_PATTERN
        ),
    )
    score_budget = UNBOUNDED_SCORE_BUDGET
    if for_opportunity_board:
        per_pat_cap = max(1, int(getattr(settings, "opportunity_board_max_tickers_per_pattern", 10)))
        score_budget = max(1, int(getattr(settings, "opportunity_board_max_ticker_scores_per_request", 360)))

    board_budget_hit = False
    score_time_budget_hit = False

    def _score_time_budget_hit() -> bool:
        if score_time_budget_s <= 0.0:
            return False
        return (_time.monotonic() - score_started_at) >= score_time_budget_s

    def _score_cache_key(raw_ticker: str) -> str:
        return str(raw_ticker or "").strip().upper()

    def _score_ticker_cached(raw_ticker: str) -> dict[str, Any] | None:
        nonlocal score_cache_hits, score_cache_misses
        cache_key = _score_cache_key(raw_ticker)
        if cache_key in score_cache:
            score_cache_hits += 1
            return score_cache[cache_key]
        if _score_failure_cooldown_active(cache_key):
            score_cooldown_keys.add(cache_key)
            score_cache[cache_key] = None
            return None
        score_cache_misses += 1
        score = _score_ticker(
            raw_ticker,
            skip_fundamentals=True,
            skip_pattern_engine=IMMINENT_SCORE_SKIP_PATTERN_ENGINE,
        )
        score_cache[cache_key] = score or None
        if score:
            _record_score_success(cache_key)
        else:
            _record_score_failure(cache_key)
        return score_cache[cache_key]

    def _append_candidate_from_score(
        *,
        pat: ScanPattern,
        ticker: str,
        score: dict[str, Any],
        flat: dict[str, Any],
        readiness: float,
        ratio: float,
        missing: list[str],
        signal_lane: str,
        readiness_gap_to_min: float | None,
        shadow_near_miss_source: str | None = None,
        hard_recert_reasons: list[str] | None = None,
    ) -> bool:
        nonlocal shadow_near_miss_admitted
        nonlocal shadow_near_miss_gap_admitted
        nonlocal shadow_near_miss_adaptive_admitted
        nonlocal hard_recert_shadow_admitted
        nonlocal offsession_stock_shadow_admitted

        eta_lo, eta_hi = estimate_breakout_eta_hours(
            readiness, pat.timeframe, k=k_eta, max_eta_hours=max_eta,
        )
        if eta_hi > max_eta:
            skip["eta_too_long"] += 1
            return False

        pq = pattern_quality_score(pat)
        entry = score.get("entry_price") or score.get("price")
        stop = score.get("stop_loss")
        target = score.get("take_profit")
        rr = risk_reward_score(
            float(entry) if entry else None,
            float(stop) if stop else None,
            float(target) if target else None,
        )
        oxp = overextension_penalty(flat)
        eta_s = eta_timeliness_score(eta_hi, max_eta)
        comp, breakdown = compute_composite_score(
            readiness=readiness,
            coverage_ratio=ratio,
            pattern_quality=pq,
            rr_score=rr,
            eta_score=eta_s,
            overext_subtract=oxp,
        )
        if apply_main_dispatch_filters and comp < min_comp_main:
            skip["below_composite_main"] += 1
            _append_suppressed({
                "ticker": ticker,
                "pattern_id": pat.id,
                "reason": "below_composite_main",
                "composite": round(comp, 4),
                "coverage": ratio,
            })
            return False

        atr_f = (flat.get("atr") or 0) or 0
        adx_f = flat.get("adx")
        try:
            entry_f = float(entry or 0)
            tgt_f = float(target or 0)
            atr_use = float(atr_f) if atr_f else (entry_f * 0.02 if entry_f else 0.01)
            _rvol_f = flat.get("rvol") or flat.get("volume_ratio")
            hold_est = _estimate_hold_duration(
                entry_f, tgt_f, atr_use,
                (pat.timeframe or "1d"), adx_f,
                rvol=_rvol_f,
            )
        except (TypeError, ValueError):
            hold_est = {"label": "n/a", "hours_low": 0, "hours_high": 0}

        ind_for_class = {
            "adx": adx_f,
            "atr": atr_f,
            "rsi": flat.get("rsi_14"),
        }
        tc = classify_trade_type(
            score.get("signals") or [],
            hold_est,
            ind_for_class,
            is_crypto=is_crypto(ticker),
        )

        candidate = {
            "pattern": pat,
            "ticker": ticker,
            "signal_lane": signal_lane,
            "readiness": readiness,
            "readiness_gap_to_min": readiness_gap_to_min,
            "eta_lo": eta_lo,
            "eta_hi": eta_hi,
            "score": score,
            "flat": flat,
            "hold_label": hold_est.get("label") or "",
            "trade_type": tc.get("type"),
            "duration_estimate": tc.get("duration") or hold_est.get("label"),
            "composite": comp,
            "score_breakdown": breakdown,
            "coverage_ratio": ratio,
            "missing_indicators": missing,
        }
        if shadow_near_miss_source:
            candidate["shadow_near_miss_source"] = shadow_near_miss_source
        if hard_recert_reasons:
            candidate["hard_recert_reasons"] = hard_recert_reasons
        candidates.append(candidate)
        if signal_lane == SHADOW_NEAR_MISS_SIGNAL_LANE:
            shadow_near_miss_admitted += 1
            if shadow_near_miss_source == SHADOW_NEAR_MISS_SOURCE_FIXED_GAP:
                shadow_near_miss_gap_admitted += 1
            elif shadow_near_miss_source == SHADOW_NEAR_MISS_SOURCE_ADAPTIVE_BUFFER:
                shadow_near_miss_adaptive_admitted += 1
        elif signal_lane == HARD_RECERT_SHADOW_SIGNAL_LANE:
            hard_recert_shadow_admitted += 1
            hard_recert_shadow_reason_counts.update(hard_recert_reasons or [])
        elif signal_lane == EQUITY_SESSION_SHADOW_SIGNAL_LANE:
            offsession_stock_shadow_admitted += 1
        return True

    def _pending_shadow_near_miss_priority(row: dict[str, Any]) -> tuple[Any, ...]:
        return (
            float(row.get("readiness_gap_to_min") or 0.0),
            -float(row.get("readiness") or 0.0),
            -float(row.get("coverage_ratio") or 0.0),
            _imminent_scan_priority_key(row["pattern"]),
            str(row.get("ticker") or ""),
        )

    for pat in patterns:
        if not all_active_patterns and not scan_pattern_eligible_main_imminent(pat):
            skip["excluded_promotion_lifecycle"] += 1
            stage = (
                (getattr(pat, "lifecycle_stage", None) or UNKNOWN_LIFECYCLE_STAGE)
                .strip()
                .lower()
            )
            excluded_lifecycle_by_stage[stage or UNKNOWN_LIFECYCLE_STAGE] += 1
            continue
        if (
            not all_active_patterns
            and (getattr(pat, "lifecycle_stage", "") or "").strip().lower()
            == SHADOW_PROMOTED_STAGE
            and int(getattr(pat, "id", 0) or 0) in poor_shadow_ids
        ):
            skip["shadow_poor_edge_cooldown"] += 1
            _append_suppressed({
                "pattern_id": pat.id,
                "reason": "shadow_poor_edge_cooldown",
                "recent_non_positive_rejects": poor_shadow_counts.get(
                    int(pat.id), 0,
                ),
                "avg_return_pct": _float_or_none(
                    getattr(pat, "avg_return_pct", None)
                ),
            })
            continue

        hard_recert_reasons_for_pat = _hard_recert_shadow_reasons_for_pattern(
            pat,
            enabled=hard_recert_shadow_enabled,
            lifecycle_stages=hard_recert_shadow_lifecycle_stages,
            hard_reasons=hard_recert_shadow_reasons,
        )
        if hard_recert_reasons_for_pat:
            hard_recert_shadow_patterns += 1

        tickers = _tickers_for_pattern(
            pat,
            global_uni,
            equity_open=eq_open,
            allow_offsession_stock_shadow=offsession_stock_shadow_enabled,
        )
        tickers, dropped_crypto, _spot_count = _filter_crypto_to_execution_universe(
            tickers,
            coinbase_spot_tickers=coinbase_spot_tickers,
        )
        skip["crypto_execution_universe_filtered"] += dropped_crypto
        if open_position_keys:
            pat_id = _pattern_id_for_rotation(pat)
            deflected: list[str] = []
            kept_tickers: list[str] = []
            for ticker in tickers:
                ticker_key = str(ticker or "").strip().upper()
                if (pat_id, ticker_key) in open_position_keys:
                    deflected.append(ticker)
                    continue
                kept_tickers.append(ticker)
            if deflected:
                skip["open_position_deflected"] += len(deflected)
                if len(suppressed) < suppressed_limit:
                    _append_suppressed({
                        "pattern_id": pat.id,
                        "reason": "open_position_deflected",
                        "tickers": deflected[:missing_indicator_limit],
                        "deflected_count": len(deflected),
                    })
                tickers = kept_tickers
        if not tickers:
            skip["pattern_no_tickers"] += 1
            continue

        conditions = _pattern_conditions(pat)
        if not conditions:
            skip["pattern_no_conditions"] += 1
            continue

        patterns_tried += 1
        if len(tickers) > per_pat_cap:
            tickers, rotation_sample = _rotated_ticker_cap_slice(
                tickers,
                cap=per_pat_cap,
                pat=pat,
                enabled=ticker_rotation_enabled,
                window_minutes=ticker_rotation_window_minutes,
                explore_count=ticker_rotation_explore_tickers,
            )
            if rotation_sample is not None:
                ticker_rotation_applied += 1
                ticker_rotation_total_available += int(
                    rotation_sample.get("available_tickers") or 0
                )
                if len(ticker_rotation_samples) < suppressed_limit:
                    ticker_rotation_samples.append(rotation_sample)
        for ticker in tickers:
            if _score_time_budget_hit():
                score_time_budget_hit = True
                break
            cache_key = _score_cache_key(ticker)
            if (
                for_opportunity_board
                and cache_key not in score_cache
                and score_cache_misses >= score_budget
            ):
                board_budget_hit = True
                break
            score = _score_ticker_cached(ticker)
            if not score:
                if cache_key in score_cooldown_keys:
                    skip["score_failure_cooldown"] += 1
                else:
                    skip["score_failed"] += 1
                continue
            tickers_scored += 1

            res = resistance_from_score(score)
            if res is None:
                res = recent_swing_resistance(ticker)
            flat = flat_indicators_from_score(score, resistance=res)

            readiness, all_pass, ratio, missing = evaluate_readiness_with_gates(
                conditions,
                flat,
                min_coverage_ratio=eval_floor_board,
                min_evaluable_absolute=2,
                allow_shortcut_two_evaluable=allow_shortcut,
            )
            if readiness is None:
                skip["readiness_unusable"] += 1
                _append_suppressed({
                    "ticker": ticker,
                    "pattern_id": pat.id,
                    "reason": "readiness_unusable",
                    "coverage": ratio,
                    "missing_indicators": _sample_missing_indicators(missing),
                })
                continue

            if apply_main_dispatch_filters and ratio < min_cov_main:
                skip["insufficient_coverage_main"] += 1
                _append_suppressed({
                    "ticker": ticker,
                    "pattern_id": pat.id,
                    "reason": "insufficient_coverage_main",
                    "coverage": ratio,
                    "missing_indicators": _sample_missing_indicators(missing),
                })
                continue

            if all_pass:
                skip["all_conditions_met"] += 1
                continue
            hard_recert_reason_list = sorted(hard_recert_reasons_for_pat)
            signal_lane = (
                HARD_RECERT_SHADOW_SIGNAL_LANE
                if hard_recert_reason_list
                else STANDARD_SIGNAL_LANE
            )
            if (
                signal_lane == STANDARD_SIGNAL_LANE
                and offsession_stock_shadow_enabled
                and not eq_open
                and not is_crypto(ticker)
            ):
                signal_lane = EQUITY_SESSION_SHADOW_SIGNAL_LANE
            readiness_gap_to_min: float | None = None
            shadow_near_miss_source: str | None = None
            if readiness < min_rd:
                readiness_gap_to_min = min_rd - readiness
                _append_readiness_band_miss(
                    reason=READINESS_SKIP_BELOW_MIN,
                    ticker=ticker,
                    pat=pat,
                    readiness=readiness,
                    coverage=ratio,
                    missing=missing,
                    gap=readiness_gap_to_min,
                )
                pat_stage = (getattr(pat, "lifecycle_stage", "") or "").strip().lower()
                shadow_near_miss_base_ok = (
                    shadow_near_miss_enabled
                    and apply_main_dispatch_filters
                    and pat_stage in shadow_near_miss_lifecycle_stages
                )
                shadow_near_miss_ok = (
                    shadow_near_miss_base_ok
                    and readiness_gap_to_min <= shadow_near_miss_max_gap
                )
                if not hard_recert_reason_list and shadow_near_miss_ok:
                    signal_lane = SHADOW_NEAR_MISS_SIGNAL_LANE
                    shadow_near_miss_source = SHADOW_NEAR_MISS_SOURCE_FIXED_GAP
                    shadow_near_miss_eligible += 1
                    shadow_near_miss_gap_eligible += 1
                elif (
                    not hard_recert_reason_list
                    and
                    shadow_near_miss_adaptive_enabled
                    and shadow_near_miss_adaptive_max_per_run > 0
                    and shadow_near_miss_base_ok
                    and readiness >= shadow_near_miss_adaptive_min_readiness
                ):
                    pending_shadow_near_misses.append({
                        "pattern": pat,
                        "ticker": ticker,
                        "score": score,
                        "flat": flat,
                        "readiness": readiness,
                        "readiness_gap_to_min": readiness_gap_to_min,
                        "coverage_ratio": ratio,
                        "missing_indicators": missing,
                    })
                    shadow_near_miss_eligible += 1
                    shadow_near_miss_adaptive_eligible += 1
                    continue
                else:
                    skip["readiness_outside_band"] += 1
                    skip[READINESS_SKIP_BELOW_MIN] += 1
                    continue
            elif readiness >= cap_rd:
                skip["readiness_outside_band"] += 1
                skip[READINESS_SKIP_AT_OR_ABOVE_CAP] += 1
                _append_readiness_band_miss(
                    reason=READINESS_SKIP_AT_OR_ABOVE_CAP,
                    ticker=ticker,
                    pat=pat,
                    readiness=readiness,
                    coverage=ratio,
                    missing=missing,
                    gap=readiness - cap_rd,
                )
                continue

            if hard_recert_reason_list:
                hard_recert_shadow_eligible += 1

            _append_candidate_from_score(
                pat=pat,
                ticker=ticker,
                score=score,
                flat=flat,
                readiness=readiness,
                ratio=ratio,
                missing=missing,
                signal_lane=signal_lane,
                readiness_gap_to_min=readiness_gap_to_min,
                shadow_near_miss_source=shadow_near_miss_source,
                hard_recert_reasons=hard_recert_reason_list,
            )

        if board_budget_hit or score_time_budget_hit:
            break

    pending_shadow_near_misses.sort(key=_pending_shadow_near_miss_priority)
    selected_shadow_near_misses = pending_shadow_near_misses[
        :shadow_near_miss_adaptive_max_per_run
    ]
    shadow_near_miss_adaptive_selected = len(selected_shadow_near_misses)
    shadow_near_miss_adaptive_skipped_not_selected = max(
        0,
        len(pending_shadow_near_misses) - shadow_near_miss_adaptive_selected,
    )
    if shadow_near_miss_adaptive_skipped_not_selected:
        skip["readiness_outside_band"] += shadow_near_miss_adaptive_skipped_not_selected
        skip[READINESS_SKIP_BELOW_MIN] += shadow_near_miss_adaptive_skipped_not_selected
    for row in selected_shadow_near_misses:
        _append_candidate_from_score(
            pat=row["pattern"],
            ticker=row["ticker"],
            score=row["score"],
            flat=row["flat"],
            readiness=float(row["readiness"]),
            ratio=float(row["coverage_ratio"]),
            missing=list(row["missing_indicators"]),
            signal_lane=SHADOW_NEAR_MISS_SIGNAL_LANE,
            readiness_gap_to_min=float(row["readiness_gap_to_min"]),
            shadow_near_miss_source=SHADOW_NEAR_MISS_SOURCE_ADAPTIVE_BUFFER,
        )

    candidates.sort(key=lambda x: (-x["composite"], x["eta_hi"]))
    readiness_band_near_misses.sort(key=lambda row: (row["gap"], -row["readiness"]))
    meta = {
        "patterns_active": len(patterns),
        "patterns_with_tickers_evaluated": patterns_tried,
        "global_ticker_universe": len(global_uni),
        "universe_by_source": uni_counts,
        "tickers_scored": tickers_scored,
        "score_cache_size": len(score_cache),
        "score_cache_hits": score_cache_hits,
        "score_cache_misses": score_cache_misses,
        "score_skip_pattern_engine": IMMINENT_SCORE_SKIP_PATTERN_ENGINE,
        "score_failure_cooldown_cache_size": len(_SCORE_FAILURE_CACHE),
        "score_time_budget_seconds": score_time_budget_s,
        "score_time_budget_hit": score_time_budget_hit,
        "score_elapsed_seconds": _time.monotonic() - score_started_at,
        "per_pattern_ticker_cap": per_pat_cap,
        "crypto_execution_filter_spot_tickers": len(coinbase_spot_tickers),
        "skip_reasons": skip,
        "excluded_lifecycle_by_stage": dict(excluded_lifecycle_by_stage),
        "readiness_band_near_misses": readiness_band_near_misses[:readiness_near_miss_limit],
        "shadow_near_miss_enabled": shadow_near_miss_enabled,
        "shadow_near_miss_max_gap": shadow_near_miss_max_gap,
        "shadow_near_miss_lifecycle_stages": sorted(shadow_near_miss_lifecycle_stages),
        "shadow_near_miss_eligible": shadow_near_miss_eligible,
        "shadow_near_miss_admitted": shadow_near_miss_admitted,
        "shadow_near_miss_gap_eligible": shadow_near_miss_gap_eligible,
        "shadow_near_miss_gap_admitted": shadow_near_miss_gap_admitted,
        "shadow_near_miss_adaptive_enabled": shadow_near_miss_adaptive_enabled,
        "shadow_near_miss_adaptive_max_per_run": shadow_near_miss_adaptive_max_per_run,
        "shadow_near_miss_adaptive_min_readiness_fraction": (
            shadow_near_miss_adaptive_min_readiness_fraction
        ),
        "shadow_near_miss_adaptive_min_readiness": (
            shadow_near_miss_adaptive_min_readiness
        ),
        "shadow_near_miss_adaptive_eligible": shadow_near_miss_adaptive_eligible,
        "shadow_near_miss_adaptive_selected": shadow_near_miss_adaptive_selected,
        "shadow_near_miss_adaptive_admitted": shadow_near_miss_adaptive_admitted,
        "shadow_near_miss_adaptive_skipped_not_selected": (
            shadow_near_miss_adaptive_skipped_not_selected
        ),
        "hard_recert_shadow_enabled": hard_recert_shadow_enabled,
        "hard_recert_shadow_lifecycle_stages": sorted(
            hard_recert_shadow_lifecycle_stages
        ),
        "hard_recert_shadow_reasons": sorted(hard_recert_shadow_reasons),
        "hard_recert_shadow_patterns": hard_recert_shadow_patterns,
        "hard_recert_shadow_eligible": hard_recert_shadow_eligible,
        "hard_recert_shadow_admitted": hard_recert_shadow_admitted,
        "hard_recert_shadow_reason_counts": dict(hard_recert_shadow_reason_counts),
        "offsession_stock_shadow_enabled": offsession_stock_shadow_enabled,
        "offsession_stock_shadow_admitted": offsession_stock_shadow_admitted,
        "ticker_rotation_enabled": ticker_rotation_enabled,
        "ticker_rotation_window_minutes": ticker_rotation_window_minutes,
        "ticker_rotation_explore_tickers": min(
            ticker_rotation_explore_tickers,
            per_pat_cap,
        ),
        "ticker_rotation_applied": ticker_rotation_applied,
        "ticker_rotation_total_available": ticker_rotation_total_available,
        "ticker_rotation_samples": ticker_rotation_samples,
        "open_position_deflection_enabled": open_position_deflection_enabled,
        "open_position_deflection_keys": len(open_position_keys),
        "pattern_priority_top_stage_counts": dict(pattern_priority_top_stage_counts),
        "top_suppressed": suppressed,
        "equity_session_open": eq_open,
    }
    if for_opportunity_board:
        meta["for_opportunity_board"] = True
        meta["board_eval_budget_hit"] = board_budget_hit
        meta["board_per_pattern_cap"] = per_pat_cap
        meta["board_score_budget"] = score_budget
    return candidates, meta


def _candidate_pattern_stage(candidate: dict[str, Any]) -> str:
    pat = candidate.get("pattern")
    return (getattr(pat, "lifecycle_stage", None) or "").strip().lower()


def _candidate_signal_lane(candidate: dict[str, Any]) -> str:
    return str(candidate.get("signal_lane") or STANDARD_SIGNAL_LANE).strip().lower()


def _is_shadow_observation_candidate(candidate: dict[str, Any]) -> bool:
    return (
        _candidate_pattern_stage(candidate) == SHADOW_PROMOTED_STAGE
        or _candidate_signal_lane(candidate) in SHADOW_OBSERVATION_SIGNAL_LANES
    )


def _candidate_identity(candidate: dict[str, Any]) -> tuple[int, str]:
    pat = candidate.get("pattern")
    try:
        pid = int(getattr(pat, "id", 0) or 0)
    except Exception:
        pid = 0
    return pid, str(candidate.get("ticker") or "").upper()


def _shadow_reserved_dispatch_order(
    candidates: list[dict[str, Any]],
    *,
    shadow_reserve: int,
) -> list[dict[str, Any]]:
    """Pull top shadow-promoted observations forward without changing scores."""
    reserve = max(0, int(shadow_reserve or 0))
    if reserve <= 0:
        return list(candidates)
    picked: list[dict[str, Any]] = []
    picked_ids: set[tuple[int, str]] = set()
    for c in candidates:
        if len(picked) >= reserve:
            break
        if not _is_shadow_observation_candidate(c):
            continue
        ident = _candidate_identity(c)
        if ident in picked_ids:
            continue
        picked.append(c)
        picked_ids.add(ident)
    if not picked:
        return list(candidates)
    return picked + [c for c in candidates if _candidate_identity(c) not in picked_ids]


def run_pattern_imminent_scan(
    db: Session,
    user_id: int | None,
    *,
    equity_session_open: bool | None = None,
    dry_run: bool | None = None,
) -> dict[str, Any]:
    """Evaluate promoted/live patterns; dispatch imminent alerts by composite rank."""
    if not getattr(settings, "pattern_imminent_alert_enabled", True):
        return {"ok": True, "skipped": True, "reason": "disabled"}

    eq_open = equity_session_open if equity_session_open is not None else us_stock_session_open()
    do_dry = dry_run if dry_run is not None else bool(
        getattr(settings, "pattern_imminent_debug_dry_run", False)
    )

    max_alerts = int(settings.pattern_imminent_max_per_run)
    cooldown_h = float(settings.pattern_imminent_cooldown_hours)
    # R33 (2026-04-30): crypto cooldown is shorter than equity (24/7 markets,
    # tighter-coupled to news/whales). _cooldown_active called below picks
    # whichever value applies based on ticker asset class.
    cooldown_h_crypto = float(getattr(settings, "pattern_imminent_cooldown_hours_crypto", 0.5))
    max_per_ticker = max(1, int(getattr(settings, "pattern_imminent_max_per_ticker_per_run", 2)))
    max_per_pattern = max(1, int(getattr(settings, "pattern_imminent_max_per_pattern_per_run", 3)))
    shadow_enabled = bool(getattr(settings, "pattern_imminent_shadow_observation_enabled", True))
    shadow_reserve = max(0, int(getattr(settings, "pattern_imminent_shadow_reserve_per_run", 4) or 0))
    shadow_extra = max(0, int(getattr(settings, "pattern_imminent_shadow_extra_per_run", 4) or 0))
    shadow_quota = shadow_reserve + shadow_extra
    shadow_max_per_ticker = max(
        1, int(getattr(settings, "pattern_imminent_shadow_max_per_ticker_per_run", 2) or 2)
    )
    shadow_max_per_pattern = max(
        1, int(getattr(settings, "pattern_imminent_shadow_max_per_pattern_per_run", 2) or 2)
    )
    shadow_cooldown_h = float(getattr(settings, "pattern_imminent_shadow_cooldown_hours", 1.0))
    shadow_cooldown_h_crypto = float(
        getattr(settings, "pattern_imminent_shadow_cooldown_hours_crypto", 0.25)
    )
    max_eta = float(settings.pattern_imminent_max_eta_hours)
    min_rd = float(settings.pattern_imminent_min_readiness)
    cap_rd = float(settings.pattern_imminent_readiness_cap)
    eval_floor_board = float(settings.pattern_imminent_evaluable_ratio_floor)
    min_cov_main = float(getattr(settings, "pattern_imminent_min_feature_coverage_main", 0.45))
    min_comp_main = float(getattr(settings, "pattern_imminent_min_composite_main", 0.42))

    candidates, meta = gather_imminent_candidate_rows(
        db,
        user_id,
        equity_session_open=eq_open,
        all_active_patterns=False,
        apply_main_dispatch_filters=True,
    )

    sent = 0
    main_sent = 0
    shadow_sent = 0
    delivery_failed = 0
    skipped_cd = 0
    shadow_skipped_cd = 0
    per_ticker: dict[str, int] = {}
    per_pattern: dict[int, int] = {}
    shadow_per_ticker: dict[str, int] = {}
    shadow_per_pattern: dict[int, int] = {}
    diversity_skipped = 0
    shadow_diversity_skipped = 0
    main_quota_skipped = 0
    shadow_quota_skipped = 0
    dispatch_candidates = (
        _shadow_reserved_dispatch_order(candidates, shadow_reserve=shadow_reserve)
        if shadow_enabled and shadow_quota > 0
        else candidates
    )

    for c in dispatch_candidates:
        is_shadow = shadow_enabled and _is_shadow_observation_candidate(c)
        if sent >= max_alerts + shadow_extra:
            break
        if is_shadow:
            if shadow_sent >= shadow_quota:
                shadow_quota_skipped += 1
                continue
        elif main_sent >= max_alerts:
            main_quota_skipped += 1
            continue
        pat = c["pattern"]
        ticker = c["ticker"]
        # R33: pick crypto cooldown for crypto tickers, equity cooldown otherwise.
        if is_shadow:
            ticker_cooldown_h = (
                shadow_cooldown_h_crypto if is_crypto(ticker) else shadow_cooldown_h
            )
        else:
            ticker_cooldown_h = cooldown_h_crypto if is_crypto(ticker) else cooldown_h
        if _cooldown_active(db, user_id, ticker, pat.id, ticker_cooldown_h):
            if is_shadow:
                shadow_skipped_cd += 1
            else:
                skipped_cd += 1
            continue
        if is_shadow:
            if shadow_per_ticker.get(ticker, 0) >= shadow_max_per_ticker:
                shadow_diversity_skipped += 1
                diversity_skipped += 1
                continue
            if shadow_per_pattern.get(pat.id, 0) >= shadow_max_per_pattern:
                shadow_diversity_skipped += 1
                diversity_skipped += 1
                continue
        else:
            if per_ticker.get(ticker, 0) >= max_per_ticker:
                diversity_skipped += 1
                continue
            if per_pattern.get(pat.id, 0) >= max_per_pattern:
                diversity_skipped += 1
                continue

        eta_txt = format_eta_range(c["eta_lo"], c["eta_hi"])
        sc = c["score"]
        desc = (pat.description or "")[:120].replace("\n", " ")
        hold_line = c.get("duration_estimate") or c.get("hold_label") or ""
        sigs = "; ".join((sc.get("signals") or [])[:4])

        msg = format_pattern_imminent(
            ticker=ticker,
            pattern_name=pat.name,
            pattern_id=pat.id,
            price=sc.get("price"),
            readiness=c["readiness"],
            composite_score=c["composite"],
            eta_txt=eta_txt,
            hold_line=hold_line,
            entry_price=sc.get("entry_price"),
            stop_loss=sc.get("stop_loss"),
            take_profit=sc.get("take_profit"),
            description=desc,
            signals=sigs,
        )

        # Publish to mesh sensor (nm_imminent_eval) for aggregation
        try:
            from .brain_neural_mesh.publisher import publish_imminent_eval
            publish_imminent_eval(
                db,
                scan_pattern_id=pat.id,
                ticker=ticker,
                composite_score=float(c["composite"]),
                readiness=float(c["readiness"]),
                eta_lo=float(c["eta_lo"]),
                eta_hi=float(c["eta_hi"]),
                price=sc.get("price", 0),
                user_id=user_id,
            )
        except Exception:
            logger.debug("[pattern_imminent] mesh publish failed for %s", ticker, exc_info=True)

        delivered = do_dry
        if not do_dry:
            # Persist the BreakoutAlert row BEFORE attempting external
            # delivery. The autotrader consumes these rows directly — its
            # availability must not depend on Telegram/SMS egress. Prior
            # behavior gated persistence on dispatch_alert's bool, so a
            # network-unreachable Telegram silently starved the autotrader.
            try:
                _insert_imminent_breakout_alert(
                    db,
                    user_id,
                    pat,
                    ticker,
                    sc,
                    c["flat"],
                    composite=float(c["composite"]),
                    score_breakdown=dict(c["score_breakdown"]),
                    readiness=float(c["readiness"]),
                    coverage_ratio=float(c["coverage_ratio"]),
                    eta_lo=float(c["eta_lo"]),
                    eta_hi=float(c["eta_hi"]),
                    signal_lane=str(c.get("signal_lane") or STANDARD_SIGNAL_LANE),
                    readiness_gap_to_min=_float_or_none(c.get("readiness_gap_to_min")),
                    shadow_near_miss_source=(
                        str(c.get("shadow_near_miss_source") or "").strip() or None
                    ),
                    hard_recert_reasons=list(c.get("hard_recert_reasons") or []),
                )
            except Exception as e:
                logger.warning("[pattern_imminent] BreakoutAlert insert failed: %s", e)

            delivered = dispatch_alert(
                db,
                user_id,
                PATTERN_BREAKOUT_IMMINENT,
                ticker,
                msg,
                price=sc.get("price"),
                trade_type=c.get("trade_type"),
                duration_estimate=hold_line[:60] if hold_line else None,
                scan_pattern_id=pat.id,
                confidence=min(0.95, 0.55 + 0.5 * float(c["composite"])),
            )
            if not delivered:
                delivery_failed += 1
        if is_shadow:
            shadow_per_ticker[ticker] = shadow_per_ticker.get(ticker, 0) + 1
            shadow_per_pattern[pat.id] = shadow_per_pattern.get(pat.id, 0) + 1
            shadow_sent += 1
        else:
            per_ticker[ticker] = per_ticker.get(ticker, 0) + 1
            per_pattern[pat.id] = per_pattern.get(pat.id, 0) + 1
            main_sent += 1
        # Count as sent whenever we persisted the DB row — that is the
        # autotrader's contract. delivery_failed separately tracks the
        # external-channel outcome for observability.
        sent += 1

    summary: dict[str, Any] = {
        **meta,
        "ok": True,
        "dry_run": do_dry,
        "candidates": len(candidates),
        "alerts_sent": sent,
        "main_alerts_sent": main_sent,
        "shadow_alerts_sent": shadow_sent,
        "delivery_failed": delivery_failed,
        "cooldown_skipped": skipped_cd,
        "shadow_cooldown_skipped": shadow_skipped_cd,
        "diversity_skipped": diversity_skipped,
        "shadow_diversity_skipped": shadow_diversity_skipped,
        "main_quota_skipped": main_quota_skipped,
        "shadow_quota_skipped": shadow_quota_skipped,
        "shadow_observation_enabled": shadow_enabled,
        "shadow_reserve_per_run": shadow_reserve,
        "shadow_extra_per_run": shadow_extra,
        "us_session_context": describe_us_session_context(),
        "thresholds": {
            "min_readiness": min_rd,
            "readiness_cap": cap_rd,
            "max_eta_hours": max_eta,
            "evaluable_ratio_floor": eval_floor_board,
            "min_feature_coverage_main": min_cov_main,
            "min_composite_main": min_comp_main,
        },
    }
    logger.info("[pattern_imminent] %s", summary)
    return summary


# Re-export for tests / pattern_engine consumers
__all__ = [
    "build_imminent_ticker_universe",
    "gather_imminent_candidate_rows",
    "describe_us_session_context",
    "estimate_breakout_eta_hours",
    "evaluate_imminent_readiness",
    "flat_indicators_from_score",
    "format_eta_range",
    "recent_swing_resistance",
    "run_pattern_imminent_scan",
    "timeframe_to_hours_per_step",
    "us_stock_session_open",
    "us_stock_extended_session_open",
]

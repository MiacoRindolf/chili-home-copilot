"""AutoTrader v1 orchestrator: pattern-imminent alerts → gates → paper or RH live."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import time
import traceback
from datetime import datetime, time as datetime_time, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, or_, text
from sqlalchemy.orm import Session

from ...config import (
    AUTOTRADER_DEFAULT_CANDIDATE_BATCH_SIZE,
    AUTOTRADER_DEFAULT_TICK_INTERVAL_SECONDS,
    AUTOTRADER_DEFAULT_TICK_MAX_SECONDS,
    AUTOTRADER_FRESH_CANDIDATE_BURST_DEFAULT_ENABLED,
    AUTOTRADER_FRESH_CANDIDATE_FASTLANE_DEFAULT_MAX_AGE_SECONDS,
    AUTOTRADER_NON_STOCK_CANDIDATE_MAX_AGE_DEFAULT_MINUTES,
    AUTOTRADER_NON_STOCK_CANDIDATE_MAX_AGE_MAX_MINUTES,
    AUTOTRADER_STALE_CANDIDATE_SWEEP_DEFAULT_SECONDS,
    AUTOTRADER_STALE_CANDIDATE_SWEEP_MAX_SECONDS,
    AUTOTRADER_STOCK_CANDIDATE_MAX_AGE_DEFAULT_MINUTES,
    AUTOTRADER_STOCK_CANDIDATE_MAX_AGE_MAX_MINUTES,
    AUTOTRADER_LLM_REVALIDATION_DEFAULT_SKIP_OPTIONS_PATH,
    AUTOTRADER_LLM_REVALIDATION_DEFAULT_SKIP_SHADOW_OBSERVATION,
    AUTOTRADER_MAX_CANDIDATE_BATCH_SIZE,
    AUTOTRADER_MAX_TICK_MAX_SECONDS,
    AUTOTRADER_MIN_TICK_MAX_SECONDS,
    AUTOTRADER_BROKER_REJECT_SUPPRESSION_DEFAULT_ENABLED,
    AUTOTRADER_BROKER_REJECT_SUPPRESSION_DEFAULT_MINUTES,
    AUTOTRADER_BROKER_REJECT_SUPPRESSION_DEFAULT_THRESHOLD,
    AUTOTRADER_STOCK_SESSION_DEFER_DEFAULT_ENABLED,
    AUTOTRADER_STOCK_SESSION_DEFER_DEFAULT_MAX_AGE_HOURS,
    AUTOTRADER_SHADOW_OBSERVATION_DIAGNOSTIC_SIZING_DEFAULT_ENABLED,
    AUTOTRADER_SHADOW_OBSERVATION_EVIDENCE_NOTIONAL_DEFAULT_USD,
    AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_BACKTEST_PRIORITY,
    AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_ENABLED,
    AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_LIFECYCLE_STAGES,
    AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_MIN_EXPECTED_NET_PCT,
    AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_REBOOST_COOLDOWN_MINUTES,
    AUTOTRADER_PAPER_SHADOW_DEFAULT_DEDUPE_SAME_ALERT_REASON_FAMILY,
    AUTOTRADER_PAPER_SHADOW_DEFAULT_DEDUPE_RECENT_REASON_FAMILY_MINUTES,
    AUTOTRADER_PAPER_SHADOW_DEFAULT_JANITOR_BUFFER,
    AUTOTRADER_PAPER_SHADOW_DEFAULT_JANITOR_MAX_AGE_HOURS,
    AUTOTRADER_PAPER_SHADOW_MAX_JANITOR_MAX_AGE_HOURS,
    AUTOTRADER_PAPER_SHADOW_DEFAULT_MAX_OPEN,
    AUTOTRADER_PAPER_SHADOW_DEFAULT_REJECT_ALLOW_DUPLICATE_OPEN,
    AUTOTRADER_PAPER_SHADOW_DEFAULT_REJECT_LIGHTWEIGHT_SIZING_ENABLED,
    AUTOTRADER_PROBATION_DEFAULT_CRYPTO_MAX_TRADES_PER_DAY,
    AUTOTRADER_PROBATION_DEFAULT_CRYPTO_MIN_EXPECTED_NET_PCT_FOR_EXTRA_QUOTA,
    AUTOTRADER_PROBATION_DEFAULT_MAX_TRADES_PER_PATTERN_TICKER_PER_DAY,
    AUTOTRADER_OPTIONS_SUBSTITUTE_DEFAULT_REQUIRES_UNDERLYING_POSITIVE_EDGE,
    PATTERN_IMMINENT_EQUITY_SESSION_SHADOW_SIGNAL_LANE,
    PATTERN_IMMINENT_HARD_RECERT_SHADOW_SIGNAL_LANE,
    AUTOTRADER_SYNERGY_RETRY_DEFAULT_LOOKBACK_MINUTES,
    AUTOTRADER_SYNERGY_RETRY_DEFAULT_MAX_PER_TICK,
    AUTOTRADER_SYNERGY_RETRY_MAX_LOOKBACK_MINUTES,
    AUTOTRADER_SYNERGY_RETRY_MIN_LOOKBACK_MINUTES,
    settings,
)
from ...models.trading import AutoTraderRun, BreakoutAlert, PaperTrade, ScanPattern, Trade
from .auto_trader_llm import run_revalidation_llm
from .auto_trader_rules import (
    MANAGED_EDGE_GEOMETRY_SOURCE,
    RuleGateContext,
    alert_confidence_from_score,
    autotrader_paper_realized_pnl_today_et,
    autotrader_realized_pnl_today_et,
    breakout_alert_already_processed,
    count_autotrader_v1_open,
    count_autotrader_v1_open_by_lane,
    evaluate_entry_edge,
    passes_rule_gate,
    resolve_pattern_signal_context,
)
from .autotrader_desk import effective_autotrader_runtime
from .autopilot_scope import (
    AUTOPILOT_AUTO_TRADER_V1,
    check_autopilot_entry_gate,
)
from .auto_trader_synergy import (
    SCALE_IN_ALERT_IDS_SNAPSHOT_KEY,
    SCALE_IN_PATTERN_IDS_SNAPSHOT_KEY,
    find_open_autotrader_paper,
    find_open_autotrader_trade,
    maybe_scale_in,
    used_scale_in_pattern_ids,
)
from .options.contracts import (
    normalize_option_meta,
    option_price_domains_snapshot,
    parse_contract_quantity,
)
from .coinbase_maker_pricing import plan_post_only_buy_limit
from .management_scope import MANAGEMENT_SCOPE_AUTO_TRADER_V1
from .ops_log_prefixes import CHILI_MARKET_DATA

logger = logging.getLogger(__name__)

SYNERGY_RETRY_SOURCE_REASON = "synergy_not_applicable"
SYNERGY_RETRY_EXHAUSTED_REASON = "synergy_retry_not_applicable"

QUALIFIED_BLOCK_PAPER_SHADOW_DECISIONS = frozenset({
    "blocked_coinbase_cap",
    "blocked_llm_not_viable",
    "blocked_llm_unavailable",
    "blocked_max_concurrent_crypto",
    "blocked_max_concurrent_equity",
    "blocked_max_concurrent_global",
    "blocked_max_concurrent_options",
    "blocked_no_order_id",
    "blocked_option_entry_no_fill",
    "blocked_regime_gate",
    "blocked_recert_required",
    "blocked_shadow_promoted",
    "blocked_venue_health",
    "skipped_duplicate_pattern_already_open",
    "skipped_non_positive_expected_edge",
    "skipped_pending_entry_already_working",
    "skipped_synergy_disabled_second_signal",
    f"skipped_{SYNERGY_RETRY_SOURCE_REASON}",
    f"skipped_{SYNERGY_RETRY_EXHAUSTED_REASON}",
})

AUTOTRADER_VERSION = "v1"
PROBATION_RECERT_ALLOWANCE = "probation"
PILOT_BOOTSTRAP_RECERT_ALLOWANCE = "pilot_bootstrap"
PROBATION_TIMEZONE = "America/New_York"
ENTRY_EXECUTION_SNAPSHOT_KEY = "entry_execution"
PROBATION_ENTRY_FLAG = "probation_recert_allowed"
PROBATION_ENTRY_POLICY = "reduced_risk_soft_oos_recert"
PROBATION_JSON_TRUE = "true"
PROBATION_JSON_FALSE = "false"
PROBATION_DEFAULT_NOTIONAL_MULTIPLIER = 0.25
PROBATION_QUOTA_REASON_PATTERN_TICKER = "probation_quota_exceeded:pattern_ticker"
MONEY_ROUND_DIGITS = 2
MULTIPLIER_ROUND_DIGITS = 6
QUANTITY_ROUND_DIGITS = 8
PERCENT_SCALE = 100.0
DEFAULT_PER_TRADE_RISK_PCT = 1.0
SCALE_IN_PROTECTION_UNPROVEN_REASON = "scale_in_protection_unproven"
SCALE_IN_PROTECTION_BLOCKING_RECONCILIATION_KINDS = frozenset({
    "broker_down",
    "missing_stop",
    "orphan_stop",
    "price_drift",
    "qty_drift",
    "state_drift",
    "unreconciled",
})
SCALE_IN_PROTECTION_PROVEN_STATES = frozenset({
    "authoritative",
    "authoritative_submitted",
    "authoritative_reconciled",
    "confirmed_at_broker",
    "reconciled",
})
SCALE_IN_PROTECTION_BLOCKING_REASON_PREFIXES = tuple(
    f"{kind}:" for kind in SCALE_IN_PROTECTION_BLOCKING_RECONCILIATION_KINDS
)
PROBATION_QUOTA_REASON_PATTERN = "probation_quota_exceeded:pattern"
PROBATION_QUOTA_REASON_PORTFOLIO = "probation_quota_exceeded:portfolio"
PROBATION_OPTIONS_PATH_BLOCKED_REASON = "probation_options_path_blocked"
PROBATION_NOTIONAL_UNAVAILABLE_REASON = "probation_notional_unavailable"
PROBATION_NOTIONAL_BELOW_TRADE_UNIT_REASON = "probation_notional_below_trade_unit"
OPTIONS_SUBSTITUTE_UNDERLYING_EDGE_BLOCK_REASON = "underlying_expected_edge_not_positive"
OPTIONS_SUBSTITUTE_SHADOW_OBSERVATION_BLOCK_REASON = "shadow_observation_only"
PAPER_SHADOW_DUPLICATE_POLICY_STRICT = "strict_open_dedupe"
PAPER_SHADOW_DUPLICATE_POLICY_REJECT_BYPASS = "reject_observation_bypass"
PAPER_SHADOW_DUPLICATE_SKIP_REASON_SAME_ALERT_FAMILY = (
    "duplicate_same_alert_reason_family"
)
PAPER_SHADOW_DUPLICATE_SKIP_REASON_RECENT_CANDIDATE_FAMILY = (
    "duplicate_recent_candidate_reason_family"
)
PAPER_SHADOW_REJECT_QTY_SOURCE_EXISTING_LIVE_POSITION = "existing_live_position"
PAPER_SHADOW_REJECT_QTY_SOURCE_LIGHTWEIGHT = "lightweight_notional"
PAPER_SHADOW_REJECT_QTY_SOURCE_RISK_NOTIONAL = "risk_notional"
PAPER_SHADOW_REASON_FAMILY_SYNERGY_NOT_APPLICABLE = "synergy_not_applicable"
PAPER_SHADOW_REASON_FAMILY_PREFIXES = ("skipped_", "blocked_")
PAPER_SHADOW_REASON_FAMILY_SNAPSHOT_KEYS = (
    "shadow_decision",
    "paper_shadow_reject_decision",
    "paper_shadow_reject_reason",
)
PAPER_SHADOW_AUDIT_PREFIX = "paper_shadow_"
MANAGED_EDGE_PRICE_ROUND_DIGITS = 8
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
SHADOW_OBSERVATION_REASON_STAGE = "selector:shadow_promoted_pattern_eval"
SHADOW_OBSERVATION_REASON_SIGNAL_LANE = "selector:shadow_observation_signal_lane"
SHADOW_OBSERVATION_REASON_SIGNAL_LANE_DISABLED = (
    "selector:shadow_observation_signal_lane_disabled"
)
EXIT_GEOMETRY_REFRESH_REASON = "execution_stop_loss_too_wide"
EXIT_GEOMETRY_REFRESH_SOURCE = "autotrader_execution_stop_loss_too_wide"
SHADOW_OBSERVATION_SIZING_MODE_BASE_RISK = "base_risk_notional"
SHADOW_OBSERVATION_SIZING_MODE_FULL_DIAGNOSTICS = "full_diagnostics"
SHADOW_OBSERVATION_DIAGNOSTIC_SIZING_SETTING = (
    "chili_autotrader_shadow_observation_diagnostic_sizing_enabled"
)
SHADOW_OBSERVATION_EVIDENCE_NOTIONAL_SETTING = (
    "chili_autotrader_shadow_observation_evidence_notional_usd"
)
SHADOW_OBSERVATION_ADVISORY_SIZING_SKIP_REASON = "shadow_observation_only"
SHADOW_OBSERVATION_LIGHTWEIGHT_DIAL = 1.0
SHADOW_OBSERVATION_NOTIONAL_SOURCE_EVIDENCE = "shadow_observation_evidence_notional"
SHADOW_OBSERVATION_NOTIONAL_SOURCE_ASSUMED = "shadow_observation_assumed_capital_pct"
SHADOW_OBSERVATION_NOTIONAL_SOURCE_UNAVAILABLE = "shadow_observation_capital_unavailable"
LLM_REVALIDATION_SKIP_REASON_SHADOW_OBSERVATION = "shadow_observation_only"
LLM_REVALIDATION_SKIP_REASON_OPTIONS_PATH = "options_path"
PENDING_ENTRY_ALREADY_WORKING_REASON = "pending_entry_already_working"
RECENT_LIVE_EXIT_COOLDOWN_REASON = "recent_live_exit_cooldown"
LIVE_REENTRY_COOLDOWN_DEFAULT_ASSET_TYPES = "stock"
LIVE_REENTRY_COOLDOWN_DEFAULT_MINUTES = 30.0
LIVE_STOP_REENTRY_COOLDOWN_DEFAULT_MINUTES = 120.0
LIVE_REENTRY_COOLDOWN_MINUTES_FLOOR = 0.0
LIVE_REENTRY_COOLDOWN_MINUTES_CEILING = 24.0 * 60.0
STOCK_ASSET_TYPE = "stock"
STOCK_SESSION_DEFER_REASON_CLOSED = "stock_session_closed"
STOCK_SESSION_DEFER_REASON_DISABLED = "stock_session_defer_disabled"
STOCK_SESSION_DEFER_REASON_RTH_GATE_DISABLED = "stock_session_gate_disabled"
SECONDS_PER_HOUR = 60.0 * 60.0
_OPTION_ENTRY_FILLED_STATES = frozenset({"filled", "done", "completed", "complete"})
_OPTION_ENTRY_PARTIAL_STATES = frozenset(
    {"partially_filled", "partial", "partial_filled"}
)
_OPTION_ENTRY_TERMINAL_STATES = frozenset(
    {"cancelled", "canceled", "rejected", "failed", "expired"}
)


def _alert_signal_lane(alert: BreakoutAlert) -> str:
    snap = alert.indicator_snapshot if isinstance(alert.indicator_snapshot, dict) else {}
    scorecard = snap.get("imminent_scorecard") if isinstance(snap, dict) else {}
    if not isinstance(scorecard, dict):
        return ""
    return str(scorecard.get("signal_lane") or "").strip().lower()


def _alert_requests_shadow_observation(alert: BreakoutAlert) -> bool:
    return _alert_signal_lane(alert) in SHADOW_OBSERVATION_SIGNAL_LANES


def _should_run_llm_revalidation(alert: BreakoutAlert) -> tuple[bool, str | None]:
    if not bool(getattr(settings, "chili_autotrader_llm_revalidation_enabled", True)):
        return False, None
    if (
        bool(getattr(alert, "_chili_shadow_observation_only", False))
        and bool(
            getattr(
                settings,
                "chili_autotrader_llm_revalidation_skip_shadow_observation",
                AUTOTRADER_LLM_REVALIDATION_DEFAULT_SKIP_SHADOW_OBSERVATION,
            )
        )
    ):
        return False, LLM_REVALIDATION_SKIP_REASON_SHADOW_OBSERVATION
    if (
        (alert.asset_type or "").strip().lower() == "options"
        and bool(
            getattr(
                settings,
                "chili_autotrader_llm_revalidation_skip_options_path",
                AUTOTRADER_LLM_REVALIDATION_DEFAULT_SKIP_OPTIONS_PATH,
            )
        )
    ):
        return False, LLM_REVALIDATION_SKIP_REASON_OPTIONS_PATH
    return True, None


def _llm_revalidation_block_reason(llm_snapshot: dict[str, Any] | None) -> str:
    """Classify LLM gate failures without weakening the fail-closed gate."""
    snap = llm_snapshot if isinstance(llm_snapshot, dict) else {}
    error = str(snap.get("error") or "").strip().lower()
    raw_preview = str(snap.get("raw_preview") or "").strip()
    if error in {"llm_unavailable", "provider_unavailable", "not_configured"}:
        return "llm_unavailable"
    if error == "parse_failed" and not raw_preview:
        return "llm_unavailable"
    return "llm_not_viable"


def _bounded_minutes_setting(name: str, default: float) -> float:
    try:
        value = float(getattr(settings, name, default))
    except (TypeError, ValueError):
        value = float(default)
    if not math.isfinite(value):
        return float(default)
    return max(
        LIVE_REENTRY_COOLDOWN_MINUTES_FLOOR,
        min(LIVE_REENTRY_COOLDOWN_MINUTES_CEILING, value),
    )


def _live_reentry_cooldown_asset_enabled(asset_type: str) -> bool:
    raw = str(
        getattr(
            settings,
            "chili_autotrader_live_reentry_cooldown_asset_types",
            LIVE_REENTRY_COOLDOWN_DEFAULT_ASSET_TYPES,
        )
        or ""
    )
    allowed = {part.strip().lower() for part in raw.split(",") if part.strip()}
    if not allowed:
        return False
    asset = (asset_type or "").strip().lower() or STOCK_ASSET_TYPE
    return "*" in allowed or "all" in allowed or asset in allowed


def _recent_live_exit_cooldown_snapshot(
    db: Session,
    *,
    user_id: int | None,
    alert: BreakoutAlert,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return a block snapshot when a ticker is churning after a live exit."""
    asset_type = str(getattr(alert, "asset_type", "") or STOCK_ASSET_TYPE).lower()
    if not _live_reentry_cooldown_asset_enabled(asset_type):
        return None

    base_minutes = _bounded_minutes_setting(
        "chili_autotrader_live_reentry_cooldown_minutes",
        LIVE_REENTRY_COOLDOWN_DEFAULT_MINUTES,
    )
    stop_minutes = _bounded_minutes_setting(
        "chili_autotrader_live_stop_reentry_cooldown_minutes",
        LIVE_STOP_REENTRY_COOLDOWN_DEFAULT_MINUTES,
    )
    if base_minutes <= 0 and stop_minutes <= 0:
        return None

    now_utc = now or datetime.utcnow()
    lookback_minutes = max(base_minutes, stop_minutes)
    cutoff = now_utc - timedelta(minutes=lookback_minutes)
    ticker = str(getattr(alert, "ticker", "") or "").upper()
    if not ticker:
        return None

    q = (
        db.query(Trade)
        .filter(
            Trade.ticker == ticker,
            Trade.status == "closed",
            Trade.exit_date.isnot(None),
            Trade.exit_date >= cutoff,
            Trade.scan_pattern_id.isnot(None),
        )
    )
    if user_id is not None:
        q = q.filter(or_(Trade.user_id == user_id, Trade.user_id.is_(None)))
    q = q.order_by(Trade.exit_date.desc(), Trade.id.desc()).limit(10)

    for trade in q.all():
        exit_dt = getattr(trade, "exit_date", None)
        if exit_dt is None:
            continue
        exit_reason = str(getattr(trade, "exit_reason", "") or "").lower()
        is_stop = "stop" in exit_reason
        active_minutes = stop_minutes if is_stop else base_minutes
        if active_minutes <= 0:
            continue
        cooldown_until = exit_dt + timedelta(minutes=active_minutes)
        if now_utc < cooldown_until:
            return {
                "ticker": ticker,
                "asset_type": asset_type,
                "recent_exit_trade_id": getattr(trade, "id", None),
                "recent_exit_scan_pattern_id": getattr(trade, "scan_pattern_id", None),
                "candidate_scan_pattern_id": getattr(alert, "scan_pattern_id", None),
                "recent_exit_reason": getattr(trade, "exit_reason", None),
                "recent_exit_at": exit_dt.isoformat(),
                "cooldown_until": cooldown_until.isoformat(),
                "cooldown_minutes": active_minutes,
                "cooldown_scope": "ticker",
                "cooldown_policy": (
                    "stop_reentry" if is_stop else "post_exit_reentry"
                ),
            }
    return None


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) and out > 0 else None


def _normalize_option_meta_for_alert(
    alert: BreakoutAlert,
    meta: dict[str, Any],
    *,
    underlying_price: float | None = None,
) -> dict[str, Any]:
    return normalize_option_meta(
        meta,
        underlying=getattr(alert, "ticker", None),
        current_underlying_price=underlying_price,
    )


def _paper_entry_context_for_alert(
    alert: BreakoutAlert,
    *,
    px: float,
    snap: dict[str, Any] | None,
) -> tuple[float | None, dict[str, Any]]:
    underlying = _float_or_none(px)
    if not isinstance(snap, dict):
        return underlying, {}
    option_meta = snap.get("option_meta") if isinstance(snap.get("option_meta"), dict) else {}
    if not (snap.get("options_path") and option_meta):
        return underlying, {}
    if underlying is None:
        return None, {
            "asset_type": "options",
            "asset_kind": "option",
            "options_path": True,
            "option_meta": dict(option_meta),
            "price_domains": option_price_domains_snapshot(),
            "paper_entry_price_error": "invalid_underlying_price",
        }
    option_meta = _normalize_option_meta_for_alert(
        alert,
        option_meta,
        underlying_price=underlying,
    )
    premium = (
        _float_or_none(option_meta.get("limit_price"))
        or _float_or_none(alert.entry_price)
    )
    signal = {
        "asset_type": "options",
        "asset_kind": "option",
        "options_path": True,
        "option_meta": option_meta,
        "option_contract_key": option_meta.get("contract_key"),
        "price_domains": option_price_domains_snapshot(),
        "underlying_price_at_entry": underlying,
        "paper_entry_price_source": "option_premium",
    }
    if premium is None:
        signal["paper_entry_price_error"] = "missing_option_premium"
        return None, signal
    return float(premium), signal


def _order_state_from_response(res: dict[str, Any]) -> str:
    raw = res.get("raw") if isinstance(res.get("raw"), dict) else {}
    return str(
        res.get("state")
        or res.get("status")
        or raw.get("state")
        or raw.get("status")
        or ""
    ).strip().lower()


def _entry_fill_price_from_response(
    res: dict[str, Any],
    alert: BreakoutAlert,
    *,
    px: float,
    snap: dict[str, Any],
) -> float:
    raw = res.get("raw") if isinstance(res.get("raw"), dict) else {}
    option_meta = snap.get("option_meta") if isinstance(snap.get("option_meta"), dict) else {}
    is_option_entry = bool(res.get("_chili_options_path") or snap.get("options_path"))
    candidates: list[Any] = [
        raw.get("average_price"),
        raw.get("avg_price"),
        raw.get("price"),
        res.get("average_price"),
        res.get("avg_price"),
        res.get("price"),
    ]
    if is_option_entry:
        candidates.extend(
            [
                res.get("limit_price"),
                option_meta.get("limit_price"),
                alert.entry_price,
            ]
        )
    else:
        candidates.append(px)
    for candidate in candidates:
        parsed = _float_or_none(candidate)
        if parsed is not None:
            return parsed
    return float(px)


def _entry_broker_fill_price_from_response(res: dict[str, Any]) -> float | None:
    raw = res.get("raw") if isinstance(res.get("raw"), dict) else {}
    for candidate in (
        raw.get("average_price"),
        raw.get("average_filled_price"),
        raw.get("avg_fill_price"),
        raw.get("avg_price"),
        res.get("average_price"),
        res.get("average_filled_price"),
        res.get("avg_fill_price"),
        res.get("avg_price"),
    ):
        parsed = _float_or_none(candidate)
        if parsed is not None:
            return parsed
    return None


def _entry_tca_reference_price(
    res: dict[str, Any],
    alert: BreakoutAlert,
    *,
    px: float,
    snap: dict[str, Any],
    fill: float | None,
) -> float | None:
    raw = res.get("raw") if isinstance(res.get("raw"), dict) else {}
    option_meta = (
        snap.get("option_meta") if isinstance(snap.get("option_meta"), dict) else {}
    )
    response_option_meta = (
        res.get("_chili_option_meta")
        if isinstance(res.get("_chili_option_meta"), dict)
        else {}
    )
    is_option_entry = bool(res.get("_chili_options_path") or snap.get("options_path"))
    if is_option_entry:
        candidates: tuple[Any, ...] = (
            res.get("limit_price"),
            raw.get("limit_price"),
            response_option_meta.get("limit_price"),
            option_meta.get("limit_price"),
            alert.entry_price,
            fill,
        )
        for candidate in candidates:
            parsed = _float_or_none(candidate)
            if parsed is not None:
                return parsed
        return None
    return _float_or_none(px)


def _filled_qty_from_response(res: dict[str, Any], *, default_qty: float) -> float | None:
    raw = res.get("raw") if isinstance(res.get("raw"), dict) else {}

    def _nonnegative_float(value: Any) -> float | None:
        try:
            if value is None:
                return None
            out = float(value)
        except (TypeError, ValueError):
            return None
        return out if math.isfinite(out) and out >= 0.0 else None

    for key in (
        "filled_quantity",
        "cumulative_filled_quantity",
        "cumulative_quantity",
        "processed_quantity",
        "quantity_filled",
        "filled_size",
    ):
        parsed = _nonnegative_float(res.get(key))
        if parsed is not None:
            return min(parsed, default_qty)
        parsed = _nonnegative_float(raw.get(key))
        if parsed is not None:
            return min(parsed, default_qty)
    return None


def _entry_lifecycle_from_response(
    *,
    broker_source: str,
    res: dict[str, Any],
    snap: dict[str, Any],
    qty: float,
) -> tuple[str, str | None, float | None, float | None]:
    is_coinbase_entry = broker_source == "coinbase"
    if is_coinbase_entry:
        return "working", "accepted", 0.0, qty

    is_option_entry = bool(res.get("_chili_options_path") or snap.get("options_path"))
    if is_option_entry:
        state = _order_state_from_response(res)
        filled_qty = _filled_qty_from_response(res, default_qty=qty)
        if state in _OPTION_ENTRY_FILLED_STATES:
            if filled_qty is not None:
                if filled_qty <= 0.0:
                    return "cancelled", "filled_zero_quantity", 0.0, 0.0
                if filled_qty + 1e-9 < qty:
                    return "open", "partially_filled_cancelled", filled_qty, 0.0
            filled = filled_qty if filled_qty is not None else qty
            return "open", state or "filled", filled, max(qty - filled, 0.0)
        if state in _OPTION_ENTRY_PARTIAL_STATES:
            filled = filled_qty if filled_qty is not None else 0.0
            return "working", state or "partially_filled", filled, max(qty - filled, 0.0)
        if state in _OPTION_ENTRY_TERMINAL_STATES and filled_qty is not None and filled_qty > 0:
            filled = min(float(filled_qty), float(qty))
            if filled + 1e-9 < qty:
                return "open", "partially_filled_cancelled", filled, 0.0
            return "open", "filled", filled, 0.0
        if state in _OPTION_ENTRY_TERMINAL_STATES:
            return "cancelled", state or "cancelled", 0.0, 0.0
        return "working", state or "accepted", 0.0, qty

    return "open", None, None, None


def _entry_quantity_for_trade(
    *,
    is_option_entry: bool,
    requested_qty: float,
    entry_broker_status: str | None,
    entry_filled_qty: float | None,
) -> float:
    if (
        is_option_entry
        and entry_broker_status == "partially_filled_cancelled"
        and entry_filled_qty is not None
        and entry_filled_qty > 0
        and entry_filled_qty + 1e-9 < requested_qty
    ):
        return float(entry_filled_qty)
    return float(requested_qty)


def _detach_mismatched_option_position_link(db: Session, trade: Trade) -> bool:
    """Remove a trigger-created link from an option trade to an equity position.

    The current trading_positions natural key does not include option contract
    identity. Until the position schema grows an instrument dimension, the
    safer behavior is no position_id for option trades rather than a false link
    to the underlying equity row.
    """
    try:
        pos_id = getattr(trade, "position_id", None)
        if pos_id is None:
            return False
        row = db.execute(
            text("SELECT asset_kind FROM trading_positions WHERE id = :pid"),
            {"pid": int(pos_id)},
        ).first()
        asset_kind = str((row[0] if row else "") or "").strip().lower()
        if asset_kind in {"option", "options"}:
            return False
        db.execute(
            text(
                "UPDATE trading_trades "
                "SET position_id = NULL "
                "WHERE id = :tid AND position_id = :pid"
            ),
            {"tid": int(getattr(trade, "id")), "pid": int(pos_id)},
        )
        trade.position_id = None
        db.commit()
        logger.warning(
            "[autotrader_options] detached option trade_id=%s from non-option "
            "position_id=%s asset_kind=%s",
            getattr(trade, "id", None),
            pos_id,
            asset_kind or "unknown",
        )
        return True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.debug(
            "[autotrader_options] position-link detach failed for trade_id=%s",
            getattr(trade, "id", None),
            exc_info=True,
        )
        return False


def _autotrader_tick_note(
    out: dict[str, Any],
    *,
    kind: str,
    reason: str,
    alert: BreakoutAlert | None = None,
) -> None:
    """Record the latest tick outcome for a single INFO summary line."""
    out["tick_last_kind"] = kind
    out["tick_last_reason"] = (reason or "")[:500]
    if alert is not None:
        out["tick_last_alert_id"] = int(alert.id)
        out["tick_last_ticker"] = (alert.ticker or "").upper()


def _record_slowest_tick_alert(
    out: dict[str, Any],
    *,
    alert_id: int,
    ticker: str | None,
    elapsed_seconds: float,
) -> None:
    """Track the slowest alert in a tick for execution-latency forensics."""
    elapsed = round(max(0.0, elapsed_seconds), 3)
    current = float(out.get("tick_slowest_alert_elapsed_seconds") or 0.0)
    if elapsed < current:
        return
    out["tick_slowest_alert_elapsed_seconds"] = elapsed
    out["tick_slowest_alert_id"] = int(alert_id)
    out["tick_slowest_alert_ticker"] = (ticker or "").upper()


_CANDIDATE_POOL_ZERO_DIAG_INTERVAL_S = 300.0
_last_candidate_pool_zero_diag_at = 0.0


def _candidate_pool_zero_context(
    db: Session,
    *,
    uid: int,
    lookback_minutes: int = 120,
) -> dict[str, Any]:
    """Explain whether an empty pool means no supply or consumed supply."""
    rows = db.execute(
        text(
            """
            SELECT
                ba.id AS alert_id,
                ba.ticker,
                ba.alerted_at,
                ba.scan_pattern_id,
                COALESCE(sp.lifecycle_stage, 'none') AS lifecycle_stage,
                COALESCE(sp.active, TRUE) AS pattern_active,
                COALESCE(sp.recert_required, FALSE) AS recert_required,
                ar.id AS run_id,
                ar.decision,
                ar.reason,
                ar.created_at AS processed_at
            FROM trading_breakout_alerts ba
            LEFT JOIN scan_patterns sp ON sp.id = ba.scan_pattern_id
            LEFT JOIN trading_autotrader_runs ar ON ar.breakout_alert_id = ba.id
            WHERE ba.alert_tier = 'pattern_imminent'
              AND (ba.user_id = :uid OR ba.user_id IS NULL)
              AND ba.alerted_at >= NOW() - (:lookback_minutes * INTERVAL '1 minute')
            ORDER BY ba.id DESC
            LIMIT 200
            """
        ),
        {"uid": uid, "lookback_minutes": int(lookback_minutes)},
    ).mappings().all()
    lifecycle_counts: dict[str, int] = {}
    processed = 0
    unprocessed = 0
    latest: dict[str, Any] | None = None
    latest_unprocessed: dict[str, Any] | None = None
    for row in rows:
        r = dict(row)
        is_processed = r.get("run_id") is not None
        if is_processed:
            processed += 1
        else:
            unprocessed += 1
        stage = str(r.get("lifecycle_stage") or "none")
        key = f"{stage}:{'processed' if is_processed else 'unprocessed'}"
        lifecycle_counts[key] = lifecycle_counts.get(key, 0) + 1
        compact = {
            "alert_id": r.get("alert_id"),
            "ticker": r.get("ticker"),
            "scan_pattern_id": r.get("scan_pattern_id"),
            "lifecycle_stage": stage,
            "pattern_active": bool(r.get("pattern_active")),
            "recert_required": bool(r.get("recert_required")),
            "processed": is_processed,
            "decision": r.get("decision"),
            "reason": r.get("reason"),
            "alerted_at": str(r.get("alerted_at")) if r.get("alerted_at") else None,
            "processed_at": str(r.get("processed_at")) if r.get("processed_at") else None,
        }
        if latest is None:
            latest = compact
        if latest_unprocessed is None and not is_processed:
            latest_unprocessed = compact
    return {
        "lookback_minutes": int(lookback_minutes),
        "recent_alerts": len(rows),
        "processed": processed,
        "unprocessed": unprocessed,
        "lifecycle_counts": lifecycle_counts,
        "latest": latest,
        "latest_unprocessed": latest_unprocessed,
    }


def _maybe_log_candidate_pool_zero(db: Session, *, uid: int) -> dict[str, Any] | None:
    global _last_candidate_pool_zero_diag_at
    now = time.monotonic()
    if now - _last_candidate_pool_zero_diag_at < _CANDIDATE_POOL_ZERO_DIAG_INTERVAL_S:
        return None
    _last_candidate_pool_zero_diag_at = now
    try:
        diag = _candidate_pool_zero_context(db, uid=uid)
    except Exception as exc:
        logger.debug("[autotrader] candidate_pool_zero_diag failed: %s", exc, exc_info=True)
        return None
    latest = diag.get("latest") or {}
    logger.info(
        "[autotrader] candidate_pool_zero_diag uid=%s lookback_min=%s "
        "recent_alerts=%s processed=%s unprocessed=%s latest_alert_id=%s "
        "latest_ticker=%s latest_lifecycle=%s latest_decision=%s latest_reason=%s "
        "lifecycle_counts=%s",
        uid,
        diag.get("lookback_minutes"),
        diag.get("recent_alerts"),
        diag.get("processed"),
        diag.get("unprocessed"),
        latest.get("alert_id") or "-",
        latest.get("ticker") or "-",
        latest.get("lifecycle_stage") or "-",
        latest.get("decision") or "-",
        latest.get("reason") or "-",
        diag.get("lifecycle_counts") or {},
    )
    return diag


def _settings_int_clamped(name: str, default: int, *, lower: int, upper: int) -> int:
    raw = getattr(settings, name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = int(default)
    return max(int(lower), min(int(upper), value))


def _settings_float_clamped(
    name: str,
    default: float,
    *,
    lower: float,
    upper: float,
) -> float:
    raw = getattr(settings, name, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = float(default)
    if value != value:
        value = float(default)
    return max(float(lower), min(float(upper), value))


def _autotrader_candidate_batch_size() -> int:
    return _settings_int_clamped(
        "chili_autotrader_candidate_batch_size",
        AUTOTRADER_DEFAULT_CANDIDATE_BATCH_SIZE,
        lower=1,
        upper=AUTOTRADER_MAX_CANDIDATE_BATCH_SIZE,
    )


def _autotrader_tick_soft_budget_seconds() -> int:
    return _settings_int_clamped(
        "chili_autotrader_tick_max_seconds",
        AUTOTRADER_DEFAULT_TICK_MAX_SECONDS,
        lower=AUTOTRADER_MIN_TICK_MAX_SECONDS,
        upper=AUTOTRADER_MAX_TICK_MAX_SECONDS,
    )


def _candidate_actionability_state(
    settings_name: str,
    default_minutes: int,
    max_minutes: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    max_age_minutes = _settings_int_clamped(
        settings_name,
        default_minutes,
        lower=0,
        upper=max_minutes,
    )
    now_utc = now or datetime.utcnow()
    cutoff = (
        now_utc - timedelta(minutes=max_age_minutes)
        if max_age_minutes > 0
        else None
    )
    return {
        "enabled": max_age_minutes > 0,
        "max_age_minutes": max_age_minutes,
        "cutoff": cutoff,
    }


def _non_stock_candidate_actionability_state(
    now: datetime | None = None,
) -> dict[str, Any]:
    return _candidate_actionability_state(
        "chili_autotrader_non_stock_candidate_max_age_minutes",
        AUTOTRADER_NON_STOCK_CANDIDATE_MAX_AGE_DEFAULT_MINUTES,
        AUTOTRADER_NON_STOCK_CANDIDATE_MAX_AGE_MAX_MINUTES,
        now=now,
    )


def _stock_candidate_actionability_state(
    now: datetime | None = None,
) -> dict[str, Any]:
    return _candidate_actionability_state(
        "chili_autotrader_stock_candidate_max_age_minutes",
        AUTOTRADER_STOCK_CANDIDATE_MAX_AGE_DEFAULT_MINUTES,
        AUTOTRADER_STOCK_CANDIDATE_MAX_AGE_MAX_MINUTES,
        now=now,
    )


def _fresh_candidate_fastlane_state(
    now: datetime | None = None,
) -> dict[str, Any]:
    enabled = bool(
        getattr(
            settings,
            "chili_autotrader_fresh_candidate_fastlane_enabled",
            False,
        )
    )
    max_age_seconds = _settings_int_clamped(
        "chili_autotrader_fresh_candidate_fastlane_max_age_seconds",
        AUTOTRADER_FRESH_CANDIDATE_FASTLANE_DEFAULT_MAX_AGE_SECONDS,
        lower=AUTOTRADER_DEFAULT_TICK_INTERVAL_SECONDS,
        upper=AUTOTRADER_DEFAULT_TICK_INTERVAL_SECONDS * 30,
    )
    now_utc = now or datetime.utcnow()
    return {
        "enabled": enabled,
        "max_age_seconds": max_age_seconds,
        "cutoff": now_utc - timedelta(seconds=max_age_seconds),
    }


_last_stale_candidate_sweep_at = 0.0


def _stale_candidate_sweep_interval_seconds() -> int:
    return _settings_int_clamped(
        "chili_autotrader_stale_candidate_sweep_interval_seconds",
        AUTOTRADER_STALE_CANDIDATE_SWEEP_DEFAULT_SECONDS,
        lower=0,
        upper=AUTOTRADER_STALE_CANDIDATE_SWEEP_MAX_SECONDS,
    )


def _should_probe_stale_candidates(now: float | None = None) -> tuple[bool, int]:
    """Throttle older alert probes so fresh-entry ticks stay lightweight."""
    global _last_stale_candidate_sweep_at
    interval = _stale_candidate_sweep_interval_seconds()
    if interval <= 0:
        return True, interval
    observed_now = time.monotonic() if now is None else float(now)
    if observed_now - _last_stale_candidate_sweep_at >= interval:
        _last_stale_candidate_sweep_at = observed_now
        return True, interval
    return False, interval


def _candidate_order_by_clauses(
    fastlane_state: dict[str, Any],
) -> list[Any]:
    if not bool(fastlane_state.get("enabled")):
        return [BreakoutAlert.id.asc()]
    cutoff = fastlane_state.get("cutoff")
    if not isinstance(cutoff, datetime):
        return [BreakoutAlert.id.asc()]
    return [
        case((BreakoutAlert.alerted_at >= cutoff, 0), else_=1).asc(),
        BreakoutAlert.alerted_at.desc(),
        BreakoutAlert.id.desc(),
    ]


def _fresh_candidate_burst_batch_size(
    *,
    base_limit: int,
    fresh_fastlane_state: dict[str, Any],
    fresh_candidate_count: int,
) -> tuple[int, dict[str, Any]]:
    enabled = bool(
        getattr(
            settings,
            "chili_autotrader_fresh_candidate_burst_enabled",
            AUTOTRADER_FRESH_CANDIDATE_BURST_DEFAULT_ENABLED,
        )
    )
    meta: dict[str, Any] = {
        "enabled": enabled,
        "base_limit": int(base_limit),
        "effective_limit": int(base_limit),
        "fresh_candidate_count": max(0, int(fresh_candidate_count or 0)),
        "fresh_window_count": 1,
    }
    if (
        not enabled
        or not bool(fresh_fastlane_state.get("enabled"))
        or fresh_candidate_count <= base_limit
    ):
        return int(base_limit), meta

    tick_interval = _settings_int_clamped(
        "chili_autotrader_tick_interval_seconds",
        AUTOTRADER_DEFAULT_TICK_INTERVAL_SECONDS,
        lower=5,
        upper=120,
    )
    try:
        max_age_seconds = int(fresh_fastlane_state.get("max_age_seconds") or 0)
    except (TypeError, ValueError):
        max_age_seconds = AUTOTRADER_FRESH_CANDIDATE_FASTLANE_DEFAULT_MAX_AGE_SECONDS
    if max_age_seconds <= 0:
        max_age_seconds = AUTOTRADER_FRESH_CANDIDATE_FASTLANE_DEFAULT_MAX_AGE_SECONDS

    fresh_window_count = max(1, (max_age_seconds + tick_interval - 1) // tick_interval)
    window_capacity = min(
        AUTOTRADER_MAX_CANDIDATE_BATCH_SIZE,
        int(base_limit) * fresh_window_count,
    )
    effective_limit = max(
        int(base_limit),
        min(int(fresh_candidate_count), window_capacity),
    )
    meta.update(
        {
            "effective_limit": int(effective_limit),
            "fresh_window_count": int(fresh_window_count),
            "tick_interval_seconds": int(tick_interval),
            "fresh_window_seconds": int(max_age_seconds),
            "window_capacity": int(window_capacity),
        }
    )
    return int(effective_limit), meta


def _candidate_price_prefetch_enabled() -> bool:
    return bool(
        getattr(
            settings,
            "chili_autotrader_candidate_price_prefetch_enabled",
            False,
        )
    )


def _quote_price_from_snapshot(snapshot: Any) -> float | None:
    if not isinstance(snapshot, dict):
        return None
    for key in ("price", "last_price", "mid"):
        raw = snapshot.get(key)
        try:
            price = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            price = None
        if price is not None and price > 0:
            return price
    return None


def _quote_source_from_snapshot(snapshot: Any, default: str) -> str:
    if not isinstance(snapshot, dict):
        return default
    for key in ("source", "provider", "quote_source"):
        raw = snapshot.get(key)
        if raw is None:
            continue
        source = str(raw).strip()
        if source:
            return source
    return default


_LAST_CURRENT_PRICE_SOURCE_BY_TICKER: dict[str, str] = {}


def _prefetch_candidate_prices(
    candidates: list[BreakoutAlert],
) -> tuple[dict[str, float], dict[str, str], dict[str, Any]]:
    meta: dict[str, Any] = {
        "enabled": _candidate_price_prefetch_enabled(),
        "requested": 0,
        "hits": 0,
        "price_bus_hits": 0,
        "batch_requested": 0,
        "batch_hits": 0,
        "elapsed_seconds": 0.0,
        "error": None,
    }
    if not meta["enabled"] or not candidates:
        return {}, {}, meta
    tickers = sorted({
        str(getattr(alert, "ticker", "") or "").strip().upper()
        for alert in candidates
        if str(getattr(alert, "ticker", "") or "").strip()
    })
    meta["requested"] = len(tickers)
    if not tickers:
        return {}, {}, meta
    started = time.monotonic()
    prices: dict[str, float] = {}
    sources: dict[str, str] = {}
    try:
        try:
            from .price_bus import get_live_quote
        except Exception:
            get_live_quote = None  # type: ignore[assignment]
        if get_live_quote is not None:
            for ticker in tickers:
                quote = get_live_quote(ticker)
                price = _quote_price_from_snapshot(quote)
                if price is not None:
                    prices[ticker] = price
                    sources[ticker] = _quote_source_from_snapshot(quote, "price_bus")
        meta["price_bus_hits"] = len(prices)
        missing = [ticker for ticker in tickers if ticker not in prices]
        if not missing:
            meta["hits"] = len(prices)
            return prices, sources, meta
        from .market_data import fetch_quotes_batch

        meta["batch_requested"] = len(missing)
        quotes = fetch_quotes_batch(missing, allow_provider_fallback=False) or {}
        batch_hits = 0
        for ticker in missing:
            quote = quotes.get(ticker) or quotes.get(ticker.upper())
            price = _quote_price_from_snapshot(quote)
            if price is not None:
                prices[ticker] = price
                sources[ticker] = _quote_source_from_snapshot(quote, "batch_prefetch")
                batch_hits += 1
        meta["batch_hits"] = batch_hits
    except Exception as exc:
        meta["error"] = f"{type(exc).__name__}: {exc}"[:240]
        logger.debug("[autotrader] candidate price prefetch failed", exc_info=True)
    finally:
        meta["elapsed_seconds"] = round(time.monotonic() - started, 3)
    meta["hits"] = len(prices)
    return prices, sources, meta


def _attach_prefetched_prices(
    candidates: list[BreakoutAlert],
    prices: dict[str, float],
    sources: dict[str, str] | None = None,
) -> None:
    if not prices:
        return
    sources = sources or {}
    for alert in candidates:
        key = str(getattr(alert, "ticker", "") or "").strip().upper()
        price = prices.get(key)
        if price is None:
            continue
        setattr(alert, "_chili_prefetched_current_price", float(price))
        setattr(
            alert,
            "_chili_prefetched_current_price_source",
            str(sources.get(key) or "batch_prefetch").strip() or "batch_prefetch",
        )


def _current_price_for_alert(alert: BreakoutAlert) -> float | None:
    prefetched = getattr(alert, "_chili_prefetched_current_price", None)
    try:
        prefetched_price = float(prefetched) if prefetched is not None else None
    except (TypeError, ValueError):
        prefetched_price = None
    if prefetched_price is not None and prefetched_price > 0:
        source = str(
            getattr(alert, "_chili_prefetched_current_price_source", None)
            or "batch_prefetch"
        ).strip()
        setattr(alert, "_chili_current_price_source", source or "batch_prefetch")
        setattr(alert, "_chili_current_price_prefetch_used", True)
        return prefetched_price
    price = _current_price(alert.ticker)
    source = _LAST_CURRENT_PRICE_SOURCE_BY_TICKER.get(
        str(getattr(alert, "ticker", "") or "").strip().upper(),
        "single_fetch",
    )
    setattr(alert, "_chili_current_price_source", source or "single_fetch")
    setattr(alert, "_chili_current_price_prefetch_used", False)
    return price


def _stock_session_defer_state(now: datetime | None = None) -> dict[str, Any]:
    """Return candidate-selector state for stock alerts while the venue is shut.

    The rule gate still owns the hard live session block. This pre-filter only
    prevents fresh stock alerts from being permanently consumed by that block
    during closed hours, and keeps stale carryover bounded by configuration.
    """
    enabled = bool(
        getattr(
            settings,
            "chili_autotrader_stock_session_defer_enabled",
            AUTOTRADER_STOCK_SESSION_DEFER_DEFAULT_ENABLED,
        )
    )
    max_age_hours = _settings_float_clamped(
        "chili_autotrader_stock_session_defer_max_age_hours",
        AUTOTRADER_STOCK_SESSION_DEFER_DEFAULT_MAX_AGE_HOURS,
        lower=0.0,
        upper=float(AUTOTRADER_PAPER_SHADOW_MAX_JANITOR_MAX_AGE_HOURS),
    )
    now_utc = now or datetime.utcnow()
    cutoff = now_utc - timedelta(seconds=max_age_hours * SECONDS_PER_HOUR)
    state: dict[str, Any] = {
        "enabled": enabled,
        "active": False,
        "reason": STOCK_SESSION_DEFER_REASON_DISABLED,
        "max_age_hours": max_age_hours,
        "cutoff": cutoff,
    }
    if not enabled:
        return state
    if not bool(getattr(settings, "chili_autotrader_rth_only", True)):
        state["enabled"] = False
        state["reason"] = STOCK_SESSION_DEFER_REASON_RTH_GATE_DISABLED
        return state

    allow_ext = bool(getattr(settings, "chili_autotrader_allow_extended_hours", False))
    try:
        from .pattern_imminent_alerts import (
            us_stock_extended_session_open,
            us_stock_session_open,
        )

        session_open = (
            us_stock_extended_session_open() if allow_ext else us_stock_session_open()
        )
    except Exception:
        logger.debug("[autotrader] stock session defer probe failed", exc_info=True)
        state["enabled"] = False
        state["reason"] = "stock_session_probe_failed"
        return state

    state["reason"] = (
        "stock_session_open" if session_open else STOCK_SESSION_DEFER_REASON_CLOSED
    )
    state["active"] = not bool(session_open)
    return state


def _synergy_retry_limits(batch_slots_available: int) -> tuple[bool, int, int]:
    enabled = bool(getattr(settings, "chili_autotrader_synergy_retry_enabled", True))
    if not enabled or batch_slots_available <= 0:
        return False, 0, 0
    lookback_minutes = _settings_int_clamped(
        "chili_autotrader_synergy_retry_lookback_minutes",
        AUTOTRADER_SYNERGY_RETRY_DEFAULT_LOOKBACK_MINUTES,
        lower=AUTOTRADER_SYNERGY_RETRY_MIN_LOOKBACK_MINUTES,
        upper=AUTOTRADER_SYNERGY_RETRY_MAX_LOOKBACK_MINUTES,
    )
    max_per_tick = _settings_int_clamped(
        "chili_autotrader_synergy_retry_max_per_tick",
        AUTOTRADER_SYNERGY_RETRY_DEFAULT_MAX_PER_TICK,
        lower=0,
        upper=AUTOTRADER_MAX_CANDIDATE_BATCH_SIZE,
    )
    limit = min(int(batch_slots_available), int(max_per_tick))
    return limit > 0, lookback_minutes, limit


def _synergy_retry_candidates(
    db: Session,
    *,
    uid: int,
    limit: int,
) -> tuple[int, list[BreakoutAlert]]:
    enabled, lookback_minutes, capped_limit = _synergy_retry_limits(limit)
    if not enabled:
        return 0, []

    rows = db.execute(
        text(
            """
            WITH latest AS (
                SELECT DISTINCT ON (ar.breakout_alert_id)
                       ar.breakout_alert_id,
                       ar.id AS source_run_id,
                       ar.reason,
                       ar.created_at
                  FROM trading_autotrader_runs ar
                  JOIN trading_breakout_alerts ba
                    ON ba.id = ar.breakout_alert_id
                 WHERE ar.created_at >= NOW() - (:lookback_minutes * INTERVAL '1 minute')
                   AND ba.alert_tier = 'pattern_imminent'
                   AND (ba.user_id = :uid OR ba.user_id IS NULL)
                 ORDER BY ar.breakout_alert_id, ar.created_at DESC, ar.id DESC
            ),
            eligible AS (
                SELECT ba.id AS alert_id,
                       latest.source_run_id,
                       latest.created_at,
                       COUNT(*) OVER () AS retry_pool
                  FROM latest
                  JOIN trading_breakout_alerts ba
                    ON ba.id = latest.breakout_alert_id
                  JOIN LATERAL (
                        SELECT t.id, t.scan_pattern_id, t.entry_date
                          FROM trading_trades t
                         WHERE UPPER(t.ticker) = UPPER(ba.ticker)
                           AND t.status = 'open'
                           AND t.auto_trader_version = :autotrader_version
                           AND (t.user_id = :uid OR t.user_id IS NULL)
                         ORDER BY t.entry_date DESC NULLS LAST, t.id DESC
                         LIMIT 1
                  ) open_trade ON TRUE
                 WHERE latest.reason = :source_reason
                   AND COALESCE(open_trade.scan_pattern_id, 0)
                       <> COALESCE(ba.scan_pattern_id, 0)
                 ORDER BY latest.created_at DESC, ba.id DESC
                 LIMIT :query_limit
            )
            SELECT alert_id, source_run_id, retry_pool
              FROM eligible
            """
        ),
        {
            "uid": int(uid),
            "lookback_minutes": int(lookback_minutes),
            "query_limit": int(AUTOTRADER_MAX_CANDIDATE_BATCH_SIZE),
            "source_reason": SYNERGY_RETRY_SOURCE_REASON,
            "autotrader_version": AUTOTRADER_VERSION,
        },
    ).mappings().all()
    if not rows:
        return 0, []

    retry_pool = int(rows[0].get("retry_pool") or len(rows))
    ordered_ids = [int(row["alert_id"]) for row in rows]
    source_run_by_alert = {
        int(row["alert_id"]): int(row["source_run_id"]) for row in rows
    }
    alerts = (
        db.query(BreakoutAlert)
        .filter(BreakoutAlert.id.in_(ordered_ids))
        .all()
    )
    by_id = {int(alert.id): alert for alert in alerts}
    out: list[BreakoutAlert] = []
    for alert_id in ordered_ids:
        alert = by_id.get(alert_id)
        if alert is None:
            continue
        open_trade = find_open_autotrader_trade(
            db,
            user_id=uid,
            ticker=alert.ticker,
        )
        if open_trade is not None:
            try:
                if int(alert.scan_pattern_id or 0) in used_scale_in_pattern_ids(
                    db, open_trade,
                ):
                    continue
            except Exception:
                logger.debug(
                    "[autotrader] synergy retry used-pattern filter failed "
                    "alert_id=%s",
                    alert_id,
                    exc_info=True,
                )
        setattr(alert, "_chili_synergy_retry", True)
        setattr(
            alert,
            "_chili_synergy_retry_source_run_id",
            source_run_by_alert.get(alert_id),
        )
        setattr(alert, "_chili_synergy_retry_lookback_minutes", lookback_minutes)
        out.append(alert)
        if len(out) >= capped_limit:
            break
    return retry_pool, out

# Namespace byte for advisory locks so we can't collide with other
# subsystems that also use pg_advisory_lock on alert-shaped ints. The
# lock key is (NAMESPACE << 32) | breakout_alert_id — fits a signed
# bigint and is deterministic per alert.
_ALERT_CLAIM_LOCK_NAMESPACE = 0x4154  # "AT"


def _alert_claim_lock_key(alert_id: int) -> int:
    return (_ALERT_CLAIM_LOCK_NAMESPACE << 32) | (int(alert_id) & 0xFFFFFFFF)


def _try_claim_alert(db: Session, alert_id: int) -> bool:
    """Acquire a Postgres advisory lock on this alert so only one worker
    processes it. Released when the session closes (or explicitly via
    :func:`_release_alert_claim`). Returns False if another session holds
    the lock — caller should skip.

    Safer than TOCTOU around ``breakout_alert_already_processed``: that
    check reads the AutoTraderRun audit table, but two workers can both
    pass it concurrently before either writes. SQLite (tests) doesn't
    have advisory locks, so we fail open on non-Postgres dialects — the
    existing audit-row dedupe still covers the single-process case the
    test fixtures use.
    """
    try:
        dialect = db.bind.dialect.name if db.bind else ""
    except Exception:
        dialect = ""
    if dialect != "postgresql":
        return True
    try:
        got = db.execute(
            text("SELECT pg_try_advisory_lock(:k)"),
            {"k": _alert_claim_lock_key(alert_id)},
        ).scalar()
        return bool(got)
    except Exception:
        # DB-level failure must not block the loop; treat as 'claimed'
        # and rely on the audit-row check + idempotency_store as fallback.
        logger.warning(
            "[autotrader] advisory lock acquire failed for alert=%s; falling back",
            alert_id, exc_info=True,
        )
        return True


def _release_alert_claim(db: Session, alert_id: int) -> None:
    try:
        dialect = db.bind.dialect.name if db.bind else ""
    except Exception:
        dialect = ""
    if dialect != "postgresql":
        return
    try:
        db.execute(
            text("SELECT pg_advisory_unlock(:k)"),
            {"k": _alert_claim_lock_key(alert_id)},
        )
    except Exception:
        logger.debug(
            "[autotrader] advisory unlock failed for alert=%s; will release on session close",
            alert_id, exc_info=True,
        )


# AAA -- janitor: kill leaked autotrader advisory-lock holders.
#
# When XX's outer wall-clock budget abandons a hung worker thread, the
# thread's DB session stays alive (Python can't safely kill a thread).
# That orphan session keeps any pg_advisory_lock it acquired -- which
# means every subsequent tick fails to claim the same alert with
# advisory_lock_busy. Diagnosed via pg_stat_activity + pg_locks:
# sessions stuck "idle in transaction" with state_change_age > N seconds,
# holding a lock in our 0x4154 namespace, are leaked.
#
# This janitor runs at the START of every autotrader tick, before any
# work. It's cheap (one indexed query) and idempotent -- pg_terminate_backend
# is a no-op on a session that has already finished. Threshold is
# generous (default 120s) so we don't fight legitimate slow ticks; the
# tick budget is 45s so anything older is definitely orphaned.

def _cleanup_leaked_advisory_locks(db: Session) -> int:
    """Terminate orphan sessions holding autotrader advisory locks.

    Returns count of sessions terminated. Best-effort: any failure logs
    and returns 0 -- never raises into the tick.
    """
    try:
        dialect = db.bind.dialect.name if db.bind else ""
    except Exception:
        dialect = ""
    if dialect != "postgresql":
        return 0
    try:
        threshold_s = max(60, int(getattr(settings, "chili_autotrader_leak_cleanup_threshold_s", 120)))
    except Exception:
        threshold_s = 120
    try:
        rows = db.execute(
            text(
                "SELECT pa.pid, "
                "       EXTRACT(EPOCH FROM (NOW() - pa.state_change))::int AS age_s, "
                "       pa.state "
                "FROM pg_stat_activity pa "
                "JOIN pg_locks l ON l.pid = pa.pid "
                "WHERE l.locktype = 'advisory' "
                "  AND l.classid::int = :ns "
                "  AND pa.state IN ('idle in transaction', 'idle in transaction (aborted)') "
                "  AND EXTRACT(EPOCH FROM (NOW() - pa.state_change)) > :thr "
            ),
            {"ns": _ALERT_CLAIM_LOCK_NAMESPACE, "thr": threshold_s},
        ).fetchall()
        killed = 0
        for r in rows or []:
            pid, age_s, state = int(r[0]), int(r[1] or 0), r[2]
            try:
                db.execute(
                    text("SELECT pg_terminate_backend(:p)"),
                    {"p": pid},
                )
                killed += 1
                logger.warning(
                    "[autotrader] AAA janitor: terminated leaked session "
                    "pid=%s state=%s age=%ss (orphan lock from prior abandoned tick)",
                    pid, state, age_s,
                )
            except Exception as e:
                logger.debug("[autotrader] AAA janitor terminate pid=%s failed: %s", pid, e)
        if killed:
            db.commit()
        return killed
    except Exception as e:
        logger.debug("[autotrader] AAA janitor pass failed: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
        return 0


def _resolve_user_id() -> Optional[int]:
    return getattr(settings, "chili_autotrader_user_id", None) or getattr(
        settings, "brain_default_user_id", None
    )


def _resolve_entry_risk_notional(
    db: Session,
    *,
    uid: int | None,
) -> tuple[float, dict[str, Any]]:
    """Resolve the base entry notional from account risk, not a fixed ticket.

    The old path fell back to a hard $300 entry size. This helper prefers live
    broker equity times the configured risk budget and risk dial. Capital
    resolver ``fallback:*`` values are treated as unproven and do not size
    entries; an explicit dollar fallback is honored only when the operator
    sets it above zero.
    """
    from .auto_trader_rules import (
        resolve_brain_risk_context,
        resolve_effective_capital,
    )

    snap: dict[str, Any] = {}
    try:
        fallback_notional = float(
            getattr(settings, "chili_autotrader_per_trade_notional_usd", 0.0) or 0.0
        )
    except Exception:
        fallback_notional = 0.0
    try:
        per_trade_pct = float(
            getattr(
                settings,
                "chili_autotrader_per_trade_risk_pct",
                DEFAULT_PER_TRADE_RISK_PCT,
            )
            or 0.0
        )
    except Exception:
        per_trade_pct = DEFAULT_PER_TRADE_RISK_PCT

    equity, cap_source = resolve_effective_capital(db, settings)
    brain_ctx = resolve_brain_risk_context(
        db, user_id=uid, settings_override=settings,
    )
    try:
        dial = float(brain_ctx.get("dial_value", 1.0))
    except Exception:
        dial = 1.0

    snap["notional_risk_pct"] = per_trade_pct
    snap["notional_dial"] = dial
    snap["notional_capital_source"] = cap_source
    snap["notional_capital_usd"] = round(float(equity or 0.0), 2)
    snap["notional_explicit_fallback_usd"] = round(float(fallback_notional), 2)

    capital_source_is_fallback = str(cap_source or "").startswith("fallback:")
    snap["notional_capital_proven"] = not capital_source_is_fallback
    if capital_source_is_fallback:
        snap["notional_capital_unproven"] = True

    if equity > 0 and per_trade_pct > 0 and not capital_source_is_fallback:
        return equity * (per_trade_pct / PERCENT_SCALE) * dial, {
            **snap,
            "notional_source": "equity_pct_dial",
        }
    if fallback_notional > 0:
        return fallback_notional * dial, {
            **snap,
            "notional_source": "explicit_env_notional_dial",
        }
    return 0.0, {**snap, "notional_source": "capital_unavailable"}


def _resolve_shadow_observation_lightweight_notional() -> tuple[float, dict[str, Any]]:
    """Resolve paper evidence size without live broker/account calls."""
    try:
        evidence_notional = float(
            getattr(
                settings,
                SHADOW_OBSERVATION_EVIDENCE_NOTIONAL_SETTING,
                AUTOTRADER_SHADOW_OBSERVATION_EVIDENCE_NOTIONAL_DEFAULT_USD,
            )
            or 0.0
        )
    except Exception:
        evidence_notional = AUTOTRADER_SHADOW_OBSERVATION_EVIDENCE_NOTIONAL_DEFAULT_USD
    try:
        per_trade_pct = float(
            getattr(
                settings,
                "chili_autotrader_per_trade_risk_pct",
                DEFAULT_PER_TRADE_RISK_PCT,
            )
            or 0.0
        )
    except Exception:
        per_trade_pct = 0.0
    try:
        assumed_capital = float(
            getattr(settings, "chili_autotrader_assumed_capital_usd", 0.0) or 0.0
        )
    except Exception:
        assumed_capital = 0.0

    snap: dict[str, Any] = {
        "notional_risk_pct": per_trade_pct,
        "notional_dial": SHADOW_OBSERVATION_LIGHTWEIGHT_DIAL,
        "notional_capital_source": "shadow_observation_lightweight",
        "notional_capital_usd": round(float(assumed_capital), MONEY_ROUND_DIGITS),
        "notional_explicit_fallback_usd": round(
            float(evidence_notional),
            MONEY_ROUND_DIGITS,
        ),
        "notional_evidence_configured_usd": round(
            float(evidence_notional),
            MONEY_ROUND_DIGITS,
        ),
        "notional_broker_lookup_skipped": True,
    }
    if evidence_notional > 0.0:
        return evidence_notional * SHADOW_OBSERVATION_LIGHTWEIGHT_DIAL, {
            **snap,
            "notional_source": SHADOW_OBSERVATION_NOTIONAL_SOURCE_EVIDENCE,
        }
    if assumed_capital > 0.0 and per_trade_pct > 0.0:
        return (
            assumed_capital
            * (per_trade_pct / PERCENT_SCALE)
            * SHADOW_OBSERVATION_LIGHTWEIGHT_DIAL
        ), {
            **snap,
            "notional_source": SHADOW_OBSERVATION_NOTIONAL_SOURCE_ASSUMED,
        }
    return 0.0, {
        **snap,
        "notional_source": SHADOW_OBSERVATION_NOTIONAL_SOURCE_UNAVAILABLE,
    }


def _managed_edge_execution_levels(
    alert: BreakoutAlert,
    *,
    px: float,
    snap: dict[str, Any] | None,
) -> tuple[float | None, float | None, dict[str, Any] | None]:
    stop_price = float(alert.stop_loss) if alert.stop_loss is not None else None
    target_price = float(alert.target_price) if alert.target_price is not None else None
    if not isinstance(snap, dict):
        return stop_price, target_price, None
    edge = snap.get("entry_edge")
    if not isinstance(edge, dict):
        return stop_price, target_price, None
    managed = edge.get("managed_exit_edge")
    if not isinstance(managed, dict) or not bool(managed.get("selected")):
        return stop_price, target_price, None
    if edge.get("edge_geometry_source") != MANAGED_EDGE_GEOMETRY_SOURCE:
        return stop_price, target_price, None
    geometry = managed.get("geometry")
    if not isinstance(geometry, dict):
        geometry = {}

    try:
        px_f = float(px)
    except (TypeError, ValueError):
        px_f = 0.0
    managed_target = geometry.get("managed_target_price")
    managed_stop = geometry.get("managed_stop_price")
    if managed_target is None:
        try:
            managed_target = px_f * (1.0 + float(edge.get("reward_fraction")))
        except (TypeError, ValueError):
            managed_target = None
    if managed_stop is None:
        try:
            managed_stop = px_f * (1.0 - float(edge.get("stop_loss_fraction")))
        except (TypeError, ValueError):
            managed_stop = None

    applied: dict[str, Any] = {
        "source": MANAGED_EDGE_GEOMETRY_SOURCE,
        "full_bracket_target_price": target_price,
        "full_bracket_stop_price": stop_price,
    }
    try:
        target_f = float(managed_target)
    except (TypeError, ValueError):
        target_f = 0.0
    if target_f > 0.0 and (px_f <= 0.0 or target_f > px_f):
        target_price = round(target_f, MANAGED_EDGE_PRICE_ROUND_DIGITS)
        applied["managed_target_price"] = target_price
    try:
        stop_f = float(managed_stop)
    except (TypeError, ValueError):
        stop_f = 0.0
    if stop_f > 0.0 and (px_f <= 0.0 or stop_f < px_f):
        stop_price = round(stop_f, MANAGED_EDGE_PRICE_ROUND_DIGITS)
        applied["managed_stop_price"] = stop_price
    if (
        applied.get("managed_target_price") is None
        and applied.get("managed_stop_price") is None
    ):
        return stop_price, target_price, None
    return stop_price, target_price, applied


def _paper_shadow_reason_family(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    for prefix in PAPER_SHADOW_REASON_FAMILY_PREFIXES:
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    if raw in {SYNERGY_RETRY_SOURCE_REASON, SYNERGY_RETRY_EXHAUSTED_REASON}:
        return PAPER_SHADOW_REASON_FAMILY_SYNERGY_NOT_APPLICABLE
    return raw or None


def _paper_shadow_reason_families(
    decision: str | None,
    snap: dict[str, Any] | None,
) -> frozenset[str]:
    values: list[Any] = [decision]
    if isinstance(snap, dict):
        values.extend(snap.get(key) for key in PAPER_SHADOW_REASON_FAMILY_SNAPSHOT_KEYS)
    return frozenset(
        family for family in (_paper_shadow_reason_family(value) for value in values)
        if family
    )


def _find_same_alert_reason_family_shadow(
    db: Session,
    *,
    uid: int,
    alert: BreakoutAlert,
    reason_families: frozenset[str],
) -> tuple[PaperTrade | None, str | None]:
    if not reason_families:
        return None, None
    rows = (
        db.query(PaperTrade)
        .filter(
            PaperTrade.user_id == uid,
            PaperTrade.paper_shadow_of_alert_id == int(alert.id),
        )
        .order_by(PaperTrade.id.desc())
        .all()
    )
    for row in rows:
        sig = row.signal_json if isinstance(row.signal_json, dict) else {}
        existing_families = _paper_shadow_reason_families(
            str(sig.get("shadow_decision") or ""),
            sig,
        )
        matched = reason_families & existing_families
        if matched:
            return row, sorted(matched)[0]
    return None, None


def _find_recent_candidate_reason_family_shadow(
    db: Session,
    *,
    uid: int,
    alert: BreakoutAlert,
    reason_families: frozenset[str],
    window_minutes: int,
) -> tuple[PaperTrade | None, str | None]:
    if not reason_families or window_minutes <= 0:
        return None, None
    try:
        now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        cutoff = now_utc_naive - timedelta(minutes=int(window_minutes))
        pattern_id = int(alert.scan_pattern_id or 0)
    except (TypeError, ValueError):
        return None, None
    if pattern_id <= 0:
        return None, None
    rows = (
        db.query(PaperTrade)
        .filter(
            PaperTrade.user_id == uid,
            PaperTrade.ticker == (alert.ticker or "").upper(),
            PaperTrade.scan_pattern_id == pattern_id,
            PaperTrade.paper_shadow_of_alert_id.isnot(None),
            PaperTrade.entry_date >= cutoff,
        )
        .order_by(PaperTrade.id.desc())
        .all()
    )
    for row in rows:
        sig = row.signal_json if isinstance(row.signal_json, dict) else {}
        existing_families = _paper_shadow_reason_families(
            str(sig.get("shadow_decision") or ""),
            sig,
        )
        matched = reason_families & existing_families
        if matched:
            return row, sorted(matched)[0]
    return None, None


def _paper_shadow_has_option_context(snap: dict[str, Any] | None) -> bool:
    if not isinstance(snap, dict):
        return False
    option_meta = snap.get("option_meta")
    return (
        bool(snap.get("options_path"))
        and isinstance(option_meta, dict)
        and bool(option_meta)
    )


def _paper_shadow_asset_class(
    alert: BreakoutAlert,
    snap: dict[str, Any] | None,
) -> str:
    candidates: list[Any] = []
    if isinstance(snap, dict):
        candidates.extend(
            snap.get(key) for key in ("asset_class", "asset_type", "asset_kind")
        )
    candidates.append(getattr(alert, "asset_type", None))
    has_option_context = _paper_shadow_has_option_context(snap)
    for raw in candidates:
        value = str(raw or "").strip().lower()
        if value in {"crypto", "coin", "coinbase_spot"}:
            return "crypto"
        if value in {"option", "options", "robinhood_options"} and has_option_context:
            return "options"
        if value in {
            "stock",
            "stocks",
            "equity",
            "equities",
            "robinhood",
            "robinhood_equity",
        }:
            return "stock"
    ticker = str(getattr(alert, "ticker", "") or "").strip().upper()
    if ticker.endswith("-USD"):
        return "crypto"
    return "stock"


def _paper_shadow_asset_type(
    alert: BreakoutAlert,
    snap: dict[str, Any] | None,
    *,
    asset_class: str,
) -> str:
    raw = None
    if isinstance(snap, dict):
        raw = (
            snap.get("asset_type")
            or snap.get("asset_class")
            or snap.get("asset_kind")
        )
    if raw is None:
        raw = getattr(alert, "asset_type", None)
    value = str(raw or "").strip().lower()
    if value in {"crypto", "coin", "coinbase_spot"}:
        return "crypto"
    if value in {"option", "options", "robinhood_options"}:
        return "options" if asset_class == "options" else asset_class
    if value in {
        "stock",
        "stocks",
        "equity",
        "equities",
        "robinhood",
        "robinhood_equity",
    }:
        return "stock"
    return asset_class


def _paper_shadow_signal_json(
    alert: BreakoutAlert,
    snap: dict[str, Any] | None,
    decision: str,
    *,
    duplicate_policy: str,
) -> dict[str, Any]:
    safe_snap = snap if isinstance(snap, dict) else {}
    sig: dict[str, Any] = {
        "auto_trader_v1": True,
        "breakout_alert_id": int(alert.id),
        "paper_shadow": True,
        "shadow_of_alert_id": int(alert.id),
        "shadow_decision": decision,
        "projected": safe_snap.get("projected_profit_pct"),
        "paper_shadow_duplicate_policy": duplicate_policy,
    }
    entry_edge = safe_snap.get("entry_edge")
    if isinstance(entry_edge, dict):
        sig["entry_edge"] = dict(entry_edge)
    expected_net_pct = _expected_net_pct_from_snapshot(safe_snap)
    if expected_net_pct is not None:
        sig["entry_edge_expected_net_pct"] = expected_net_pct
    asset_class = _paper_shadow_asset_class(alert, safe_snap)
    sig["asset_class"] = asset_class
    sig["asset_type"] = _paper_shadow_asset_type(
        alert,
        safe_snap,
        asset_class=asset_class,
    )
    if safe_snap.get("paper_observation_signal_lane"):
        sig["paper_observation_signal_lane"] = safe_snap.get(
            "paper_observation_signal_lane"
        )
    sig.update({
        k: v
        for k, v in safe_snap.items()
        if isinstance(k, str) and k.startswith(PAPER_SHADOW_AUDIT_PREFIX)
    })
    return sig


def _paper_shadow_capacity_admission(
    db: Session,
    *,
    uid: int | None,
    scan_pattern_id: int | None,
    signal_json: dict[str, Any],
    shadow_max_open: int,
    buffer: int,
) -> dict[str, Any]:
    """Decide whether a new shadow row is worth evicting existing evidence."""
    open_limit = max(1, int(shadow_max_open or 1))
    target_open = max(0, open_limit - max(0, int(buffer or 0)))
    capacity_trigger = max(1, target_open)
    try:
        from .paper_trading import (
            _is_autotrader_paper_shadow_row,
            _paper_shadow_evidence_priority,
            _paper_shadow_evict_key,
            _paper_shadow_pattern_stage_map,
        )

        q = db.query(PaperTrade).filter(PaperTrade.status == "open")
        if uid is not None:
            q = q.filter(PaperTrade.user_id == uid)
        rows = [pt for pt in q.all() if _is_autotrader_paper_shadow_row(pt)]
        if len(rows) < capacity_trigger:
            return {
                "admit": True,
                "reason": "capacity_available",
                "open_count": len(rows),
                "capacity_trigger": capacity_trigger,
            }

        candidate = SimpleNamespace(
            scan_pattern_id=scan_pattern_id,
            signal_json=dict(signal_json or {}),
            entry_date=datetime.utcnow(),
        )
        pattern_stage_by_id = _paper_shadow_pattern_stage_map(
            db,
            [*rows, candidate],
        )
        candidate_priority = int(
            _paper_shadow_evidence_priority(
                candidate,
                pattern_stage_by_id=pattern_stage_by_id,
            ).get("priority")
            or 0
        )
        weakest = min(
            rows,
            key=lambda pt: _paper_shadow_evict_key(
                pt,
                pattern_stage_by_id=pattern_stage_by_id,
            ),
        )
        weakest_priority = int(
            _paper_shadow_evidence_priority(
                weakest,
                pattern_stage_by_id=pattern_stage_by_id,
            ).get("priority")
            or 0
        )
        admit = candidate_priority > weakest_priority
        return {
            "admit": admit,
            "reason": (
                "candidate_priority_beats_weakest"
                if admit
                else "candidate_priority_not_above_capacity_floor"
            ),
            "open_count": len(rows),
            "capacity_trigger": capacity_trigger,
            "candidate_priority": candidate_priority,
            "weakest_priority": weakest_priority,
            "weakest_paper_trade_id": getattr(weakest, "id", None),
        }
    except Exception:
        logger.debug("[autotrader_paper_shadow] capacity admission failed", exc_info=True)
        return {"admit": True, "reason": "admission_check_failed_open"}


def _maybe_open_paper_shadow(
    db: Session,
    *,
    uid: int | None,
    alert: BreakoutAlert,
    qty: float,
    px: float,
    snap: dict[str, Any],
    decision: str,
    allow_duplicate_open: bool = False,
) -> None:
    """f-add-paper-shadow-mode (2026-05-06): always-on within the live
    branch when ``chili_autotrader_paper_shadow_enabled`` is True. Opens
    a paper trade in parallel with each live decision so we have
    matched live-vs-shadow pairs for execution-alpha-drag analysis,
    pure-strategy pattern evidence, and brain learning during low-
    live-placement-rate periods.

    Idempotent at the (user_id, ticker, pattern_id) tuple via the
    existing dedupe in ``open_paper_trade`` unless a qualified block/reject
    is allowed to bypass duplicate paper rows for counterfactual outcome
    learning. Failures swallowed at this boundary -- shadow must never
    break the live decision flow.
    """
    base_enabled = bool(getattr(settings, "chili_autotrader_paper_shadow_enabled", False))
    qualified_enabled = bool(
        getattr(settings, "chili_autotrader_paper_shadow_qualified_blocks_enabled", True)
    )
    if not base_enabled and not (
        qualified_enabled and decision in QUALIFIED_BLOCK_PAPER_SHADOW_DECISIONS
    ):
        return
    if uid is None:
        return
    try:
        effective_allow_duplicate_open = bool(
            allow_duplicate_open
            or (
                decision in QUALIFIED_BLOCK_PAPER_SHADOW_DECISIONS
                and bool(
                    getattr(
                        settings,
                        "chili_autotrader_paper_shadow_reject_allow_duplicate_open",
                        AUTOTRADER_PAPER_SHADOW_DEFAULT_REJECT_ALLOW_DUPLICATE_OPEN,
                    )
                )
            )
        )
        from .paper_trading import (
            PAPER_TRADE_CAPACITY_SCOPE_AUTOTRADER_SHADOW,
            open_paper_trade,
            prune_autotrader_paper_shadow_capacity,
        )
        sig = _paper_shadow_signal_json(
            alert,
            snap,
            decision,
            duplicate_policy=(
                PAPER_SHADOW_DUPLICATE_POLICY_REJECT_BYPASS
                if effective_allow_duplicate_open
                else PAPER_SHADOW_DUPLICATE_POLICY_STRICT
            ),
        )
        if bool(
            getattr(
                settings,
                "chili_autotrader_paper_shadow_dedupe_same_alert_reason_family",
                AUTOTRADER_PAPER_SHADOW_DEFAULT_DEDUPE_SAME_ALERT_REASON_FAMILY,
            )
        ):
            reason_families = _paper_shadow_reason_families(decision, sig)
            duplicate, duplicate_family = _find_same_alert_reason_family_shadow(
                db,
                uid=uid,
                alert=alert,
                reason_families=reason_families,
            )
            if duplicate is not None:
                logger.info(
                    "[autotrader_paper_shadow] alert_id=%s pattern_id=%s ticker=%s "
                    "decision=%s skipped reason=%s family=%s existing_paper_trade_id=%s",
                    alert.id,
                    alert.scan_pattern_id,
                    alert.ticker,
                    decision,
                    PAPER_SHADOW_DUPLICATE_SKIP_REASON_SAME_ALERT_FAMILY,
                    duplicate_family,
                    getattr(duplicate, "id", None),
                )
                return
        if effective_allow_duplicate_open:
            reason_families = _paper_shadow_reason_families(decision, sig)
            recent_window_minutes = int(
                getattr(
                    settings,
                    "chili_autotrader_paper_shadow_dedupe_recent_reason_family_minutes",
                    AUTOTRADER_PAPER_SHADOW_DEFAULT_DEDUPE_RECENT_REASON_FAMILY_MINUTES,
                )
                or 0
            )
            duplicate, duplicate_family = _find_recent_candidate_reason_family_shadow(
                db,
                uid=uid,
                alert=alert,
                reason_families=reason_families,
                window_minutes=recent_window_minutes,
            )
            if duplicate is not None:
                logger.info(
                    "[autotrader_paper_shadow] alert_id=%s pattern_id=%s ticker=%s "
                    "decision=%s skipped reason=%s family=%s "
                    "existing_paper_trade_id=%s window_minutes=%s",
                    alert.id,
                    alert.scan_pattern_id,
                    alert.ticker,
                    decision,
                    PAPER_SHADOW_DUPLICATE_SKIP_REASON_RECENT_CANDIDATE_FAMILY,
                    duplicate_family,
                    getattr(duplicate, "id", None),
                    recent_window_minutes,
                )
                return
        shadow_max_open = int(
            getattr(
                settings,
                "chili_autotrader_paper_shadow_max_open",
                AUTOTRADER_PAPER_SHADOW_DEFAULT_MAX_OPEN,
            )
            or AUTOTRADER_PAPER_SHADOW_DEFAULT_MAX_OPEN
        )
        shadow_buffer = int(
            getattr(
                settings,
                "chili_autotrader_paper_shadow_janitor_buffer",
                AUTOTRADER_PAPER_SHADOW_DEFAULT_JANITOR_BUFFER,
            )
            or 0
        )
        capacity_admission = _paper_shadow_capacity_admission(
            db,
            uid=uid,
            scan_pattern_id=alert.scan_pattern_id,
            signal_json=sig,
            shadow_max_open=shadow_max_open,
            buffer=shadow_buffer,
        )
        if not bool(capacity_admission.get("admit", True)):
            logger.info(
                "[autotrader_paper_shadow] alert_id=%s pattern_id=%s ticker=%s "
                "decision=%s skipped reason=shadow_capacity_floor %s",
                alert.id,
                alert.scan_pattern_id,
                alert.ticker,
                decision,
                capacity_admission,
            )
            return
        if bool(getattr(settings, "chili_autotrader_paper_shadow_janitor_enabled", True)):
            prune_autotrader_paper_shadow_capacity(
                db,
                uid,
                max_open=shadow_max_open,
                max_age_hours=int(
                    getattr(
                        settings,
                        "chili_autotrader_paper_shadow_janitor_max_age_hours",
                        AUTOTRADER_PAPER_SHADOW_DEFAULT_JANITOR_MAX_AGE_HOURS,
                    )
                    or AUTOTRADER_PAPER_SHADOW_DEFAULT_JANITOR_MAX_AGE_HOURS
                ),
                buffer=shadow_buffer,
            )
        paper_entry_px, option_paper_sig = _paper_entry_context_for_alert(
            alert,
            px=px,
            snap=snap,
        )
        if paper_entry_px is None:
            logger.info(
                "[autotrader_paper_shadow] alert_id=%s ticker=%s skipped: "
                "paper entry price unavailable",
                alert.id,
                alert.ticker,
            )
            return
        if option_paper_sig:
            sig.update(option_paper_sig)
        shadow_stop, shadow_target, managed_exit_execution = (
            _managed_edge_execution_levels(alert, px=px, snap=snap)
        )
        if option_paper_sig:
            shadow_stop = None
            shadow_target = None
        if managed_exit_execution is not None:
            sig["managed_exit_execution"] = managed_exit_execution
        pt = open_paper_trade(
            db, uid, alert.ticker, paper_entry_px,
            scan_pattern_id=alert.scan_pattern_id,
            stop_price=shadow_stop,
            target_price=shadow_target,
            direction="long",
            quantity=float(qty),
            signal_json=sig,
            paper_shadow_of_alert_id=int(alert.id),
            max_open_trades=shadow_max_open,
            capacity_scope=PAPER_TRADE_CAPACITY_SCOPE_AUTOTRADER_SHADOW,
            allow_duplicate_open=effective_allow_duplicate_open,
        )
        if pt is None:
            logger.info(
                "[autotrader_paper_shadow] alert_id=%s pattern_id=%s ticker=%s "
                "decision=%s skipped reason=paper_open_failed_or_duplicate max_open=%s",
                alert.id, alert.scan_pattern_id, alert.ticker, decision, shadow_max_open,
            )
            return
        db.commit()
        logger.info(
            "[autotrader_paper_shadow] alert_id=%s pattern_id=%s ticker=%s "
            "qty=%s px=%s decision=%s opened",
            alert.id, alert.scan_pattern_id, alert.ticker, qty, px, decision,
        )
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.debug(
            "[autotrader_paper_shadow] open failed alert_id=%s decision=%s",
            getattr(alert, "id", None), decision, exc_info=True,
        )


def _qualified_reject_shadow_decision(reason: str | None) -> str | None:
    """Map live-only reject reasons to safe paper-shadow observation labels."""
    r = (reason or "").strip()
    if r.startswith("regime_gate:"):
        return "blocked_regime_gate"
    if r in {
        "duplicate_pattern_already_open",
        "non_positive_expected_edge",
    }:
        return f"skipped_{r}"
    if r in {
        "llm_not_viable",
        "llm_unavailable",
    }:
        return f"blocked_{r}"
    if r in {
        "synergy_disabled_second_signal",
        "synergy_not_applicable",
        SYNERGY_RETRY_EXHAUSTED_REASON,
    }:
        return f"skipped_{r}"
    if r in {
        "max_concurrent_crypto",
        "max_concurrent_equity",
        "max_concurrent_global",
        "max_concurrent_options",
    }:
        return f"blocked_{r}"
    return None


def _maybe_open_reject_paper_shadow(
    db: Session,
    *,
    uid: int | None,
    alert: BreakoutAlert,
    px: float,
    snap: dict[str, Any] | None,
    reason: str | None,
    existing_qty: float | None = None,
) -> None:
    """Paper-shadow live rejects that are useful for learning, not execution.

    This deliberately does not loosen the live gate. It records a counterfactual
    paper observation for candidate classes the operator needs to study:
    edge-model rejects, duplicate same-pattern live alerts, and cap rejects.
    """
    decision = _qualified_reject_shadow_decision(reason)
    if decision is None or uid is None:
        return
    try:
        px_f = float(px or 0.0)
    except (TypeError, ValueError):
        px_f = 0.0
    if not math.isfinite(px_f) or px_f <= 0.0:
        return

    shadow_snap = dict(snap or {})
    shadow_snap["paper_shadow_reject_reason"] = reason
    qty = 0.0
    if existing_qty is not None:
        try:
            qty = float(existing_qty or 0.0)
            shadow_snap["paper_shadow_qty_source"] = (
                PAPER_SHADOW_REJECT_QTY_SOURCE_EXISTING_LIVE_POSITION
            )
        except (TypeError, ValueError):
            qty = 0.0
    if qty <= 0.0:
        try:
            if bool(
                getattr(
                    settings,
                    "chili_autotrader_paper_shadow_reject_lightweight_sizing_enabled",
                    AUTOTRADER_PAPER_SHADOW_DEFAULT_REJECT_LIGHTWEIGHT_SIZING_ENABLED,
                )
            ):
                notional, notional_snap = (
                    _resolve_shadow_observation_lightweight_notional()
                )
                qty_source = PAPER_SHADOW_REJECT_QTY_SOURCE_LIGHTWEIGHT
            else:
                notional, notional_snap = _resolve_entry_risk_notional(db, uid=uid)
                qty_source = PAPER_SHADOW_REJECT_QTY_SOURCE_RISK_NOTIONAL
            shadow_snap.update({
                f"paper_shadow_{k}": v
                for k, v in (notional_snap or {}).items()
            })
            if notional > 0.0:
                from .tick_normalizer import normalize_quantity

                qty = float(normalize_quantity(float(notional) / px_f, alert.ticker))
                shadow_snap["paper_shadow_qty_source"] = qty_source
        except Exception:
            logger.debug(
                "[autotrader_paper_shadow] reject qty resolve failed "
                "alert_id=%s reason=%s",
                getattr(alert, "id", None), reason, exc_info=True,
            )
            return
    if qty <= 0.0:
        return
    _maybe_open_paper_shadow(
        db,
        uid=uid,
        alert=alert,
        qty=qty,
        px=px_f,
        snap=shadow_snap,
        decision=decision,
        allow_duplicate_open=bool(
            getattr(
                settings,
                "chili_autotrader_paper_shadow_reject_allow_duplicate_open",
                AUTOTRADER_PAPER_SHADOW_DEFAULT_REJECT_ALLOW_DUPLICATE_OPEN,
            )
        ),
    )


def _queue_recert_for_blocked_signal(
    db: Session,
    *,
    alert: BreakoutAlert,
    pattern: ScanPattern | None,
    reason: str,
) -> dict[str, Any] | None:
    """Fast-lane recert work for patterns that are actively emitting signals."""
    if not bool(getattr(settings, "chili_autotrader_recert_signal_fastlane_enabled", True)):
        return None
    try:
        pattern_id = int(getattr(alert, "scan_pattern_id", None) or 0)
    except (TypeError, ValueError):
        pattern_id = 0
    if pattern_id <= 0:
        return None
    try:
        from .recert_queue_service import queue_scheduler

        result = queue_scheduler(
            db,
            scan_pattern_id=pattern_id,
            pattern_name=getattr(pattern, "name", None),
            as_of_date=datetime.utcnow().date(),
            reason=f"autotrader_signal:{reason}",
            severity="red",
            payload={
                "origin": "autotrader_signal_fastlane",
                "alert_id": int(getattr(alert, "id", 0) or 0),
                "ticker": (getattr(alert, "ticker", "") or "").upper(),
                "pattern_recert_reason": getattr(pattern, "recert_reason", None),
                "pattern_lifecycle_stage": getattr(pattern, "lifecycle_stage", None),
            },
            mode_override=getattr(settings, "brain_recert_queue_mode", None),
        )
        if result is None:
            return {"queued": False, "reason": "recert_queue_off"}
        return {
            "queued": True,
            "log_id": result.log_id,
            "recert_id": result.recert_id,
            "status": result.status,
            "mode": result.mode,
        }
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.debug(
            "[autotrader] recert signal fastlane failed alert_id=%s pattern_id=%s",
            getattr(alert, "id", None),
            pattern_id,
            exc_info=True,
        )
        return {"queued": False, "reason": "queue_failed"}


def _csv_tokens(raw: Any) -> frozenset[str]:
    return frozenset(
        token.strip().lower()
        for token in str(raw or "").split(",")
        if token.strip()
    )


def _expected_net_pct_from_snapshot(snap: dict[str, Any] | None) -> float | None:
    if not isinstance(snap, dict):
        return None
    edge = snap.get("entry_edge")
    raw = None
    if isinstance(edge, dict):
        raw = edge.get("expected_net_pct")
    if raw is None:
        raw = snap.get("entry_edge_expected_net_pct")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value:
        return None
    return value


def _entry_edge_from_snapshot(snap: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(snap, dict):
        return {}
    edge = snap.get("entry_edge")
    return edge if isinstance(edge, dict) else snap


def _snapshot_float(snap: dict[str, Any] | None, key: str) -> float | None:
    if not isinstance(snap, dict):
        return None
    edge = _entry_edge_from_snapshot(snap)
    raw = edge.get(key)
    if raw is None:
        raw = snap.get(key)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


def _cost_gate_edge_pct_from_snapshot(
    snap: dict[str, Any] | None,
) -> tuple[float | None, str]:
    """Prefer rule-gate expected net edge for venue cost admission."""
    expected = _snapshot_float(snap, "expected_net_pct")
    if expected is None:
        expected = _snapshot_float(snap, "entry_edge_expected_net_pct")
    if expected is not None:
        return expected, "entry_edge_expected_net_pct"
    return _snapshot_float(snap, "projected_profit_pct"), "projected_profit_pct"


def _queue_exit_geometry_variant_work(
    db: Session,
    *,
    alert: BreakoutAlert,
    pattern: ScanPattern | None,
    reason: str,
    snap: dict[str, Any],
) -> dict[str, Any] | None:
    """Send positive-EV but unexecutable stop geometry into learned exit evolution."""
    if str(reason or "") != EXIT_GEOMETRY_REFRESH_REASON:
        return None
    try:
        pattern_id = int(getattr(alert, "scan_pattern_id", None) or 0)
    except (TypeError, ValueError):
        pattern_id = 0
    if pattern_id <= 0 or pattern is None:
        return None

    expected_net_pct = _expected_net_pct_from_snapshot(snap)
    if expected_net_pct is None or expected_net_pct <= 0.0:
        return {
            "queued": False,
            "reason": "expected_net_not_positive_for_exit_geometry_work",
            "expected_net_pct": expected_net_pct,
        }

    execution_loss = _snapshot_float(snap, "execution_stop_loss_fraction")
    max_execution_loss = _snapshot_float(snap, "max_execution_stop_loss_fraction")
    if (
        execution_loss is None
        or max_execution_loss is None
        or execution_loss <= max_execution_loss
    ):
        return {
            "queued": False,
            "reason": "missing_or_not_wide_execution_geometry",
            "execution_stop_loss_fraction": execution_loss,
            "max_execution_stop_loss_fraction": max_execution_loss,
        }

    edge = _entry_edge_from_snapshot(snap)
    ticker = (getattr(alert, "ticker", "") or "").upper()
    asset_class = (getattr(alert, "asset_type", "") or "").strip().lower() or None
    fingerprint_body = {
        "reason": EXIT_GEOMETRY_REFRESH_REASON,
        "scan_pattern_id": pattern_id,
        "ticker": ticker,
        "asset_class": asset_class,
        "entry_price": edge.get("entry_price") or snap.get("entry_price"),
        "stop_price": edge.get("stop_price") or snap.get("stop_price"),
        "target_price": edge.get("target_price") or snap.get("target_price"),
        "execution_stop_loss_fraction": execution_loss,
        "max_execution_stop_loss_fraction": max_execution_loss,
        "execution_stop_loss_source": edge.get("execution_stop_loss_source")
        or snap.get("execution_stop_loss_source"),
        "expected_net_pct": expected_net_pct,
    }
    evidence_fingerprint = hashlib.sha256(
        json.dumps(fingerprint_body, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    stop_excess_fraction = max(0.0, execution_loss - max_execution_loss)
    expected_evidence_value = max(0.0, expected_net_pct) + (
        stop_excess_fraction * PERCENT_SCALE
    )
    payload = {
        "alert_id": int(getattr(alert, "id", 0) or 0),
        "ticker": ticker,
        "reason": EXIT_GEOMETRY_REFRESH_REASON,
        "source": EXIT_GEOMETRY_REFRESH_SOURCE,
        "lifecycle_stage": getattr(pattern, "lifecycle_stage", None),
        "promotion_status": getattr(pattern, "promotion_status", None),
        "entry_price": fingerprint_body["entry_price"],
        "stop_price": fingerprint_body["stop_price"],
        "target_price": fingerprint_body["target_price"],
        "expected_net_pct": expected_net_pct,
        "probability": edge.get("probability"),
        "probability_source": edge.get("probability_source"),
        "probability_sample_n": edge.get("probability_sample_n"),
        "reward_fraction": edge.get("reward_fraction"),
        "stop_loss_fraction": edge.get("stop_loss_fraction"),
        "static_reward_fraction": edge.get("target_reward_fraction"),
        "static_stop_loss_fraction": edge.get("hard_stop_loss_fraction"),
        "execution_stop_loss_fraction": execution_loss,
        "max_execution_stop_loss_fraction": max_execution_loss,
        "stop_excess_fraction": round(stop_excess_fraction, 8),
        "execution_stop_loss_source": fingerprint_body["execution_stop_loss_source"],
        "cash_deployment_category": "positive_ev_execution_blocked",
        "graduation_blocker": EXIT_GEOMETRY_REFRESH_REASON,
        "recommended_work_event": "exit_variant_refresh",
        "expected_evidence_value": round(expected_evidence_value, 6),
    }
    try:
        from .edge_reliability import (
            EXIT_VARIANT_REFRESH,
            emit_targeted_profitability_work,
        )

        event_id = emit_targeted_profitability_work(
            db,
            event_type=EXIT_VARIANT_REFRESH,
            scan_pattern_id=pattern_id,
            source=EXIT_GEOMETRY_REFRESH_SOURCE,
            asset_class=asset_class,
            evidence_fingerprint=evidence_fingerprint,
            payload=payload,
        )
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.debug(
            "[autotrader] exit geometry variant work enqueue failed alert_id=%s pattern_id=%s",
            getattr(alert, "id", None),
            pattern_id,
            exc_info=True,
        )
        return {"queued": False, "reason": "queue_failed"}

    return {
        "queued": event_id is not None,
        "event_id": event_id,
        "pattern_id": pattern_id,
        "expected_net_pct": expected_net_pct,
        "execution_stop_loss_fraction": execution_loss,
        "max_execution_stop_loss_fraction": max_execution_loss,
        "evidence_fingerprint": evidence_fingerprint,
    }


def _queue_shadow_stock_fastlane_for_observation(
    db: Session,
    *,
    alert: BreakoutAlert,
    pattern: ScanPattern | None,
    reason: str,
    snap: dict[str, Any],
) -> dict[str, Any] | None:
    """Boost live-quality stock observations toward fresh graduation evidence.

    This does not place a broker order and does not change lifecycle rules. It
    only makes the backtest queue notice stock shadow observations that already
    passed the normal positive-edge gate, so their patterns can earn or fail
    promotion evidence sooner.
    """
    if not bool(
        getattr(
            settings,
            "chili_autotrader_shadow_stock_fastlane_enabled",
            AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_ENABLED,
        )
    ):
        return None
    if (getattr(alert, "asset_type", "") or "").strip().lower() != "stock":
        return None
    ticker = str(getattr(alert, "ticker", "") or "").strip().upper()
    try:
        pattern_id = int(getattr(alert, "scan_pattern_id", None) or 0)
    except (TypeError, ValueError):
        pattern_id = 0
    if pattern_id <= 0 or pattern is None:
        return None
    lifecycle = (getattr(pattern, "lifecycle_stage", "") or "").strip().lower()
    allowed_lifecycles = _csv_tokens(
        getattr(
            settings,
            "chili_autotrader_shadow_stock_fastlane_lifecycle_stages",
            AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_LIFECYCLE_STAGES,
        )
    )
    if allowed_lifecycles and lifecycle not in allowed_lifecycles:
        return None

    expected_net_pct = _expected_net_pct_from_snapshot(snap)
    min_expected_net_pct = float(
        getattr(
            settings,
            "chili_autotrader_shadow_stock_fastlane_min_expected_net_pct",
            AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_MIN_EXPECTED_NET_PCT,
        )
        or 0.0
    )
    if expected_net_pct is None or expected_net_pct <= min_expected_net_pct:
        return {
            "queued": False,
            "reason": "expected_net_below_fastlane_floor",
            "expected_net_pct": expected_net_pct,
            "min_expected_net_pct": min_expected_net_pct,
            "lifecycle_stage": lifecycle,
        }

    reboost_cooldown_minutes = max(
        0.0,
        float(
            getattr(
                settings,
                "chili_autotrader_shadow_stock_fastlane_reboost_cooldown_minutes",
                AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_REBOOST_COOLDOWN_MINUTES,
            )
            or 0.0
        ),
    )
    last_backtest_at = getattr(pattern, "last_backtest_at", None)
    if reboost_cooldown_minutes > 0.0 and isinstance(last_backtest_at, datetime):
        last_bt = last_backtest_at
        if last_bt.tzinfo is not None:
            last_bt = last_bt.astimezone(timezone.utc).replace(tzinfo=None)
        cooldown_until = last_bt + timedelta(minutes=reboost_cooldown_minutes)
        now_utc = datetime.utcnow()
        if now_utc < cooldown_until:
            return {
                "queued": False,
                "reason": "recent_backtest_cooldown",
                "pattern_id": pattern_id,
                "expected_net_pct": expected_net_pct,
                "min_expected_net_pct": min_expected_net_pct,
                "lifecycle_stage": lifecycle,
                "last_backtest_at": last_bt.isoformat(),
                "cooldown_until": cooldown_until.isoformat(),
                "reboost_cooldown_minutes": reboost_cooldown_minutes,
            }

    priority = max(
        1,
        int(
            getattr(
                settings,
                "chili_autotrader_shadow_stock_fastlane_backtest_priority",
                AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_BACKTEST_PRIORITY,
            )
            or AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_BACKTEST_PRIORITY
        ),
    )
    previous_priority = int(getattr(pattern, "backtest_priority", 0) or 0)

    def _emit_fastlane_work() -> int | None:
        from .backtest_queue import invalidate_queue_status_cache
        from .brain_work.emitters import emit_backtest_requested_for_pattern

        event_id = emit_backtest_requested_for_pattern(
            db,
            pattern_id,
            source="autotrader_shadow_stock_fastlane",
            asset_class="stock",
            expected_evidence_value=expected_net_pct,
            payload={
                "alert_id": int(getattr(alert, "id", 0) or 0),
                "ticker": ticker,
                "reason": reason,
                "lifecycle_stage": lifecycle,
                "promotion_status": getattr(pattern, "promotion_status", None),
                "expected_net_pct": round(float(expected_net_pct), 6),
                "cash_deployment_category": "positive_ev_shadow",
                "graduation_blocker": "shadow_observation_signal_lane",
                "recommended_work_event": "backtest_requested",
            },
        )
        invalidate_queue_status_cache()
        return event_id

    if previous_priority >= priority:
        try:
            event_id = _emit_fastlane_work()
        except Exception:
            logger.debug(
                "[autotrader] shadow stock fastlane refresh failed alert_id=%s "
                "pattern_id=%s",
                getattr(alert, "id", None),
                pattern_id,
                exc_info=True,
            )
            event_id = None
        return {
            "queued": event_id is not None,
            "reason": (
                "already_boosted_evidence_refreshed"
                if event_id is not None
                else "already_boosted"
            ),
            "pattern_id": pattern_id,
            "priority": previous_priority,
            "target_priority": priority,
            "expected_net_pct": expected_net_pct,
            "lifecycle_stage": lifecycle,
            "work_event_id": event_id,
        }

    pattern.backtest_priority = priority
    event_id: int | None = None
    try:
        event_id = _emit_fastlane_work()
    except Exception:
        logger.debug(
            "[autotrader] shadow stock fastlane emit failed alert_id=%s pattern_id=%s",
            getattr(alert, "id", None),
            pattern_id,
            exc_info=True,
        )
    db.flush()
    return {
        "queued": True,
        "work_event_id": event_id,
        "pattern_id": pattern_id,
        "priority": priority,
        "previous_priority": previous_priority,
        "expected_net_pct": expected_net_pct,
        "lifecycle_stage": lifecycle,
        "reason": reason,
    }


def _audit(
    db: Session,
    *,
    user_id: Optional[int],
    alert: BreakoutAlert,
    decision: str,
    reason: str,
    rule_snapshot: dict[str, Any] | None = None,
    llm_snapshot: dict[str, Any] | None = None,
    trade_id: Optional[int] = None,
) -> None:
    row = AutoTraderRun(
        user_id=user_id,
        breakout_alert_id=alert.id,
        scan_pattern_id=alert.scan_pattern_id,
        ticker=(alert.ticker or "").upper(),
        decision=decision,
        reason=reason[:2000] if reason else None,
        rule_snapshot=rule_snapshot,
        llm_snapshot=llm_snapshot,
        management_scope=MANAGEMENT_SCOPE_AUTO_TRADER_V1,
        trade_id=trade_id,
    )
    db.add(row)
    db.commit()

    # Q1.T3 phase 2 — shadow-consume unified_signals.
    # No-op when chili_unified_signal_consumer_enabled is False (default).
    # When True, looks up the matching unified_signals row and logs any
    # parity discrepancies. Does NOT change the decision in any way.
    try:
        from .contracts.signal_consumer import maybe_shadow_consume
        _entry_price = None
        try:
            _entry_price = float(alert.entry_price) if alert.entry_price is not None else None
        except (TypeError, ValueError, AttributeError):
            _entry_price = None
        maybe_shadow_consume(
            db,
            alert_id=int(alert.id),
            alert_ticker=(alert.ticker or "").upper(),
            alert_entry_price=_entry_price,
            decision=decision,
            decision_reason=reason,
        )
    except Exception:  # pragma: no cover - never raise from audit hook
        logger.debug("[autotrader] shadow_consume failed; ignored", exc_info=True)


def _classify_autotrader_exception(exc: BaseException) -> str:
    msg = str(exc or "")
    if "Query.filter()" in msg and "LIMIT or OFFSET" in msg:
        return "query_filter_after_limit"
    if "timeout" in msg.lower():
        return "timeout"
    if isinstance(exc, OSError):
        return "transport"
    return "unclassified"


def _exception_audit_snapshot(
    alert: BreakoutAlert,
    exc: BaseException,
    *,
    phase: str,
) -> dict[str, Any]:
    frames: list[dict[str, Any]] = []
    try:
        extracted = traceback.extract_tb(exc.__traceback__)
    except Exception:
        extracted = []
    for frame in extracted[-8:]:
        filename = str(frame.filename or "").replace("\\", "/")
        app_idx = filename.find("/app/")
        if app_idx >= 0:
            filename = filename[app_idx + 1:]
        frames.append(
            {
                "file": filename,
                "line": int(frame.lineno or 0),
                "function": str(frame.name or ""),
            }
        )
    try:
        exc_only = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    except Exception:
        exc_only = str(exc or "")
    return {
        "autotrader_exception": True,
        "error_phase": phase,
        "error_type": type(exc).__name__,
        "error_classification": _classify_autotrader_exception(exc),
        "error_message": str(exc or "")[:500],
        "error_summary": exc_only[:500],
        "error_frames": frames,
        "alert_id": getattr(alert, "id", None),
        "alert_ticker": (getattr(alert, "ticker", None) or "").upper() or None,
        "alert_asset_type": getattr(alert, "asset_type", None),
        "alert_scan_pattern_id": getattr(alert, "scan_pattern_id", None),
    }


def _broker_reject_action_fingerprint(
    alert: BreakoutAlert,
    *,
    venue: str,
    side: str,
    qty: float,
    snap: dict[str, Any] | None,
    order_hint: str | None = None,
) -> str:
    """Stable key for a broker submission shape, excluding transient prices."""
    meta = {}
    if isinstance(snap, dict):
        opt = snap.get("option_meta")
        if isinstance(opt, dict):
            meta = {
                "expiration": opt.get("expiration"),
                "strike": opt.get("strike"),
                "option_type": opt.get("option_type"),
                "legs": opt.get("legs") if isinstance(opt.get("legs"), list) else None,
            }
    payload = {
        "venue": str(venue or "unknown").strip().lower(),
        "side": str(side or "buy").strip().lower(),
        "ticker": str(getattr(alert, "ticker", "") or "").upper(),
        "asset_type": str(getattr(alert, "asset_type", "") or "").lower(),
        "scan_pattern_id": getattr(alert, "scan_pattern_id", None),
        "qty": round(float(qty or 0.0), QUANTITY_ROUND_DIGITS),
        "order_hint": str(order_hint or "market").strip().lower(),
        "options_path": (
            bool((snap or {}).get("options_path")) if isinstance(snap, dict) else False
        ),
        "option_meta": meta,
    }
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def _broker_reject_suppression(
    db: Session | None,
    alert: BreakoutAlert,
    fingerprint: str,
) -> dict[str, Any] | None:
    if db is None:
        return None
    if not bool(
        getattr(
            settings,
            "chili_autotrader_broker_reject_suppression_enabled",
            AUTOTRADER_BROKER_REJECT_SUPPRESSION_DEFAULT_ENABLED,
        )
    ):
        return None
    minutes = int(
        getattr(
            settings,
            "chili_autotrader_broker_reject_suppression_minutes",
            AUTOTRADER_BROKER_REJECT_SUPPRESSION_DEFAULT_MINUTES,
        )
        or 0
    )
    if minutes <= 0:
        return None
    threshold = max(
        1,
        int(
            getattr(
                settings,
                "chili_autotrader_broker_reject_suppression_threshold",
                AUTOTRADER_BROKER_REJECT_SUPPRESSION_DEFAULT_THRESHOLD,
            )
            or AUTOTRADER_BROKER_REJECT_SUPPRESSION_DEFAULT_THRESHOLD
        ),
    )
    cutoff = datetime.utcnow() - timedelta(minutes=minutes)
    ticker = str(getattr(alert, "ticker", "") or "").upper()
    q = (
        db.query(AutoTraderRun)
        .filter(AutoTraderRun.ticker == ticker)
        .filter(AutoTraderRun.created_at >= cutoff)
        .filter(AutoTraderRun.reason.like("broker:%"))
    )
    if getattr(alert, "scan_pattern_id", None) is not None:
        q = q.filter(AutoTraderRun.scan_pattern_id == alert.scan_pattern_id)
    q = q.order_by(AutoTraderRun.created_at.desc()).limit(max(10, threshold * 5))
    matches = []
    for row in q.all():
        row_snap = row.rule_snapshot if isinstance(row.rule_snapshot, dict) else {}
        if str(row_snap.get("broker_reject_fingerprint") or "") == fingerprint:
            matches.append(row)
            if len(matches) >= threshold:
                break
    if len(matches) < threshold:
        return None
    last = matches[0]
    last_reason = str(last.reason or "broker:unknown")
    return {
        "fingerprint": fingerprint,
        "recent_reject_count": len(matches),
        "window_minutes": minutes,
        "last_reject_run_id": int(last.id),
        "last_reject_reason": last_reason,
    }


def _annotate_broker_reject(
    snap: dict[str, Any],
    *,
    fingerprint: str,
    venue: str,
    error: Any,
) -> None:
    snap["broker_reject_fingerprint"] = fingerprint
    snap["broker_reject_venue"] = str(venue or "unknown").strip().lower()
    snap["broker_reject_error"] = str(error or "unknown")[:500]


def _broker_response_reject_venue_and_hint(
    res: dict[str, Any],
    snap: dict[str, Any],
) -> tuple[str, str]:
    """Recover the broker boundary shape for post-call failures."""
    if bool(res.get("_chili_options_path")) or bool(snap.get("options_path")):
        meta = snap.get("option_meta") if isinstance(snap.get("option_meta"), dict) else {}
        legs = meta.get("legs") if isinstance(meta, dict) else None
        return "robinhood_options", "option_spread" if isinstance(legs, list) and len(legs) > 1 else "option_limit"
    source = str(res.get("_chili_broker_source") or "").strip().lower()
    if source == "coinbase":
        return "coinbase", "limit_post_only" if bool(res.get("_chili_maker_only")) else "market"
    return "robinhood", "market"


def _annotate_missing_order_id_broker_reject(
    alert: BreakoutAlert,
    *,
    qty: float,
    snap: dict[str, Any],
    res: dict[str, Any],
) -> str:
    venue, order_hint = _broker_response_reject_venue_and_hint(res, snap)
    try:
        reject_qty = float(res.get("base_size") or qty or 0.0)
    except (TypeError, ValueError):
        reject_qty = float(qty or 0.0)
    fingerprint = _broker_reject_action_fingerprint(
        alert,
        venue=venue,
        side="buy",
        qty=reject_qty,
        snap=snap,
        order_hint=order_hint,
    )
    _annotate_broker_reject(
        snap,
        fingerprint=fingerprint,
        venue=venue,
        error="place_no_order_id",
    )
    snap["broker_reject_missing_order_id"] = True
    snap["broker_reject_order_hint"] = order_hint
    client_order_id = str(res.get("client_order_id") or "").strip()
    if client_order_id:
        snap["broker_reject_client_order_id"] = client_order_id[:128]
    return fingerprint


def _maybe_block_repeated_broker_reject(
    db: Session | None,
    *,
    uid: int,
    alert: BreakoutAlert,
    fingerprint: str,
    snap: dict[str, Any],
    llm_snap: dict[str, Any] | None,
    out: dict[str, Any],
) -> bool:
    suppression = _broker_reject_suppression(db, alert, fingerprint)
    if suppression is None:
        return False
    snap["broker_reject_suppression"] = suppression
    reason_tail = str(suppression.get("last_reject_reason") or "broker:unknown")
    if reason_tail.startswith("broker:"):
        reason_tail = reason_tail[len("broker:"):]
    _block_live_order(
        db,
        uid=uid,
        alert=alert,
        reason=f"broker_reject_suppressed:{reason_tail}"[:255],
        snap=snap,
        llm_snap=llm_snap,
        out=out,
    )
    return True


def _block_live_order(
    db: Session,
    *,
    uid: int,
    alert: BreakoutAlert,
    reason: str,
    snap: dict[str, Any] | None,
    llm_snap: dict[str, Any] | None,
    out: dict[str, Any],
) -> None:
    rsn = (reason or "blocked")[:255]
    _audit(
        db,
        user_id=uid,
        alert=alert,
        decision="blocked",
        reason=rsn,
        rule_snapshot=snap,
        llm_snapshot=llm_snap,
    )
    out["skipped"] += 1
    _autotrader_tick_note(out, kind="blocked", reason=rsn, alert=alert)


def _block_live_order_with_paper_shadow(
    db: Session,
    *,
    uid: int,
    alert: BreakoutAlert,
    reason: str,
    snap: dict[str, Any] | None,
    llm_snap: dict[str, Any] | None,
    out: dict[str, Any],
    qty: float,
    px: float | None,
    shadow_decision: str,
) -> None:
    shadow_snap = dict(snap or {})
    shadow_snap["paper_shadow_reject_reason"] = reason
    _block_live_order(
        db,
        uid=uid,
        alert=alert,
        reason=reason,
        snap=shadow_snap,
        llm_snap=llm_snap,
        out=out,
    )
    try:
        shadow_qty = float(qty)
        shadow_px = float(px)
    except (TypeError, ValueError):
        return
    if shadow_qty <= 0.0 or shadow_px <= 0.0:
        return
    _maybe_open_paper_shadow(
        db,
        uid=uid,
        alert=alert,
        qty=shadow_qty,
        px=shadow_px,
        snap=shadow_snap,
        decision=shadow_decision,
    )


def _record_option_entry_no_fill_with_shadow(
    db: Session,
    *,
    uid: int,
    alert: BreakoutAlert,
    snap: dict[str, Any],
    llm_snap: dict[str, Any] | None,
    out: dict[str, Any],
    qty: float,
    px: float,
    entry_broker_status: str | None,
) -> None:
    terminal_state = entry_broker_status or "terminal"
    snap["option_entry_terminal_state"] = terminal_state
    _audit(
        db,
        user_id=uid,
        alert=alert,
        decision="blocked",
        reason=f"broker:option_entry_no_fill:{terminal_state}",
        rule_snapshot=snap,
        llm_snapshot=llm_snap,
    )
    out["skipped"] += 1
    _autotrader_tick_note(
        out,
        kind="blocked",
        reason="broker:option_entry_no_fill",
        alert=alert,
    )
    _maybe_open_paper_shadow(
        db,
        uid=uid,
        alert=alert,
        qty=qty,
        px=px,
        snap=snap,
        decision="blocked_option_entry_no_fill",
        allow_duplicate_open=True,
    )


def _live_venue_health_block_reason(db: Session, *, venue: str) -> str | None:
    venue_key = (venue or "").strip().lower() or "unknown"
    require_healthy = bool(
        getattr(settings, "chili_autotrader_live_require_venue_health_enabled", True)
    )
    try:
        from .venue.venue_health import summarize_venue
    except Exception as exc:
        logger.warning(
            "[autotrader] venue_health imports unavailable for venue=%s; failing closed",
            venue_key,
            exc_info=True,
        )
        return f"venue_health_unavailable:{venue_key}:{type(exc).__name__}"

    try:
        summary = summarize_venue(db, venue=venue_key)
    except Exception as exc:
        logger.warning(
            "[autotrader] venue_health summary failed for venue=%s; failing closed",
            venue_key,
            exc_info=True,
        )
        return f"venue_health_unavailable:{venue_key}:{type(exc).__name__}"

    status = str((summary or {}).get("status") or "").strip().lower()
    if status == "degraded":
        reason_detail = (summary or {}).get("reason") or "unknown"
        return f"venue_degraded:{venue_key}:{reason_detail}"
    if status == "disabled" and require_healthy:
        return f"venue_health_required_disabled:{venue_key}"
    if require_healthy and status != "healthy":
        reason_detail = (summary or {}).get("reason") or "unknown"
        if status == "insufficient_data":
            return f"venue_health_insufficient_data:{venue_key}:{reason_detail}"
        return f"venue_health_not_healthy:{venue_key}:{status or 'unknown'}"
    return None


def _last_scalar_feature_value(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        for item in reversed(value):
            scalar = _last_scalar_feature_value(item)
            if scalar is not None:
                return scalar
        return None
    if isinstance(value, dict):
        return None
    return value


def _collect_feature_values(src: Any, *, allowed: set[str], out: dict[str, Any]) -> None:
    if not isinstance(src, dict):
        return
    for key, value in src.items():
        key_s = str(key)
        if key_s in allowed and key_s not in out:
            scalar = _last_scalar_feature_value(value)
            if scalar is not None:
                out[key_s] = scalar
        if isinstance(value, dict):
            _collect_feature_values(value, allowed=allowed, out=out)


def _feature_parity_decision_snapshot(
    alert: BreakoutAlert,
    rule_snapshot: dict[str, Any] | None,
    *,
    feature_keys: set[str],
) -> dict[str, Any]:
    """Extract the feature vector that actually drove the entry decision."""
    out: dict[str, Any] = {}
    _collect_feature_values(alert.indicator_snapshot, allowed=feature_keys, out=out)
    _collect_feature_values(alert.signals_snapshot, allowed=feature_keys, out=out)
    _collect_feature_values(rule_snapshot, allowed=feature_keys, out=out)
    if "price" in feature_keys and "price" not in out:
        for candidate in (
            (rule_snapshot or {}).get("current_price") if isinstance(rule_snapshot, dict) else None,
            getattr(alert, "price_at_alert", None),
            getattr(alert, "entry_price", None),
        ):
            scalar = _last_scalar_feature_value(candidate)
            if scalar is not None:
                out["price"] = scalar
                break
    return out


def _pattern_name(db: Session, scan_pattern_id: Optional[int]) -> str | None:
    if not scan_pattern_id:
        return None
    p = db.query(ScanPattern).filter(ScanPattern.id == int(scan_pattern_id)).first()
    return p.name if p else None


def _pattern_row(db: Session, scan_pattern_id: Optional[int]) -> ScanPattern | None:
    if not scan_pattern_id:
        return None
    return db.query(ScanPattern).filter(ScanPattern.id == int(scan_pattern_id)).first()


# ── Market-data fetches (Phase B) ────────────────────────────────────
#
# These two helpers are on the auto-trader hot path: a bad quote here sizes
# a live order. Previously both sites swallowed every exception silently,
# meaning a transient timeout from the quote provider returned None and
# the gate logic blocked the alert as ``no_quote`` — indistinguishable
# from the ticker simply not quoting. That blinded ops to provider
# outages and made every missed entry a noop investigation.
#
# Phase B behavior:
#   - Up to 3 attempts per call, exponential backoff (0.5s, 1.0s).
#   - Timeouts (asyncio/sync) logged as ``kind=timeout`` → retry.
#   - Transport / network errors logged as ``kind=transport`` → retry.
#   - Empty results logged as ``kind=empty`` → retry.
#   - Unexpected exceptions logged as ``kind=upstream`` with exc_info → retry.
#   - Exhausted attempts log a final ``kind=exhausted`` line at WARNING.
#   - Every outcome is prefixed ``[chili_market_data]`` so the phase-C
#     observability registry can index it.
#
# Contract unchanged: both still return None on failure — no exception
# escapes to callers. The difference is visibility. The kill switch +
# drawdown breaker still gate the downstream execution regardless of
# what we return here.

_MARKET_DATA_MAX_ATTEMPTS = 3
_MARKET_DATA_BACKOFF_BASE_SEC = 0.5


def _classify_market_data_exc(exc: BaseException) -> str:
    """Map a raised exception to a short log-kind token.

    The upstream ``fetch_*`` layer does not raise a structured taxonomy
    today — this mapping gives the ops log a consistent vocabulary
    without requiring a contract change to ``market_data.py``.
    """
    if isinstance(exc, TimeoutError):
        return "timeout"
    # OSError covers ConnectionError / socket timeouts / refused / reset
    if isinstance(exc, OSError):
        return "transport"
    return "upstream"


def _ohlcv_summary(ticker: str) -> str | None:
    """Fetch a short OHLCV summary for LLM revalidation; retry + structured log."""
    from .market_data import fetch_ohlcv_df

    last_kind = "unknown"
    last_err: str | None = None
    for attempt in range(1, _MARKET_DATA_MAX_ATTEMPTS + 1):
        try:
            df = fetch_ohlcv_df(ticker, "5m", period="5d")
            if df is None or df.empty:
                last_kind = "empty"
                last_err = None
                logger.info(
                    f"{CHILI_MARKET_DATA} source=ohlcv kind=empty ticker=%s attempt=%d",
                    ticker, attempt,
                )
            else:
                logger.debug(
                    f"{CHILI_MARKET_DATA} source=ohlcv kind=ok ticker=%s attempt=%d rows=%d",
                    ticker, attempt, len(df),
                )
                tail = df.tail(15)
                if "Close" in tail.columns:
                    return tail[["Close"]].to_string(max_rows=20)[:3500]
                return tail.to_string(max_rows=10)[:3500]
        except Exception as e:  # noqa: BLE001 — classified below, re-logged
            last_kind = _classify_market_data_exc(e)
            last_err = repr(e)
            logger.warning(
                f"{CHILI_MARKET_DATA} source=ohlcv kind=%s ticker=%s attempt=%d err=%s",
                last_kind, ticker, attempt, last_err,
                exc_info=(last_kind == "upstream"),
            )
        if attempt < _MARKET_DATA_MAX_ATTEMPTS:
            time.sleep(_MARKET_DATA_BACKOFF_BASE_SEC * (2 ** (attempt - 1)))
    logger.warning(
        f"{CHILI_MARKET_DATA} source=ohlcv kind=exhausted ticker=%s attempts=%d last_kind=%s last_err=%s",
        ticker, _MARKET_DATA_MAX_ATTEMPTS, last_kind, last_err,
    )
    return None


def _current_price_with_source(ticker: str) -> tuple[float | None, str]:
    """Fetch the current price plus provider source for entry auditability."""
    from .market_data import fetch_quote

    last_kind = "unknown"
    last_err: str | None = None
    for attempt in range(1, _MARKET_DATA_MAX_ATTEMPTS + 1):
        try:
            q = fetch_quote(ticker)
            if not q:
                last_kind = "empty"
                last_err = None
                logger.info(
                    f"{CHILI_MARKET_DATA} source=quote kind=empty ticker=%s attempt=%d",
                    ticker, attempt,
                )
            else:
                raw_p = q.get("price") or q.get("last_price")
                try:
                    price = float(raw_p) if raw_p is not None else None
                except (TypeError, ValueError):
                    price = None
                if price is None:
                    last_kind = "empty"
                    last_err = f"unparseable price={raw_p!r}"
                    logger.info(
                        f"{CHILI_MARKET_DATA} source=quote kind=empty ticker=%s attempt=%d note=unparseable",
                        ticker, attempt,
                    )
                else:
                    logger.debug(
                        f"{CHILI_MARKET_DATA} source=quote kind=ok ticker=%s attempt=%d price=%s",
                        ticker, attempt, price,
                    )
                    return price, _quote_source_from_snapshot(q, "market_data")
        except Exception as e:  # noqa: BLE001 — classified below, re-logged
            last_kind = _classify_market_data_exc(e)
            last_err = repr(e)
            logger.warning(
                f"{CHILI_MARKET_DATA} source=quote kind=%s ticker=%s attempt=%d err=%s",
                last_kind, ticker, attempt, last_err,
                exc_info=(last_kind == "upstream"),
            )
        if attempt < _MARKET_DATA_MAX_ATTEMPTS:
            time.sleep(_MARKET_DATA_BACKOFF_BASE_SEC * (2 ** (attempt - 1)))
    logger.warning(
        f"{CHILI_MARKET_DATA} source=quote kind=exhausted ticker=%s attempts=%d last_kind=%s last_err=%s",
        ticker, _MARKET_DATA_MAX_ATTEMPTS, last_kind, last_err,
    )
    return None, "single_fetch"


def _current_price(ticker: str) -> float | None:
    """Fetch the current price for gate sizing; retry + structured log."""
    price, _source = _current_price_with_source(ticker)
    key = str(ticker or "").strip().upper()
    if key:
        _LAST_CURRENT_PRICE_SOURCE_BY_TICKER[key] = _source or "single_fetch"
    return price


def run_auto_trader_tick(db: Session) -> dict[str, Any]:
    """Process a small batch of unprocessed pattern-imminent BreakoutAlerts."""
    tick_started = time.monotonic()
    if not getattr(settings, "chili_autotrader_enabled", False):
        return {"ok": True, "skipped": True, "reason": "disabled"}

    from .governance import is_kill_switch_active_for_session

    if is_kill_switch_active_for_session(db):
        return {"ok": True, "skipped": True, "reason": "kill_switch"}

    rt = effective_autotrader_runtime(db)
    if not rt.get("tick_allowed"):
        return {"ok": True, "skipped": True, "reason": "paused_or_disabled", "runtime": rt}

    runtime_gate_elapsed_s = round(time.monotonic() - tick_started, 3)
    cleanup_started = time.monotonic()
    # AAA -- janitor pass: kill any leaked autotrader advisory-lock holders
    # from previous abandoned ticks. Cheap, idempotent. Default threshold
    # 120s -- well past the 45s tick budget so legitimate slow ticks never
    # get killed by us.
    _cleanup_leaked_advisory_locks(db)
    cleanup_elapsed_s = round(time.monotonic() - cleanup_started, 3)

    uid = _resolve_user_id()
    if uid is None:
        logger.debug("[autotrader] No user id (chili_autotrader_user_id / brain_default_user_id)")
        return {"ok": False, "error": "no_user_id"}

    candidate_select_started = time.monotonic()
    # Match alerts scoped to this autotrader user AND system-generated
    # (``user_id IS NULL``) pattern-imminent alerts. The imminent generator
    # writes alerts without a specific owner; treating them as processable by
    # the configured autotrader user is the intended behavior (single-tenant
    # deployment). Use ``OR`` so explicit-user alerts are still honored.
    processed_alert_exists = (
        db.query(AutoTraderRun.id)
        .filter(AutoTraderRun.breakout_alert_id == BreakoutAlert.id)
        .exists()
    )
    stock_defer = _stock_session_defer_state()
    stock_asset_filter = BreakoutAlert.asset_type == STOCK_ASSET_TYPE
    non_stock_asset_filter = or_(
        BreakoutAlert.asset_type.is_(None),
        BreakoutAlert.asset_type != STOCK_ASSET_TYPE,
    )
    stock_actionability = _stock_candidate_actionability_state()
    non_stock_actionability = _non_stock_candidate_actionability_state()
    candidate_base = (
        db.query(BreakoutAlert)
        .filter(
            BreakoutAlert.alert_tier == "pattern_imminent",
            or_(BreakoutAlert.user_id == uid, BreakoutAlert.user_id.is_(None)),
            ~processed_alert_exists,
        )
    )
    stock_actionability_cutoff = stock_actionability.get("cutoff")
    non_stock_actionability_cutoff = non_stock_actionability.get("cutoff")
    actionability_filters = []
    if bool(stock_actionability.get("enabled")) and isinstance(
        stock_actionability_cutoff,
        datetime,
    ):
        actionability_filters.append(
            and_(
                stock_asset_filter,
                BreakoutAlert.alerted_at >= stock_actionability_cutoff,
            )
        )
    else:
        actionability_filters.append(stock_asset_filter)
    if bool(non_stock_actionability.get("enabled")) and isinstance(
        non_stock_actionability_cutoff,
        datetime,
    ):
        actionability_filters.append(
            and_(
                non_stock_asset_filter,
                BreakoutAlert.alerted_at >= non_stock_actionability_cutoff,
            )
        )
    else:
        actionability_filters.append(non_stock_asset_filter)
    candidate_base = candidate_base.filter(or_(*actionability_filters))
    if stock_defer.get("enabled"):
        candidate_base = candidate_base.filter(
            or_(
                non_stock_asset_filter,
                BreakoutAlert.alerted_at >= stock_defer["cutoff"],
            )
        )
        if stock_defer.get("active"):
            candidate_base = candidate_base.filter(non_stock_asset_filter)
    batch_limit = _autotrader_candidate_batch_size()
    tick_budget_s = _autotrader_tick_soft_budget_seconds()
    fresh_fastlane = _fresh_candidate_fastlane_state()
    candidate_query_limit, query_limit_meta = _fresh_candidate_burst_batch_size(
        base_limit=batch_limit,
        fresh_fastlane_state=fresh_fastlane,
        fresh_candidate_count=AUTOTRADER_MAX_CANDIDATE_BATCH_SIZE,
    )
    stock_deferred_pool = 0
    stock_stale_unprocessed = 0
    fresh_cutoff = fresh_fastlane.get("cutoff")
    stale_candidate_sweep_checked = True
    stale_candidate_sweep_interval_s = _stale_candidate_sweep_interval_seconds()
    if bool(fresh_fastlane.get("enabled")) and isinstance(fresh_cutoff, datetime):
        fresh_candidate_refs = (
            candidate_base.filter(BreakoutAlert.alerted_at >= fresh_cutoff)
            .order_by(BreakoutAlert.alerted_at.desc(), BreakoutAlert.id.desc())
            .with_entities(BreakoutAlert.id, BreakoutAlert.alerted_at)
            .limit(candidate_query_limit)
            .all()
        )
        remaining_candidate_slots = max(
            0,
            int(candidate_query_limit) - len(fresh_candidate_refs),
        )
        if remaining_candidate_slots > 0:
            (
                stale_candidate_sweep_checked,
                stale_candidate_sweep_interval_s,
            ) = _should_probe_stale_candidates()
        if remaining_candidate_slots > 0 and stale_candidate_sweep_checked:
            older_candidate_refs = (
                candidate_base.filter(
                    or_(
                        BreakoutAlert.alerted_at < fresh_cutoff,
                        BreakoutAlert.alerted_at.is_(None),
                    )
                )
                .order_by(BreakoutAlert.alerted_at.desc(), BreakoutAlert.id.desc())
                .with_entities(BreakoutAlert.id, BreakoutAlert.alerted_at)
                .limit(remaining_candidate_slots)
                .all()
            )
        else:
            older_candidate_refs = []
        candidate_refs = [*fresh_candidate_refs, *older_candidate_refs]
        fresh_candidate_pool = len(fresh_candidate_refs)
        fresh_candidate_pool_exact = len(fresh_candidate_refs) < candidate_query_limit
    else:
        candidate_refs = (
            candidate_base.order_by(BreakoutAlert.id.asc())
            .with_entities(BreakoutAlert.id, BreakoutAlert.alerted_at)
            .limit(candidate_query_limit)
            .all()
        )
        fresh_candidate_pool = len(candidate_refs)
        fresh_candidate_pool_exact = len(candidate_refs) < candidate_query_limit
    stock_session_defer_counts_checked = bool(
        stock_defer.get("enabled") and stock_defer.get("active")
    )
    if stock_session_defer_counts_checked:
        stock_unprocessed_base = (
            db.query(BreakoutAlert)
            .filter(
                BreakoutAlert.alert_tier == "pattern_imminent",
                or_(BreakoutAlert.user_id == uid, BreakoutAlert.user_id.is_(None)),
                ~processed_alert_exists,
                stock_asset_filter,
            )
        )
        if stock_defer.get("active"):
            stock_deferred_pool = int(
                stock_unprocessed_base.filter(
                    BreakoutAlert.alerted_at >= stock_defer["cutoff"]
                ).count()
            )
        stock_stale_unprocessed = int(
            stock_unprocessed_base.filter(
                BreakoutAlert.alerted_at < stock_defer["cutoff"]
            ).count()
        )
    effective_batch_limit, fresh_burst_meta = _fresh_candidate_burst_batch_size(
        base_limit=batch_limit,
        fresh_fastlane_state=fresh_fastlane,
        fresh_candidate_count=fresh_candidate_pool,
    )
    candidate_pool_base = len(candidate_refs)
    candidate_pool_exact = (
        len(candidate_refs) < candidate_query_limit
        and bool(stale_candidate_sweep_checked)
    )
    candidate_ids = [int(row_id) for row_id, _ in candidate_refs[:effective_batch_limit]]
    if candidate_ids:
        selected_candidates = (
            db.query(BreakoutAlert)
            .filter(BreakoutAlert.id.in_(candidate_ids))
            .all()
        )
        candidate_by_id = {int(candidate.id): candidate for candidate in selected_candidates}
        candidates = [
            candidate_by_id[candidate_id]
            for candidate_id in candidate_ids
            if candidate_id in candidate_by_id
        ]
    else:
        candidates = []
    retry_pool = 0
    retry_candidates: list[BreakoutAlert] = []
    spare_slots = max(0, batch_limit - len(candidates))
    if spare_slots > 0:
        retry_pool, retry_candidates = _synergy_retry_candidates(
            db,
            uid=uid,
            limit=spare_slots,
        )
        candidates.extend(retry_candidates)
    candidate_pool = candidate_pool_base + retry_pool
    candidate_select_elapsed_s = round(time.monotonic() - candidate_select_started, 3)
    prefetched_prices, prefetched_sources, price_prefetch_meta = (
        _prefetch_candidate_prices(candidates)
    )
    _attach_prefetched_prices(candidates, prefetched_prices, prefetched_sources)

    out: dict[str, Any] = {
        "processed": 0,
        "placed": 0,
        "scaled_in": 0,
        "skipped": 0,
        "candidate_pool": candidate_pool,
        "candidate_batch_base_size": batch_limit,
        "candidate_batch_effective_size": effective_batch_limit,
        "candidate_query_limit": int(candidate_query_limit),
        "candidate_pool_exact": bool(candidate_pool_exact),
        "stale_candidate_sweep_checked": bool(stale_candidate_sweep_checked),
        "stale_candidate_sweep_interval_seconds": int(stale_candidate_sweep_interval_s),
        "stock_candidate_max_age_enabled": bool(
            stock_actionability.get("enabled")
        ),
        "stock_candidate_max_age_minutes": int(
            stock_actionability.get("max_age_minutes") or 0
        ),
        "non_stock_candidate_max_age_enabled": bool(
            non_stock_actionability.get("enabled")
        ),
        "non_stock_candidate_max_age_minutes": int(
            non_stock_actionability.get("max_age_minutes") or 0
        ),
        "fresh_candidate_pool": fresh_candidate_pool,
        "fresh_candidate_pool_exact": bool(fresh_candidate_pool_exact),
        "fresh_candidate_fastlane_enabled": bool(fresh_fastlane.get("enabled")),
        "fresh_candidate_fastlane_max_age_seconds": fresh_fastlane.get("max_age_seconds"),
        "fresh_candidate_burst_enabled": bool(fresh_burst_meta.get("enabled")),
        "fresh_candidate_burst_query_limit": int(
            query_limit_meta.get("effective_limit") or candidate_query_limit
        ),
        "fresh_candidate_burst_window_count": int(
            fresh_burst_meta.get("fresh_window_count") or 1
        ),
        "fresh_candidate_burst_window_capacity": int(
            fresh_burst_meta.get("window_capacity") or batch_limit
        ),
        "candidate_price_prefetch_enabled": bool(price_prefetch_meta.get("enabled")),
        "candidate_price_prefetch_requested": int(
            price_prefetch_meta.get("requested") or 0
        ),
        "candidate_price_prefetch_hits": int(price_prefetch_meta.get("hits") or 0),
        "candidate_price_prefetch_price_bus_hits": int(
            price_prefetch_meta.get("price_bus_hits") or 0
        ),
        "candidate_price_prefetch_batch_requested": int(
            price_prefetch_meta.get("batch_requested") or 0
        ),
        "candidate_price_prefetch_batch_hits": int(
            price_prefetch_meta.get("batch_hits") or 0
        ),
        "candidate_price_prefetch_elapsed_seconds": price_prefetch_meta.get(
            "elapsed_seconds"
        ),
        "candidate_price_prefetch_error": price_prefetch_meta.get("error"),
        "stock_session_defer_active": bool(stock_defer.get("active")),
        "stock_session_defer_reason": stock_defer.get("reason"),
        "stock_session_defer_max_age_hours": stock_defer.get("max_age_hours"),
        "stock_session_defer_counts_checked": bool(
            stock_session_defer_counts_checked
        ),
        "stock_session_deferred_pool": stock_deferred_pool,
        "stock_session_stale_unprocessed": stock_stale_unprocessed,
        "synergy_retry_pool": retry_pool,
        "synergy_retry_batch": len(retry_candidates),
        "tick_budget_seconds": tick_budget_s,
        "tick_budget_deferred": 0,
        "tick_budget_exhausted": False,
        "tick_last_kind": None,
        "tick_last_reason": None,
        "tick_last_alert_id": None,
        "tick_last_ticker": None,
        "tick_runtime_gate_elapsed_seconds": runtime_gate_elapsed_s,
        "tick_lock_cleanup_elapsed_seconds": cleanup_elapsed_s,
        "tick_candidate_select_elapsed_seconds": candidate_select_elapsed_s,
        "tick_processing_elapsed_seconds": 0.0,
        "tick_candidate_pool_zero_diag_elapsed_seconds": 0.0,
        "tick_slowest_alert_elapsed_seconds": 0.0,
        "tick_slowest_alert_id": None,
        "tick_slowest_alert_ticker": None,
    }

    processing_started = time.monotonic()
    for idx, alert in enumerate(candidates):
        if out["processed"] > 0 and (time.monotonic() - tick_started) >= tick_budget_s:
            deferred = len(candidates) - idx
            out["tick_budget_deferred"] = deferred
            out["tick_budget_exhausted"] = True
            _autotrader_tick_note(
                out,
                kind="deferred",
                reason="tick_budget_exhausted",
                alert=alert,
            )
            logger.warning(
                "[autotrader] tick budget exhausted uid=%s processed=%d "
                "deferred=%d budget_s=%s elapsed_s=%.2f",
                uid,
                out["processed"],
                deferred,
                tick_budget_s,
                time.monotonic() - tick_started,
            )
            break
        alert_started = time.monotonic()
        alert_id = int(alert.id)
        alert_ticker = (alert.ticker or "").upper()
        # P0.2 — acquire advisory lock before the TOCTOU window. Without
        # this, two ticks (different scheduler replicas) can both pass the
        # audit-row check and both call place_market_order. The lock is
        # held until we explicitly unlock or the session closes.
        if not _try_claim_alert(db, alert_id):
            _autotrader_tick_note(
                out,
                kind="unclaimed",
                reason="advisory_lock_busy",
                alert=alert,
            )
            _record_slowest_tick_alert(
                out,
                alert_id=alert_id,
                ticker=alert_ticker,
                elapsed_seconds=time.monotonic() - alert_started,
            )
            continue

        try:
            # Re-check race (another worker may have inserted between the
            # outer candidate query and our claim).
            db.expire_all()
            is_synergy_retry = bool(getattr(alert, "_chili_synergy_retry", False))
            if (
                breakout_alert_already_processed(db, alert_id)
                and not is_synergy_retry
            ):
                _autotrader_tick_note(
                    out,
                    kind="skipped",
                    reason="already_processed_race",
                    alert=alert,
                )
                continue

            try:
                _process_one_alert(db, uid, alert, out, rt)
            except Exception as e:
                logger.exception("[autotrader] alert %s failed: %s", alert_id, e)
                _audit(
                    db,
                    user_id=uid,
                    alert=alert,
                    decision="error",
                    reason=str(e)[:500],
                    rule_snapshot=_exception_audit_snapshot(
                        alert,
                        e,
                        phase="process_alert",
                    ),
                )
                _autotrader_tick_note(
                    out, kind="error", reason=str(e)[:500], alert=alert
                )
            out["processed"] += 1
        finally:
            try:
                _release_alert_claim(db, alert_id)
            finally:
                _record_slowest_tick_alert(
                    out,
                    alert_id=alert_id,
                    ticker=alert_ticker,
                    elapsed_seconds=time.monotonic() - alert_started,
                )

    out["tick_processing_elapsed_seconds"] = round(
        time.monotonic() - processing_started,
        3,
    )
    if candidate_pool == 0:
        diag_started = time.monotonic()
        diag = _maybe_log_candidate_pool_zero(db, uid=uid)
        out["tick_candidate_pool_zero_diag_elapsed_seconds"] = round(
            time.monotonic() - diag_started,
            3,
        )
        if diag is not None:
            out["candidate_pool_zero_diag"] = diag
    out["tick_elapsed_seconds"] = round(time.monotonic() - tick_started, 3)

    logger.info(
        "[autotrader] tick uid=%s candidate_pool=%d pool_exact=%s "
        "query_limit=%d stale_sweep_checked=%s stale_sweep_interval_s=%s "
        "stock_max_age_min=%s non_stock_max_age_min=%s "
        "batch=%d base_batch=%d "
        "effective_batch=%d processed=%d placed=%d "
        "scaled_in=%d skipped=%d fresh_pool=%d synergy_retry_pool=%d "
        "synergy_retry_batch=%d stock_defer_active=%s stock_deferred_pool=%d "
        "stock_stale_unprocessed=%d tick_budget_s=%s tick_deferred=%d "
        "fresh_fastlane=%s fresh_burst=%s price_prefetch_hits=%s/%s "
        "price_prefetch_elapsed_s=%s "
        "phase_s=runtime:%s cleanup:%s select:%s process:%s diag:%s "
        "slowest_alert_s=%s slowest_alert_id=%s slowest_ticker=%s "
        "tick_elapsed_s=%.3f last_kind=%s last_reason=%s last_alert_id=%s "
        "last_ticker=%s",
        uid,
        candidate_pool,
        out.get("candidate_pool_exact"),
        out.get("candidate_query_limit"),
        out.get("stale_candidate_sweep_checked"),
        out.get("stale_candidate_sweep_interval_seconds"),
        out.get("stock_candidate_max_age_minutes"),
        out.get("non_stock_candidate_max_age_minutes"),
        len(candidates),
        batch_limit,
        effective_batch_limit,
        out["processed"],
        out["placed"],
        out["scaled_in"],
        out["skipped"],
        fresh_candidate_pool,
        retry_pool,
        len(retry_candidates),
        out.get("stock_session_defer_active"),
        out.get("stock_session_deferred_pool"),
        out.get("stock_session_stale_unprocessed"),
        out.get("tick_budget_seconds"),
        out.get("tick_budget_deferred"),
        out.get("fresh_candidate_fastlane_enabled"),
        out.get("fresh_candidate_burst_enabled"),
        out.get("candidate_price_prefetch_hits"),
        out.get("candidate_price_prefetch_requested"),
        out.get("candidate_price_prefetch_elapsed_seconds"),
        out.get("tick_runtime_gate_elapsed_seconds"),
        out.get("tick_lock_cleanup_elapsed_seconds"),
        out.get("tick_candidate_select_elapsed_seconds"),
        out.get("tick_processing_elapsed_seconds"),
        out.get("tick_candidate_pool_zero_diag_elapsed_seconds"),
        out.get("tick_slowest_alert_elapsed_seconds"),
        out.get("tick_slowest_alert_id")
        if out.get("tick_slowest_alert_id") is not None
        else "-",
        out.get("tick_slowest_alert_ticker") or "-",
        out.get("tick_elapsed_seconds"),
        out.get("tick_last_kind") or "-",
        out.get("tick_last_reason") or "-",
        out.get("tick_last_alert_id") if out.get("tick_last_alert_id") is not None else "-",
        out.get("tick_last_ticker") or "-",
    )

    return {"ok": True, **out}


def _maybe_substitute_with_options(
    db: Session,
    alert: BreakoutAlert,
    spot: float,
    *,
    uid: int | None,
) -> None:
    """Phase 3 — when the substitute flag is on, translate a bullish
    equity alert into a long-call entry by writing option_meta into
    ``alert.indicator_snapshot`` and flipping ``alert.asset_type`` to
    'options'. Mutates the in-memory alert; doesn't touch the DB row.

    Synthesis tunables (DTE target, max spread, etc.) come from the
    StrategyParameter ledger so the brain's learning loop adapts them
    from realized outcomes — no hardcoded values.

    Skips silently (leaves the alert as equity) when:
      - Flag is OFF
      - Alert isn't a bullish stock alert
      - Option chain is illiquid or no tradable contract exists
      - Synthesis raises (broker hiccup, etc.)
    """
    try:
        if not bool(getattr(settings, "chili_autotrader_options_substitute_enabled", False)):
            return
        if bool(getattr(alert, "_chili_probation_recert_allowed", False)):
            logger.info(
                "[autotrader_options_substitute] skipped probation recert alert_id=%s",
                getattr(alert, "id", None),
            )
            return
        if bool(getattr(alert, "_chili_shadow_observation_only", False)):
            logger.info(
                "[autotrader_options_substitute] skipped alert_id=%s ticker=%s reason=%s",
                getattr(alert, "id", None),
                getattr(alert, "ticker", None),
                OPTIONS_SUBSTITUTE_SHADOW_OBSERVATION_BLOCK_REASON,
            )
            return
        if (alert.asset_type or "").lower() != "stock":
            return
        # Bullish-only check: target above entry
        ent = float(alert.entry_price or 0)
        tgt = float(alert.target_price or 0)
        if not (ent > 0 and tgt > ent):
            return

        if bool(
            getattr(
                settings,
                "chili_autotrader_options_substitute_requires_underlying_positive_edge",
                AUTOTRADER_OPTIONS_SUBSTITUTE_DEFAULT_REQUIRES_UNDERLYING_POSITIVE_EDGE,
            )
        ):
            pat_ctx = resolve_pattern_signal_context(
                db,
                pattern_id=alert.scan_pattern_id,
            )
            confidence = alert_confidence_from_score(alert)
            edge_decision = evaluate_entry_edge(
                db,
                alert,
                settings=settings,
                pat_ctx=pat_ctx,
                confidence=confidence,
            )
            snap = alert.indicator_snapshot if isinstance(alert.indicator_snapshot, dict) else {}
            snap = dict(snap)
            snap["options_substitution_underlying_edge"] = edge_decision.snapshot
            snap["options_substitution_underlying_edge_reason"] = edge_decision.reason
            alert.indicator_snapshot = snap
            if not edge_decision.allowed:
                logger.info(
                    "[autotrader_options_substitute] skipped alert_id=%s ticker=%s "
                    "reason=%s edge_reason=%s expected_net_pct=%s",
                    getattr(alert, "id", None),
                    getattr(alert, "ticker", None),
                    OPTIONS_SUBSTITUTE_UNDERLYING_EDGE_BLOCK_REASON,
                    edge_decision.reason,
                    edge_decision.snapshot.get("expected_net_pct"),
                )
                return

        from .options.synthesis import synthesize_option_meta
        notional, _notional_snap = _resolve_entry_risk_notional(db, uid=uid)
        if notional <= 0:
            return
        opt_meta = synthesize_option_meta(
            db=db,
            underlying=str(alert.ticker),
            spot=float(spot),
            notional_usd=notional,
            underlying_target=float(alert.target_price) if alert.target_price is not None else None,
            underlying_stop=float(alert.stop_loss) if alert.stop_loss is not None else None,
            confidence=alert_confidence_from_score(alert),
        )
        if not opt_meta:
            return
        opt_meta = _normalize_option_meta_for_alert(
            alert,
            opt_meta,
            underlying_price=float(spot),
        )

        snap = alert.indicator_snapshot if isinstance(alert.indicator_snapshot, dict) else {}
        snap = dict(snap)  # copy so we don't mutate ORM state inadvertently
        snap["asset_type"] = "options"
        snap["asset_kind"] = "option"
        snap["options_path"] = True
        snap["option_meta"] = opt_meta
        snap["option_contract_key"] = opt_meta.get("contract_key")
        snap["price_domains"] = option_price_domains_snapshot()
        snap["original_asset_type"] = alert.asset_type
        alert.indicator_snapshot = snap
        alert.asset_type = "options"
        # Override entry_price to the option premium so downstream code
        # using alert.entry_price as "limit" stays consistent.
        alert.entry_price = float(opt_meta.get("limit_price") or alert.entry_price)
        logger.info(
            "[autotrader_options_substitute] %s -> %s %s %s%s qty=%d limit=%.2f",
            alert.ticker, alert.ticker,
            opt_meta["expiration"], opt_meta["strike"], opt_meta["option_type"],
            opt_meta["quantity"], opt_meta["limit_price"],
        )
    except Exception:
        logger.debug("[autotrader_options_substitute] failed; falling back to equity", exc_info=True)


def _eligible_lifecycle_stages(*, live: bool = False) -> set[str]:
    """Lifecycle stages allowed to enter a new order path.

    Paper/shadow can evaluate promoted patterns. Real broker orders are
    stricter by default: full-size live entries require the explicit ``live``
    lifecycle stage, while ``pilot_promoted`` remains an opt-in broker ramp.
    """
    if live and bool(getattr(settings, "chili_autotrader_live_requires_live_lifecycle", True)):
        stages = {"live"}
        if (
            bool(getattr(settings, "chili_pilot_promoted_enabled", True))
            and bool(getattr(settings, "chili_autotrader_allow_pilot_promoted_live", True))
        ):
            stages.add("pilot_promoted")
        return stages
    raw = (getattr(settings, "chili_autotrader_eligible_lifecycle_stages", "promoted,live") or "")
    stages = {s.strip().lower() for s in raw.split(",") if s.strip()}
    if bool(getattr(settings, "chili_pilot_promoted_enabled", True)):
        stages.add("pilot_promoted")
    return stages


# f-netedge-live-wiring (Phase D of evidence-fidelity, 2026-05-14):
# Module-level state for the regime-diagnostic emitter. Emits at most once
# every _NETEDGE_DIAG_MIN_INTERVAL_S seconds when >50% of recent NetEdge
# shadow rows have unknown/empty regime — signals that the regime_ledger
# feed isn't flowing into the autotrader path.
_NETEDGE_DIAG_LAST_EMIT_TS: float = 0.0
_NETEDGE_DIAG_MIN_INTERVAL_S: float = 300.0


def _emit_netedge_shadow_score(
    db: Session,
    alert: BreakoutAlert,
    entry_price: float,
) -> None:
    """Shadow-log a NetEdge score for *alert* alongside the heuristic gate.

    Stage 1 of f-netedge-live-wiring: the live autotrader bypasses
    ``portfolio_allocator.evaluate`` (which is where NetEdge is fed today),
    so every recent NetEdgeScoreLog row has ``scan_pattern_id=null`` and
    ``regime=unknown``. This helper fixes that by computing a shadow score
    from the autotrader's own context — pattern, regime-at-alert, stop,
    target, asset_class — so the calibrator can learn per-pattern and
    per-regime.

    Failure of any step (import, DB read, score) MUST NOT bubble up and
    block the autotrader. The caller's contract is "write-only side
    effect, never raise."
    """
    try:
        from . import net_edge_ranker as _net_edge

        if not _net_edge.mode_is_active():
            return
        if alert is None or entry_price is None or float(entry_price) <= 0:
            return

        stop = float(alert.stop_loss) if alert.stop_loss is not None else 0.0
        if stop <= 0:
            return
        target = float(alert.target_price) if alert.target_price is not None else None

        from .asset_class import (
            PATTERN_ASSET_CLASS_CRYPTO,
            PATTERN_ASSET_CLASS_OPTIONS,
            normalize_pattern_asset_class,
        )

        normalized_asset = normalize_pattern_asset_class(getattr(alert, "asset_type", None))
        if normalized_asset == PATTERN_ASSET_CLASS_CRYPTO:
            asset_class = "crypto"
        elif normalized_asset == PATTERN_ASSET_CLASS_OPTIONS:
            asset_class = "options"
        else:
            asset_class = "stock"

        raw_prob: float | None = None
        if alert.scan_pattern_id:
            from .pattern_stats_accessor import get_corrected_pattern_stats

            pat = (
                db.query(ScanPattern)
                .filter(ScanPattern.id == int(alert.scan_pattern_id))
                .one_or_none()
            )
            if pat is not None:
                stats = get_corrected_pattern_stats(pat)
                if stats.win_rate is not None:
                    wr = float(stats.win_rate)
                    raw_prob = wr / 100.0 if wr > 1.0 else wr
        if raw_prob is None:
            return

        regime = (alert.regime_at_alert or "").strip() or None
        if regime is None:
            try:
                from .regime import get_regime_indicators

                regime = (
                    str(get_regime_indicators().get("regime_composite") or "").strip() or None
                )
            except Exception:
                regime = None

        timeframe = (alert.timeframe or "").strip() or None

        _net_edge.score(
            db,
            _net_edge.NetEdgeSignalContext(
                ticker=alert.ticker,
                asset_class=asset_class,
                scan_pattern_id=int(alert.scan_pattern_id) if alert.scan_pattern_id else None,
                raw_prob=float(raw_prob),
                entry_price=float(entry_price),
                stop_price=stop,
                target_price=target,
                direction=str(getattr(alert, "direction", None) or "long"),
                regime=regime,
                timeframe=timeframe,
                heuristic_score=None,
            ),
        )
    except Exception as exc:
        logger.debug("[autotrader] netedge shadow score failed: %s", exc)


def _maybe_emit_regime_diagnostic(db: Session) -> None:
    """Warn when >50% of recent NetEdge shadow rows have unknown regime.

    Rate-limited to once per ``_NETEDGE_DIAG_MIN_INTERVAL_S`` seconds so it
    doesn't spam the log. Reads only — never raises. Looks at the last 100
    rows from the past hour; if ``regime`` is null/empty/unknown for >50%
    of them, logs a WARNING so the operator notices the regime_ledger
    isn't flowing into the autotrader path.
    """
    global _NETEDGE_DIAG_LAST_EMIT_TS
    try:
        now_ts = time.time()
        if now_ts - _NETEDGE_DIAG_LAST_EMIT_TS < _NETEDGE_DIAG_MIN_INTERVAL_S:
            return
        from datetime import timedelta as _td

        from ...models.trading import NetEdgeScoreLog

        cutoff = datetime.utcnow() - _td(hours=1)
        rows = (
            db.query(NetEdgeScoreLog.regime)
            .filter(NetEdgeScoreLog.created_at >= cutoff)
            .order_by(NetEdgeScoreLog.id.desc())
            .limit(100)
            .all()
        )
        n = len(rows)
        if n < 10:
            return
        unknown = sum(
            1
            for (r,) in rows
            if r is None or str(r).strip().lower() in ("", "unknown", "none", "na")
        )
        frac = unknown / n
        if frac > 0.5:
            logger.warning(
                "[autotrader] netedge regime-snapshot diagnostic: %d/%d recent "
                "rows have unknown/empty regime (%.0f%%) — regime_ledger feed "
                "may be stale or missing",
                unknown,
                n,
                frac * 100.0,
            )
            _NETEDGE_DIAG_LAST_EMIT_TS = now_ts
    except Exception as exc:
        logger.debug("[autotrader] netedge regime diagnostic failed: %s", exc)


def is_shadow_promoted_pattern(pat: ScanPattern) -> bool:
    """f-promotion-pipeline-rebalance Phase 3 (2026-05-10):
    True iff *pat* is at lifecycle_stage 'shadow_promoted' AND the
    flag chili_shadow_promoted_lifecycle_enabled is True. Pure read on
    the already-loaded ORM row — no DB query, no side effects.

    When True, the autotrader's _process_one_alert routes the alert to
    shadow-log only (audit row with reason
    'selector:shadow_promoted_pattern_eval'); no broker call, no Trade
    row. When False (flag off, or stage != 'shadow_promoted'), control
    falls through to the existing eligible-lifecycle gate. Paper can still
    evaluate promoted patterns; real broker orders require live approval by
    default.
    """
    if pat is None:
        return False
    stage = (getattr(pat, "lifecycle_stage", None) or "").strip().lower()
    if stage != "shadow_promoted":
        return False
    return bool(getattr(settings, "chili_shadow_promoted_lifecycle_enabled", True))


def _live_recert_block_applies(pat: ScanPattern) -> bool:
    """Return True when recert debt must block live entry for this pattern."""
    if not bool(getattr(pat, "recert_required", False)):
        return False
    if not bool(getattr(settings, "chili_autotrader_block_live_on_recert_required", True)):
        return False
    return _live_recert_allowance(pat) is None


def _live_recert_allowance(pat: ScanPattern) -> str | None:
    """Return the reduced-risk lane that may pass recert debt, if any."""
    if not bool(getattr(pat, "recert_required", False)):
        return None
    if not bool(getattr(settings, "chili_autotrader_block_live_on_recert_required", True)):
        return PILOT_BOOTSTRAP_RECERT_ALLOWANCE
    try:
        from .alpha_portfolio_gate import broker_risk_probation_allows_live

        # Pilot-promoted patterns are already a broker-risk ramp. If they also
        # carry recert debt, they stay in the observation/recert lane; pilot
        # live fills cannot be the mechanism that certifies missing evidence.
        if broker_risk_probation_allows_live(pat, settings_=settings):
            return PROBATION_RECERT_ALLOWANCE
    except Exception:
        logger.debug("[autotrader] recert soft-gate check failed", exc_info=True)
    return None


def _log_expected_edge_reject(alert: BreakoutAlert, snap: dict[str, Any] | None) -> None:
    edge = (snap or {}).get("entry_edge") if isinstance(snap, dict) else None
    if not isinstance(edge, dict):
        edge = snap if isinstance(snap, dict) else {}
    logger.info(
        "[autotrader_edge_reject] alert_id=%s pattern_id=%s ticker=%s "
        "prob=%s breakeven=%s prob_edge=%s reward=%s loss=%s cost=%s "
        "expected_net_pct=%s source=%s sample_n=%s",
        getattr(alert, "id", None),
        getattr(alert, "scan_pattern_id", None),
        getattr(alert, "ticker", None),
        edge.get("probability"),
        edge.get("breakeven_probability"),
        edge.get("probability_edge"),
        edge.get("reward_fraction"),
        edge.get("stop_loss_fraction"),
        edge.get("cost_fraction", edge.get("empirical_cost_fraction")),
        edge.get("expected_net_pct"),
        edge.get("probability_source"),
        edge.get("probability_sample_n"),
    )


def _probation_day_start_utc(now: datetime | None = None) -> datetime:
    now_utc = now or datetime.utcnow()
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    et = ZoneInfo(PROBATION_TIMEZONE)
    now_et = now_utc.astimezone(et)
    start_et = datetime.combine(now_et.date(), datetime_time.min, tzinfo=et)
    return start_et.astimezone(timezone.utc).replace(tzinfo=None)


def _probation_trade_count_today(
    db: Session,
    *,
    uid: int | None,
    pattern_id: int | None = None,
    ticker: str | None = None,
    asset_class: str | None = None,
    now: datetime | None = None,
) -> int:
    start_utc = _probation_day_start_utc(now)
    pattern_clause = ""
    ticker_clause = ""
    asset_clause = ""
    params: dict[str, Any] = {
        "uid": uid,
        "version": AUTOTRADER_VERSION,
        "start_utc": start_utc,
        "flag": PROBATION_JSON_TRUE,
    }
    if pattern_id is not None:
        pattern_clause = "AND scan_pattern_id = :pattern_id"
        params["pattern_id"] = int(pattern_id)
    if ticker:
        ticker_clause = "AND UPPER(ticker) = :ticker"
        params["ticker"] = str(ticker).strip().upper()
    asset = str(asset_class or "").strip().lower()
    if asset == "crypto":
        asset_clause = """
          AND (
              LOWER(COALESCE(asset_kind, '')) = 'crypto'
              OR UPPER(ticker) LIKE '%-USD'
          )
        """
    row = db.execute(text(f"""
        SELECT COUNT(*) AS n
        FROM trading_trades
        WHERE user_id IS NOT DISTINCT FROM :uid
          AND COALESCE(auto_trader_version, '') = :version
          AND entry_date >= :start_utc
          AND COALESCE(
              jsonb_extract_path_text(
                  indicator_snapshot,
                  :entry_execution_key,
                  :probation_flag_key
              ),
              :false_flag
          ) = :flag
          {pattern_clause}
          {ticker_clause}
          {asset_clause}
    """), {
        **params,
        "entry_execution_key": ENTRY_EXECUTION_SNAPSHOT_KEY,
        "probation_flag_key": PROBATION_ENTRY_FLAG,
        "false_flag": PROBATION_JSON_FALSE,
    }).scalar()
    return int(row or 0)


def _probation_asset_class(*, ticker: str | None, asset_type: str | None) -> str:
    raw = str(asset_type or "").strip().lower()
    if raw in {"crypto", "coin", "coinbase_spot"}:
        return "crypto"
    if raw in {"option", "options"}:
        return "options"
    sym = str(ticker or "").strip().upper()
    if sym.endswith("-USD"):
        return "crypto"
    return "stock"


def _probation_portfolio_limit_for_asset(
    *,
    asset_class: str,
    expected_net_pct: float | None,
    base_limit: int,
) -> int:
    if asset_class != "crypto":
        return int(base_limit)
    crypto_limit = int(
        getattr(
            settings,
            "chili_autotrader_probation_crypto_max_trades_per_day",
            AUTOTRADER_PROBATION_DEFAULT_CRYPTO_MAX_TRADES_PER_DAY,
        )
        or 0
    )
    if crypto_limit <= int(base_limit):
        return int(base_limit)
    min_edge = float(
        getattr(
            settings,
            "chili_autotrader_probation_crypto_min_expected_net_pct_for_extra_quota",
            AUTOTRADER_PROBATION_DEFAULT_CRYPTO_MIN_EXPECTED_NET_PCT_FOR_EXTRA_QUOTA,
        )
        or 0.0
    )
    if expected_net_pct is None or expected_net_pct < min_edge:
        return int(base_limit)
    return crypto_limit


def _probation_quota_block_reason(
    db: Session,
    *,
    uid: int | None,
    pattern_id: int | None,
    ticker: str | None = None,
    asset_type: str | None = None,
    expected_net_pct: float | None = None,
) -> str | None:
    if pattern_id is None:
        return PROBATION_QUOTA_REASON_PORTFOLIO
    per_pattern_limit = int(
        getattr(settings, "chili_autotrader_probation_max_trades_per_pattern_per_day", 0)
        or 0
    )
    per_pattern_ticker_limit = int(
        getattr(
            settings,
            "chili_autotrader_probation_max_trades_per_pattern_ticker_per_day",
            AUTOTRADER_PROBATION_DEFAULT_MAX_TRADES_PER_PATTERN_TICKER_PER_DAY,
        )
        or 0
    )
    portfolio_limit = int(
        getattr(settings, "chili_autotrader_probation_max_trades_per_day", 0)
        or 0
    )
    if per_pattern_limit <= 0 or portfolio_limit <= 0:
        return PROBATION_QUOTA_REASON_PORTFOLIO
    asset_class = _probation_asset_class(ticker=ticker, asset_type=asset_type)
    if asset_class == "crypto" and per_pattern_ticker_limit > 0 and ticker:
        pattern_ticker_count = _probation_trade_count_today(
            db,
            uid=uid,
            pattern_id=pattern_id,
            ticker=ticker,
        )
        if pattern_ticker_count >= per_pattern_ticker_limit:
            return PROBATION_QUOTA_REASON_PATTERN_TICKER
    else:
        pattern_count = _probation_trade_count_today(
            db,
            uid=uid,
            pattern_id=pattern_id,
        )
        if pattern_count >= per_pattern_limit:
            return PROBATION_QUOTA_REASON_PATTERN
    base_portfolio_limit = portfolio_limit
    portfolio_limit = _probation_portfolio_limit_for_asset(
        asset_class=asset_class,
        expected_net_pct=expected_net_pct,
        base_limit=portfolio_limit,
    )
    portfolio_count = _probation_trade_count_today(
        db,
        uid=uid,
        asset_class=(
            "crypto"
            if asset_class == "crypto" and portfolio_limit > base_portfolio_limit
            else None
        ),
    )
    if portfolio_count >= portfolio_limit:
        return PROBATION_QUOTA_REASON_PORTFOLIO
    return None


def _process_one_alert(
    db: Session,
    uid: int,
    alert: BreakoutAlert,
    out: dict[str, Any],
    runtime: dict[str, Any],
) -> None:
    live = bool(runtime.get("live_orders_effective"))
    shadow_signal_lane = _alert_signal_lane(alert)
    if _alert_requests_shadow_observation(alert):
        if bool(
            getattr(
                settings,
                "chili_autotrader_shadow_signal_lane_observation_enabled",
                True,
            )
        ):
            setattr(alert, "_chili_shadow_observation_only", True)
            setattr(
                alert,
                "_chili_shadow_observation_reason",
                SHADOW_OBSERVATION_REASON_SIGNAL_LANE,
            )
            setattr(alert, "_chili_shadow_observation_signal_lane", shadow_signal_lane)
        else:
            _audit(
                db,
                user_id=uid,
                alert=alert,
                decision="blocked",
                reason=SHADOW_OBSERVATION_REASON_SIGNAL_LANE_DISABLED,
            )
            out["skipped"] += 1
            _autotrader_tick_note(
                out,
                kind="blocked",
                reason=SHADOW_OBSERVATION_REASON_SIGNAL_LANE_DISABLED,
                alert=alert,
            )
            return
    # 2026-04-28 lifecycle gate. Evidence audit demotes patterns to 'challenged'
    # but the entry funnel had been ignoring lifecycle_stage, so 32 of 34 entries
    # last week landed on demoted patterns (driving most of the bleed). Enforce
    # the audit's intent at trade-placement. Override via
    # CHILI_AUTOTRADER_ELIGIBLE_LIFECYCLE_STAGES env var to widen the set.
    if alert.scan_pattern_id:
        _pat = db.query(ScanPattern).filter(ScanPattern.id == int(alert.scan_pattern_id)).first()
        if _pat is not None:
            _stage = (_pat.lifecycle_stage or "").strip().lower()
            if not bool(getattr(_pat, "active", True)):
                _reason = "pattern_inactive"
                _audit(db, user_id=uid, alert=alert, decision="skipped", reason=_reason)
                out["skipped"] += 1
                _autotrader_tick_note(out, kind="skipped", reason=_reason, alert=alert)
                return
            # f-promotion-pipeline-rebalance Phase 3 (2026-05-10):
            # shadow_promoted patterns fire imminent alerts but are
            # routed to shadow-log only — no broker call, no Trade row.
            # Gated on chili_shadow_promoted_lifecycle_enabled (default
            # True). When False, falls through to the lifecycle-not-
            # eligible gate below (pre-Phase-3 behavior). Helper returns
            # False for any stage != 'shadow_promoted', preserving
            # byte-identical behavior for promoted/live/challenged/etc.
            if is_shadow_promoted_pattern(_pat):
                if bool(
                    getattr(
                        settings,
                        "chili_autotrader_shadow_promoted_paper_observation_enabled",
                        True,
                    )
                ):
                    setattr(alert, "_chili_shadow_observation_only", True)
                    if not getattr(alert, "_chili_shadow_observation_reason", None):
                        setattr(
                            alert,
                            "_chili_shadow_observation_reason",
                            SHADOW_OBSERVATION_REASON_STAGE,
                        )
                else:
                    _shadow_reason = SHADOW_OBSERVATION_REASON_STAGE
                    _audit(db, user_id=uid, alert=alert,
                           decision="blocked", reason=_shadow_reason)
                    out["skipped"] += 1
                    _autotrader_tick_note(
                        out, kind="blocked", reason=_shadow_reason, alert=alert,
                    )
                    return
            elif (
                live
                and bool(getattr(_pat, "recert_required", False))
            ):
                _recert_allowance = _live_recert_allowance(_pat)
                if _recert_allowance is None:
                    setattr(alert, "_chili_recert_required", True)
                elif _recert_allowance == PROBATION_RECERT_ALLOWANCE:
                    setattr(alert, "_chili_probation_recert_allowed", True)
                else:
                    setattr(alert, "_chili_pilot_bootstrap_recert_allowed", True)
            _allowed = _eligible_lifecycle_stages(live=live)
            if (
                not bool(getattr(alert, "_chili_shadow_observation_only", False))
                and _stage not in _allowed
            ):
                _prefix = (
                    "pattern_lifecycle_not_live_approved"
                    if live and bool(getattr(settings, "chili_autotrader_live_requires_live_lifecycle", True))
                    else "pattern_lifecycle_not_eligible"
                )
                _reason = f"{_prefix}:{_stage or 'none'}"
                _audit(db, user_id=uid, alert=alert, decision="skipped", reason=_reason)
                out["skipped"] += 1
                _autotrader_tick_note(out, kind="skipped", reason=_reason, alert=alert)
                return

    # 2026-04-28 regime gate. Reads pattern x regime ledger; blocks entries
    # for (pattern, ticker_regime) pairs with confident negative expectancy.
    # Default mode is "shadow" — logs would-be-blocks without enforcing,
    # so we accumulate audit history before flipping to "live".
    try:
        from .regime_gate import regime_gate_blocks_entry
        _rg_block, _rg_reason = regime_gate_blocks_entry(
            db,
            pattern_id=alert.scan_pattern_id,
            ticker=alert.ticker,
        )
        if _rg_block:
            _rg_full_reason = f"regime_gate:{_rg_reason}"
            _audit(db, user_id=uid, alert=alert, decision="skipped",
                   reason=_rg_full_reason)
            if live:
                _rg_px = _current_price(alert.ticker)
                if _rg_px is not None:
                    _maybe_open_reject_paper_shadow(
                        db,
                        uid=uid,
                        alert=alert,
                        px=_rg_px,
                        snap={
                            "paper_observation_reason": _rg_full_reason,
                            "regime_gate_reason": _rg_reason,
                        },
                        reason=_rg_full_reason,
                    )
            out["skipped"] += 1
            _autotrader_tick_note(out, kind="skipped",
                                  reason=_rg_full_reason, alert=alert)
            return
    except Exception as _rg_exc:
        # Defense-in-depth: never let the regime gate's wiring error block
        # an alert. The gate is an additive safety, not a critical path.
        logger.debug("[regime_gate] eval skipped due to error: %s", _rg_exc)

    px = _current_price_for_alert(alert)
    if px is None:
        _audit(
            db,
            user_id=uid,
            alert=alert,
            decision="skipped",
            reason="no_quote",
            rule_snapshot={
                "entry_quote_source": getattr(
                    alert,
                    "_chili_current_price_source",
                    "single_fetch",
                ),
            },
        )
        out["skipped"] += 1
        _autotrader_tick_note(out, kind="skipped", reason="no_quote", alert=alert)
        return

    # Phase 3: when the substitute flag is on, mutate this in-memory
    # equity alert into an options alert. The rule gate's options_path
    # branch then picks it up just like an explicitly-queued option
    # alert. No-op when flag is off (leaves the alert as equity).
    _maybe_substitute_with_options(db, alert, px, uid=uid)

    open_n = count_autotrader_v1_open(db, uid, paper_mode=not live)
    open_by_lane = count_autotrader_v1_open_by_lane(db, uid, paper_mode=not live)
    loss_today = (
        autotrader_paper_realized_pnl_today_et(db, uid)
        if not live
        else autotrader_realized_pnl_today_et(db, uid)
    )
    ctx = RuleGateContext(
        current_price=px,
        autotrader_open_count=open_n,
        realized_loss_today_usd=loss_today,
        autotrader_open_count_by_lane=open_by_lane,
    )

    existing_trade = None
    existing_paper = None
    if live:
        existing_trade = find_open_autotrader_trade(db, user_id=uid, ticker=alert.ticker)
    else:
        existing_paper = find_open_autotrader_paper(db, user_id=uid, ticker=alert.ticker)

    scale_plan = None
    if live and existing_trade is not None and str(existing_trade.status or "").lower() == "working":
        pending_snap = {
            "existing_trade_id": getattr(existing_trade, "id", None),
            "existing_trade_status": getattr(existing_trade, "status", None),
            "existing_trade_broker_status": getattr(existing_trade, "broker_status", None),
            "existing_trade_broker_order_id": getattr(existing_trade, "broker_order_id", None),
            "existing_trade_entry_date": (
                existing_trade.entry_date.isoformat()
                if getattr(existing_trade, "entry_date", None) else None
            ),
        }
        _audit(
            db,
            user_id=uid,
            alert=alert,
            decision="skipped",
            reason=PENDING_ENTRY_ALREADY_WORKING_REASON,
            rule_snapshot=pending_snap,
        )
        _maybe_open_reject_paper_shadow(
            db,
            uid=uid,
            alert=alert,
            px=px,
            snap=pending_snap,
            reason=PENDING_ENTRY_ALREADY_WORKING_REASON,
            existing_qty=getattr(existing_trade, "quantity", None),
        )
        out["skipped"] += 1
        _autotrader_tick_note(
            out,
            kind="skipped",
            reason=PENDING_ENTRY_ALREADY_WORKING_REASON,
            alert=alert,
        )
        return
    if live and existing_trade is not None:
        scale_plan = maybe_scale_in(
            db,
            user_id=uid,
            ticker=alert.ticker,
            new_scan_pattern_id=alert.scan_pattern_id,
            new_stop=float(alert.stop_loss) if alert.stop_loss is not None else None,
            new_target=float(alert.target_price) if alert.target_price is not None else None,
            current_price=px,
            settings=settings,
        )

    if existing_trade is not None:
        if int(existing_trade.scan_pattern_id or 0) == int(alert.scan_pattern_id or 0):
            _audit(db, user_id=uid, alert=alert, decision="skipped", reason="duplicate_pattern_already_open")
            _maybe_open_reject_paper_shadow(
                db,
                uid=uid,
                alert=alert,
                px=px,
                snap={
                    "existing_trade_id": getattr(existing_trade, "id", None),
                    "existing_trade_entry_date": (
                        existing_trade.entry_date.isoformat()
                        if getattr(existing_trade, "entry_date", None) else None
                    ),
                },
                reason="duplicate_pattern_already_open",
                existing_qty=getattr(existing_trade, "quantity", None),
            )
            out["skipped"] += 1
            _autotrader_tick_note(
                out, kind="skipped", reason="duplicate_pattern_already_open", alert=alert
            )
            return
        if scale_plan is None:
            is_synergy_retry = bool(getattr(alert, "_chili_synergy_retry", False))
            reason = (
                "synergy_disabled_second_signal"
                if not getattr(settings, "chili_autotrader_synergy_enabled", False)
                else (
                    SYNERGY_RETRY_EXHAUSTED_REASON
                    if is_synergy_retry
                    else SYNERGY_RETRY_SOURCE_REASON
                )
            )
            synergy_snap = {
                "existing_trade_id": getattr(existing_trade, "id", None),
                "existing_trade_scan_pattern_id": getattr(
                    existing_trade, "scan_pattern_id", None,
                ),
                "existing_trade_entry_date": (
                    existing_trade.entry_date.isoformat()
                    if getattr(existing_trade, "entry_date", None) else None
                ),
                "existing_trade_scale_in_count": int(
                    getattr(existing_trade, "scale_in_count", 0) or 0
                ),
            }
            if is_synergy_retry:
                synergy_snap["synergy_retry"] = True
                synergy_snap["synergy_retry_source_reason"] = (
                    SYNERGY_RETRY_SOURCE_REASON
                )
                synergy_snap["synergy_retry_source_run_id"] = getattr(
                    alert, "_chili_synergy_retry_source_run_id", None,
                )
                synergy_snap["synergy_retry_lookback_minutes"] = getattr(
                    alert, "_chili_synergy_retry_lookback_minutes", None,
                )
            _audit(
                db,
                user_id=uid,
                alert=alert,
                decision="skipped",
                reason=reason,
                rule_snapshot=synergy_snap,
            )
            _maybe_open_reject_paper_shadow(
                db,
                uid=uid,
                alert=alert,
                px=px,
                snap=synergy_snap,
                reason=reason,
                existing_qty=getattr(existing_trade, "quantity", None),
            )
            out["skipped"] += 1
            _autotrader_tick_note(out, kind="skipped", reason=reason, alert=alert)
            return

    if not live and existing_paper is not None:
        if int(existing_paper.scan_pattern_id or 0) == int(alert.scan_pattern_id or 0):
            _audit(db, user_id=uid, alert=alert, decision="skipped", reason="duplicate_pattern_paper_open")
            out["skipped"] += 1
            _autotrader_tick_note(
                out, kind="skipped", reason="duplicate_pattern_paper_open", alert=alert
            )
            return
        if getattr(settings, "chili_autotrader_synergy_enabled", False):
            _audit(db, user_id=uid, alert=alert, decision="skipped", reason="paper_synergy_not_supported")
            skip_reason = "paper_synergy_not_supported"
        else:
            _audit(db, user_id=uid, alert=alert, decision="skipped", reason="synergy_disabled_second_signal")
            skip_reason = "synergy_disabled_second_signal"
        out["skipped"] += 1
        _autotrader_tick_note(out, kind="skipped", reason=skip_reason, alert=alert)
        return

    for_new = scale_plan is None

    if live and for_new:
        cooldown_snap = _recent_live_exit_cooldown_snapshot(
            db,
            user_id=uid,
            alert=alert,
        )
        if cooldown_snap is not None:
            _audit(
                db,
                user_id=uid,
                alert=alert,
                decision="blocked",
                reason=RECENT_LIVE_EXIT_COOLDOWN_REASON,
                rule_snapshot=cooldown_snap,
            )
            _maybe_open_reject_paper_shadow(
                db,
                uid=uid,
                alert=alert,
                px=px,
                snap=cooldown_snap,
                reason=RECENT_LIVE_EXIT_COOLDOWN_REASON,
            )
            out["skipped"] += 1
            _autotrader_tick_note(
                out,
                kind="blocked",
                reason=RECENT_LIVE_EXIT_COOLDOWN_REASON,
                alert=alert,
            )
            return

    # Venue health is checked after broker selection in _execute_broker_buy so
    # Coinbase-routed alerts are gated on Coinbase health instead of RH health.

    # P0.4 — autopilot mutual exclusion. Only gate LIVE orders: the lease
    # signal for momentum_neural is a mode="live" TradingAutomationSession,
    # so paper v1 can't contend on the schema level. For live v1:
    #   * scale-in (scale_plan != None) → our own existing Trade is the lease,
    #     gate returns owner_self → allowed.
    #   * new entry → gate blocks if momentum_neural already owns the symbol.
    if live:
        gate = check_autopilot_entry_gate(
            db,
            candidate=AUTOPILOT_AUTO_TRADER_V1,
            symbol=alert.ticker,
            user_id=uid,
        )
        if not gate.get("allowed"):
            rsn = f"autopilot_mutex:{gate.get('reason')}:owner={gate.get('owner') or 'none'}"
            _audit(
                db,
                user_id=uid,
                alert=alert,
                decision="blocked",
                reason=rsn,
            )
            out["skipped"] += 1
            _autotrader_tick_note(out, kind="blocked", reason=rsn, alert=alert)
            return

    ok, reason, snap = passes_rule_gate(
        db, alert, settings=settings, ctx=ctx, for_new_entry=for_new, fallback_user_id=uid,
    )
    snap["entry_quote_source"] = getattr(
        alert,
        "_chili_current_price_source",
        "single_fetch",
    )
    snap["entry_quote_prefetch_used"] = bool(
        getattr(alert, "_chili_current_price_prefetch_used", False)
    )
    snap["entry_quote_fetch_path"] = (
        "batch_prefetch" if snap["entry_quote_prefetch_used"] else "single_fetch"
    )
    if not ok:
        if (
            bool(getattr(alert, "_chili_shadow_observation_only", False))
            or shadow_signal_lane in SHADOW_OBSERVATION_SIGNAL_LANES
        ):
            shadow_reason = str(
                getattr(
                    alert,
                    "_chili_shadow_observation_reason",
                    SHADOW_OBSERVATION_REASON_STAGE,
                )
                or SHADOW_OBSERVATION_REASON_STAGE
            )
            snap["paper_observation_reason"] = shadow_reason
            snap["paper_observation_live_orders_effective"] = bool(live)
            snap["rule_gate_reject_reason"] = str(reason)
            if shadow_signal_lane:
                snap["paper_observation_signal_lane"] = shadow_signal_lane
        if reason == "non_positive_expected_edge":
            _log_expected_edge_reject(alert, snap)
        if reason == EXIT_GEOMETRY_REFRESH_REASON:
            pattern = _pattern_row(db, alert.scan_pattern_id)
            exit_geometry_work = _queue_exit_geometry_variant_work(
                db,
                alert=alert,
                pattern=pattern,
                reason=reason,
                snap=snap,
            )
            if exit_geometry_work is not None:
                snap["exit_geometry_variant_work"] = exit_geometry_work
        _audit(db, user_id=uid, alert=alert, decision="skipped", reason=reason, rule_snapshot=snap)
        if live:
            _maybe_open_reject_paper_shadow(
                db,
                uid=uid,
                alert=alert,
                px=px,
                snap=snap,
                reason=reason,
            )
        out["skipped"] += 1
        _autotrader_tick_note(out, kind="skipped", reason=str(reason), alert=alert)
        return

    llm_snap: dict[str, Any] | None = None
    _run_llm_revalidation, _llm_skip_reason = _should_run_llm_revalidation(alert)
    if _llm_skip_reason:
        snap["llm_revalidation_skipped"] = True
        snap["llm_revalidation_skip_reason"] = _llm_skip_reason
    if _run_llm_revalidation:
        ohlcv = _ohlcv_summary(alert.ticker)
        viable, llm_snap = run_revalidation_llm(
            alert,
            current_price=px,
            ohlcv_summary=ohlcv,
            pattern_name=_pattern_name(db, alert.scan_pattern_id),
            trace_id=f"autotrader-{alert.id}",
        )
        if not viable:
            llm_block_reason = _llm_revalidation_block_reason(llm_snap)
            snap["llm_revalidation_block_reason"] = llm_block_reason
            if llm_block_reason == "llm_unavailable":
                snap["llm_unavailable"] = True
            _audit(
                db,
                user_id=uid,
                alert=alert,
                decision="blocked",
                reason=llm_block_reason,
                rule_snapshot=snap,
                llm_snapshot=llm_snap,
            )
            if live:
                _maybe_open_reject_paper_shadow(
                    db,
                    uid=uid,
                    alert=alert,
                    px=px,
                    snap={**(snap or {}), "llm_snapshot": llm_snap},
                    reason=llm_block_reason,
                )
            out["skipped"] += 1
            _autotrader_tick_note(out, kind="blocked", reason=llm_block_reason, alert=alert)
            return

    # P1.4 — runtime feature-parity assertion at entry. Fetches a fresh
    # OHLCV frame, computes the live indicator snapshot, and verifies it
    # matches the canonical compute_all_from_df output on the same frame.
    # If enabled, compute/fetch/check failures fail closed by default so a
    # broken canary cannot silently bless a live entry. In ``soft`` mode,
    # successful drift checks still allow through and record a
    # TradingExecutionEvent with event_type='feature_parity_drift'. In
    # ``hard`` mode, critical drift blocks entry.
    # Only gates live paths: paper skip is cheap and paper drift still
    # records for auditing below.
    if live:
        _parity_blocked = _maybe_check_feature_parity(
            db,
            alert=alert,
            rule_snapshot=snap,
            ticker=alert.ticker,
            scan_pattern_id=alert.scan_pattern_id,
            venue="robinhood",
            source="auto_trader_v1",
        )
        if _parity_blocked is not None:
            rsn = _parity_blocked[:255]
            _audit(
                db,
                user_id=uid,
                alert=alert,
                decision="blocked",
                reason=rsn,
                rule_snapshot=snap,
                llm_snapshot=llm_snap,
            )
            out["skipped"] += 1
            _autotrader_tick_note(out, kind="blocked", reason=rsn, alert=alert)
            return

    # f-netedge-live-wiring Stage 1: parallel NetEdge shadow score.
    # Runs after every gate that could have rejected the alert but BEFORE
    # broker placement. Shadow-log only — never gates the decision; the
    # heuristic path below remains authoritative until a future brief
    # flips brain_net_edge_ranker_mode to ``authoritative``.
    _emit_netedge_shadow_score(db, alert, float(px))
    _maybe_emit_regime_diagnostic(db)

    if scale_plan is not None and bool(
        getattr(alert, "_chili_shadow_observation_only", False)
    ):
        shadow_snap = dict(snap or {})
        shadow_snap["shadow_scale_in_blocked"] = True
        shadow_snap["shadow_scale_in_existing_trade_id"] = getattr(
            getattr(scale_plan, "trade", None), "id", None
        )
        shadow_snap["shadow_scale_in_policy"] = "observation_only_no_live_mutation"
        qty = _normalized_scale_in_quantity(alert, scale_plan, shadow_snap)
        _record_shadow_observation_entry(
            db,
            uid=uid,
            alert=alert,
            qty=qty,
            px=px,
            snap=shadow_snap,
            llm_snap=llm_snap,
            live=live,
            out=out,
        )
        return

    if scale_plan is not None:
        _execute_scale_in(db, uid, alert, scale_plan, px, snap, llm_snap, live, out)
        return

    _execute_new_entry(db, uid, alert, px, snap, llm_snap, live, out)


def _maybe_check_feature_parity(
    db: Session,
    *,
    alert: BreakoutAlert,
    rule_snapshot: dict[str, Any] | None,
    ticker: str,
    scan_pattern_id: int | None,
    venue: str,
    source: str,
) -> str | None:
    """Run the P1.4 parity check. Returns a ``reason`` string when the live
    entry should be blocked, ``None`` otherwise. Never raises.

    **Short-circuits on the feature flag BEFORE any OHLCV fetch / compute
    work.** The flag is off by default, so this function must be near-zero
    cost when unwired — otherwise every live alert pays a network fetch for
    nothing, which in a Windows test environment has been observed to exhaust
    the ephemeral socket pool (WinError 10055).
    """
    if not bool(getattr(settings, "chili_feature_parity_enabled", False)):
        if bool(getattr(settings, "chili_autotrader_live_require_feature_parity", True)):
            return "feature_parity_required_disabled"
        return None

    fail_closed = bool(getattr(settings, "chili_feature_parity_fail_closed_on_error", True))

    try:
        from .feature_parity import (
            DEFAULT_FEATURES,
            check_entry_feature_parity,
        )
        from .market_data import fetch_ohlcv_df
    except Exception:
        logger.warning(
            "[autotrader] feature_parity imports unavailable for %s",
            ticker, exc_info=True,
        )
        return "feature_parity_unavailable:import" if fail_closed else None

    try:
        df = fetch_ohlcv_df(ticker, "1d", "6mo")
    except Exception:
        logger.warning(
            "[autotrader] feature_parity OHLCV fetch failed for %s",
            ticker, exc_info=True,
        )
        return "feature_parity_unavailable:ohlcv" if fail_closed else None
    if df is None or df.empty:
        return "feature_parity_unavailable:no_reference_df" if fail_closed else None

    live_snap = _feature_parity_decision_snapshot(
        alert,
        rule_snapshot,
        feature_keys=set(DEFAULT_FEATURES),
    )
    if not live_snap:
        return "feature_parity_unavailable:no_live_snapshot" if fail_closed else None
    if not (set(live_snap.keys()) - {"price"}):
        return "feature_parity_unavailable:no_signal_features" if fail_closed else None
    features_to_check = set(live_snap.keys())

    try:
        result = check_entry_feature_parity(
            db,
            ticker=ticker,
            live_snap=live_snap,
            reference_df=df,
            features=features_to_check,
            source=source,
            scan_pattern_id=scan_pattern_id,
            venue=venue,
        )
    except Exception:
        logger.warning(
            "[autotrader] feature_parity check raised for %s",
            ticker, exc_info=True,
        )
        return "feature_parity_unavailable:check_failed" if fail_closed else None
    if result.ok:
        return None
    # Hard-mode critical block.
    return f"feature_parity:{result.reason or result.severity}"


def _execute_broker_buy(
    db: Session,
    *,
    uid: int,
    alert: BreakoutAlert,
    qty: float,
    client_order_id: str,
    snap: dict[str, Any],
    llm_snap: dict[str, Any] | None,
    out: dict[str, Any],
    px: float | None = None,
) -> dict[str, Any] | None:
    """Place a live buy via Robinhood with the full safety envelope.

    Phase D (tech-debt): previously this exact sequence was duplicated
    between ``_execute_scale_in`` and ``_execute_new_entry`` — a
    kill-switch fix applied to one path but not the other was
    always-one-edit away. Centralized here so both callers share the
    same gate order: kill-switch recheck → adapter enabled → broker
    place → error surface.

    Returns the broker result dict on success (caller writes the trade
    row), or ``None`` if the path short-circuited. Every short-circuit
    path also writes an ``AutoTraderRun`` audit row and increments
    ``out["skipped"]`` so the caller can return immediately.
    """
    from .governance import is_kill_switch_active_for_session
    from .venue.factory import get_adapter

    # P0.5 — re-check kill switch immediately before submitting. The
    # initial check at tick entry can go stale if an operator flips the
    # switch while gates are evaluating (feature_parity / LLM can take
    # seconds). Cheap (in-memory lock + bool), so no reason not to.
    if is_kill_switch_active_for_session(db):
        _audit(
            db,
            user_id=uid,
            alert=alert,
            decision="blocked",
            reason="kill_switch_activated_mid_flight",
            rule_snapshot=snap,
            llm_snapshot=llm_snap,
        )
        out["skipped"] += 1
        _autotrader_tick_note(
            out, kind="blocked", reason="kill_switch_activated_mid_flight", alert=alert
        )
        return None

    try:
        from .portfolio_risk import circuit_breaker_entry_block_reason

        breaker_reason = circuit_breaker_entry_block_reason(db, user_id=uid)
    except Exception as exc:
        breaker_reason = f"Circuit breaker active: gate_exception:{type(exc).__name__}"
    if breaker_reason is not None:
        _block_live_order(
            db,
            uid=uid,
            alert=alert,
            reason=f"portfolio_blocked:{breaker_reason}",
            snap=snap,
            llm_snap=llm_snap,
            out=out,
        )
        return None

    cap_source = str(snap.get("notional_capital_source") or snap.get("capital_source") or "")
    if cap_source.startswith("fallback:") and bool(
        getattr(settings, "chili_autotrader_block_live_on_capital_fallback", True)
    ):
        _block_live_order(
            db,
            uid=uid,
            alert=alert,
            reason=f"capital_unavailable:{cap_source}",
            snap=snap,
            llm_snap=llm_snap,
            out=out,
        )
        return None

    # Task MM Phase 2 — when this is an options alert, branch to the
    # options venue adapter instead of the spot adapter. The rule gate
    # has already validated the option metadata exists, so we just
    # extract it and call place_option_buy. snap['option_meta'] is set
    # by the gate when options_path=True.
    if snap.get("options_path") and snap.get("option_meta"):
        opt_meta = _normalize_option_meta_for_alert(
            alert,
            snap["option_meta"],
            underlying_price=px,
        )
        snap["option_meta"] = opt_meta
        snap["asset_type"] = "options"
        snap["asset_kind"] = "option"
        snap["option_contract_key"] = opt_meta.get("contract_key")
        snap["price_domains"] = option_price_domains_snapshot()
        contract_qty = parse_contract_quantity(qty)
        option_limit_price = (
            _float_or_none(opt_meta.get("limit_price"))
            or _float_or_none(alert.entry_price)
        )
        if contract_qty is None:
            snap["options_quantity_error"] = "invalid_contract_quantity"
            _audit(
                db,
                user_id=uid,
                alert=alert,
                decision="blocked",
                reason="options_order_invalid_quantity",
                rule_snapshot=snap,
                llm_snapshot=llm_snap,
            )
            out["skipped"] += 1
            _autotrader_tick_note(
                out, kind="blocked", reason="options_order_invalid_quantity", alert=alert,
            )
            return None
        if option_limit_price is None:
            snap["options_limit_price_error"] = "invalid_limit_price"
            _audit(
                db,
                user_id=uid,
                alert=alert,
                decision="blocked",
                reason="options_order_invalid_limit_price",
                rule_snapshot=snap,
                llm_snapshot=llm_snap,
            )
            out["skipped"] += 1
            _autotrader_tick_note(
                out, kind="blocked", reason="options_order_invalid_limit_price", alert=alert,
            )
            return None
        venue_reason = _live_venue_health_block_reason(db, venue="robinhood")
        if venue_reason is not None:
            _block_live_order_with_paper_shadow(
                db,
                uid=uid,
                alert=alert,
                reason=venue_reason,
                snap=snap,
                llm_snap=llm_snap,
                out=out,
                qty=float(contract_qty),
                px=float(option_limit_price),
                shadow_decision="blocked_venue_health",
            )
            return None
        from .venue.robinhood_options import RobinhoodOptionsAdapter
        opt_ad = RobinhoodOptionsAdapter()
        if not opt_ad.is_enabled():
            _audit(
                db, user_id=uid, alert=alert,
                decision="blocked", reason="rh_options_adapter_off",
                rule_snapshot=snap, llm_snapshot=llm_snap,
            )
            out["skipped"] += 1
            _autotrader_tick_note(out, kind="blocked", reason="rh_options_adapter_off", alert=alert)
            return None
        # Phase 4 — multi-leg branch. When option_meta carries `legs`
        # (a list of >1 leg dicts), submit as a spread atomically via
        # the spread adapter method instead of single-leg place_option_buy.
        # The strategy layer (Q2.T1 vertical_spread / iron_condor /
        # etc.) emits the legs + direction; the autotrader just routes.
        legs = opt_meta.get("legs")
        option_order_hint = (
            "option_spread"
            if isinstance(legs, list) and len(legs) > 1
            else "option_limit"
        )
        reject_fp = _broker_reject_action_fingerprint(
            alert,
            venue="robinhood_options",
            side="buy",
            qty=float(contract_qty),
            snap=snap,
            order_hint=option_order_hint,
        )
        if _maybe_block_repeated_broker_reject(
            db,
            uid=uid,
            alert=alert,
            fingerprint=reject_fp,
            snap=snap,
            llm_snap=llm_snap,
            out=out,
        ):
            return None
        try:
            if isinstance(legs, list) and len(legs) > 1:
                res = opt_ad.place_spread(
                    underlying=str(alert.ticker),
                    legs=legs,
                    quantity=contract_qty,
                    limit_price=option_limit_price,
                    direction=str(opt_meta.get("direction", "debit")),
                )
            else:
                # qty here represents number of CONTRACTS (each = 100
                # underlying shares). The rule gate's notional sizing
                # already converted cash → contract count.
                res = opt_ad.place_option_buy(
                    underlying=str(alert.ticker),
                    expiration=str(opt_meta["expiration"]),
                    strike=float(opt_meta["strike"]),
                    option_type=str(opt_meta["option_type"]),
                    quantity=contract_qty,
                    limit_price=option_limit_price,
                )
        except Exception as exc:
            res = {"ok": False, "error": f"options_adapter_exception:{exc}"}
        if not res.get("ok"):
            _annotate_broker_reject(
                snap,
                fingerprint=reject_fp,
                venue="robinhood_options",
                error=res.get("error"),
            )
            _audit(
                db, user_id=uid, alert=alert,
                decision="blocked", reason=f"broker:{res.get('error')}",
                rule_snapshot=snap, llm_snapshot=llm_snap,
            )
            out["skipped"] += 1
            _autotrader_tick_note(
                out, kind="blocked", reason=f"broker:{res.get('error')}", alert=alert,
            )
            return None
        res.setdefault("_chili_broker_source", "robinhood")
        res["_chili_options_path"] = True
        res["_chili_option_meta"] = opt_meta
        res["_chili_option_order_state"] = _order_state_from_response(res)
        res.setdefault("base_size", contract_qty)
        res.setdefault("limit_price", option_limit_price)
        return res

    # f-coinbase-autotrader-enablement-phase-5-cost-aware-sizing
    # (2026-05-09): cost-aware min-edge gate runs BEFORE the broker
    # selector. RH-eligible tickers get fee=0 (no behavior change vs
    # pre-Phase-5; the gate is a no-op). Coinbase-only tickers must
    # have positive expected net edge that clears the Tier-1 fee floor
    # (default 120bps round-trip + 30bps buffer = 150bps min). If the
    # rule snapshot lacks expected-net evidence, fall back to the legacy
    # gross projected-profit field.
    _cost_gate_error: str | None = None
    try:
        from .cost_aware_gate import cost_aware_min_edge_gate as _cost_gate
        _cost_gate_edge_pct, _cost_gate_edge_source = (
            _cost_gate_edge_pct_from_snapshot(snap)
        )
        snap["cost_gate_edge_pct"] = _cost_gate_edge_pct
        snap["cost_gate_edge_pct_source"] = _cost_gate_edge_source
        _cost_decision = _cost_gate(
            ticker=alert.ticker,
            projected_profit_pct=_cost_gate_edge_pct,
            db=db,
        )
    except Exception as exc:
        logger.warning(
            "[autotrader] cost gate unavailable for ticker=%s; will fail closed if routed to Coinbase",
            alert.ticker,
            exc_info=True,
        )
        _cost_gate_error = f"{type(exc).__name__}"
        _cost_decision = None
    if _cost_decision is not None and not _cost_decision.allowed:
        snap["cost_gate_edge_bps"] = _cost_decision.edge_bps
        snap["cost_gate_threshold_bps"] = _cost_decision.threshold_bps
        snap["cost_gate_fee_bps"] = _cost_decision.fee_bps
        snap["cost_gate_tca_cost_bps"] = _cost_decision.tca_cost_bps
        if _cost_decision.tca_snapshot is not None:
            snap["cost_gate_tca_snapshot"] = _cost_decision.tca_snapshot
        _cost_reason = f"cost_gate:{_cost_decision.reason}"
        _audit(
            db,
            user_id=uid,
            alert=alert,
            decision="blocked",
            reason=_cost_reason,
            rule_snapshot=snap,
            llm_snapshot=llm_snap,
        )
        out["skipped"] += 1
        _autotrader_tick_note(
            out, kind="blocked", reason=_cost_reason, alert=alert,
        )
        logger.info(
            "[autotrader] cost gate blocked alert ticker=%s "
            "edge_bps=%d threshold_bps=%d fee_bps=%d reason=%s",
            alert.ticker, _cost_decision.edge_bps,
            _cost_decision.threshold_bps, _cost_decision.fee_bps,
            _cost_decision.reason,
        )
        return None
    if _cost_decision is not None:
        snap["cost_gate_edge_bps"] = _cost_decision.edge_bps
        snap["cost_gate_threshold_bps"] = _cost_decision.threshold_bps
        snap["cost_gate_fee_bps"] = _cost_decision.fee_bps
        snap["cost_gate_tca_cost_bps"] = _cost_decision.tca_cost_bps
        if _cost_decision.tca_snapshot is not None:
            snap["cost_gate_tca_snapshot"] = _cost_decision.tca_snapshot

    # f-coinbase-autotrader-enablement-phase-3-broker-selector
    # (2026-05-09): broker selector. RH path is BYTE-IDENTICAL
    # post-Phase-3; the selector decides which venue to use, then
    # either falls through to the existing RH code (decision.venue ==
    # 'rh') or routes to Coinbase (decision.venue == 'coinbase',
    # gated on CHILI_COINBASE_AUTOTRADER_LIVE — default OFF = shadow-
    # log only). Skip decisions audit + return early.
    try:
        from .broker_selector import select_venue
        _venue_decision = select_venue(ticker=alert.ticker, db=db)
    except Exception as exc:
        _block_live_order(
            db,
            uid=uid,
            alert=alert,
            reason=f"broker_selector_unavailable:{type(exc).__name__}",
            snap=snap,
            llm_snap=llm_snap,
            out=out,
        )
        logger.warning(
            "[autotrader] broker selector failed for ticker=%s; blocked live order",
            alert.ticker,
            exc_info=True,
        )
        return None

    if _venue_decision.venue == "skip":
        _selector_reason = f"selector:{_venue_decision.reason}"
        _audit(
            db,
            user_id=uid,
            alert=alert,
            decision="blocked",
            reason=_selector_reason,
            rule_snapshot=snap,
            llm_snapshot=llm_snap,
        )
        out["skipped"] += 1
        _autotrader_tick_note(
            out, kind="blocked", reason=_selector_reason, alert=alert,
        )
        return None

    if _venue_decision.venue == "coinbase":
        # Phase 3: Coinbase routing is SHADOW-LOG only by default.
        # Operator flips CHILI_COINBASE_AUTOTRADER_LIVE=1 once
        # Phase 4 bracket writer + Phase 5 cost-aware sizing are
        # ready. The shadow-log path audits the routing decision so
        # the operator can grep "would have routed Coinbase" rows
        # without any broker call risk.
        from ...config import settings as _cfg_p3
        _coinbase_live = bool(
            getattr(_cfg_p3, "chili_coinbase_autotrader_live", False)
        )
        if not _coinbase_live:
            _shadow_reason = "selector:coinbase_routing_shadow_log"
            _audit(
                db, user_id=uid, alert=alert,
                decision="blocked", reason=_shadow_reason,
                rule_snapshot=snap, llm_snapshot=llm_snap,
            )
            out["skipped"] += 1
            _autotrader_tick_note(
                out, kind="blocked", reason=_shadow_reason, alert=alert,
            )
            logger.info(
                "[autotrader] selector routed alert ticker=%s to Coinbase "
                "but CHILI_COINBASE_AUTOTRADER_LIVE=0; SHADOW-LOG only "
                "(reason=%s)",
                alert.ticker, _venue_decision.reason,
            )
            return None

        if _cost_gate_error is not None:
            _block_live_order(
                db,
                uid=uid,
                alert=alert,
                reason=f"cost_gate_unavailable:{_cost_gate_error}",
                snap=snap,
                llm_snap=llm_snap,
                out=out,
            )
            return None

        venue_reason = _live_venue_health_block_reason(db, venue="coinbase")
        if venue_reason is not None:
            _block_live_order_with_paper_shadow(
                db,
                uid=uid,
                alert=alert,
                reason=venue_reason,
                snap=snap,
                llm_snap=llm_snap,
                out=out,
                qty=float(qty),
                px=px or snap.get("entry_price") or alert.entry_price,
                shadow_decision="blocked_venue_health",
            )
            return None

        # f-coinbase-autotrader-enablement-phase-5-cost-aware-sizing
        # (2026-05-09): per-venue notional + concurrent-position cap.
        # Independent from RH cap per Phase 1 design constraint #1.
        _cap_px = (
            px
            if px is not None
            else snap.get("current_price")
            or snap.get("price")
            or snap.get("entry_price")
            or getattr(alert, "entry_price", None)
            or getattr(alert, "price_at_alert", None)
        )
        try:
            _proposed_notional = float(qty) * float(_cap_px or 0.0)
        except (TypeError, ValueError):
            _proposed_notional = 0.0
        if _proposed_notional <= 0.0:
            _block_live_order(
                db,
                uid=uid,
                alert=alert,
                reason="coinbase_cap_unavailable:missing_price",
                snap=snap,
                llm_snap=llm_snap,
                out=out,
            )
            logger.warning(
                "[autotrader] Coinbase cap missing usable price for ticker=%s; blocked live order",
                alert.ticker,
            )
            return None

        try:
            from .cost_aware_gate import per_venue_cap_check as _cap_check
            _cap = _cap_check(
                venue="coinbase",
                proposed_notional_usd=_proposed_notional,
                db=db, user_id=uid,
            )
        except Exception as exc:
            # f-phase3-stop-bleed D2 — expose the unbound identifier on
            # NameError so the rejection histogram pins the source bug
            # instead of reporting an anonymous ``coinbase_cap_unavailable:
            # NameError`` 54x/week. ``NameError.name`` is the unbound name
            # (Python 3.10+).
            _exc_detail = type(exc).__name__
            if isinstance(exc, NameError) and getattr(exc, "name", None):
                _exc_detail = f"NameError:{exc.name}"
            _block_live_order(
                db,
                uid=uid,
                alert=alert,
                reason=f"coinbase_cap_unavailable:{_exc_detail}",
                snap=snap,
                llm_snap=llm_snap,
                out=out,
            )
            logger.warning(
                "[autotrader] Coinbase cap check failed for ticker=%s; blocked live order",
                alert.ticker,
                exc_info=True,
            )
            return None
        if _cap is not None and not _cap.allowed:
            _cap_reason = f"coinbase_cap:{_cap.reason}"
            _audit(
                db, user_id=uid, alert=alert,
                decision="blocked", reason=_cap_reason,
                rule_snapshot=snap, llm_snapshot=llm_snap,
            )
            out["skipped"] += 1
            _autotrader_tick_note(
                out, kind="blocked", reason=_cap_reason, alert=alert,
            )
            _maybe_open_paper_shadow(
                db,
                uid=uid,
                alert=alert,
                qty=qty,
                px=float(_cap_px),
                snap=snap,
                decision="blocked_coinbase_cap",
            )
            logger.info(
                "[autotrader] Coinbase cap blocked alert ticker=%s "
                "current_positions=%d current_notional_usd=%.2f reason=%s",
                alert.ticker, _cap.current_positions,
                _cap.current_notional_usd, _cap.reason,
            )
            return None

        cb_ad = get_adapter("coinbase")
        if cb_ad is None or not cb_ad.is_enabled():
            _audit(
                db, user_id=uid, alert=alert,
                decision="blocked", reason="coinbase_adapter_off",
                rule_snapshot=snap, llm_snapshot=llm_snap,
            )
            out["skipped"] += 1
            _autotrader_tick_note(
                out, kind="blocked", reason="coinbase_adapter_off",
                alert=alert,
            )
            return None

        # f-coinbase-maker-only-routing (2026-05-19): when the flag is
        # ON, attempt a post_only limit-buy at current best-bid instead
        # of a crossing market order. 2026-05-18 TCA showed +102 bps
        # avg entry slippage on crypto, eating ~60% of pattern 585's
        # 168 bps gross edge. Maker-only trades off "missed entries
        # when price moves up" for "no taker fees + no adverse fill".
        # If best-bid is unavailable OR maker_only is False, fall
        # through to the original market path (preserves byte-identical
        # behavior when flag is OFF).
        cb_res = None
        _maker_only = False
        try:
            from ...config import settings as _settings
            _maker_only = bool(getattr(
                _settings, "chili_coinbase_maker_only_enabled", False,
            ))
        except Exception:
            _maker_only = False

        if _maker_only:
            try:
                _bbo, _fr = cb_ad.get_best_bid_ask(alert.ticker)
                _bid = getattr(_bbo, "bid", None) if _bbo is not None else None
                _ask = getattr(_bbo, "ask", None) if _bbo is not None else None
                if _bid is None or float(_bid) <= 0:
                    logger.warning(
                        "[autotrader] maker-only: no best_bid for %s; "
                        "falling back to market order",
                        alert.ticker,
                    )
                else:
                    _prod = None
                    try:
                        _prod, _ = cb_ad.get_product(alert.ticker)
                    except Exception:
                        _prod = None
                    _price_increment = None
                    if _prod is not None:
                        _price_increment = (
                            getattr(_prod, "price_increment", None)
                            or getattr(_prod, "quote_increment", None)
                        )
                    _limit_plan = plan_post_only_buy_limit(
                        bid=_bid,
                        ask=_ask,
                        price_increment=_price_increment,
                        improve_ticks=int(getattr(
                            _settings,
                            "chili_coinbase_maker_only_improve_bid_ticks",
                            0,
                        ) or 0),
                    )
                    if _limit_plan is None:
                        logger.warning(
                            "[autotrader] maker-only: unusable best_bid for %s; "
                            "falling back to market order",
                            alert.ticker,
                        )
                        cb_res = None
                    else:
                        cb_reject_fp = _broker_reject_action_fingerprint(
                            alert,
                            venue="coinbase",
                            side="buy",
                            qty=float(qty),
                            snap=snap,
                            order_hint="limit_post_only",
                        )
                        if _maybe_block_repeated_broker_reject(
                            db,
                            uid=uid,
                            alert=alert,
                            fingerprint=cb_reject_fp,
                            snap=snap,
                            llm_snap=llm_snap,
                            out=out,
                        ):
                            return None
                        cb_res = cb_ad.place_limit_order_gtc(
                            product_id=alert.ticker,
                            side="buy",
                            base_size=str(qty),
                            limit_price=_limit_plan.limit_price_text,
                            client_order_id=client_order_id,
                            post_only=True,
                        )
                    if _limit_plan is not None and isinstance(cb_res, dict):
                        cb_res["_chili_maker_only"] = True
                        cb_res["_chili_maker_limit_price"] = _limit_plan.limit_price
                        cb_res["_chili_maker_bid"] = _limit_plan.bid
                        cb_res["_chili_maker_ask"] = _limit_plan.ask
                        cb_res["_chili_maker_price_increment"] = _limit_plan.price_increment
                        cb_res["_chili_maker_improved_ticks"] = _limit_plan.improved_ticks
                    if _limit_plan is not None and isinstance(cb_res, dict) and cb_res.get("ok"):
                        logger.info(
                            "[autotrader] maker-only accepted limit_buy %s "
                            "qty=%s limit=%s bid=%s ask=%s improved_ticks=%s "
                            "order_id=%s post_only=True",
                            alert.ticker,
                            cb_res.get("base_size") or qty,
                            cb_res.get("limit_price") or _limit_plan.limit_price_text,
                            _limit_plan.bid,
                            _limit_plan.ask,
                            _limit_plan.improved_ticks,
                            cb_res.get("order_id"),
                        )
                    elif _limit_plan is not None:
                        logger.warning(
                            "[autotrader] maker-only rejected limit_buy %s "
                            "qty=%s limit=%s error=%s",
                            alert.ticker,
                            qty,
                            _limit_plan.limit_price_text,
                            cb_res.get("error") if isinstance(cb_res, dict) else cb_res,
                        )
            except Exception:
                logger.warning(
                    "[autotrader] maker-only routing failed for %s; "
                    "falling back to market order",
                    alert.ticker, exc_info=True,
                )
                cb_res = None

        if cb_res is None:
            cb_reject_fp = _broker_reject_action_fingerprint(
                alert,
                venue="coinbase",
                side="buy",
                qty=float(qty),
                snap=snap,
                order_hint="market",
            )
            if _maybe_block_repeated_broker_reject(
                db,
                uid=uid,
                alert=alert,
                fingerprint=cb_reject_fp,
                snap=snap,
                llm_snap=llm_snap,
                out=out,
            ):
                return None
            cb_res = cb_ad.place_market_order(
                product_id=alert.ticker,
                side="buy",
                base_size=str(qty),
                client_order_id=client_order_id,
            )
        if not cb_res.get("ok"):
            _annotate_broker_reject(
                snap,
                fingerprint=cb_reject_fp,
                venue="coinbase",
                error=cb_res.get("error"),
            )
            _audit(
                db, user_id=uid, alert=alert,
                decision="blocked",
                reason=f"broker:{cb_res.get('error')}",
                rule_snapshot=snap, llm_snapshot=llm_snap,
            )
            out["skipped"] += 1
            _autotrader_tick_note(
                out, kind="blocked",
                reason=f"broker:{cb_res.get('error')}", alert=alert,
            )
            return None
        # f-coinbase-autotrader-enablement-phase-4-bracket-writer-path
        # (2026-05-09): tag the broker response so the downstream Trade
        # row gets `broker_source='coinbase'` instead of the
        # hard-coded 'robinhood' default. The bracket reconciler reads
        # broker_source to dispatch venue-aware repair sweeps; without
        # this tag a Coinbase entry would land with broker_source=
        # 'robinhood' and the RH reconciler would try to use the equity
        # API on it (regression of the ADA/SOL crash class).
        cb_res["_chili_broker_source"] = "coinbase"
        return cb_res

    # decision.venue == 'rh' -- fall through to the existing RH path
    # BYTE-IDENTICAL. The selector adds a pre-route hop but does NOT
    # change any of the call args below; the RH adapter receives
    # exactly the same arguments it did pre-Phase-3.
    venue_reason = _live_venue_health_block_reason(db, venue="robinhood")
    if venue_reason is not None:
        _block_live_order_with_paper_shadow(
            db,
            uid=uid,
            alert=alert,
            reason=venue_reason,
            snap=snap,
            llm_snap=llm_snap,
            out=out,
            qty=float(qty),
            px=px or snap.get("entry_price") or alert.entry_price,
            shadow_decision="blocked_venue_health",
        )
        return None

    ad = get_adapter("robinhood")
    if ad is None or not ad.is_enabled():
        _audit(
            db,
            user_id=uid,
            alert=alert,
            decision="blocked",
            reason="rh_adapter_off",
            rule_snapshot=snap,
            llm_snapshot=llm_snap,
        )
        out["skipped"] += 1
        _autotrader_tick_note(out, kind="blocked", reason="rh_adapter_off", alert=alert)
        return None

    # FIX A-3 (2026-04-29 third-pass audit): pre-flight crypto-supported
    # check. The audit found 48/24h crypto alerts blocked AFTER the
    # broker call returned ``crypto_not_supported_on_robinhood:<BASE>``
    # (AKT, 1INCH, 2Z, ...). Burns broker quota and produces noisy
    # blocked rows.
    #
    # audit-unsupported-crypto-prefilter (2026-05-04): two-layer check.
    # Layer 1 is the static whitelist (cheap, offline, deterministic) —
    # the dominant path for the steady-state list. Layer 2 is the
    # existing quote-probe (cached 5min via get_crypto_quote) — kept as
    # defense-in-depth to self-heal when Robinhood adds a pair that the
    # static whitelist doesn't yet list. The post-broker-call check
    # downstream stays in place as third-line defense.
    _ticker = (alert.ticker or "").upper()
    if _ticker.endswith("-USD"):
        try:
            from ...services.broker_service import (
                is_robinhood_supported_crypto,
                _is_crypto_supported_on_robinhood,
            )
            _base = _ticker[:-4]
            # Layer 1 (static whitelist) — cheap, fail-fast.
            unsupported = not is_robinhood_supported_crypto(_base)
            # Layer 2 (probe) — only when the static layer rejected.
            # Lets us self-heal on broker-side additions without a code change.
            if unsupported and _is_crypto_supported_on_robinhood(_base):
                unsupported = False
            if unsupported:
                _reason = f"pre_broker:venue_unsupported_crypto:{_base}"
                _audit(
                    db,
                    user_id=uid,
                    alert=alert,
                    decision="blocked",
                    reason=_reason,
                    rule_snapshot=snap,
                    llm_snapshot=llm_snap,
                )
                out["skipped"] += 1
                _autotrader_tick_note(
                    out, kind="blocked",
                    reason=_reason,
                    alert=alert,
                )
                return None
        except Exception:
            logger.debug(
                "[autotrader] FIX A-3 pre-flight crypto check failed for %s",
                _ticker, exc_info=True,
            )

    rh_reject_fp = _broker_reject_action_fingerprint(
        alert,
        venue="robinhood",
        side="buy",
        qty=float(qty),
        snap=snap,
        order_hint="market",
    )
    if _maybe_block_repeated_broker_reject(
        db,
        uid=uid,
        alert=alert,
        fingerprint=rh_reject_fp,
        snap=snap,
        llm_snap=llm_snap,
        out=out,
    ):
        return None

    res = ad.place_market_order(
        product_id=alert.ticker,
        side="buy",
        base_size=str(qty),
        client_order_id=client_order_id,
    )
    if not res.get("ok"):
        _annotate_broker_reject(
            snap,
            fingerprint=rh_reject_fp,
            venue="robinhood",
            error=res.get("error"),
        )
        _audit(
            db,
            user_id=uid,
            alert=alert,
            decision="blocked",
            reason=f"broker:{res.get('error')}",
            rule_snapshot=snap,
            llm_snapshot=llm_snap,
        )
        out["skipped"] += 1
        _autotrader_tick_note(
            out,
            kind="blocked",
            reason=f"broker:{res.get('error')}",
            alert=alert,
        )
        return None
    return res


def _row_field(row: Any, key: str, index: int) -> Any:
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        return mapping.get(key)
    if hasattr(row, "get"):
        try:
            return row.get(key)
        except Exception:
            pass
    try:
        return row[index]
    except Exception:
        return getattr(row, key, None)


def _json_time(value: Any) -> str | None:
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        try:
            return str(iso())
        except Exception:
            return str(value)
    return str(value)


def _utc_naive_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _scale_in_protection_max_age_sec() -> int:
    raw = getattr(settings, "chili_bracket_watchdog_stale_after_sec", 300) or 300
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 300


def _freshness_age_seconds(
    observed_at: Any,
    *,
    max_age_sec: int,
    now: datetime | None = None,
) -> tuple[bool, float | None]:
    observed = _utc_naive_datetime(observed_at)
    if observed is None:
        return False, None
    now_utc = now or datetime.now(timezone.utc).replace(tzinfo=None)
    age = now_utc - observed
    age_sec = max(0.0, age.total_seconds())
    return age_sec <= float(max_age_sec), age_sec


def _live_scale_in_protection_status(
    db: Session,
    trade: Any,
) -> tuple[bool, str, dict[str, Any]]:
    """Return whether an existing live trade has proven protective coverage.

    Scale-ins add risk to a position that should already be protected. The
    broker-facing reconciler is the authority for that proof; the advisory
    broker order-id mirror is intentionally not consulted here.
    """
    snap: dict[str, Any] = {
        "trade_id": getattr(trade, "id", None),
        "ticker": (getattr(trade, "ticker", None) or "").upper() or None,
        "broker_source": getattr(trade, "broker_source", None),
        "trade_status": getattr(trade, "status", None),
    }
    max_age_sec = _scale_in_protection_max_age_sec()
    snap["max_age_sec"] = max_age_sec
    if db is None:
        return False, "db_unavailable", snap
    try:
        trade_id = int(getattr(trade, "id", 0) or 0)
    except (TypeError, ValueError):
        return False, "trade_id_invalid", snap
    if trade_id <= 0:
        return False, "trade_id_invalid", snap
    if not str(getattr(trade, "broker_source", "") or "").strip():
        return False, "broker_source_missing", snap

    try:
        intent_row = db.execute(
            text(
                """
                SELECT id, intent_state, last_observed_at, last_diff_reason
                FROM trading_bracket_intents
                WHERE trade_id = :trade_id
                ORDER BY id DESC
                LIMIT 1
                """
            ),
            {"trade_id": trade_id},
        ).fetchone()
        if intent_row is None:
            return False, "no_bracket_intent", snap

        intent_state = str(_row_field(intent_row, "intent_state", 1) or "").strip().lower()
        last_diff_reason = str(
            _row_field(intent_row, "last_diff_reason", 3) or ""
        ).strip().lower()
        intent_observed_at = _row_field(intent_row, "last_observed_at", 2)
        snap.update(
            bracket_intent_id=_row_field(intent_row, "id", 0),
            bracket_intent_state=intent_state or None,
            bracket_intent_last_observed_at=_json_time(
                intent_observed_at
            ),
            bracket_intent_last_diff_reason=last_diff_reason or None,
        )

        reconciliation_row = db.execute(
            text(
                """
                SELECT kind, severity, observed_at
                FROM trading_bracket_reconciliation_log
                WHERE trade_id = :trade_id
                ORDER BY observed_at DESC, id DESC
                LIMIT 1
                """
            ),
            {"trade_id": trade_id},
        ).fetchone()
    except Exception as exc:
        snap["lookup_error"] = type(exc).__name__
        return False, f"lookup_failed:{type(exc).__name__}", snap

    if reconciliation_row is not None:
        latest_kind = str(_row_field(reconciliation_row, "kind", 0) or "").strip().lower()
        reconciliation_observed_at = _row_field(reconciliation_row, "observed_at", 2)
        reconciliation_fresh, reconciliation_age_sec = _freshness_age_seconds(
            reconciliation_observed_at,
            max_age_sec=max_age_sec,
        )
        snap.update(
            latest_reconciliation_kind=latest_kind or None,
            latest_reconciliation_severity=_row_field(reconciliation_row, "severity", 1),
            latest_reconciliation_observed_at=_json_time(
                reconciliation_observed_at
            ),
            latest_reconciliation_age_sec=(
                round(reconciliation_age_sec, 3)
                if reconciliation_age_sec is not None
                else None
            ),
            latest_reconciliation_fresh=reconciliation_fresh,
        )
        if latest_kind == "agree":
            if not reconciliation_fresh:
                stale_reason = (
                    "latest_reconciliation_unobserved"
                    if reconciliation_age_sec is None
                    else "latest_reconciliation_stale:agree"
                )
                return False, stale_reason, snap
            return True, "latest_reconciliation:agree", snap
        if latest_kind in SCALE_IN_PROTECTION_BLOCKING_RECONCILIATION_KINDS:
            return False, f"latest_reconciliation:{latest_kind}", snap
        return False, f"latest_reconciliation:{latest_kind or 'unknown'}", snap

    if last_diff_reason.startswith(SCALE_IN_PROTECTION_BLOCKING_REASON_PREFIXES):
        return False, f"last_diff_reason:{last_diff_reason.split(':', 1)[0]}", snap
    if intent_state in SCALE_IN_PROTECTION_PROVEN_STATES:
        intent_fresh, intent_age_sec = _freshness_age_seconds(
            intent_observed_at,
            max_age_sec=max_age_sec,
        )
        snap.update(
            bracket_intent_age_sec=(
                round(intent_age_sec, 3) if intent_age_sec is not None else None
            ),
            bracket_intent_fresh=intent_fresh,
        )
        if not intent_fresh:
            stale_reason = (
                "intent_state_unobserved"
                if intent_age_sec is None
                else f"intent_state_stale:{intent_state}"
            )
            return False, stale_reason, snap
        return True, f"intent_state:{intent_state}", snap
    return False, f"intent_state:{intent_state or 'unknown'}", snap


def _execute_scale_in(
    db: Session,
    uid: int,
    alert: BreakoutAlert,
    plan: Any,
    px: float,
    snap: dict[str, Any],
    llm_snap: dict[str, Any] | None,
    live: bool,
    out: dict[str, Any],
) -> None:
    t = plan.trade
    add_q = _normalized_scale_in_quantity(alert, plan, snap)
    if add_q <= 0.0:
        _audit(
            db,
            user_id=uid,
            alert=alert,
            decision="skipped",
            reason="scale_in_notional_below_trade_unit",
            rule_snapshot=snap,
        )
        out["skipped"] = out.get("skipped", 0) + 1
        _autotrader_tick_note(
            out,
            kind="skipped",
            reason="scale_in_notional_below_trade_unit",
            alert=alert,
        )
        return
    if live:
        protection_ok, protection_reason, protection_snap = (
            _live_scale_in_protection_status(db, t)
        )
        snap["scale_in_protection"] = protection_snap
        snap["scale_in_protection_reason"] = protection_reason
        if not protection_ok:
            _block_live_order(
                db,
                uid=uid,
                alert=alert,
                reason=f"{SCALE_IN_PROTECTION_UNPROVEN_REASON}:{protection_reason}",
                snap=snap,
                llm_snap=llm_snap,
                out=out,
            )
            return
        if bool(getattr(settings, "chili_autotrader_block_live_on_capital_fallback", True)):
            try:
                from .auto_trader_rules import resolve_effective_capital

                _, cap_source = resolve_effective_capital(db, settings)
            except Exception as exc:
                cap_source = f"fallback:scale_in_capital_check:{type(exc).__name__}"
            snap["scale_in_capital_source"] = cap_source
            snap.setdefault("notional_capital_source", cap_source)
            if str(cap_source).startswith("fallback:"):
                _block_live_order(
                    db,
                    uid=uid,
                    alert=alert,
                    reason=f"capital_unavailable:{cap_source}",
                    snap=snap,
                    llm_snap=llm_snap,
                    out=out,
                )
                return
        res = _execute_broker_buy(
            db,
            uid=uid,
            alert=alert,
            qty=add_q,
            client_order_id=f"atv1-{alert.id}-scale",
            snap=snap,
            llm_snap=llm_snap,
            out=out,
            px=px,
        )
        if res is None:
            return

    t.entry_price = float(plan.new_avg_entry)
    t.quantity = float(t.quantity) + add_q
    t.stop_loss = float(plan.new_stop)
    t.take_profit = float(plan.new_target)
    t.scale_in_count = int(t.scale_in_count or 0) + 1
    if t.indicator_snapshot is None:
        t.indicator_snapshot = {}
    if isinstance(t.indicator_snapshot, dict):
        confirming_pattern_id = getattr(
            plan, "confirming_pattern_id", getattr(alert, "scan_pattern_id", None)
        )
        def _snapshot_list(value: Any) -> list[Any]:
            if value is None:
                return []
            if isinstance(value, list):
                return list(value)
            if isinstance(value, tuple):
                return list(value)
            return [value]

        existing_alert_ids = _snapshot_list(
            t.indicator_snapshot.get(SCALE_IN_ALERT_IDS_SNAPSHOT_KEY)
        )
        existing_pattern_ids = _snapshot_list(
            t.indicator_snapshot.get(SCALE_IN_PATTERN_IDS_SNAPSHOT_KEY)
        )
        t.indicator_snapshot = {
            **t.indicator_snapshot,
            SCALE_IN_ALERT_IDS_SNAPSHOT_KEY: existing_alert_ids + [alert.id],
            SCALE_IN_PATTERN_IDS_SNAPSHOT_KEY: existing_pattern_ids
            + ([confirming_pattern_id] if confirming_pattern_id is not None else []),
        }
    db.add(t)
    db.commit()
    _audit(
        db,
        user_id=uid,
        alert=alert,
        decision="scaled_in",
        reason="ok",
        rule_snapshot=snap,
        llm_snapshot=llm_snap,
        trade_id=t.id,
    )
    out["scaled_in"] += 1
    _autotrader_tick_note(out, kind="scaled_in", reason="ok", alert=alert)


def _normalized_scale_in_quantity(
    alert: BreakoutAlert,
    plan: Any,
    snap: dict[str, Any],
) -> float:
    """Return broker-safe scale-in quantity and persist the normalization audit."""
    try:
        raw_add_q = float(getattr(plan, "added_quantity", 0.0) or 0.0)
    except (TypeError, ValueError):
        raw_add_q = 0.0
    try:
        from .tick_normalizer import normalize_quantity

        normalized = float(normalize_quantity(raw_add_q, alert.ticker))
        source = "tick_normalizer"
    except Exception:
        normalized = raw_add_q
        source = "raw_fallback"
        snap["scale_in_qty_normalization_error"] = "tick_normalizer_failed"
    snap["scale_in_qty_raw"] = round(raw_add_q, QUANTITY_ROUND_DIGITS)
    snap["scale_in_qty_normalized"] = round(normalized, QUANTITY_ROUND_DIGITS)
    snap["scale_in_qty_source"] = source
    return normalized if normalized > 0.0 else 0.0


def _record_shadow_observation_entry(
    db: Session,
    *,
    uid: int,
    alert: BreakoutAlert,
    qty: float,
    px: float,
    snap: dict[str, Any],
    llm_snap: dict[str, Any] | None,
    live: bool,
    out: dict[str, Any],
) -> None:
    _reason = str(
        getattr(
            alert,
            "_chili_shadow_observation_reason",
            SHADOW_OBSERVATION_REASON_STAGE,
        )
        or SHADOW_OBSERVATION_REASON_STAGE
    )
    snap["paper_observation_reason"] = _reason
    snap["paper_observation_live_orders_effective"] = bool(live)
    if getattr(alert, "_chili_shadow_observation_signal_lane", None):
        snap["paper_observation_signal_lane"] = getattr(
            alert,
            "_chili_shadow_observation_signal_lane",
            None,
        )
    pattern = _pattern_row(db, alert.scan_pattern_id)
    _shadow_stock_fastlane = _queue_shadow_stock_fastlane_for_observation(
        db,
        alert=alert,
        pattern=pattern,
        reason=_reason,
        snap=snap,
    )
    if _shadow_stock_fastlane is not None:
        snap["shadow_stock_fastlane"] = _shadow_stock_fastlane
    _signal_lane = str(
        getattr(alert, "_chili_shadow_observation_signal_lane", "") or ""
    ).strip().lower()
    if (
        _signal_lane == HARD_RECERT_SHADOW_SIGNAL_LANE
        or bool(getattr(pattern, "recert_required", False))
    ):
        _recert_fastlane = _queue_recert_for_blocked_signal(
            db,
            alert=alert,
            pattern=pattern,
            reason=_reason,
        )
        if _recert_fastlane is not None:
            snap["recert_signal_fastlane"] = _recert_fastlane
    _audit(
        db,
        user_id=uid,
        alert=alert,
        decision="blocked",
        reason=_reason,
        rule_snapshot=snap,
        llm_snapshot=llm_snap,
    )
    out["skipped"] += 1
    _autotrader_tick_note(out, kind="blocked", reason=_reason, alert=alert)
    _maybe_open_paper_shadow(
        db,
        uid=uid,
        alert=alert,
        qty=qty,
        px=px,
        snap=snap,
        decision="blocked_shadow_promoted",
    )


def _execute_new_entry(
    db: Session,
    uid: int,
    alert: BreakoutAlert,
    px: float,
    snap: dict[str, Any],
    llm_snap: dict[str, Any] | None,
    live: bool,
    out: dict[str, Any],
) -> None:
    try:
        px = float(px)
    except (TypeError, ValueError):
        px = 0.0
    if not math.isfinite(px) or px <= 0:
        _audit(db, user_id=uid, alert=alert, decision="skipped", reason="bad_px", rule_snapshot=snap)
        out["skipped"] += 1
        _autotrader_tick_note(out, kind="skipped", reason="bad_px", alert=alert)
        return

    _shadow_observation_only = bool(
        getattr(alert, "_chili_shadow_observation_only", False)
    )
    _shadow_observation_diagnostic_sizing_enabled = bool(
        getattr(
            settings,
            SHADOW_OBSERVATION_DIAGNOSTIC_SIZING_SETTING,
            AUTOTRADER_SHADOW_OBSERVATION_DIAGNOSTIC_SIZING_DEFAULT_ENABLED,
        )
    )
    _shadow_observation_lightweight_sizing_supported = not bool(snap.get("options_path"))
    _skip_shadow_observation_advisory_sizing = (
        _shadow_observation_only
        and _shadow_observation_lightweight_sizing_supported
        and not _shadow_observation_diagnostic_sizing_enabled
    )

    # Entry size starts from risk budget and account equity. A fixed dollar
    # fallback is honored only when explicitly configured above zero.
    if _skip_shadow_observation_advisory_sizing:
        notional, _notional_snap = _resolve_shadow_observation_lightweight_notional()
    else:
        notional, _notional_snap = _resolve_entry_risk_notional(db, uid=uid)
    snap.update(_notional_snap)
    if bool(getattr(alert, "_chili_pilot_bootstrap_recert_allowed", False)):
        snap["pilot_bootstrap_recert_allowed"] = True
        snap["pilot_bootstrap_recert_policy"] = "allowed_for_pilot_only"
    if bool(getattr(alert, "_chili_probation_recert_allowed", False)):
        snap[PROBATION_ENTRY_FLAG] = True
        snap["probation_recert_policy"] = PROBATION_ENTRY_POLICY
    equity = float(_notional_snap.get("notional_capital_usd") or 0.0)
    fallback_notional = float(_notional_snap.get("notional_explicit_fallback_usd") or 0.0)
    if notional <= 0.0:
        _audit(
            db, user_id=uid, alert=alert,
            decision="skipped", reason="entry_notional_unavailable",
            rule_snapshot=snap,
        )
        out["skipped"] += 1
        _autotrader_tick_note(
            out, kind="skipped", reason="entry_notional_unavailable", alert=alert,
        )
        return

    # The base notional is not floored or upsized here. If the risk budget
    # cannot buy the instrument's minimum trade unit, the entry is skipped.
    snap["notional_effective"] = round(notional, 2)
    _risk_pct = float(_notional_snap.get("notional_risk_pct") or 0.0)
    _fallback_equity = (
        fallback_notional / (_risk_pct / PERCENT_SCALE)
        if fallback_notional > 0 and _risk_pct > 0
        else fallback_notional
    )
    if _shadow_observation_only:
        snap["shadow_observation_lightweight_sizing_supported"] = (
            _shadow_observation_lightweight_sizing_supported
        )
        snap["shadow_observation_diagnostic_sizing_enabled"] = (
            _shadow_observation_diagnostic_sizing_enabled
        )
        snap["shadow_observation_sizing_mode"] = (
            SHADOW_OBSERVATION_SIZING_MODE_BASE_RISK
            if _skip_shadow_observation_advisory_sizing
            else SHADOW_OBSERVATION_SIZING_MODE_FULL_DIAGNOSTICS
        )
    if _skip_shadow_observation_advisory_sizing:
        snap["shadow_observation_advisory_sizing_skipped"] = True
        snap["shadow_observation_advisory_sizing_skip_reason"] = (
            SHADOW_OBSERVATION_ADVISORY_SIZING_SKIP_REASON
        )
        if not snap.get("options_path"):
            from .tick_normalizer import normalize_quantity

            qty_raw = notional / px
            qty = float(normalize_quantity(qty_raw, alert.ticker))
            snap["qty_source"] = "risk_notional_fractional"
            if qty <= 0 and px > 0:
                snap["qty_raw"] = round(qty_raw, QUANTITY_ROUND_DIGITS)
                _audit(
                    db, user_id=uid, alert=alert,
                    decision="skipped",
                    reason="notional_below_trade_unit",
                    rule_snapshot=snap,
                )
                out["skipped"] += 1
                _autotrader_tick_note(
                    out,
                    kind="skipped",
                    reason="notional_below_trade_unit",
                    alert=alert,
                )
                return
            snap["notional_effective"] = round(qty * px, MONEY_ROUND_DIGITS)
            snap["qty_raw"] = round(qty_raw, QUANTITY_ROUND_DIGITS)
            _record_shadow_observation_entry(
                db,
                uid=uid,
                alert=alert,
                qty=qty,
                px=px,
                snap=snap,
                llm_snap=llm_snap,
                live=live,
                out=out,
            )
            return

    # Q1.T5 — HRP shadow sizing (and live override when flag ON).
    # Always logged for shadow comparison; the chosen_sizing field of the
    # decision tells us which to honor. Naive fallback when HRP is
    # unavailable (insufficient history etc.) so flag-flip is safe.
    if not _skip_shadow_observation_advisory_sizing:
        try:
            from .hrp_sizing import decide_position_size as _hrp_decide
            _hrp_decision = _hrp_decide(
                db,
                symbol=(alert.ticker or "").upper(),
                account_equity_usd=float(equity if equity > 0 else _fallback_equity),
                user_id=uid,
            )
            snap["hrp_naive_size_usd"] = _hrp_decision.naive_size_usd
            snap["hrp_size_usd"] = _hrp_decision.hrp_size_usd
            snap["hrp_weight"] = _hrp_decision.hrp_weight
            snap["hrp_chosen_sizing"] = _hrp_decision.chosen_sizing
            snap["hrp_n_active_positions"] = _hrp_decision.n_active_positions
            if _hrp_decision.chosen_sizing == "hrp" and _hrp_decision.hrp_size_usd:
                # Flag is ON and HRP succeeded: override notional with HRP value.
                snap["notional_before_hrp"] = round(notional, 2)
                notional = float(_hrp_decision.hrp_size_usd)
                snap["notional_effective"] = round(notional, 2)
                snap["notional_source"] = "hrp_allocated"
        except Exception as _hrp_e:
            snap["hrp_error"] = str(_hrp_e)[:200]

    # K Phase 3 S.4 — survival-classifier sizing multiplier.
    # Composes AFTER HRP (so HRP allocates risk-parity weight, then K
    # nudges based on per-pattern survival probability). Always called,
    # logs to pattern_survival_decision_log; returns no_op when any of
    # the gates is OFF or no prediction exists. Failures are
    # deliberately swallowed — sizing must never crash the entry path.
    try:
        from .pattern_survival.decisions import compute_decision as _ps_decide
        _ps_result = _ps_decide(
            db,
            scan_pattern_id=int(alert.scan_pattern_id),
            consumer="sizing",
            input_context={"input_notional": float(notional)},
        )
        snap["ps_sizing_decision"] = _ps_result["decision"]
        snap["ps_sizing_predicted"] = _ps_result.get("predicted_survival")
        if _ps_result["decision"] == "apply":
            mult = float(_ps_result["details"]["multiplier"])
            snap["notional_before_ps_sizing"] = round(notional, 2)
            snap["ps_sizing_multiplier"] = mult
            notional = float(_ps_result["details"]["output_notional"])
            snap["notional_effective"] = round(notional, 2)
            snap["notional_source"] = (
                snap.get("notional_source", "unknown") + "+ps_sizing"
            )
        else:
            # no_op — multiplier was 1.0 effectively. Still surface
            # the skip_reason so the operator can confirm gating.
            snap["ps_sizing_skip_reason"] = (
                _ps_result.get("details") or {}
            ).get("skip_reason")
    except Exception as _ps_e:
        # Hard rule: sizing must never crash entry. Fall back to the
        # HRP-allocated notional unchanged.
        snap["ps_sizing_error"] = str(_ps_e)[:200]

    # Pilot-promoted patterns are broker-eligible, but not full-risk eligible.
    # Size them by the Bayesian confidence used by the shadow-vetting
    # finalizer. This runs after HRP/survival so pilot exposure cannot be
    # inflated back to normal promoted notional by downstream allocators.
    try:
        from .pattern_shadow_vetting import pilot_promoted_risk_multiplier

        _pilot_mult = pilot_promoted_risk_multiplier(
            db,
            int(alert.scan_pattern_id) if alert.scan_pattern_id else None,
        )
    except Exception as _pilot_e:
        _pilot_mult = None
        snap["pilot_sizing_error"] = str(_pilot_e)[:200]

    if _pilot_mult is not None:
        snap["pilot_promoted_risk_multiplier"] = round(float(_pilot_mult), 6)
        if float(_pilot_mult) <= 0.0:
            _audit(
                db, user_id=uid, alert=alert,
                decision="skipped",
                reason="pilot_promoted_confidence_below_policy",
                rule_snapshot=snap,
            )
            out["skipped"] += 1
            _autotrader_tick_note(
                out, kind="skipped",
                reason="pilot_promoted_confidence_below_policy", alert=alert,
            )
            return
        snap["notional_before_pilot_sizing"] = round(notional, 2)
        notional = float(notional) * float(_pilot_mult)
        snap["notional_effective"] = round(notional, 2)
        snap["notional_source"] = (
            snap.get("notional_source", "unknown") + "+pilot_confidence"
        )

    if snap.get("options_path") and snap.get("pilot_promoted_risk_multiplier") is not None:
        _audit(
            db, user_id=uid, alert=alert,
            decision="skipped",
            reason="pilot_promoted_options_path_blocked",
            rule_snapshot=snap,
        )
        out["skipped"] += 1
        _autotrader_tick_note(
            out, kind="skipped",
            reason="pilot_promoted_options_path_blocked", alert=alert,
        )
        return

    # f-stop-engine-payoff-ratio-gate (2026-05-19): payoff-ratio-aware
    # sizing scaler. Composes AFTER HRP / survival / pilot_promoted
    # multipliers; uses the Tier A scan_pattern.payoff_ratio +
    # payoff_ratio_n (mig 246, refreshed nightly by realized_stats_sync).
    # The multiplier is posterior-smoothed instead of threshold-cliffed:
    # thin samples shrink toward neutral 1.0x, while mature high-payoff
    # patterns earn size gradually up to the configured cap.
    # Wrapped in try/except: sizing must NEVER crash an entry attempt.
    # Default OFF; operator flips after paper-soak comparison.
    try:
        _po_enabled = bool(getattr(
            settings, "chili_autotrader_payoff_sizing_enabled", False,
        ))
    except Exception:
        _po_enabled = False

    if _po_enabled and getattr(alert, "scan_pattern_id", None):
        try:
            from sqlalchemy import text as _po_t
            _po_row = db.execute(_po_t(
                "SELECT payoff_ratio, payoff_ratio_n FROM scan_patterns "
                "WHERE id = :pid"
            ), {"pid": int(alert.scan_pattern_id)}).first()
            if _po_row is not None:
                _po_pr = float(_po_row[0]) if _po_row[0] is not None else None
                _po_pn = int(_po_row[1] or 0)
                from .payoff_sizing import compute_payoff_sizing
                _po_decision = compute_payoff_sizing(
                    payoff_ratio=_po_pr,
                    payoff_ratio_n=_po_pn,
                    min_n=int(getattr(settings, "chili_autotrader_payoff_min_n", 5)),
                    prior_ratio=float(getattr(
                        settings, "chili_autotrader_payoff_prior_ratio", 1.0,
                    )),
                    prior_n=int(getattr(settings, "chili_autotrader_payoff_prior_n", 20)),
                    min_multiplier=float(getattr(
                        settings, "chili_autotrader_payoff_min_multiplier", 0.5,
                    )),
                    max_multiplier=float(getattr(
                        settings, "chili_autotrader_payoff_max_multiplier", 1.5,
                    )),
                )
                _po_mult = float(_po_decision.multiplier)
                snap.update(_po_decision.to_snapshot())
                if _po_mult != 1.0:
                    snap["notional_before_payoff_sizing"] = round(notional, 2)
                    notional = float(notional) * _po_mult
                    snap["notional_effective"] = round(notional, 2)
                    snap["notional_source"] = (
                        snap.get("notional_source", "unknown") + "+payoff"
                    )
        except Exception as _po_e:
            snap["payoff_sizing_error"] = str(_po_e)[:200]

    # Canonical Kelly/cost/correlation sizer for stock/crypto entries. Options
    # keep their dedicated option-quality and contract-sizing path.
    if not snap.get("options_path"):
        try:
            from .position_sizer_emitter import EmitterSignal, emit_shadow_proposal
            from .position_sizer_writer import LegacySizing, mode_is_authoritative

            _asset_class = (
                "crypto" if (alert.asset_type or "").strip().lower() == "crypto"
                else "equity"
            )
            _psizer_result = emit_shadow_proposal(
                db,
                signal=EmitterSignal(
                    source="auto_trader.entry",
                    ticker=alert.ticker,
                    direction="long",
                    entry_price=float(px),
                    stop_price=float(alert.stop_loss) if alert.stop_loss is not None else 0.0,
                    capital=float(equity if equity > 0 else _fallback_equity),
                    target_price=float(alert.target_price) if alert.target_price is not None else None,
                    asset_class=_asset_class,
                    user_id=uid,
                    pattern_id=int(alert.scan_pattern_id) if alert.scan_pattern_id else None,
                    regime=getattr(alert, "regime_at_alert", None),
                    confidence=alert_confidence_from_score(alert),
                ),
                legacy=LegacySizing(
                    notional=float(notional),
                    quantity=None,
                    source=snap.get("notional_source", "autotrader_chain"),
                ),
            )
            if _psizer_result is not None:
                snap["position_sizer_proposal_id"] = _psizer_result.proposal_id
                snap["position_sizer_mode"] = _psizer_result.mode
                snap["position_sizer_proposed_notional"] = round(
                    float(_psizer_result.proposed_notional), 2
                )
                snap["position_sizer_divergence_bps"] = _psizer_result.divergence_bps
                if mode_is_authoritative():
                    if float(_psizer_result.proposed_notional) <= 0.0:
                        _audit(
                            db, user_id=uid, alert=alert,
                            decision="skipped",
                            reason="position_sizer_zero_notional",
                            rule_snapshot=snap,
                        )
                        out["skipped"] += 1
                        _autotrader_tick_note(
                            out, kind="skipped",
                            reason="position_sizer_zero_notional", alert=alert,
                        )
                        return
                    snap["notional_before_position_sizer"] = round(notional, 2)
                    notional = float(_psizer_result.proposed_notional)
                    snap["notional_effective"] = round(notional, 2)
                    snap["notional_source"] = (
                        snap.get("notional_source", "unknown")
                        + "+position_sizer_authoritative"
                    )
        except Exception as _psizer_e:
            snap["position_sizer_error"] = str(_psizer_e)[:200]

    if bool(getattr(alert, "_chili_probation_recert_allowed", False)):
        if snap.get("options_path") or (alert.asset_type or "").strip().lower() == "options":
            _audit(
                db, user_id=uid, alert=alert,
                decision="skipped",
                reason=PROBATION_OPTIONS_PATH_BLOCKED_REASON,
                rule_snapshot=snap,
            )
            out["skipped"] += 1
            _autotrader_tick_note(
                out,
                kind="skipped",
                reason=PROBATION_OPTIONS_PATH_BLOCKED_REASON,
                alert=alert,
            )
            return
        _quota_reason = _probation_quota_block_reason(
            db,
            uid=uid,
            pattern_id=int(alert.scan_pattern_id) if alert.scan_pattern_id else None,
            ticker=alert.ticker,
            asset_type=alert.asset_type,
            expected_net_pct=_expected_net_pct_from_snapshot(snap),
        )
        if _quota_reason is not None:
            _audit(
                db, user_id=uid, alert=alert,
                decision="skipped",
                reason=_quota_reason,
                rule_snapshot=snap,
            )
            out["skipped"] += 1
            _autotrader_tick_note(
                out, kind="skipped", reason=_quota_reason, alert=alert,
            )
            return
        _probation_mult = max(
            0.0,
            min(
                1.0,
                float(
                    getattr(
                        settings,
                        "chili_autotrader_probation_notional_multiplier",
                        PROBATION_DEFAULT_NOTIONAL_MULTIPLIER,
                    )
                    or 0.0
                ),
            ),
        )
        if _probation_mult <= 0.0:
            _audit(
                db, user_id=uid, alert=alert,
                decision="skipped",
                reason=PROBATION_NOTIONAL_UNAVAILABLE_REASON,
                rule_snapshot=snap,
            )
            out["skipped"] += 1
            _autotrader_tick_note(
                out,
                kind="skipped",
                reason=PROBATION_NOTIONAL_UNAVAILABLE_REASON,
                alert=alert,
            )
            return
        snap["notional_before_probation_sizing"] = round(notional, MONEY_ROUND_DIGITS)
        snap["probation_recert_notional_multiplier"] = round(
            _probation_mult,
            MULTIPLIER_ROUND_DIGITS,
        )
        notional = float(notional) * _probation_mult
        snap["notional_effective"] = round(notional, MONEY_ROUND_DIGITS)
        snap["notional_source"] = (
            snap.get("notional_source", "unknown") + "+probation_recert"
        )

    # Options use contract quantity from option_meta. Stock/crypto quantities
    # use the broker tick normalizer, so fractional sizing is preserved when
    # the venue supports it.
    if snap.get("options_path") and snap.get("option_meta"):
        _opt_meta = snap["option_meta"]
        qty = parse_contract_quantity(_opt_meta.get("quantity"))
        if qty is None:
            snap["options_quantity_error"] = "invalid_contract_quantity"
            _audit(
                db,
                user_id=uid,
                alert=alert,
                decision="skipped",
                reason="options_meta_invalid_quantity",
                rule_snapshot=snap,
            )
            out["skipped"] += 1
            _autotrader_tick_note(
                out,
                kind="skipped",
                reason="options_meta_invalid_quantity",
                alert=alert,
            )
            return
        try:
            _premium = float(_opt_meta.get("limit_price") or alert.entry_price or 0)
        except Exception:
            _premium = 0.0
        qty_raw = float(qty)  # populate for snapshot logging
        snap["qty_source"] = "options_meta"
        snap["notional_effective"] = round(_premium * 100.0 * qty, 2)
    else:
        from .tick_normalizer import normalize_quantity

        qty_raw = notional / px
        qty = float(normalize_quantity(qty_raw, alert.ticker))
        snap["qty_source"] = "risk_notional_fractional"

    if qty <= 0 and px > 0:
        if bool(getattr(alert, "_chili_probation_recert_allowed", False)):
            _audit(
                db,
                user_id=uid,
                alert=alert,
                decision="skipped",
                reason=PROBATION_NOTIONAL_BELOW_TRADE_UNIT_REASON,
                rule_snapshot=snap,
            )
            out["skipped"] += 1
            _autotrader_tick_note(
                out,
                kind="skipped",
                reason=PROBATION_NOTIONAL_BELOW_TRADE_UNIT_REASON,
                alert=alert,
            )
            return
        if snap.get("pilot_promoted_risk_multiplier") is not None:
            _audit(
                db, user_id=uid, alert=alert,
                decision="skipped",
                reason="pilot_promoted_notional_below_trade_unit",
                rule_snapshot=snap,
            )
            out["skipped"] += 1
            _autotrader_tick_note(
                out, kind="skipped",
                reason="pilot_promoted_notional_below_trade_unit", alert=alert,
            )
            return
        snap["qty_raw"] = round(qty_raw, QUANTITY_ROUND_DIGITS)
        _audit(
            db, user_id=uid, alert=alert,
            decision="skipped",
            reason="notional_below_trade_unit",
            rule_snapshot=snap,
        )
        out["skipped"] += 1
        _autotrader_tick_note(
            out, kind="skipped", reason="notional_below_trade_unit", alert=alert
        )
        return
    else:
        # qty > 0: stock/crypto notional is the tick-normalized spot cost.
        # Options keep the premium * contract-multiplier notional set above.
        if not snap.get("options_path"):
            snap["notional_effective"] = round(qty * px, 2)
        snap["qty_raw"] = round(qty_raw, QUANTITY_ROUND_DIGITS)

    if _shadow_observation_only:
        _record_shadow_observation_entry(
            db,
            uid=uid,
            alert=alert,
            qty=qty,
            px=px,
            snap=snap,
            llm_snap=llm_snap,
            live=live,
            out=out,
        )
        return

    if live:
        if bool(getattr(alert, "_chili_recert_required", False)):
            _reason = "pattern_recert_required"
            snap["paper_observation_reason"] = _reason
            _recert_fastlane = _queue_recert_for_blocked_signal(
                db,
                alert=alert,
                pattern=_pattern_row(db, alert.scan_pattern_id),
                reason=_reason,
            )
            if _recert_fastlane is not None:
                snap["recert_signal_fastlane"] = _recert_fastlane
            _audit(
                db,
                user_id=uid,
                alert=alert,
                decision="blocked",
                reason=_reason,
                rule_snapshot=snap,
                llm_snapshot=llm_snap,
            )
            out["skipped"] += 1
            _autotrader_tick_note(out, kind="blocked", reason=_reason, alert=alert)
            _maybe_open_paper_shadow(
                db,
                uid=uid,
                alert=alert,
                qty=qty,
                px=px,
                snap=snap,
                decision="blocked_recert_required",
            )
            return

        from .governance import is_kill_switch_active_for_session

        if is_kill_switch_active_for_session(db):
            _block_live_order(
                db,
                uid=uid,
                alert=alert,
                reason="kill_switch_activated_mid_flight",
                snap=snap,
                llm_snap=llm_snap,
                out=out,
            )
            return

        # FIX C1 (2026-04-29 third-pass audit): PDT-aware entry gate.
        # The third-pass audit found 1,333/1,349 monitor exits in 24h
        # rejected with "Sell may cause PDT designation." -- the autotrader
        # was opening positions it could not legally close intraday.
        # Refuse to open if we cannot prove either (a) account equity is
        # >= $25K (PDT does not apply), or (b) day_trades_5d < 3 (4th
        # would trigger). Per no-hardcoded-fallback: unknown state =>
        # refuse, never assume.
        #
        # R35 (2026-04-30): SEC PDT rule applies ONLY to margin-account
        # securities trades. Crypto is a 24/7 cash market and is exempt.
        # Pass ticker context so the gate can short-circuit for crypto.
        # Without this, post-R34 crypto candidates were 100% blocked by
        # 'pdt_limit_reached:43>=3' even though the 43 day-trades were
        # mostly crypto round-trips that PDT doesn't count.
        from .pdt_guard import can_open_intraday_round_trip
        _pdt_result = can_open_intraday_round_trip(
            db, user_id=uid, ticker=getattr(alert, "ticker", None),
        )
        if not _pdt_result.allowed:
            _pdt_audit = dict(snap)
            _pdt_audit["pdt_gate"] = _pdt_result.snapshot
            _audit(
                db, user_id=uid, alert=alert,
                decision="blocked",
                reason=f"pdt_guard:{_pdt_result.reason}",
                rule_snapshot=_pdt_audit,
                llm_snapshot=llm_snap,
            )
            out["blocked"] = out.get("blocked", 0) + 1
            _autotrader_tick_note(
                out, kind="blocked",
                reason=f"pdt_guard:{_pdt_result.reason}", alert=alert,
            )
            _maybe_open_paper_shadow(
                db, uid=uid, alert=alert, qty=qty, px=px,
                snap=snap, decision="blocked_pdt",
            )
            return

        res = _execute_broker_buy(
            db,
            uid=uid,
            alert=alert,
            qty=qty,
            client_order_id=f"atv1-{alert.id}-buy",
            snap=snap,
            llm_snap=llm_snap,
            out=out,
            px=px,
        )
        if res is None:
            return
        # Phantom-trade guard (deep audit 2026-04-28 CRIT #3): refuse to
        # insert a Trade row when the broker call returned ok=True but
        # didn't surface an order_id. Mig 201 cleaned up 7 such rows
        # (CRDL/CCCC/GEO/ELTX/JOB + ETH-USD trade 404). The original
        # bug was: ``broker_order_id=str(res.get("order_id") or "")``
        # silently coerces missing IDs to "" and creates a Trade that
        # the reconciler can never match. Treat missing order_id as a
        # broker failure even when ``ok=True``.
        order_id_raw = res.get("order_id") or ""
        if not str(order_id_raw).strip():
            _annotate_missing_order_id_broker_reject(
                alert,
                qty=float(qty),
                snap=snap,
                res=res,
            )
            _audit(
                db, user_id=uid, alert=alert,
                decision="blocked",
                reason="broker:place_no_order_id",
                rule_snapshot=snap,
                llm_snapshot=llm_snap,
            )
            out["skipped"] += 1
            _autotrader_tick_note(
                out, kind="blocked",
                reason="broker:place_no_order_id", alert=alert,
            )
            _maybe_open_paper_shadow(
                db, uid=uid, alert=alert, qty=qty, px=px,
                snap=snap, decision="blocked_no_order_id",
            )
            return
        fill = _entry_fill_price_from_response(res, alert, px=px, snap=snap)
        _broker_fill_price = _entry_broker_fill_price_from_response(res)

        # f-coinbase-autotrader-enablement-phase-4-bracket-writer-path
        # (2026-05-09): broker_source from the response side-channel.
        # Phase 3 + 4 Coinbase routing tags the response with
        # `_chili_broker_source='coinbase'`; everything else
        # (RH, options) defaults to 'robinhood' (BYTE-IDENTICAL with
        # the prior hardcoded value).
        _broker_source_for_trade = res.get("_chili_broker_source") or "robinhood"
        try:
            _broker_qty = float(res.get("base_size") or qty)
        except (TypeError, ValueError):
            _broker_qty = float(qty)
        _requested_broker_qty = float(_broker_qty)
        _entry_now = datetime.utcnow()
        _is_coinbase_entry = _broker_source_for_trade == "coinbase"
        _is_option_entry = bool(res.get("_chili_options_path") or snap.get("options_path"))
        _tca_ref_entry = _entry_tca_reference_price(
            res,
            alert,
            px=px,
            snap=snap,
            fill=fill,
        )
        _tca_ref_domain = "option_premium" if _is_option_entry else "underlying_spot"
        (
            _entry_status,
            _entry_broker_status,
            _entry_filled_qty,
            _entry_remaining_qty,
        ) = _entry_lifecycle_from_response(
            broker_source=_broker_source_for_trade,
            res=res,
            snap=snap,
            qty=float(_requested_broker_qty),
        )
        _broker_qty = _entry_quantity_for_trade(
            is_option_entry=_is_option_entry,
            requested_qty=float(_requested_broker_qty),
            entry_broker_status=_entry_broker_status,
            entry_filled_qty=_entry_filled_qty,
        )
        _entry_avg_fill_price = (
            _broker_fill_price
            if _entry_status == "open" or float(_entry_filled_qty or 0.0) > 0.0
            else None
        )
        if (
            _is_option_entry
            and _entry_status == "cancelled"
            and float(_entry_filled_qty or 0.0) <= 0.0
        ):
            _record_option_entry_no_fill_with_shadow(
                db,
                uid=uid,
                alert=alert,
                snap=snap,
                llm_snap=llm_snap,
                out=out,
                qty=float(_requested_broker_qty),
                px=px,
                entry_broker_status=_entry_broker_status,
            )
            return
        _option_terminal_partial_entry = (
            _is_option_entry
            and _entry_broker_status == "partially_filled_cancelled"
            and _entry_filled_qty is not None
            and _entry_filled_qty > 0
            and _requested_broker_qty > _entry_filled_qty + 1e-9
        )
        _entry_is_working = _entry_status == "working"
        _entry_is_async = _is_coinbase_entry or _is_option_entry
        _trade_stop, _trade_target, _managed_exit_execution = (
            _managed_edge_execution_levels(alert, px=px, snap=snap)
        )
        _option_meta_for_trade = (
            _normalize_option_meta_for_alert(
                alert,
                snap.get("option_meta") if isinstance(snap.get("option_meta"), dict) else {},
                underlying_price=px,
            )
            if _is_option_entry
            else {}
        )
        _entry_execution_snapshot = {
            "broker_source": _broker_source_for_trade,
            "asset_kind": "option" if _is_option_entry else None,
            "client_order_id": res.get("client_order_id"),
            "order_id": str(order_id_raw).strip(),
            "entry_quote_source": snap.get("entry_quote_source"),
            "entry_quote_prefetch_used": bool(snap.get("entry_quote_prefetch_used")),
            "entry_quote_fetch_path": snap.get("entry_quote_fetch_path"),
            "entry_quote_price": px,
            "order_type": (
                "limit_post_only"
                if _is_coinbase_entry and bool(res.get("_chili_maker_only"))
                else (
                    "option_limit"
                    if _is_option_entry
                    else ("market" if _is_coinbase_entry else None)
                )
            ),
            "active_order_type": (
                "limit_post_only"
                if _is_coinbase_entry and bool(res.get("_chili_maker_only"))
                else (
                    "option_limit"
                    if _is_option_entry
                    else ("market" if _is_coinbase_entry else None)
                )
            ),
            "option_order_state": res.get("_chili_option_order_state"),
            "option_position_verified": bool(res.get("_chili_option_position_verified")),
            "option_contract_key": _option_meta_for_trade.get("contract_key"),
            "option_occ_symbol": _option_meta_for_trade.get("occ_symbol"),
            "option_underlying": _option_meta_for_trade.get("underlying"),
            "option_price_domain": (
                "option_premium" if _is_option_entry else None
            ),
            "option_contract_multiplier": (
                _option_meta_for_trade.get("contract_multiplier")
                if _is_option_entry
                else None
            ),
            "underlying_price_at_entry": px if _is_option_entry else None,
            "option_limit_price": (
                _option_meta_for_trade.get("limit_price")
                if _is_option_entry
                else None
            ),
            "coinbase_maker_only": bool(res.get("_chili_maker_only")),
            "maker_limit_price": res.get("_chili_maker_limit_price") or res.get("limit_price"),
            "maker_best_bid": res.get("_chili_maker_bid"),
            "maker_best_ask": res.get("_chili_maker_ask"),
            "maker_price_increment": res.get("_chili_maker_price_increment"),
            "maker_improved_ticks": res.get("_chili_maker_improved_ticks"),
            "broker_base_size": _broker_qty,
            "broker_requested_size": _requested_broker_qty,
            "option_position_partial": bool(_option_terminal_partial_entry),
            "option_position_requested_quantity": (
                _requested_broker_qty if _option_terminal_partial_entry else None
            ),
            "option_position_quantity": (
                _broker_qty if _option_terminal_partial_entry else None
            ),
            "option_position_remaining_quantity": (
                0.0 if _option_terminal_partial_entry else None
            ),
            "option_position_residual_cancelled": (
                True if _option_terminal_partial_entry else None
            ),
            "option_entry_cancel_reason": (
                "partial_entry_cancelled_by_broker"
                if _option_terminal_partial_entry
                else None
            ),
            "tca_reference_entry_price": _tca_ref_entry,
            "tca_reference_domain": _tca_ref_domain,
            PROBATION_ENTRY_FLAG: bool(snap.get(PROBATION_ENTRY_FLAG)),
            "probation_recert_policy": snap.get("probation_recert_policy"),
            "probation_recert_notional_multiplier": snap.get(
                "probation_recert_notional_multiplier"
            ),
            "entry_edge_expected_net_pct": snap.get("entry_edge_expected_net_pct"),
            "cost_gate_fee_bps": snap.get("cost_gate_fee_bps"),
            "cost_gate_threshold_bps": snap.get("cost_gate_threshold_bps"),
            "cost_gate_tca_cost_bps": snap.get("cost_gate_tca_cost_bps"),
            "managed_exit_execution": _managed_exit_execution,
        }
        _trade_indicator_snapshot = {
            "breakout_alert": alert.indicator_snapshot,
            "signals": alert.signals_snapshot,
            "entry_execution": _entry_execution_snapshot,
        }
        if _is_option_entry:
            _trade_indicator_snapshot.update(
                {
                    "asset_type": "options",
                    "asset_kind": "option",
                    "options_path": True,
                    "option_meta": _option_meta_for_trade,
                    "option_contract_key": _option_meta_for_trade.get("contract_key"),
                    "price_domains": option_price_domains_snapshot(),
                }
            )
        # f-tca-writer-wiring (2026-05-18): capture the entry-side TCA
        # reference in the same price domain as the fill: underlying spot for
        # equities/crypto, option premium for options. The difference is the
        # entry slippage that ``apply_tca_on_trade_fill`` will compute
        # downstream when broker_sync runs.
        tr = Trade(
            user_id=uid,
            ticker=alert.ticker.upper(),
            direction="long",
            entry_price=fill,
            quantity=float(_broker_qty),
            entry_date=_entry_now,
            status=_entry_status,
            broker_status=_entry_broker_status,
            filled_quantity=_entry_filled_qty,
            remaining_quantity=_entry_remaining_qty,
            avg_fill_price=_entry_avg_fill_price,
            submitted_at=_entry_now if _entry_is_async else None,
            acknowledged_at=_entry_now if _entry_is_async else None,
            stop_loss=_trade_stop,
            take_profit=_trade_target,
            scan_pattern_id=alert.scan_pattern_id,
            related_alert_id=alert.id,
            broker_source=_broker_source_for_trade,
            management_scope=MANAGEMENT_SCOPE_AUTO_TRADER_V1,
            broker_order_id=str(order_id_raw).strip(),
            asset_kind="option" if _is_option_entry else None,
            indicator_snapshot=_trade_indicator_snapshot,
            tags="autotrader_v1 options" if _is_option_entry else "autotrader_v1",
            auto_trader_version=AUTOTRADER_VERSION,
            scale_in_count=0,
            tca_reference_entry_price=_tca_ref_entry,
        )
        db.add(tr)
        db.commit()
        db.refresh(tr)
        if _is_option_entry:
            _detach_mismatched_option_position_link(db, tr)
        # f-tca-writer-wiring (2026-05-18): compute entry slippage NOW, using
        # the same-domain reference and the broker's actual fill price.
        # Without this,
        # the apply_tca_on_trade_fill call in broker_service is the only
        # writer — and it depends on broker_sync re-touching the trade later.
        # Wrap in try/except per the existing tca pattern in this file.
        try:
            from .tca_service import apply_tca_on_trade_fill
            if tr.status == "open" and _entry_avg_fill_price is not None:
                apply_tca_on_trade_fill(tr, fill_price=_entry_avg_fill_price)
                db.commit()
        except Exception:
            logger.debug(
                "[autotrader] entry TCA write failed (non-fatal) for trade_id=%s",
                getattr(tr, "id", None), exc_info=True,
            )
        # Phase 2C: emit trade_lifecycle entry event and save correlation_id
        # on the Trade. On close, plasticity uses this to look up the path log
        # and reinforce/attenuate the edges that carried the signal.
        try:
            from .brain_neural_mesh.publisher import publish_trade_lifecycle

            entry_corr = None
            if tr.status == "open":
                entry_corr = publish_trade_lifecycle(
                    db,
                    trade_id=int(tr.id),
                    ticker=tr.ticker,
                    transition="entry",
                    broker_source=_broker_source_for_trade,
                    quantity=float(tr.quantity),
                    price=float(fill),
                )
            if entry_corr:
                tr.mesh_entry_correlation_id = entry_corr
                db.commit()
        except Exception:
            # Post-entry plasticity / mesh correlation is best-effort — a
            # failure must not undo a successful order placement. Log at
            # DEBUG so the silent-swallow audit stays clean while still
            # leaving a trail in the app log for ops follow-up.
            logger.debug(
                "[autotrader] plasticity mesh correlation post-entry failed "
                "(non-fatal) for trade_id=%s",
                getattr(tr, "id", None),
                exc_info=True,
            )
        _audit(
            db,
            user_id=uid,
            alert=alert,
            decision="placed",
            reason="submitted" if _entry_is_working else "ok",
            rule_snapshot=snap,
            llm_snapshot=llm_snap,
            trade_id=tr.id,
        )
        out["placed"] += 1
        _autotrader_tick_note(
            out,
            kind="placed",
            reason=(
                f"live_{_broker_source_for_trade}_submitted"
                if _entry_is_working else f"live_{_broker_source_for_trade}"
            ),
            alert=alert,
        )
        _maybe_open_paper_shadow(
            db, uid=uid, alert=alert, qty=_broker_qty, px=px,
            snap=snap, decision="placed",
        )
        return

    # Paper
    from .paper_trading import open_paper_trade

    sig = {
        "auto_trader_v1": True,
        "breakout_alert_id": alert.id,
        "projected": snap.get("projected_profit_pct"),
    }
    paper_entry_px, option_paper_sig = _paper_entry_context_for_alert(
        alert,
        px=px,
        snap=snap,
    )
    if option_paper_sig:
        sig.update(option_paper_sig)
        if paper_entry_px is None:
            _audit(
                db,
                user_id=uid,
                alert=alert,
                decision="blocked",
                reason="option_paper_entry_premium_unavailable",
                rule_snapshot=snap,
                llm_snapshot=llm_snap,
            )
            out["skipped"] += 1
            _autotrader_tick_note(
                out,
                kind="blocked",
                reason="option_paper_entry_premium_unavailable",
                alert=alert,
            )
            return
    paper_stop, paper_target, managed_exit_execution = (
        _managed_edge_execution_levels(alert, px=px, snap=snap)
    )
    if option_paper_sig:
        paper_stop = None
        paper_target = None
    if managed_exit_execution is not None:
        sig["managed_exit_execution"] = managed_exit_execution
    pt = open_paper_trade(
        db,
        uid,
        alert.ticker,
        paper_entry_px,
        scan_pattern_id=alert.scan_pattern_id,
        stop_price=paper_stop,
        target_price=paper_target,
        direction="long",
        quantity=float(qty),
        signal_json=sig,
    )
    if pt is None:
        _audit(
            db,
            user_id=uid,
            alert=alert,
            decision="blocked",
            reason="paper_open_failed",
            rule_snapshot=snap,
            llm_snapshot=llm_snap,
        )
        out["skipped"] += 1
        _autotrader_tick_note(out, kind="blocked", reason="paper_open_failed", alert=alert)
        return

    _audit(
        db,
        user_id=uid,
        alert=alert,
        decision="placed",
        reason="paper",
        rule_snapshot=snap,
        llm_snapshot=llm_snap,
        trade_id=None,
    )
    out["placed"] += 1
    _autotrader_tick_note(out, kind="placed", reason="paper", alert=alert)

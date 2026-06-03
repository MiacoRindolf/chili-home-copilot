"""All-asset cash deployment diagnostics.

This layer ranks *only* live-eligible edge-supply rows. It is intentionally
read-only: it never promotes patterns, never clears recert debt, and never
routes an order. Blocked positive-EV rows become safer work recommendations
instead of live shortcuts.
"""
from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from ...config import settings
from ...models.trading import BrainWorkEvent, BreakoutAlert
from .asset_class import (
    PATTERN_ASSET_CLASS_CRYPTO,
    PATTERN_ASSET_CLASS_OPTIONS,
    PATTERN_ASSET_CLASS_STOCKS,
    normalize_pattern_asset_class,
)
from .edge_reliability import (
    DEFAULT_TOP_LIMIT,
    DEFAULT_WINDOW_DAYS,
    EDGE_RELIABILITY_REFRESH,
    EXIT_VARIANT_DIAGNOSTIC,
    EXIT_VARIANT_REFRESH,
    PROVENANCE_BACKFILL,
    RECERT_RESCUE_REFRESH,
    edge_supply_rows,
    edge_supply_snapshot_rows,
    emit_edge_reliability_refresh_requested,
    emit_targeted_profitability_work,
    latest_edge_reliability_snapshot_slices,
    null_lineage_short_paper_candidates,
)
from .portfolio_risk import get_risk_limits, _option_premium_risk_dollars
from .return_math import (
    trade_realized_pnl as _realized_trade_pnl,
    trade_return_pct as _realized_trade_return_pct,
)

LIVE_LIFECYCLES = frozenset({"live", "promoted", "pilot_promoted"})
RECERT_BLOCKERS = frozenset({"recert_blocked", "hard_recert_blocked"})
EXECUTION_BLOCKERS = frozenset({"execution_blocked"})
SHADOW_BLOCKERS = frozenset({"shadow_evidence_collection"})


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _round(value: float | None, digits: int = 6) -> float | None:
    return round(float(value), digits) if value is not None and math.isfinite(float(value)) else None


def _mean(values: list[float]) -> float | None:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    return (sum(clean) / len(clean)) if clean else None


def _canonical_asset_class(value: Any) -> str | None:
    raw = str(value or "").strip().lower()
    if raw in {"all", "unknown", ""}:
        return None
    if raw in {"coin", "coinbase_spot"}:
        return "crypto"
    normalized = normalize_pattern_asset_class(raw)
    if normalized == PATTERN_ASSET_CLASS_STOCKS:
        return "stock"
    if normalized == PATTERN_ASSET_CLASS_CRYPTO:
        return "crypto"
    if normalized == PATTERN_ASSET_CLASS_OPTIONS:
        return "options"
    return raw


def _dominant_counter_key(value: Any) -> str | None:
    if not isinstance(value, dict) or not value:
        return None
    pairs: list[tuple[str, int]] = []
    for key, count in value.items():
        try:
            pairs.append((str(key), int(count)))
        except (TypeError, ValueError):
            continue
    if not pairs:
        return None
    pairs.sort(key=lambda item: item[1], reverse=True)
    return pairs[0][0]


def _asset_class_for_row(row: dict[str, Any]) -> str:
    direct = _canonical_asset_class(row.get("asset_class"))
    if direct:
        return direct
    from_counts = _canonical_asset_class(_dominant_counter_key(row.get("asset_types")))
    if from_counts:
        return from_counts
    symbol = str(row.get("primary_symbol") or "").strip().upper()
    if symbol.endswith("-USD"):
        return "crypto"
    return "stock"


def _symbol_for_row(row: dict[str, Any]) -> str | None:
    symbol = str(row.get("primary_symbol") or "").strip().upper()
    if symbol:
        return symbol
    tickers = row.get("tickers")
    if isinstance(tickers, dict):
        dom = _dominant_counter_key(tickers)
        if dom:
            return dom.strip().upper()
    return None


def _cost_pct(asset_class: str, row: dict[str, Any]) -> float:
    if asset_class == "crypto":
        base = _safe_float(getattr(settings, "chili_cash_deployment_crypto_cost_pct", 0.25), 0.25) or 0.0
    elif asset_class == "options":
        base = _safe_float(getattr(settings, "chili_cash_deployment_options_cost_pct", 1.0), 1.0) or 0.0
    elif asset_class == "stock":
        base = _safe_float(getattr(settings, "chili_cash_deployment_equity_cost_pct", 0.05), 0.05) or 0.0
    else:
        base = _safe_float(getattr(settings, "chili_cash_deployment_unknown_asset_cost_pct", 0.35), 0.35) or 0.0

    slip_rate = _safe_float(row.get("slippage_miss_rate"), 0.0) or 0.0
    reject_rate = _safe_float(row.get("broker_reject_rate"), 0.0) or 0.0
    slip_penalty = _safe_float(
        getattr(settings, "chili_cash_deployment_slippage_miss_penalty_pct", 0.5),
        0.5,
    ) or 0.0
    reject_penalty = _safe_float(
        getattr(settings, "chili_cash_deployment_broker_reject_penalty_pct", 1.0),
        1.0,
    ) or 0.0
    return max(0.0, base + (slip_rate * slip_penalty) + (reject_rate * reject_penalty))


def _venue_readiness(asset_class: str) -> tuple[str, float, str | None]:
    live_enabled = bool(getattr(settings, "chili_autotrader_live_enabled", True))
    if not live_enabled:
        return "live_disabled", 0.0, "autotrader_live_disabled"

    if asset_class == "crypto":
        if bool(getattr(settings, "chili_autotrader_crypto_enabled", False)):
            return "crypto_live_enabled", 1.0, None
        return "crypto_live_disabled", 0.0, "crypto_live_disabled"
    if asset_class == "options":
        options_enabled = bool(getattr(settings, "chili_autotrader_options_enabled", False))
        venue_enabled = bool(getattr(settings, "chili_options_venue_robinhood_enabled", options_enabled))
        if options_enabled and venue_enabled:
            return "options_live_enabled", 1.0, None
        return "options_live_disabled", 0.0, "options_live_disabled"
    return "equity_live_enabled", 1.0, None


def _correlation_bucket(symbol: str | None, asset_class: str) -> str:
    sym = (symbol or "").strip().upper()
    if asset_class == "crypto":
        base = sym.split("-")[0] if sym else "unknown"
        return f"crypto:{base}"
    if asset_class == "options":
        underlying = sym.split()[0] if sym else "unknown"
        return f"options:{underlying}"
    return f"{asset_class}:{sym[:1] or 'x'}"


def _trade_asset_class(trade: Any) -> str:
    try:
        from .autopilot_scope import is_option_trade

        if is_option_trade(trade):
            return "options"
    except Exception:
        pass
    explicit = _canonical_asset_class(getattr(trade, "asset_kind", None))
    if explicit:
        return explicit
    symbol = str(getattr(trade, "ticker", "") or "").strip().upper()
    if symbol.endswith("-USD"):
        return "crypto"
    snap = getattr(trade, "indicator_snapshot", None)
    if isinstance(snap, dict):
        explicit = _canonical_asset_class(snap.get("asset_class"))
        if explicit:
            return explicit
        explicit = _canonical_asset_class(snap.get("asset_type"))
        if explicit:
            return explicit
        breakout = snap.get("breakout_alert")
        if isinstance(breakout, dict):
            explicit = _canonical_asset_class(breakout.get("asset_class"))
            if explicit:
                return explicit
            explicit = _canonical_asset_class(breakout.get("asset_type"))
            if explicit:
                return explicit
    return "stock"


def _trade_heat_pct(trade: Any, *, capital: float) -> float:
    if capital <= 0.0:
        return 0.0
    if _trade_asset_class(trade) == "options":
        risk = _option_premium_risk_dollars(trade)
        if risk is not None:
            return max(0.0, risk / capital * 100.0)
    entry = _safe_float(getattr(trade, "entry_price", None), 0.0) or 0.0
    qty = _safe_float(getattr(trade, "quantity", None), 0.0) or 0.0
    stop = _safe_float(getattr(trade, "stop_loss", None))
    if entry <= 0.0 or qty <= 0.0 or stop is None:
        return 0.0
    multiplier = 100.0 if _trade_asset_class(trade) == "options" else 1.0
    risk = abs(entry - stop) * qty * multiplier
    return max(0.0, risk / capital * 100.0)


def _trade_return_pct(trade: Any) -> float | None:
    realized = _realized_trade_return_pct(trade)
    if realized is not None:
        return realized
    if _trade_asset_class(trade) == "options":
        return None
    pnl = _safe_float(getattr(trade, "pnl", None))
    entry = (
        _safe_float(getattr(trade, "avg_fill_price", None))
        or _safe_float(getattr(trade, "entry_price", None))
    )
    qty = (
        _safe_float(getattr(trade, "filled_quantity", None))
        or _safe_float(getattr(trade, "quantity", None))
    )
    notional = abs((entry or 0.0) * (qty or 0.0))
    if pnl is None or notional <= 0.0:
        return None
    return (pnl / notional) * 100.0


def _trade_realized_pnl_usd(trade: Any) -> float | None:
    realized = _realized_trade_pnl(trade)
    if realized is not None:
        return realized
    return _safe_float(getattr(trade, "pnl", None))


def _live_asset_performance(
    db: Session,
    *,
    scan_pattern_id: int | None,
    asset_class: str,
    user_id: int | None,
    window_days: int,
) -> dict[str, Any]:
    if scan_pattern_id is None:
        return {
            "live_realized_asset_window_days": int(window_days),
            "live_realized_asset_closed_count": 0,
            "live_realized_asset_pnl_usd": 0.0,
            "live_realized_asset_avg_return_pct": None,
            "live_realized_asset_win_rate": None,
            "live_realized_asset_last_exit_at": None,
        }

    from ...models.trading import Trade

    cutoff = datetime.utcnow() - timedelta(days=max(1, int(window_days)))
    q = (
        db.query(Trade)
        .filter(Trade.scan_pattern_id == int(scan_pattern_id))
        .filter(Trade.status == "closed")
        .filter(
            or_(
                Trade.exit_date >= cutoff,
                and_(Trade.exit_date.is_(None), Trade.entry_date >= cutoff),
            )
        )
    )
    if user_id is not None:
        q = q.filter(Trade.user_id == user_id)
    rows = [row for row in q.all() if _trade_asset_class(row) == asset_class]

    outcome_rows: list[tuple[Any, float]] = []
    for row in rows:
        ret = _trade_return_pct(row)
        if ret is not None:
            outcome_rows.append((row, ret))
    returns = [ret for _, ret in outcome_rows]
    pnl_values = [
        _trade_realized_pnl_usd(row) or 0.0
        for row, _ in outcome_rows
    ]
    wins = [1.0 if ret > 0.0 else 0.0 for _, ret in outcome_rows]
    latest = max(
        (
            getattr(row, "exit_date", None) or getattr(row, "entry_date", None)
            for row, _ in outcome_rows
        ),
        default=None,
    )
    return {
        "live_realized_asset_window_days": int(window_days),
        "live_realized_asset_closed_count": len(outcome_rows),
        "live_realized_asset_pnl_usd": _round(sum(pnl_values), 6),
        "live_realized_asset_avg_return_pct": (
            _round(sum(returns) / len(returns), 6) if returns else None
        ),
        "live_realized_asset_win_rate": (
            _round(sum(wins) / len(wins), 6) if wins else None
        ),
        "live_realized_asset_last_exit_at": latest.isoformat() if latest else None,
    }


def _exposure_and_notional(
    db: Session,
    *,
    user_id: int | None,
    asset_class: str,
) -> tuple[str | None, float, dict[str, Any]]:
    capital = _safe_float(getattr(settings, "chili_autotrader_assumed_capital_usd", 25_000.0), 25_000.0) or 0.0
    limits = get_risk_limits()
    from ...models.trading import Trade

    open_trades = (
        db.query(Trade)
        .filter(Trade.user_id == user_id, Trade.status == "open")
        .all()
    )
    stale_broker_rows: list[dict[str, Any]] = []
    try:
        from .broker_position_truth import filter_broker_stale_open_trades

        open_trades, stale_broker_rows = filter_broker_stale_open_trades(db, open_trades)
    except Exception:
        stale_broker_rows = []

    counts: Counter[str] = Counter(_trade_asset_class(row) for row in open_trades)
    total_heat = sum(_trade_heat_pct(row, capital=capital) for row in open_trades)
    available_heat = max(0.0, limits.max_portfolio_heat_pct - total_heat)

    blocker: str | None = None
    if len(open_trades) >= limits.max_open_positions:
        blocker = "max_open_positions"
    elif total_heat >= limits.max_portfolio_heat_pct:
        blocker = "portfolio_heat_cap"
    elif asset_class == "crypto" and counts.get("crypto", 0) >= limits.max_crypto_positions:
        blocker = "crypto_position_cap"
    elif asset_class == "stock" and counts.get("stock", 0) >= limits.max_stock_positions:
        blocker = "stock_position_cap"
    elif available_heat <= 0.0:
        blocker = "portfolio_heat_cap"

    explicit_notional = _safe_float(getattr(settings, "chili_autotrader_per_trade_notional_usd", 0.0), 0.0) or 0.0
    base_notional = explicit_notional
    if base_notional <= 0.0 and capital > 0.0:
        base_notional = capital * max(0.0, limits.max_risk_per_trade_pct) / 100.0

    if asset_class == "crypto":
        cap = _safe_float(getattr(settings, "chili_coinbase_max_notional_usd", 0.0), 0.0) or 0.0
        if cap > 0.0:
            base_notional = min(base_notional, cap) if base_notional > 0.0 else cap
    elif asset_class == "options":
        cap = _safe_float(getattr(settings, "chili_autotrader_options_max_contract_notional_usd", 0.0), 0.0) or 0.0
        if cap > 0.0:
            base_notional = min(base_notional, cap) if base_notional > 0.0 else cap

    heat_fraction = 1.0
    if limits.max_risk_per_trade_pct > 0:
        heat_fraction = _clamp(available_heat / limits.max_risk_per_trade_pct)
    max_safe = 0.0 if blocker else max(0.0, base_notional * heat_fraction)
    exposure = {
        "open_positions": len(open_trades),
        "stock_positions": int(counts.get("stock", 0)),
        "crypto_positions": int(counts.get("crypto", 0)),
        "option_positions": int(counts.get("options", 0)),
        "total_heat_pct": round(total_heat, 6),
        "available_heat_pct": round(available_heat, 6),
        "max_open_positions": limits.max_open_positions,
        "max_stock_positions": limits.max_stock_positions,
        "max_crypto_positions": limits.max_crypto_positions,
        "max_risk_per_trade_pct": limits.max_risk_per_trade_pct,
        "stale_broker_open_positions": len(stale_broker_rows),
        "stale_broker_positions": stale_broker_rows,
        "stale_broker_symbols": sorted(
            {
                str(row.get("ticker") or "").strip().upper()
                for row in stale_broker_rows
                if row.get("ticker")
            }
        ),
    }
    return blocker, round(max_safe, 6), exposure


def _cash_category(
    row: dict[str, Any],
    *,
    calibrated_ev_after_cost: float | None,
    exposure_blocker: str | None,
    execution_blocker: str | None,
) -> str:
    blocker = str(row.get("graduation_blocker") or "").strip().lower()
    lifecycle = str(row.get("lifecycle_stage") or "").strip().lower()
    expected = _safe_float(row.get("expected_ev_pct"))
    calibrated = _safe_float(row.get("calibrated_ev_pct"))
    positive_supply = (
        (calibrated is not None and calibrated > 0.0)
        or (expected is not None and expected > 0.0)
        or _safe_int(row.get("positive_expected_edge_count")) > 0
    )

    if bool(row.get("stale_broker_position")):
        return "stale_broker_local_open"
    if calibrated_ev_after_cost is None or calibrated_ev_after_cost <= 0.0:
        return "negative_ev"
    if blocker in RECERT_BLOCKERS or bool(row.get("recert_required")):
        return "positive_ev_recert" if positive_supply else "negative_ev"
    if blocker in EXECUTION_BLOCKERS or _safe_int(row.get("broker_reject_count")) > 0:
        return "positive_ev_execution_blocked" if positive_supply else "negative_ev"
    if blocker in SHADOW_BLOCKERS or lifecycle not in LIVE_LIFECYCLES:
        return "positive_ev_shadow" if positive_supply else "negative_ev"

    min_closed = int(getattr(settings, "chili_cash_deployment_min_closed_evidence", 5) or 0)
    closed_n = _safe_int(row.get("closed_evidence_count"))
    brier = _safe_float(row.get("brier_score"))
    max_brier = _safe_float(getattr(settings, "chili_cash_deployment_max_brier_score", 0.28), 0.28) or 0.28
    gap = _safe_float(row.get("paper_live_gap_pct"))
    max_gap = _safe_float(
        getattr(settings, "chili_cash_deployment_max_abs_paper_live_gap_pct", 3.0),
        3.0,
    ) or 0.0

    if blocker == "needs_more_closed_evidence" or closed_n < min_closed:
        return "needs_calibration"
    if brier is None or brier > max_brier:
        return "needs_calibration"
    if gap is not None and max_gap > 0.0 and abs(gap) > max_gap:
        return "needs_calibration"
    live_closed_n = _safe_int(row.get("live_realized_asset_closed_count"))
    live_avg = _safe_float(row.get("live_realized_asset_avg_return_pct"))
    if live_closed_n > 0 and live_avg is not None and live_avg <= 0.0:
        return "needs_calibration" if positive_supply else "negative_ev"
    if exposure_blocker or execution_blocker:
        return "positive_ev_execution_blocked"
    if blocker == "graduation_ready":
        return "live_deployable"
    if blocker == "quality_blocked":
        return "negative_ev"
    return "needs_calibration"


def _recommended_work_event(row: dict[str, Any], category: str) -> str:
    if category == "needs_provenance":
        return PROVENANCE_BACKFILL
    if category == "stale_broker_local_open":
        return EDGE_RELIABILITY_REFRESH
    if category == "positive_ev_recert":
        return RECERT_RESCUE_REFRESH
    if category == "positive_ev_shadow":
        closed_n = _safe_int(row.get("closed_evidence_count"))
        min_closed = int(getattr(settings, "chili_cash_deployment_min_closed_evidence", 5) or 0)
        return EXIT_VARIANT_REFRESH if closed_n >= min_closed else EDGE_RELIABILITY_REFRESH
    if category == "negative_ev":
        return EXIT_VARIANT_REFRESH
    if category == "needs_calibration":
        return EDGE_RELIABILITY_REFRESH
    return str(row.get("recommended_work_event") or EDGE_RELIABILITY_REFRESH)


def _execution_blocker_for_row(
    row: dict[str, Any],
    *,
    venue_blocker: str | None,
) -> str | None:
    if venue_blocker:
        return venue_blocker
    if _safe_int(row.get("broker_reject_count")) > 0:
        return "broker_rejects"
    if _safe_int(row.get("slippage_miss_count")) > 0:
        return "missed_entry_slippage"
    if str(row.get("graduation_blocker") or "").strip().lower() in EXECUTION_BLOCKERS:
        return "execution_blocked"
    return None


def _allocation_score(
    row: dict[str, Any],
    *,
    calibrated_ev_after_cost: float | None,
    venue_score: float,
    exposure_blocker: str | None,
) -> float:
    ev_component = _clamp((calibrated_ev_after_cost or 0.0) / 5.0)
    closed_n = _safe_int(row.get("closed_evidence_count"))
    min_closed = max(1, int(getattr(settings, "chili_cash_deployment_min_closed_evidence", 5) or 5))
    evidence_component = _clamp(closed_n / (min_closed * 4.0))
    brier = _safe_float(row.get("brier_score"))
    max_brier = _safe_float(getattr(settings, "chili_cash_deployment_max_brier_score", 0.28), 0.28) or 0.28
    calibration_component = 0.35 if brier is None else _clamp(1.0 - (brier / max_brier))
    realized_component = _clamp((_safe_float(row.get("realized_ev_pct"), 0.0) or 0.0) / 5.0)
    live_closed_n = _safe_int(row.get("live_realized_asset_closed_count"))
    live_avg = _safe_float(row.get("live_realized_asset_avg_return_pct"))
    live_component = 0.35 if live_closed_n <= 0 or live_avg is None else _clamp(live_avg / 5.0)
    gap = _safe_float(row.get("paper_live_gap_pct"))
    max_gap = _safe_float(
        getattr(settings, "chili_cash_deployment_max_abs_paper_live_gap_pct", 3.0),
        3.0,
    ) or 3.0
    gap_component = 1.0 if gap is None else _clamp(1.0 - (abs(gap) / max_gap))
    exposure_component = 0.0 if exposure_blocker else 1.0
    score = (
        ev_component * 0.30
        + evidence_component * 0.16
        + calibration_component * 0.16
        + realized_component * 0.10
        + live_component * 0.10
        + gap_component * 0.08
        + venue_score * 0.06
        + exposure_component * 0.04
    )
    return round(_clamp(score) * 100.0, 4)


def annotate_cash_deployment_row(
    db: Session,
    row: dict[str, Any],
    *,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Add cash-deployment diagnostics to one edge-supply row."""
    out = dict(row)
    asset_class = _asset_class_for_row(out)
    symbol = _symbol_for_row(out)
    live_perf = _live_asset_performance(
        db,
        scan_pattern_id=_safe_int(out.get("scan_pattern_id")) if out.get("scan_pattern_id") is not None else None,
        asset_class=asset_class,
        user_id=user_id,
        window_days=max(1, int(out.get("window_days") or DEFAULT_WINDOW_DAYS)),
    )
    out.update(live_perf)
    cost = _cost_pct(asset_class, out)
    calibrated = _safe_float(out.get("calibrated_ev_pct"))
    after_cost = calibrated - cost if calibrated is not None else None
    venue, venue_score, venue_blocker = _venue_readiness(asset_class)
    exposure_blocker, max_notional, exposure = _exposure_and_notional(
        db,
        user_id=user_id,
        asset_class=asset_class,
    )
    execution_blocker = _execution_blocker_for_row(out, venue_blocker=venue_blocker)
    stale_symbols = set(exposure.get("stale_broker_symbols") or [])
    stale_match = bool(symbol and symbol in stale_symbols)
    if stale_match:
        stale_detail = next(
            (
                item
                for item in exposure.get("stale_broker_positions", [])
                if str(item.get("ticker") or "").strip().upper() == symbol
            ),
            None,
        )
        out.update(
            {
                "broker_truth_status": "stale",
                "broker_truth_reason": (
                    stale_detail.get("reason") if stale_detail else "stale_broker_position"
                ),
                "stale_broker_position": True,
                "stale_reconciled_at": (
                    stale_detail.get("stale_reconciled_at") if stale_detail else None
                ),
            }
        )
    else:
        out.setdefault("broker_truth_status", "live_or_not_applicable")
        out.setdefault("broker_truth_reason", None)
        out.setdefault("stale_broker_position", False)
        out.setdefault("stale_reconciled_at", None)
    category = _cash_category(
        out,
        calibrated_ev_after_cost=after_cost,
        exposure_blocker=exposure_blocker,
        execution_blocker=execution_blocker,
    )
    work_event = _recommended_work_event(out, category)
    evidence_value = max(0.0, after_cost or 0.0) * math.log1p(
        max(0, _safe_int(out.get("edge_eval_count")) + _safe_int(out.get("closed_evidence_count")))
    )
    if category == "positive_ev_recert":
        evidence_value *= 1.25
    elif category == "positive_ev_shadow":
        evidence_value *= 1.1
    elif category == "positive_ev_execution_blocked":
        evidence_value *= 0.8

    out.update(
        {
            "asset_class": asset_class,
            "primary_symbol": symbol,
            "calibrated_ev_after_cost_pct": _round(after_cost, 6),
            "estimated_execution_cost_pct": _round(cost, 6),
            "allocation_score": _allocation_score(
                out,
                calibrated_ev_after_cost=after_cost,
                venue_score=venue_score,
                exposure_blocker=exposure_blocker,
            ),
            "cash_deployment_category": category,
            "live_deployable": category == "live_deployable",
            "max_safe_notional": max_notional if category == "live_deployable" else 0.0,
            "venue_readiness": venue,
            "venue_readiness_score": venue_score,
            "correlation_bucket": _correlation_bucket(symbol, asset_class),
            "exposure_blocker": exposure_blocker,
            "execution_blocker": execution_blocker,
            "recert_blocker": out.get("recert_reason") if category == "positive_ev_recert" else None,
            "recommended_work_event": work_event,
            "expected_evidence_value": round(evidence_value, 6),
            "portfolio_exposure": exposure,
        }
    )
    return out


def cash_deployment_rows(
    db: Session,
    *,
    user_id: int | None = None,
    pattern_ids: list[int] | set[int] | tuple[int, ...] | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    limit: int = DEFAULT_TOP_LIMIT,
) -> list[dict[str, Any]]:
    base = edge_supply_rows(
        db,
        pattern_ids=pattern_ids,
        window_days=window_days,
        limit=max(1, int(limit) * 4),
    )
    rows = [
        annotate_cash_deployment_row(db, row, user_id=user_id)
        for row in base
    ]
    rows.sort(
        key=lambda row: (
            1 if row.get("live_deployable") else 0,
            _safe_float(row.get("allocation_score"), 0.0) or 0.0,
            _safe_float(row.get("calibrated_ev_after_cost_pct"), -999.0) or -999.0,
            _safe_int(row.get("closed_evidence_count")),
            _safe_float(row.get("expected_evidence_value"), 0.0) or 0.0,
        ),
        reverse=True,
    )
    rank = 0
    for row in rows:
        if row.get("live_deployable"):
            rank += 1
            row["cash_deployment_rank"] = rank
        else:
            row["cash_deployment_rank"] = None
    return rows[: max(1, int(limit))]


def cash_deployment_snapshot_rows(
    db: Session,
    *,
    user_id: int | None = None,
    pattern_ids: list[int] | set[int] | tuple[int, ...] | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    limit: int = DEFAULT_TOP_LIMIT,
) -> list[dict[str, Any]]:
    base = edge_supply_snapshot_rows(
        db,
        pattern_ids=pattern_ids,
        window_days=window_days,
        limit=max(1, int(limit) * 4),
    )
    rows = [
        annotate_cash_deployment_row(db, row, user_id=user_id)
        for row in base
    ]
    rows.sort(
        key=lambda row: (
            1 if row.get("live_deployable") else 0,
            _safe_float(row.get("allocation_score"), 0.0) or 0.0,
            _safe_float(row.get("calibrated_ev_after_cost_pct"), -999.0) or -999.0,
            _safe_int(row.get("closed_evidence_count")),
            _safe_float(row.get("expected_evidence_value"), 0.0) or 0.0,
            str(row.get("snapshot_created_at") or ""),
        ),
        reverse=True,
    )
    rank = 0
    for row in rows:
        if row.get("live_deployable"):
            rank += 1
            row["cash_deployment_rank"] = rank
        else:
            row["cash_deployment_rank"] = None
    return rows[: max(1, int(limit))]


def cash_deployment_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    categories: Counter[str] = Counter(str(row.get("cash_deployment_category") or "unknown") for row in rows)
    assets: Counter[str] = Counter(str(row.get("asset_class") or "unknown") for row in rows)
    work: Counter[str] = Counter(str(row.get("recommended_work_event") or "unknown") for row in rows)
    deployable = [row for row in rows if row.get("live_deployable")]
    return {
        "total": len(rows),
        "live_deployable": int(categories.get("live_deployable", 0)),
        "positive_ev_shadow": int(categories.get("positive_ev_shadow", 0)),
        "positive_ev_recert": int(categories.get("positive_ev_recert", 0)),
        "positive_ev_execution_blocked": int(categories.get("positive_ev_execution_blocked", 0)),
        "negative_ev": int(categories.get("negative_ev", 0)),
        "stale_broker_local_open": int(categories.get("stale_broker_local_open", 0)),
        "needs_provenance": int(categories.get("needs_provenance", 0)),
        "needs_calibration": int(categories.get("needs_calibration", 0)),
        "deployable_cash_notional": round(
            sum(_safe_float(row.get("max_safe_notional"), 0.0) or 0.0 for row in deployable),
            6,
        ),
        "categories": dict(categories),
        "asset_classes": dict(assets),
        "recommended_work_events": dict(work),
    }


def cost_gate_execution_block_rollup(
    db: Session,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    limit: int = 10,
) -> dict[str, Any]:
    """Group recent positive-edge cost-gate blocks by pattern and venue."""
    cutoff = datetime.utcnow() - timedelta(days=max(1, int(window_days)))
    row_limit = max(1, int(limit)) * 50
    events = (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.event_kind == "work")
        .filter(BrainWorkEvent.event_type == EXIT_VARIANT_REFRESH)
        .filter(BrainWorkEvent.created_at >= cutoff)
        .filter(
            BrainWorkEvent.payload["source"].astext
            == "autotrader_cost_gate_execution_blocked"
        )
        .order_by(BrainWorkEvent.created_at.desc(), BrainWorkEvent.id.desc())
        .limit(row_limit)
        .all()
    )
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        pid = _safe_int(payload.get("scan_pattern_id"), 0)
        if pid <= 0:
            continue
        asset = _canonical_asset_class(payload.get("asset_class")) or "unknown"
        venue = str(payload.get("broker_venue") or "unknown").strip().lower()
        edge_source = str(
            payload.get("cost_gate_edge_pct_source") or "unknown"
        ).strip()
        key = (pid, asset, venue, edge_source)
        group = groups.setdefault(
            key,
            {
                "scan_pattern_id": pid,
                "asset_class": asset,
                "broker_venue": venue,
                "cost_gate_edge_pct_source": edge_source,
                "blocked_count": 0,
                "latest_event_id": None,
                "latest_created_at": None,
                "latest_status": None,
                "tickers": set(),
                "statuses": Counter(),
                "_expected_net_pct_values": [],
                "_edge_gap_pct_values": [],
                "_edge_bps_values": [],
                "_threshold_bps_values": [],
                "_tca_cost_bps_values": [],
                "_fee_bps_values": [],
            },
        )
        group["blocked_count"] += 1
        group["statuses"][str(event.status or "unknown")] += 1
        ticker = str(payload.get("ticker") or "").strip().upper()
        if ticker:
            group["tickers"].add(ticker)
        created_at = event.created_at
        if (
            group.get("latest_created_at") is None
            or (created_at is not None and created_at > group["latest_created_at"])
        ):
            group["latest_event_id"] = int(event.id)
            group["latest_created_at"] = created_at
            group["latest_status"] = str(event.status or "unknown")
        for field, bucket in (
            ("expected_net_pct", "_expected_net_pct_values"),
            ("cost_gate_edge_gap_pct", "_edge_gap_pct_values"),
            ("cost_gate_edge_bps", "_edge_bps_values"),
            ("cost_gate_threshold_bps", "_threshold_bps_values"),
            ("cost_gate_tca_cost_bps", "_tca_cost_bps_values"),
            ("cost_gate_fee_bps", "_fee_bps_values"),
        ):
            value = _safe_float(payload.get(field))
            if value is not None:
                group[bucket].append(value)

    rows: list[dict[str, Any]] = []
    for group in groups.values():
        expected_values = group.pop("_expected_net_pct_values")
        gap_values = group.pop("_edge_gap_pct_values")
        edge_values = group.pop("_edge_bps_values")
        threshold_values = group.pop("_threshold_bps_values")
        tca_values = group.pop("_tca_cost_bps_values")
        fee_values = group.pop("_fee_bps_values")
        tickers = sorted(group.pop("tickers"))
        statuses = group.pop("statuses")
        group.update(
            {
                "tickers": tickers[:10],
                "ticker_count": len(tickers),
                "statuses": dict(statuses),
                "latest_created_at": (
                    group["latest_created_at"].isoformat()
                    if isinstance(group.get("latest_created_at"), datetime)
                    else None
                ),
                "avg_expected_net_pct": _round(_mean(expected_values)),
                "max_expected_net_pct": _round(max(expected_values) if expected_values else None),
                "avg_cost_gate_edge_gap_pct": _round(_mean(gap_values)),
                "max_cost_gate_edge_gap_pct": _round(max(gap_values) if gap_values else None),
                "avg_cost_gate_edge_bps": _round(_mean(edge_values), 2),
                "max_cost_gate_threshold_bps": _round(
                    max(threshold_values) if threshold_values else None,
                    2,
                ),
                "max_cost_gate_tca_cost_bps": _round(
                    max(tca_values) if tca_values else None,
                    2,
                ),
                "max_cost_gate_fee_bps": _round(
                    max(fee_values) if fee_values else None,
                    2,
                ),
                "recommended_work_event": EXIT_VARIANT_REFRESH,
                "cash_deployment_category": "positive_ev_execution_blocked",
            }
        )
        rows.append(group)
    rows.sort(
        key=lambda row: (
            _safe_int(row.get("blocked_count")),
            _safe_float(row.get("max_cost_gate_edge_gap_pct"), 0.0) or 0.0,
            _safe_float(row.get("max_expected_net_pct"), 0.0) or 0.0,
            str(row.get("latest_created_at") or ""),
        ),
        reverse=True,
    )
    rows = rows[: max(1, int(limit))]
    total_blocks = sum(_safe_int(row.get("blocked_count")) for row in rows)
    return {
        "window_days": int(window_days),
        "total_groups": len(groups),
        "returned_groups": len(rows),
        "total_blocked_events_returned": total_blocks,
        "venues": dict(Counter(str(row.get("broker_venue") or "unknown") for row in rows)),
        "asset_classes": dict(Counter(str(row.get("asset_class") or "unknown") for row in rows)),
        "edge_sources": dict(Counter(str(row.get("cost_gate_edge_pct_source") or "unknown") for row in rows)),
        "rows": rows,
    }


def cash_deployment_null_lineage_candidates(
    db: Session,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    limit: int = 10,
) -> list[dict[str, Any]]:
    rows = null_lineage_short_paper_candidates(
        db,
        window_days=window_days,
        limit=limit,
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item.update(
            {
                "cash_deployment_category": "needs_provenance",
                "live_deployable": False,
                "cash_deployment_rank": None,
                "max_safe_notional": 0.0,
                "recommended_work_event": PROVENANCE_BACKFILL,
                "needs_provenance": True,
            }
        )
        out.append(item)
    return out


_STRUCTURAL_EXIT_NOOP_REASONS = frozenset(
    {
        "parent_missing_or_inactive",
        "max_active_variants",
        "duplicate_learned_exit_label",
        "non_positive_parent_realized_avg",
        "missing_parent_payoff_geometry",
        "learned_target_not_tighter_than_static",
        "learned_stop_not_tighter_than_static",
    }
)
_STRUCTURAL_EXIT_NOOP_PREFIXES = (
    "edge_debt_too_negative_for_exit_child:",
    "insufficient_parent_payoff_samples:",
    "reward_risk_below_floor:",
)


def _structural_exit_noop_reason(reason: Any) -> bool:
    value = str(reason or "").strip().lower()
    return value in _STRUCTURAL_EXIT_NOOP_REASONS or any(
        value.startswith(prefix) for prefix in _STRUCTURAL_EXIT_NOOP_PREFIXES
    )


def _recent_noop_profitability_work(
    db: Session,
    *,
    event_type: str,
    scan_pattern_id: int,
    evidence_fingerprint: str,
) -> bool:
    if event_type != EXIT_VARIANT_REFRESH or not evidence_fingerprint:
        return False
    minutes = _safe_int(
        getattr(settings, "brain_work_cash_deployment_noop_cooldown_minutes", 360),
        360,
    )
    if minutes <= 0:
        return False

    from ...models.trading import BrainWorkEvent

    cutoff = datetime.utcnow() - timedelta(minutes=minutes)
    rows = (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.event_kind == "outcome")
        .filter(BrainWorkEvent.event_type == EXIT_VARIANT_DIAGNOSTIC)
        .filter(BrainWorkEvent.created_at >= cutoff)
        .filter(BrainWorkEvent.payload["scan_pattern_id"].astext == str(int(scan_pattern_id)))
        .order_by(BrainWorkEvent.created_at.desc(), BrainWorkEvent.id.desc())
        .limit(20)
        .all()
    )
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        if _safe_int(payload.get("created_count"), -1) != 0:
            continue
        if str(payload.get("evidence_fingerprint") or "") == evidence_fingerprint:
            return True
        if _structural_exit_noop_reason(payload.get("skip_reason")):
            return True
    return False


def _snapshot_created_at(row: dict[str, Any] | None) -> datetime | None:
    raw = (row or {}).get("snapshot_created_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def enqueue_imminent_edge_snapshot_coverage_work(
    db: Session,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    limit: int = DEFAULT_TOP_LIMIT,
    lookback_minutes: int | None = None,
    max_snapshot_age_minutes: int | None = None,
) -> dict[str, Any]:
    """Queue snapshot refreshes for recent imminent alert pattern/asset slices.

    Cached edge/cash endpoints are only useful if the materialized snapshot
    ledger covers the current candidate surface. This producer pass is cheap:
    it looks at recent pending imminent alerts and enqueues deduped
    ``edge_reliability_refresh`` work for missing/stale slices, leaving the
    heavy reliability computation to the brain-work dispatcher.
    """
    producer_interval = max(
        1,
        _safe_int(
            getattr(settings, "brain_work_cash_deployment_producer_interval_minutes", 30),
            30,
        ),
    )
    lookback = max(1, int(lookback_minutes or producer_interval * 4))
    max_age = max(1, int(max_snapshot_age_minutes or producer_interval * 2))
    now = datetime.utcnow()
    cutoff = now - timedelta(minutes=lookback)
    stale_cutoff = now - timedelta(minutes=max_age)

    alerts = (
        db.query(BreakoutAlert)
        .filter(BreakoutAlert.alert_tier == "pattern_imminent")
        .filter(BreakoutAlert.outcome == "pending")
        .filter(BreakoutAlert.scan_pattern_id.isnot(None))
        .filter(BreakoutAlert.alerted_at >= cutoff)
        .order_by(BreakoutAlert.alerted_at.desc(), BreakoutAlert.id.desc())
        .all()
    )
    buckets: dict[tuple[int, str], dict[str, Any]] = {}
    for alert in alerts:
        pid = _safe_int(getattr(alert, "scan_pattern_id", None))
        if pid <= 0:
            continue
        asset = _canonical_asset_class(getattr(alert, "asset_type", None)) or _asset_class_for_row(
            {"primary_symbol": getattr(alert, "ticker", None)}
        )
        key = (pid, asset)
        bucket = buckets.setdefault(
            key,
            {
                "scan_pattern_id": pid,
                "asset_class": asset,
                "alert_count": 0,
                "latest_alerted_at": None,
                "latest_alert_id": None,
            },
        )
        bucket["alert_count"] += 1
        alerted_at = getattr(alert, "alerted_at", None)
        latest_at = bucket.get("latest_alerted_at")
        if latest_at is None or (alerted_at is not None and alerted_at > latest_at):
            bucket["latest_alerted_at"] = alerted_at
            bucket["latest_alert_id"] = int(alert.id)

    snapshots = latest_edge_reliability_snapshot_slices(
        db,
        scan_pattern_ids={pid for pid, _asset in buckets},
    )
    created: list[int] = []
    skipped_fresh = 0
    skipped_deduped = 0
    missing = 0
    stale = 0
    wrong_window = 0
    for (pid, asset), bucket in sorted(buckets.items()):
        snapshot = snapshots.get((pid, asset)) or snapshots.get((pid, "all"))
        snap_at = _snapshot_created_at(snapshot)
        snap_window = _safe_int(
            (snapshot or {}).get("snapshot_window_days")
            or (snapshot or {}).get("window_days"),
            0,
        )
        if snapshot is None:
            missing += 1
        elif snap_window != int(window_days):
            wrong_window += 1
        elif snap_at is None or snap_at < stale_cutoff:
            stale += 1
        else:
            skipped_fresh += 1
            continue

        latest_at = bucket.get("latest_alerted_at")
        fingerprint = (
            f"imminent_snapshot:{asset}:"
            f"{bucket.get('alert_count')}:"
            f"{latest_at.isoformat() if isinstance(latest_at, datetime) else 'unknown'}"
        )
        recent_refresh = (
            db.query(BrainWorkEvent.id)
            .filter(BrainWorkEvent.event_type == EDGE_RELIABILITY_REFRESH)
            .filter(BrainWorkEvent.created_at >= stale_cutoff)
            .filter(BrainWorkEvent.payload["scan_pattern_id"].astext == str(int(pid)))
            .filter(BrainWorkEvent.payload["asset_class"].astext == asset)
            .filter(BrainWorkEvent.payload["window_days"].astext == str(int(window_days)))
            .filter(BrainWorkEvent.payload["source"].astext == "imminent_snapshot_coverage")
            .first()
        )
        if recent_refresh:
            skipped_deduped += 1
            continue
        event_id = emit_edge_reliability_refresh_requested(
            db,
            int(pid),
            source="imminent_snapshot_coverage",
            asset_class=asset,
            window_days=window_days,
            evidence_fingerprint=fingerprint,
        )
        if event_id is None:
            skipped_deduped += 1
            continue
        created.append(int(event_id))
        if created and len(created) >= max(1, int(limit)):
            break

    return {
        "lookback_minutes": lookback,
        "max_snapshot_age_minutes": max_age,
        "considered_slices": len(buckets),
        "created": len(created),
        "event_ids": created,
        "missing_snapshot": missing,
        "stale_snapshot": stale,
        "window_mismatch": wrong_window,
        "skipped_fresh": skipped_fresh,
        "skipped_deduped": skipped_deduped,
    }


def enqueue_cash_deployment_work(
    db: Session,
    *,
    user_id: int | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    limit: int = DEFAULT_TOP_LIMIT,
    include_null_lineage: bool = True,
    include_snapshot_coverage: bool = True,
    use_snapshots: bool = False,
) -> dict[str, Any]:
    """Turn cash-deployment diagnostics into deduped brain work.

    This is intentionally conservative: it queues reliability refreshes,
    recert rescue, learned-variant refresh, and provenance work. It never
    promotes a pattern or routes an order.
    """
    snapshot_coverage = (
        enqueue_imminent_edge_snapshot_coverage_work(
            db,
            window_days=window_days,
            limit=limit,
        )
        if include_snapshot_coverage
        else {
            "considered_slices": 0,
            "created": 0,
            "event_ids": [],
            "skipped_disabled": True,
        }
    )
    row_reader = cash_deployment_snapshot_rows if use_snapshots else cash_deployment_rows
    rows = row_reader(
        db,
        user_id=user_id,
        window_days=window_days,
        limit=limit,
    )
    created: list[int] = []
    considered = 0
    skipped = 0
    skipped_noop_cooldown = 0
    by_event: Counter[str] = Counter()
    if int(snapshot_coverage.get("created") or 0) > 0:
        by_event[EDGE_RELIABILITY_REFRESH] += int(snapshot_coverage.get("created") or 0)
    for row in rows:
        event_type = str(row.get("recommended_work_event") or "").strip()
        pid = row.get("scan_pattern_id")
        if not event_type or pid is None:
            skipped += 1
            continue
        considered += 1
        payload = {
            "cash_deployment_category": row.get("cash_deployment_category"),
            "asset_class": row.get("asset_class"),
            "slice_asset_class": row.get("slice_asset_class"),
            "edge_slice_id": row.get("edge_slice_id"),
            "calibrated_ev_after_cost_pct": row.get("calibrated_ev_after_cost_pct"),
            "allocation_score": row.get("allocation_score"),
            "expected_evidence_value": row.get("expected_evidence_value"),
            "graduation_blocker": row.get("graduation_blocker"),
        }
        evidence_fingerprint = str(row.get("evidence_fingerprint") or "")
        if _recent_noop_profitability_work(
            db,
            event_type=event_type,
            scan_pattern_id=int(pid),
            evidence_fingerprint=evidence_fingerprint,
        ):
            skipped += 1
            skipped_noop_cooldown += 1
            continue
        if event_type == EDGE_RELIABILITY_REFRESH:
            event_id = emit_edge_reliability_refresh_requested(
                db,
                int(pid),
                source="cash_deployment",
                asset_class=row.get("asset_class"),
                window_days=window_days,
                evidence_fingerprint=evidence_fingerprint,
            )
        else:
            event_id = emit_targeted_profitability_work(
                db,
                event_type=event_type,
                scan_pattern_id=int(pid),
                source="cash_deployment",
                asset_class=row.get("asset_class"),
                evidence_fingerprint=evidence_fingerprint,
                payload=payload,
            )
        if event_id is None:
            skipped += 1
            continue
        created.append(int(event_id))
        by_event[event_type] += 1

    null_created: list[int] = []
    if include_null_lineage:
        for row in cash_deployment_null_lineage_candidates(
            db,
            window_days=window_days,
            limit=min(10, max(1, int(limit))),
        ):
            considered += 1
            event_id = emit_targeted_profitability_work(
                db,
                event_type=PROVENANCE_BACKFILL,
                scan_pattern_id=None,
                source="cash_deployment",
                evidence_fingerprint=str(row.get("evidence_fingerprint") or ""),
                payload={
                    "cash_deployment_category": "needs_provenance",
                    "ticker": row.get("ticker"),
                    "family": row.get("family"),
                    "closed_count": row.get("closed_count"),
                    "total_pnl": row.get("total_pnl"),
                    "paper_trade_ids": row.get("paper_trade_ids"),
                    "expected_evidence_value": max(0.0, _safe_float(row.get("total_pnl"), 0.0) or 0.0),
                },
            )
            if event_id is None:
                skipped += 1
                continue
            null_created.append(int(event_id))
            by_event[PROVENANCE_BACKFILL] += 1

    return {
        "ok": True,
        "window_days": int(window_days),
        "row_source": "snapshot" if use_snapshots else "fresh",
        "considered": considered,
        "created": len(created) + len(null_created) + int(snapshot_coverage.get("created") or 0),
        "skipped": skipped,
        "skipped_noop_cooldown": skipped_noop_cooldown,
        "event_ids": created + null_created + list(snapshot_coverage.get("event_ids") or []),
        "event_types": dict(by_event),
        "snapshot_coverage": snapshot_coverage,
    }

"""Portfolio conflict/capital allocation scoring for repeatable-edge and live sessions."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ...config import settings
from ..broker_manager import get_all_broker_statuses
from .pattern_validation_projection import read_pattern_validation_projection, write_validation_contract

logger = logging.getLogger(__name__)

ALLOCATION_STATE_VERSION = 1
_LIVE_TERMINAL_SESSION_STATES = frozenset(
    {"cancelled", "expired", "error", "archived", "finished", "live_finished", "live_cancelled", "live_error"}
)


def _utc_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_confidence(value: Any) -> float:
    raw = _safe_float(value, 0.0)
    if raw > 1.0:
        raw = raw / 10.0
    return max(0.0, min(1.0, raw))


def _win_rate_score(value: Any) -> float:
    raw = _safe_float(value, 0.0)
    if raw > 1.0:
        raw = raw / 100.0
    return max(0.0, min(1.0, raw))


def _tier_score(contract: dict[str, Any]) -> float:
    tier = (contract.get("composite_tier") or contract.get("robustness_tier") or contract.get("drift_tier") or "n/a")
    tier = str(tier).strip().lower()
    if tier == "healthy":
        return 1.0
    if tier == "warning":
        return 0.6
    if tier == "critical":
        return 0.2
    return 0.5


def _symbol_asset_family(symbol: str | None) -> str:
    sym = (symbol or "").strip().upper()
    if sym.endswith("-USD"):
        return "crypto"
    try:
        from .backtest_engine import TICKER_TO_SECTOR

        return TICKER_TO_SECTOR.get(sym, "equity")
    except Exception:
        return "equity"


def _correlation_bucket(symbol: str | None, *, asset_class: str | None = None) -> str:
    sym = (symbol or "").strip().upper()
    family = (asset_class or "").strip().lower() or _symbol_asset_family(sym)
    if sym.endswith("-USD"):
        return f"crypto:{sym.split('-')[0]}"
    return f"{family}:{sym[:1] or 'x'}"


def _venue_readiness_score(symbol: str | None) -> float:
    brokers = get_all_broker_statuses()
    sym = (symbol or "").strip().upper()
    if sym.endswith("-USD"):
        return 1.0 if brokers.get("coinbase", {}).get("connected") else 0.4
    return 1.0 if brokers.get("robinhood", {}).get("connected") else 0.5


def _collect_open_trade_conflicts(db: Session, *, user_id: int, symbol: str, sector: str, correlation_bucket: str) -> list[dict[str, Any]]:
    from ...models.trading import ScanPattern, Trade

    rows = (
        db.query(Trade)
        .filter(Trade.user_id == int(user_id), Trade.status == "open", Trade.direction == "long")
        .all()
    )
    pattern_ids = {int(t.scan_pattern_id) for t in rows if getattr(t, "scan_pattern_id", None)}
    patterns = {}
    if pattern_ids:
        for pattern in db.query(ScanPattern).filter(ScanPattern.id.in_(tuple(pattern_ids))).all():
            patterns[int(pattern.id)] = pattern
    out: list[dict[str, Any]] = []
    for trade in rows:
        trade_sector = _symbol_asset_family(trade.ticker)
        trade_corr = _correlation_bucket(trade.ticker, asset_class=trade_sector)
        buckets: list[str] = []
        if (trade.ticker or "").upper() == symbol:
            buckets.append("same_ticker")
        if trade_sector == sector:
            buckets.append("same_asset_family")
        if trade_corr == correlation_bucket:
            buckets.append("same_correlation_bucket")
        if not buckets:
            continue
        incumbent = patterns.get(int(trade.scan_pattern_id)) if getattr(trade, "scan_pattern_id", None) else None
        incumbent_score = (
            _normalize_confidence(getattr(incumbent, "confidence", None)) * 0.6
            + _win_rate_score(getattr(incumbent, "oos_win_rate", None) or getattr(incumbent, "win_rate", None)) * 0.4
        ) if incumbent else 0.5
        out.append(
            {
                "conflict_type": "open_trade",
                "ticker": trade.ticker,
                "trade_id": int(getattr(trade, "id", 0) or 0),
                "scan_pattern_id": getattr(trade, "scan_pattern_id", None),
                "buckets": buckets,
                "incumbent_score": round(incumbent_score, 4),
            }
        )
    return out


def _collect_live_session_conflicts(
    db: Session,
    *,
    user_id: int,
    symbol: str,
    sector: str,
    correlation_bucket: str,
    hypothesis_family: str | None,
) -> list[dict[str, Any]]:
    from ...models.trading import MomentumStrategyVariant, TradingAutomationSession

    rows = (
        db.query(TradingAutomationSession)
        .filter(
            TradingAutomationSession.user_id == int(user_id),
            TradingAutomationSession.mode == "live",
            ~TradingAutomationSession.state.in_(tuple(_LIVE_TERMINAL_SESSION_STATES)),
        )
        .all()
    )
    variant_ids = {int(row.variant_id) for row in rows if getattr(row, "variant_id", None)}
    variants = {}
    if variant_ids:
        for variant in db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.id.in_(tuple(variant_ids))).all():
            variants[int(variant.id)] = variant
    out: list[dict[str, Any]] = []
    candidate_family = str(hypothesis_family or "").strip().lower()
    for sess in rows:
        sess_sector = _symbol_asset_family(sess.symbol)
        sess_corr = _correlation_bucket(sess.symbol, asset_class=sess_sector)
        buckets: list[str] = []
        if (sess.symbol or "").upper() == symbol:
            buckets.append("same_ticker")
        if sess_sector == sector:
            buckets.append("same_asset_family")
        if sess_corr == correlation_bucket:
            buckets.append("same_correlation_bucket")
        variant = variants.get(int(sess.variant_id)) if getattr(sess, "variant_id", None) else None
        incumbent_family = (variant.family or "").strip().lower() if variant else ""
        if candidate_family and incumbent_family == candidate_family:
            buckets.append("same_hypothesis_family")
        if not buckets:
            continue
        out.append(
            {
                "conflict_type": "live_session",
                "session_id": int(getattr(sess, "id", 0) or 0),
                "ticker": sess.symbol,
                "variant_id": sess.variant_id,
                "buckets": sorted(set(buckets)),
                "incumbent_score": 0.55,
            }
        )
    return out


def _trade_notional_usd(trade: Any) -> float:
    px = _safe_float(getattr(trade, "avg_fill_price", None), 0.0) or _safe_float(
        getattr(trade, "entry_price", None), 0.0
    )
    qty = _safe_float(getattr(trade, "filled_quantity", None), 0.0) or _safe_float(
        getattr(trade, "quantity", None), 0.0
    )
    return max(0.0, abs(px * qty))


def _session_position_notional_usd(session: Any) -> float:
    snap = getattr(session, "risk_snapshot_json", None)
    if not isinstance(snap, dict):
        return 0.0
    for key in ("momentum_live_execution", "momentum_paper_execution"):
        lane = snap.get(key)
        if not isinstance(lane, dict):
            continue
        pos = lane.get("position")
        if not isinstance(pos, dict):
            continue
        direct = _safe_float(pos.get("notional_usd"), 0.0)
        if direct > 0:
            return direct
        qty = _safe_float(pos.get("quantity"), 0.0)
        px = (
            _safe_float(pos.get("entry_price"), 0.0)
            or _safe_float(pos.get("avg_fill_price"), 0.0)
            or _safe_float(lane.get("last_price"), 0.0)
        )
        if qty > 0 and px > 0:
            return abs(qty * px)
    return 0.0


def _portfolio_exposure_snapshot(
    db: Session,
    *,
    user_id: int,
    symbol: str,
    sector: str,
    correlation_bucket: str,
    hypothesis_family: str | None,
    execution_mode: str | None,
    intended_notional_usd: float | None,
) -> dict[str, Any]:
    from ...models.trading import MomentumStrategyVariant, Trade, TradingAutomationSession

    open_trade_rows = (
        db.query(Trade)
        .filter(Trade.user_id == int(user_id), Trade.status == "open", Trade.direction == "long")
        .all()
    )
    live_session_rows = (
        db.query(TradingAutomationSession)
        .filter(
            TradingAutomationSession.user_id == int(user_id),
            TradingAutomationSession.mode == "live",
            ~TradingAutomationSession.state.in_(tuple(_LIVE_TERMINAL_SESSION_STATES)),
        )
        .all()
    )
    variant_ids = {int(row.variant_id) for row in live_session_rows if getattr(row, "variant_id", None)}
    variants = {}
    if variant_ids:
        for variant in db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.id.in_(tuple(variant_ids))).all():
            variants[int(variant.id)] = variant

    open_trade_notional = sum(_trade_notional_usd(row) for row in open_trade_rows)
    live_session_notional = sum(_session_position_notional_usd(row) for row in live_session_rows)
    candidate_family = str(hypothesis_family or "").strip().lower()
    same_family_live = 0
    same_symbol_live = 0
    same_corr_live = 0
    same_sector_live = 0
    for sess in live_session_rows:
        sess_symbol = (getattr(sess, "symbol", "") or "").strip().upper()
        sess_sector = _symbol_asset_family(sess_symbol)
        sess_corr = _correlation_bucket(sess_symbol, asset_class=sess_sector)
        if sess_symbol == (symbol or "").strip().upper():
            same_symbol_live += 1
        if sess_sector == sector:
            same_sector_live += 1
        if sess_corr == correlation_bucket:
            same_corr_live += 1
        variant = variants.get(int(sess.variant_id)) if getattr(sess, "variant_id", None) else None
        incumbent_family = (variant.family or "").strip().lower() if variant else ""
        if candidate_family and incumbent_family == candidate_family:
            same_family_live += 1

    intended = max(0.0, _safe_float(intended_notional_usd, 0.0))
    is_live_candidate = str(execution_mode or "").strip().lower() == "live"
    projected_live_notional = live_session_notional + (intended if is_live_candidate else 0.0)
    return {
        "open_trade_count": len(open_trade_rows),
        "active_live_session_count": len(live_session_rows),
        "active_risk_item_count": len(open_trade_rows) + len(live_session_rows),
        "open_trade_notional_usd": round(open_trade_notional, 6),
        "active_live_session_notional_usd": round(live_session_notional, 6),
        "intended_notional_usd": round(intended, 6),
        "projected_live_notional_usd": round(projected_live_notional, 6),
        "same_symbol_live_sessions": same_symbol_live,
        "same_asset_family_live_sessions": same_sector_live,
        "same_correlation_bucket_live_sessions": same_corr_live,
        "same_hypothesis_family_live_sessions": same_family_live,
        "execution_mode": execution_mode,
    }


def _pattern_capital_gate(db: Session, *, scan_pattern_id: int | None, execution_mode: str) -> dict[str, Any]:
    if not scan_pattern_id:
        return {
            "status": "pass",
            "hard_block_reason": None,
            "reasons": [],
            "scan_pattern_id": None,
        }
    from ...models.trading import ScanPattern

    pattern = db.query(ScanPattern).filter(ScanPattern.id == int(scan_pattern_id)).one_or_none()
    if pattern is None:
        return {
            "status": "warn",
            "hard_block_reason": None,
            "reasons": ["pattern_missing"],
            "scan_pattern_id": int(scan_pattern_id),
        }

    lifecycle = str(getattr(pattern, "lifecycle_stage", "") or "").strip().lower()
    promotion_status = str(getattr(pattern, "promotion_status", "") or "").strip().lower()
    recert_required = bool(getattr(pattern, "recert_required", False))
    reasons: list[str] = []
    if lifecycle in {"retired", "decayed", "challenged"}:
        reasons.append(f"lifecycle_{lifecycle}")
    if recert_required:
        reasons.append("recert_required")

    is_live = str(execution_mode or "").strip().lower() == "live"
    hard_reason = None
    if is_live and recert_required and bool(getattr(settings, "chili_autotrader_block_live_on_recert_required", True)):
        pilot_soft_allowed = False
        try:
            from .alpha_portfolio_gate import pilot_bootstrap_recert_allows_live

            pilot_soft_allowed = pilot_bootstrap_recert_allows_live(
                pattern,
                settings_=settings,
            )
        except Exception:
            pilot_soft_allowed = False
        if not pilot_soft_allowed:
            hard_reason = "pattern_recert_required"
    if is_live and hard_reason is None and lifecycle in {"retired", "decayed", "challenged"}:
        hard_reason = "pattern_lifecycle_degraded"

    return {
        "status": "block" if hard_reason else ("warn" if reasons else "pass"),
        "hard_block_reason": hard_reason,
        "reasons": reasons,
        "scan_pattern_id": int(scan_pattern_id),
        "lifecycle_stage": lifecycle or None,
        "promotion_status": promotion_status or None,
        "recert_required": recert_required,
        "learning_lane_enabled": True,
    }


def _candidate_score(
    *,
    research_quality: float,
    live_drift_score: float,
    execution_score: float,
    venue_score: float,
    portfolio_heat_score: float,
) -> float:
    return round(
        (research_quality * 0.36)
        + (live_drift_score * 0.18)
        + (execution_score * 0.18)
        + (venue_score * 0.14)
        + (portfolio_heat_score * 0.14),
        4,
    )


def evaluate_allocation_candidate(
    db: Session,
    *,
    user_id: int | None,
    symbol: str,
    timeframe: str | None,
    asset_class: str | None,
    hypothesis_family: str | None,
    research_quality: float,
    live_drift_contract: dict[str, Any] | None,
    execution_contract: dict[str, Any] | None,
    context: str,
    execution_mode: str | None = None,
    intended_notional_usd: float | None = None,
) -> dict[str, Any]:
    if user_id is None:
        return {
            "version": ALLOCATION_STATE_VERSION,
            "shadow_mode": bool(getattr(settings, "brain_allocator_shadow_mode", True)),
            "context": context,
            "allowed_if_enforced": True,
            "blocked_reason": None,
            "action": "allow",
            "evaluated_at": _utc_iso(),
            "score": None,
            "score_inputs": {"reason": "user_required_for_portfolio_view"},
            "conflicts": [],
            "conflict_buckets": [],
            "portfolio_exposure": {},
        }

    sector = (asset_class or "").strip().lower() or _symbol_asset_family(symbol)
    corr_bucket = _correlation_bucket(symbol, asset_class=sector)
    live_drift_score = _tier_score(live_drift_contract or {})
    execution_score = _tier_score(execution_contract or {})
    venue_score = _venue_readiness_score(symbol)

    from ...models.trading import Trade, TradingAutomationSession

    open_trades = (
        db.query(Trade)
        .filter(Trade.user_id == int(user_id), Trade.status == "open", Trade.direction == "long")
        .count()
    )
    active_live_sessions = (
        db.query(TradingAutomationSession)
        .filter(
            TradingAutomationSession.user_id == int(user_id),
            TradingAutomationSession.mode == "live",
            ~TradingAutomationSession.state.in_(tuple(_LIVE_TERMINAL_SESSION_STATES)),
        )
        .count()
    )
    portfolio_heat = open_trades + active_live_sessions
    portfolio_heat_score = max(0.2, 1.0 - min(0.8, portfolio_heat * 0.08))

    conflicts = _collect_open_trade_conflicts(
        db,
        user_id=int(user_id),
        symbol=symbol,
        sector=sector,
        correlation_bucket=corr_bucket,
    )
    conflicts.extend(
        _collect_live_session_conflicts(
            db,
            user_id=int(user_id),
            symbol=symbol,
            sector=sector,
            correlation_bucket=corr_bucket,
            hypothesis_family=hypothesis_family,
        )
    )
    exposure = _portfolio_exposure_snapshot(
        db,
        user_id=int(user_id),
        symbol=symbol,
        sector=sector,
        correlation_bucket=corr_bucket,
        hypothesis_family=hypothesis_family,
        execution_mode=execution_mode,
        intended_notional_usd=intended_notional_usd,
    )
    buckets = sorted({bucket for row in conflicts for bucket in row.get("buckets", [])})
    score = _candidate_score(
        research_quality=research_quality,
        live_drift_score=live_drift_score,
        execution_score=execution_score,
        venue_score=venue_score,
        portfolio_heat_score=portfolio_heat_score,
    )

    blocked_reason = None
    sector_cap = int(getattr(settings, "brain_max_open_per_sector", 0) or 0)
    max_correlated = int(getattr(settings, "brain_max_correlated_positions", 0) or 0)
    max_heat = int(getattr(settings, "brain_allocator_max_active_risk_items", 0) or 0)
    max_live_notional = _safe_float(getattr(settings, "brain_allocator_max_live_notional_usd", 0.0), 0.0)
    max_same_family_live = int(getattr(settings, "brain_allocator_max_same_family_live_sessions", 0) or 0)
    is_live_candidate = str(execution_mode or "").strip().lower() == "live"
    same_symbol_conflicts = [row for row in conflicts if "same_ticker" in row.get("buckets", [])]
    if same_symbol_conflicts:
        best_incumbent = max(_safe_float(row.get("incumbent_score"), 0.5) for row in same_symbol_conflicts)
        margin = float(getattr(settings, "brain_allocator_incumbent_score_margin", 0.08) or 0.08)
        if score <= best_incumbent + margin:
            blocked_reason = "same_ticker_conflict"
    if blocked_reason is None and sector_cap > 0:
        sector_count = sum(1 for row in conflicts if "same_asset_family" in row.get("buckets", []))
        if sector_count >= sector_cap:
            blocked_reason = "sector_cap"
    if blocked_reason is None and max_correlated > 0:
        corr_count = sum(1 for row in conflicts if "same_correlation_bucket" in row.get("buckets", []))
        if corr_count >= max_correlated:
            blocked_reason = "correlation_bucket_cap"
    if blocked_reason is None and max_heat > 0:
        if int(exposure.get("active_risk_item_count") or 0) >= max_heat:
            blocked_reason = "portfolio_heat_cap"
    if blocked_reason is None and is_live_candidate and max_live_notional > 0:
        projected = _safe_float(exposure.get("projected_live_notional_usd"), 0.0)
        if projected > max_live_notional:
            blocked_reason = "portfolio_live_notional_cap"
    if blocked_reason is None and is_live_candidate and max_same_family_live > 0:
        same_family = int(exposure.get("same_hypothesis_family_live_sessions") or 0)
        if same_family >= max_same_family_live:
            blocked_reason = "strategy_family_live_cap"
    if blocked_reason is None and live_drift_contract and execution_contract:
        if _tier_score(live_drift_contract) <= 0.2 and _tier_score(execution_contract) <= 0.2:
            blocked_reason = "quality_stack_critical"

    shadow = bool(getattr(settings, "brain_allocator_shadow_mode", True))
    return {
        "version": ALLOCATION_STATE_VERSION,
        "context": context,
        "symbol": symbol,
        "timeframe": timeframe,
        "asset_class": sector,
        "hypothesis_family": hypothesis_family,
        "correlation_bucket": corr_bucket,
        "shadow_mode": shadow,
        "allowed_if_enforced": blocked_reason is None,
        "blocked_reason": blocked_reason,
        "action": "allow" if blocked_reason is None else "suppress",
        "score": score,
        "score_inputs": {
            "research_quality": round(research_quality, 4),
            "live_drift_score": round(live_drift_score, 4),
            "execution_score": round(execution_score, 4),
            "venue_readiness_score": round(venue_score, 4),
            "portfolio_heat_score": round(portfolio_heat_score, 4),
            "portfolio_heat_count": portfolio_heat,
            "intended_notional_usd": round(_safe_float(intended_notional_usd, 0.0), 6),
        },
        "portfolio_exposure": exposure,
        "conflicts": conflicts,
        "conflict_buckets": buckets,
        "evaluated_at": _utc_iso(),
        "enforcement_mode": {
            "soft_block_enabled": bool(getattr(settings, "brain_allocator_live_soft_block_enabled", False)),
            "hard_block_enabled": bool(getattr(settings, "brain_allocator_live_hard_block_enabled", False)),
        },
    }


def build_pattern_allocation_state(db: Session, pattern: Any, *, user_id: int | None, context: str) -> dict[str, Any]:
    projection = read_pattern_validation_projection(pattern)
    research_quality = round(
        (_normalize_confidence(getattr(pattern, "confidence", None)) * 0.55)
        + (_win_rate_score(getattr(pattern, "oos_win_rate", None) or getattr(pattern, "win_rate", None)) * 0.45),
        4,
    )
    state = evaluate_allocation_candidate(
        db,
        user_id=user_id,
        symbol=getattr(pattern, "scope_tickers", None) or getattr(pattern, "name", None) or "",
        timeframe=getattr(pattern, "timeframe", None),
        asset_class=getattr(pattern, "asset_class", None),
        hypothesis_family=getattr(pattern, "hypothesis_family", None),
        research_quality=research_quality,
        live_drift_contract=projection.live_drift_v2 or projection.live_drift,
        execution_contract=projection.execution_robustness_v2 or projection.execution_robustness,
        context=context,
    )
    # Prefer explicit scope ticker only when the pattern is single-name.
    symbol = (getattr(pattern, "scope_tickers", None) or "").strip()
    if "," not in symbol and symbol:
        state["symbol"] = symbol.upper()
    else:
        state["symbol"] = None
    write_validation_contract(pattern, "allocation_state", state)
    return state


def build_proposal_allocation_decision(db: Session, proposal: Any, *, user_id: int | None) -> dict[str, Any]:
    pattern_projection = read_pattern_validation_projection({})
    hypothesis_family = None
    if getattr(proposal, "scan_pattern_id", None):
        from ...models.trading import ScanPattern

        pattern = db.query(ScanPattern).filter(ScanPattern.id == int(proposal.scan_pattern_id)).first()
        if pattern:
            hypothesis_family = getattr(pattern, "hypothesis_family", None)
            pattern_projection = read_pattern_validation_projection(pattern)

    research_quality = round(
        (_normalize_confidence(getattr(proposal, "confidence", None)) * 0.65)
        + (_safe_float(getattr(proposal, "risk_reward_ratio", None), 0.0) / 4.0 * 0.35),
        4,
    )
    research_quality = max(0.0, min(1.0, research_quality))
    proposal_notional = _safe_float(getattr(proposal, "entry_price", None), 0.0) * _safe_float(
        getattr(proposal, "quantity", None), 0.0
    )
    decision = evaluate_allocation_candidate(
        db,
        user_id=user_id,
        symbol=(getattr(proposal, "ticker", None) or "").strip().upper(),
        timeframe=getattr(proposal, "timeframe", None),
        asset_class=None,
        hypothesis_family=hypothesis_family,
        research_quality=research_quality,
        live_drift_contract=pattern_projection.live_drift_v2 or pattern_projection.live_drift,
        execution_contract=pattern_projection.execution_robustness_v2 or pattern_projection.execution_robustness,
        context="proposal_approval",
        intended_notional_usd=proposal_notional,
    )
    proposal.allocation_decision_json = dict(decision)
    return decision


def build_session_allocation_decision(
    db: Session,
    session: Any,
    *,
    user_id: int | None,
    context: str,
    intended_notional_usd: float | None = None,
) -> dict[str, Any]:
    from ...models.trading import MomentumStrategyVariant, ScanPattern

    variant = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.id == int(session.variant_id)).first()
    pattern = None
    projection = read_pattern_validation_projection({})
    hypothesis_family = getattr(variant, "family", None) if variant else None
    asset_class = "crypto" if (getattr(session, "symbol", "") or "").endswith("-USD") else None
    if variant and getattr(variant, "scan_pattern_id", None):
        pattern = db.query(ScanPattern).filter(ScanPattern.id == int(variant.scan_pattern_id)).first()
    if pattern:
        hypothesis_family = getattr(pattern, "hypothesis_family", None) or hypothesis_family
        asset_class = getattr(pattern, "asset_class", None) or asset_class
        projection = read_pattern_validation_projection(pattern)

    research_quality = round(
        (_normalize_confidence((session.risk_snapshot_json or {}).get("confidence")) * 0.5)
        + (_safe_float((session.risk_snapshot_json or {}).get("viability_score"), 0.0) * 0.5),
        4,
    )
    decision = evaluate_allocation_candidate(
        db,
        user_id=user_id,
        symbol=(getattr(session, "symbol", None) or "").strip().upper(),
        timeframe=(session.risk_snapshot_json or {}).get("timeframe"),
        asset_class=asset_class,
        hypothesis_family=hypothesis_family,
        research_quality=max(0.0, min(1.0, research_quality if research_quality > 0 else 0.5)),
        live_drift_contract=projection.live_drift_v2 or projection.live_drift,
        execution_contract=projection.execution_robustness_v2 or projection.execution_robustness,
        context=context,
        execution_mode=getattr(session, "mode", None),
        intended_notional_usd=intended_notional_usd,
    )
    session.allocation_decision_json = dict(decision)
    return decision


def allocation_block_reason(decision: dict[str, Any] | None) -> str | None:
    if not decision or not isinstance(decision, dict):
        return None
    if not decision.get("allowed_if_enforced") and bool(getattr(settings, "brain_allocator_live_hard_block_enabled", False)):
        return str(decision.get("blocked_reason") or "allocator_blocked")
    return None


def _paper_terminal_states() -> frozenset:
    from .momentum_neural.paper_fsm import PAPER_RUNNER_TERMINAL_STATES

    return frozenset(PAPER_RUNNER_TERMINAL_STATES) | frozenset({"error"})


def _momentum_variant_performance_size_mult(db: Session, variant_id: int) -> float:
    """Scale notional by recent outcome Sharpe-like ratio (Phase 5d)."""
    if variant_id <= 0:
        return 1.0
    if not bool(getattr(settings, "chili_momentum_performance_sizing_enabled", True)):
        return 1.0
    from statistics import mean, pstdev

    from ...models.trading import MomentumAutomationOutcome

    rows = (
        db.query(MomentumAutomationOutcome.return_bps)
        .filter(
            MomentumAutomationOutcome.variant_id == int(variant_id),
            MomentumAutomationOutcome.return_bps.isnot(None),
        )
        .order_by(MomentumAutomationOutcome.created_at.desc())
        .limit(10)
        .all()
    )
    vals = [float(r[0]) for r in rows if r[0] is not None]
    if len(vals) < 3:
        return 1.0
    m = mean(vals)
    s = pstdev(vals) if len(vals) > 1 else 0.0
    sharpe_like = (m / s) if s > 1e-9 else 0.0
    return max(0.3, min(1.0, sharpe_like / 0.5))


def _peer_automation_sessions(
    db: Session,
    *,
    user_id: int,
    session_id: int,
    mode: str,
    limit: int,
) -> list[Any]:
    from ...models.trading import TradingAutomationSession

    if limit <= 0:
        return []
    if mode == "live":
        term = _LIVE_TERMINAL_SESSION_STATES
    else:
        term = _paper_terminal_states()
    return (
        db.query(TradingAutomationSession)
        .filter(
            TradingAutomationSession.user_id == int(user_id),
            TradingAutomationSession.mode == mode,
            TradingAutomationSession.id != int(session_id),
            ~TradingAutomationSession.state.in_(tuple(term)),
        )
        .order_by(TradingAutomationSession.updated_at.desc())
        .limit(int(limit))
        .all()
    )


def allocate_momentum_session_entry(
    db: Session,
    *,
    session: Any,
    viability: Any,
    variant: Any,
    user_id: int | None,
    max_notional_policy: float,
    quote_mid: float | None,
    spread_bps: float,
    execution_mode: str,
    regime_snapshot: dict[str, Any],
    deployment_stage: str,
    deployment_size_mult: float = -1.0,
) -> dict[str, Any]:
    """Portfolio + expectancy orchestration at entry boundary (extends existing allocator)."""
    from .capacity_governor import evaluate_capacity
    from .deployment_ladder_service import stage_size_multiplier
    from .execution_realism_service import estimate_execution_realism
    from .expectancy_service import compute_expectancy_edges

    symbol = (getattr(session, "symbol", None) or "").strip().upper()
    scan_pattern_id = int(getattr(variant, "scan_pattern_id", None) or 0) or None
    pattern_gate = _pattern_capital_gate(db, scan_pattern_id=scan_pattern_id, execution_mode=execution_mode)
    ex = viability.execution_readiness_json if isinstance(getattr(viability, "execution_readiness_json", None), dict) else {}
    vol_proxy = None
    try:
        ev = viability.evidence_window_json if isinstance(getattr(viability, "evidence_window_json", None), dict) else {}
        vol_proxy = _safe_float(ev.get("volume_usd_24h") or ev.get("quote_volume_usd"),0.0) or None
    except Exception:
        vol_proxy = None

    base_cap = _safe_float(max_notional_policy, 250.0)
    if deployment_size_mult >= 0:
        mult = deployment_size_mult
    else:
        mult = stage_size_multiplier(deployment_stage)
    if execution_mode == "live" and not bool(getattr(settings, "brain_live_deployment_enforcement", False)):
        mult = 1.0
    if execution_mode == "paper" and not bool(getattr(settings, "brain_paper_deployment_enforcement", True)):
        mult = 1.0

    intended_notional = max(10.0, base_cap * max(0.0, min(1.0, mult)))
    alloc = build_session_allocation_decision(
        db,
        session,
        user_id=user_id,
        context="momentum_entry",
        intended_notional_usd=intended_notional,
    )
    try:
        perf_mult = _momentum_variant_performance_size_mult(db, int(getattr(variant, "id", 0) or 0))
        intended_notional = max(10.0, intended_notional * perf_mult)
    except Exception:
        pass
    erc_allocation: dict[str, Any] | None = None
    try:
        from .portfolio_optimizer import equal_risk_contribution

        erc = equal_risk_contribution(db)
        if erc.get("ok"):
            for item in erc.get("allocations", []):
                if int(item.get("pattern_id") or 0) == int(scan_pattern_id or 0):
                    erc_allocation = item
                    cap = _safe_float(item.get("capital"), 0.0)
                    if cap > 0:
                        intended_notional = min(intended_notional, cap)
                    break
    except Exception:
        erc_allocation = None

    realism = estimate_execution_realism(
        symbol=symbol,
        execution_readiness=ex,
        regime_snapshot=regime_snapshot,
        quote_mid=quote_mid,
        spread_bps=spread_bps,
        intended_notional_usd=intended_notional,
        execution_mode=execution_mode,
    )

    cap_eval = evaluate_capacity(
        db,
        user_id=user_id,
        symbol=symbol,
        spread_bps=spread_bps,
        estimated_slippage_bps=_safe_float(realism.get("expected_slippage_bps"), 0.0),
        intended_notional_usd=intended_notional,
        execution_mode=execution_mode,
        adv_usd_proxy=vol_proxy,
        min_volume_usd_proxy=vol_proxy,
    )

    unc_hair = max(0.0, 1.0 - _safe_float(getattr(viability, "viability_score", None), 0.0))
    regime_mult = 1.0
    atrp = _safe_float((regime_snapshot or {}).get("atr_pct") or (regime_snapshot or {}).get("atr_percent"), 0.0)
    if atrp > 3.0:
        regime_mult = 0.85
    elif atrp < 1.0:
        regime_mult = 1.05

    corr_pen = 0.0
    if alloc.get("conflict_buckets"):
        corr_pen = min(0.4, 0.05 * len(alloc.get("conflict_buckets") or []))

    exp = compute_expectancy_edges(
        db,
        scan_pattern_id=scan_pattern_id,
        viability_score=_safe_float(getattr(viability, "viability_score", None), 0.0),
        viability_eligible=(
            bool(getattr(viability, "paper_eligible", True))
            if execution_mode != "live"
            else bool(getattr(viability, "live_eligible", False))
        ),
        regime_multiplier=regime_mult,
        uncertainty_haircut=unc_hair,
        execution_penalty=_safe_float(realism.get("execution_penalty"), 0.0),
        capacity_soft_penalty=_safe_float(cap_eval.get("soft_penalty"), 0.0),
        correlation_penalty=corr_pen,
    )

    net_edge_result: Any | None = None
    net_edge_authoritative = False

    # NetEdgeRanker (Phase E). Shadow/compare modes only log a parallel score.
    # In explicit authoritative mode, its cost-adjusted expected net P&L becomes
    # the capital-lane ranking value and the expectancy floor is enforced.
    try:
        from . import net_edge_ranker as _net_edge

        if _net_edge.mode_is_active():
            _asset = "crypto" if str(symbol).endswith("-USD") else "stock"
            _entry = _safe_float(quote_mid, 0.0) or 0.0
            _stop = _safe_float(
                (ex or {}).get("stop_price") or (ex or {}).get("stop"), 0.0
            ) or (_entry * 0.97 if _entry > 0 else 0.0)
            _target = _safe_float(
                (ex or {}).get("target_price") or (ex or {}).get("target"), 0.0
            ) or None
            if _entry > 0 and _stop > 0:
                net_edge_result = _net_edge.score(
                    db,
                    _net_edge.NetEdgeSignalContext(
                        ticker=symbol or "unknown",
                        asset_class=_asset,
                        scan_pattern_id=scan_pattern_id,
                        raw_prob=_safe_float(
                            getattr(viability, "viability_score", None), 0.0
                        ),
                        entry_price=float(_entry),
                        stop_price=float(_stop),
                        target_price=_target,
                        regime=str((regime_snapshot or {}).get("regime") or "").strip() or None,
                        timeframe=str(
                            getattr(variant, "timeframe", None) or ""
                        ).strip() or None,
                        heuristic_score=_safe_float(exp.get("expected_edge_net"), None),
                    ),
                )
                if _net_edge.mode_is_authoritative() and net_edge_result is not None:
                    ne = _safe_float(getattr(net_edge_result, "expected_net_pnl", None), None)
                    if ne is not None:
                        exp = dict(exp)
                        exp["expected_edge_net"] = ne
                        exp["net_edge_authoritative"] = True
                        exp["net_edge_decision_id"] = getattr(net_edge_result, "decision_id", None)
                        net_edge_authoritative = True
    except Exception as _net_edge_exc:
        logger.debug("[allocator] net_edge shadow score failed: %s", _net_edge_exc)

    floor = _safe_float(getattr(settings, "brain_minimum_net_expectancy_to_trade", 0.0), 0.0)
    shadow = bool(getattr(settings, "brain_expectancy_allocator_shadow_mode", False))
    enforce_exp = (
        bool(getattr(settings, "brain_enforce_net_expectancy_live", False))
        if execution_mode == "live"
        else bool(getattr(settings, "brain_enforce_net_expectancy_paper", True))
    )
    if net_edge_authoritative:
        enforce_exp = True
    regime_label = str(
        (regime_snapshot or {}).get("regime")
        or (regime_snapshot or {}).get("composite")
        or (regime_snapshot or {}).get("regime_composite")
        or ""
    ).strip().lower()

    abstain_code = None
    abstain_text = None
    shadow_override = False

    allocator_shadow_blocked = not bool(alloc.get("allowed_if_enforced", True))
    allocator_live_hard_block = execution_mode == "live" and allocation_block_reason(alloc) is not None
    if allocator_shadow_blocked:
        alloc["shadow_suppression"] = True
        alloc["shadow_suppression_reason"] = alloc.get("blocked_reason")
    if allocator_live_hard_block:
        abstain_code = "portfolio_allocator_blocked"
        abstain_text = str(alloc.get("blocked_reason") or "allocator")
    if pattern_gate.get("hard_block_reason"):
        abstain_code = abstain_code or str(pattern_gate.get("hard_block_reason"))
        abstain_text = abstain_text or ",".join(pattern_gate.get("reasons") or [])

    cap_blocked = bool(cap_eval.get("capacity_blocked"))
    if cap_blocked:
        abstain_code = abstain_code or "capacity_blocked"
        abstain_text = abstain_text or "capacity_governor"

    if mult <= 0 and (
        bool(getattr(settings, "brain_live_deployment_enforcement", False))
        if execution_mode == "live"
        else bool(getattr(settings, "brain_paper_deployment_enforcement", True))
    ):
        abstain_code = abstain_code or "deployment_stage_disabled"
        abstain_text = abstain_text or "zero_size_multiplier"

    if exp["expected_edge_net"] < floor and enforce_exp:
        abstain_code = abstain_code or "negative_net_expectancy"
        abstain_text = abstain_text or "net_expectancy_below_floor"
    if (
        regime_label == "risk_off"
        and execution_mode == "live"
        and exp["expected_edge_net"] < max(0.15, floor)
    ):
        abstain_code = abstain_code or "regime_rotation_risk_off"
        abstain_text = abstain_text or "risk_off_regime_requires_higher_edge"

    # Never override negative net expectancy — block the trade (future-proof risk).
    if shadow and abstain_code == "capacity_blocked":
        shadow_override = True
        abstain_code = None
        abstain_text = None

    primary_selected = abstain_code is None
    candidates_payload: list[dict[str, Any]] = [
        {
            "rank": 0,
            "ticker": symbol,
            "scan_pattern_id": scan_pattern_id,
            "expected_edge_gross": exp["expected_edge_gross"],
            "expected_edge_net": exp["expected_edge_net"],
            "expected_slippage_bps": realism.get("expected_slippage_bps"),
            "expected_fill_probability": realism.get("expected_fill_probability"),
            "size_cap_notional": intended_notional,
            "was_selected": primary_selected,
            "reject_reason_code": None if primary_selected else abstain_code,
            "reject_reason_text": None if primary_selected else abstain_text,
            "reject_detail_json": {
                "net_edge_authoritative": net_edge_authoritative,
                "net_edge_decision_id": exp.get("net_edge_decision_id"),
                "portfolio_allocator_shadow_blocked": allocator_shadow_blocked,
                "portfolio_allocator_live_hard_block": allocator_live_hard_block,
                "portfolio_allocator_blocked_reason": alloc.get("blocked_reason"),
                "pattern_capital_gate": pattern_gate,
            },
        }
    ]

    peer_limit = int(getattr(settings, "brain_peer_candidate_sessions_max", 4) or 0)
    if user_id and peer_limit > 0:
        peers = _peer_automation_sessions(db, user_id=int(user_id), session_id=int(session.id), mode=execution_mode, limit=peer_limit)
        for i, ps in enumerate(peers, start=1):
            candidates_payload.append(
                {
                    "rank": i,
                    "ticker": (ps.symbol or "").upper(),
                    "scan_pattern_id": None,
                    "expected_edge_gross": None,
                    "expected_edge_net": None,
                    "was_selected": False,
                    "reject_reason_code": "peer_session_not_focus",
                    "reject_reason_text": "Different automation session; informational only",
                    "reject_detail_json": {"peer_session_id": int(ps.id)},
                }
            )

    proceed = abstain_code is None
    return {
        "proceed": proceed,
        "abstain_reason_code": abstain_code,
        "abstain_reason_text": abstain_text,
        "recommended_notional": intended_notional if proceed else 0.0,
        "expected_edge_gross": exp["expected_edge_gross"],
        "expected_edge_net": exp["expected_edge_net"],
        "net_edge_authoritative": net_edge_authoritative,
        "net_edge_decision_id": exp.get("net_edge_decision_id"),
        "realism": realism,
        "capacity": cap_eval,
        "allocation_decision": alloc,
        "deployment_stage": deployment_stage,
        "deployment_size_mult": mult,
        "candidates_payload": candidates_payload,
        "correlation_penalty": corr_pen,
        "uncertainty_haircut": exp["uncertainty_haircut"],
        "execution_penalty": _safe_float(realism.get("execution_penalty"), 0.0),
        "shadow_override": shadow_override,
        "capacity_blocked_flag": bool(cap_eval.get("capacity_hard_signals")),
        "erc_allocation": erc_allocation,
        "portfolio_allocator_shadow_blocked": allocator_shadow_blocked,
        "portfolio_allocator_live_hard_block": allocator_live_hard_block,
        "pattern_capital_gate": pattern_gate,
    }

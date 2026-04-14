"""Portfolio conflict/capital allocation scoring for repeatable-edge and live sessions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ...config import settings
from ..broker_manager import get_all_broker_statuses
from .pattern_validation_projection import read_pattern_validation_projection, write_validation_contract

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
        if variant and (variant.family or "").strip().lower():
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
        )
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
        },
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
    )
    proposal.allocation_decision_json = dict(decision)
    return decision


def build_session_allocation_decision(
    db: Session,
    session: Any,
    *,
    user_id: int | None,
    context: str,
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
    ex = viability.execution_readiness_json if isinstance(getattr(viability, "execution_readiness_json", None), dict) else {}
    vol_proxy = None
    try:
        ev = viability.evidence_window_json if isinstance(getattr(viability, "evidence_window_json", None), dict) else {}
        vol_proxy = _safe_float(ev.get("volume_usd_24h") or ev.get("quote_volume_usd"),0.0) or None
    except Exception:
        vol_proxy = None

    alloc = build_session_allocation_decision(db, session, user_id=user_id, context="momentum_entry")

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

    floor = _safe_float(getattr(settings, "brain_minimum_net_expectancy_to_trade", 0.0), 0.0)
    shadow = bool(getattr(settings, "brain_expectancy_allocator_shadow_mode", False))
    enforce_exp = bool(getattr(settings, "brain_enforce_net_expectancy_live", False)) if execution_mode == "live" else bool(
        getattr(settings, "brain_enforce_net_expectancy_paper", True)
    )
    regime_label = str(
        (regime_snapshot or {}).get("regime")
        or (regime_snapshot or {}).get("composite")
        or (regime_snapshot or {}).get("regime_composite")
        or ""
    ).strip().lower()

    abstain_code = None
    abstain_text = None
    shadow_override = False

    if not alloc.get("allowed_if_enforced"):
        abstain_code = "portfolio_allocator_blocked"
        abstain_text = str(alloc.get("blocked_reason") or "allocator")

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
    }


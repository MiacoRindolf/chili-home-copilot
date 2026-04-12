"""Durable PostgreSQL backing for neural momentum (variants + viability + automation audit)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ....models.trading import (
    MomentumStrategyVariant,
    MomentumSymbolViability,
    TradingAutomationEvent,
    TradingAutomationRuntimeSnapshot,
    TradingAutomationSession,
    TradingAutomationSessionBinding,
    TradingAutomationSimulatedFill,
)
from .features import ExecutionReadinessFeatures
from .strategy_params import family_default_params, normalize_strategy_params, summarize_strategy_params
from .variants import iter_momentum_families
from .viability_scope import infer_viability_scope

_log = logging.getLogger(__name__)

KEY_PAPER_EXEC = "momentum_paper_execution"
KEY_LIVE_EXEC = "momentum_live_execution"


def _momentum_tables_present(db: Session) -> bool:
    try:
        bind = db.get_bind()
        names = set(sa_inspect(bind).get_table_names())
    except Exception:
        return False
    return "momentum_strategy_variants" in names and "momentum_symbol_viability" in names


def ensure_momentum_strategy_variants(db: Session) -> None:
    """Ensure seed registry rows exist and carry runner-consumable params."""
    for fam in iter_momentum_families():
        row = (
            db.query(MomentumStrategyVariant)
            .filter(
                MomentumStrategyVariant.family == fam.family_id,
                MomentumStrategyVariant.variant_key == fam.family_id,
                MomentumStrategyVariant.version == int(fam.version),
            )
            .one_or_none()
        )
        if row is None:
            db.add(
                MomentumStrategyVariant(
                    family=fam.family_id,
                    variant_key=fam.family_id,
                    version=int(fam.version),
                    label=fam.label,
                    params_json=family_default_params(fam.family_id),
                    is_active=True,
                    execution_family="coinbase_spot",
                    refinement_meta_json={},
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )
            )
            continue
        row.label = fam.label
        row.execution_family = row.execution_family or "coinbase_spot"
        row.params_json = normalize_strategy_params(row.params_json, family_id=fam.family_id)
        if not isinstance(row.refinement_meta_json, dict):
            row.refinement_meta_json = {}
        row.updated_at = datetime.utcnow()


def _variant_id_for_family(db: Session, family_id: str, version: int) -> Optional[int]:
    row = (
        db.query(MomentumStrategyVariant.id)
        .filter(
            MomentumStrategyVariant.family == family_id,
            MomentumStrategyVariant.variant_key == family_id,
            MomentumStrategyVariant.version == int(version),
        )
        .one_or_none()
    )
    return int(row[0]) if row else None


def active_variant_for_family(db: Session, family_id: str) -> MomentumStrategyVariant | None:
    fam = (family_id or "").strip()
    if not fam:
        return None
    return (
        db.query(MomentumStrategyVariant)
        .filter(
            MomentumStrategyVariant.family == fam,
            MomentumStrategyVariant.is_active.is_(True),
        )
        .order_by(MomentumStrategyVariant.version.desc(), MomentumStrategyVariant.id.desc())
        .first()
    )


def variant_for_id(db: Session, variant_id: int) -> MomentumStrategyVariant | None:
    return (
        db.query(MomentumStrategyVariant)
        .filter(MomentumStrategyVariant.id == int(variant_id))
        .one_or_none()
    )


def _lane_for_session(mode: str | None, state: str | None) -> str:
    m = (mode or "paper").strip().lower()
    st = (state or "").strip().lower()
    if m == "paper":
        return "simulation"
    if m == "live" and st in ("live_arm_pending", "armed_pending_runner"):
        return "live-armed"
    if m == "live":
        return "live"
    return "simulation"


def _paper_exec(snap: dict[str, Any]) -> dict[str, Any]:
    pe = snap.get(KEY_PAPER_EXEC)
    return pe if isinstance(pe, dict) else {}


def _live_exec(snap: dict[str, Any]) -> dict[str, Any]:
    le = snap.get(KEY_LIVE_EXEC)
    return le if isinstance(le, dict) else {}


def default_session_binding(
    *,
    venue: str,
    mode: str,
    execution_family: str,
    chart_provider: str | None = None,
    quote_source: str | None = None,
    gating_reason: str | None = None,
) -> dict[str, Any]:
    chart = (quote_source or chart_provider or "massive").strip().lower()
    sim_mode = (mode or "paper").strip().lower()
    source_provider = venue.strip().lower() if sim_mode == "live" else chart
    source_exchange = venue.strip().lower() if sim_mode == "live" else None
    if sim_mode == "live":
        fidelity = "venue_guarded_live"
        latency = "venue_realtime"
    elif chart in ("massive", "polygon", "massive_ws", "fetch_quote", "test"):
        fidelity = "consolidated_quote_sim"
        latency = "realtime_consolidated"
    else:
        fidelity = "provider_sim"
        latency = "derived"
    return {
        "discovery_provider": "massive",
        "chart_provider": chart,
        "signal_provider": "momentum_brain",
        "source_of_truth_provider": source_provider,
        "source_of_truth_exchange": source_exchange,
        "bar_builder": "provider_ohlcv",
        "latency_class": latency,
        "simulation_fidelity": fidelity,
        "gating_reason": gating_reason,
        "meta_json": {
            "execution_family": execution_family,
            "provider_hierarchy": ["massive", "polygon", "yfinance"],
        },
    }


def build_runtime_snapshot_values(
    sess: TradingAutomationSession,
    *,
    variant: MomentumStrategyVariant | None = None,
    viability: MomentumSymbolViability | None = None,
    trade_count: int | None = None,
    last_action: str | None = None,
    execution_readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
    pe = _paper_exec(snap)
    le = _live_exec(snap)
    pos = pe.get("position") if isinstance(pe.get("position"), dict) else None
    live_pos = le.get("position") if isinstance(le.get("position"), dict) else None
    lane = _lane_for_session(sess.mode, sess.state)
    runtime_seconds: int | None = None
    anchor = sess.started_at or sess.created_at
    if anchor:
        runtime_seconds = max(0, int((datetime.utcnow() - anchor).total_seconds()))

    conf = None
    if viability is not None:
        try:
            conf = float(viability.viability_score)
        except (TypeError, ValueError):
            conf = None
    if conf is None:
        risk = snap.get("momentum_risk")
        if isinstance(risk, dict):
            try:
                conf = 1.0 if risk.get("allowed") else 0.25
            except Exception:
                conf = None

    latest_levels: dict[str, Any] = {}
    last_price = None
    if pos:
        latest_levels.update(
            {
                "entry": pos.get("entry_price"),
                "stop": pos.get("stop_price"),
                "target": pos.get("target_price"),
            }
        )
        last_price = pe.get("last_mid")
        position_state = "long"
        pnl = pe.get("realized_pnl_usd")
    elif live_pos:
        latest_levels.update(
            {
                "entry": live_pos.get("avg_entry_price"),
                "stop": live_pos.get("stop_price"),
                "target": live_pos.get("target_price"),
            }
        )
        last_price = le.get("last_mid")
        position_state = "live-long"
        pnl = le.get("realized_pnl_usd")
    else:
        if sess.mode == "paper":
            last_price = pe.get("last_mid")
            pnl = pe.get("realized_pnl_usd")
        else:
            last_price = le.get("last_mid")
            pnl = le.get("realized_pnl_usd")
        position_state = "flat"

    thesis_bits = []
    if variant is not None:
        thesis_bits.append(f"{variant.label}")
    thesis_bits.append(f"{sess.symbol} is in {sess.state.replace('_', ' ')}")
    if latest_levels.get("entry"):
        thesis_bits.append(
            f"tracking entry {latest_levels.get('entry')} with stop {latest_levels.get('stop')} and target {latest_levels.get('target')}"
        )
    elif viability is not None:
        thesis_bits.append(f"viability {round(float(viability.viability_score or 0.0), 3)}")
    thesis = ". ".join(str(x) for x in thesis_bits if x)

    readiness_payload = execution_readiness or {}
    if not readiness_payload and viability is not None and isinstance(viability.execution_readiness_json, dict):
        readiness_payload = dict(viability.execution_readiness_json)
    risk = snap.get("momentum_risk")
    if isinstance(risk, dict):
        readiness_payload = {
            **readiness_payload,
            "allowed": bool(risk.get("allowed", True)),
            "severity": risk.get("severity"),
            "reasons": list(risk.get("errors") or [])[:4] + list(risk.get("warnings") or [])[:2],
        }

    metrics_json = {
        "event_correlation_id": sess.correlation_id,
        "strategy_params_summary": summarize_strategy_params(variant.params_json if variant is not None else {}),
        "paper_execution": {
            "tick_count": pe.get("tick_count"),
            "last_quote_source": pe.get("last_quote_source"),
            "cooldown_until_utc": pe.get("cooldown_until_utc"),
            "last_exit_reason": pe.get("last_exit_reason"),
        },
        "live_execution": {
            "tick_count": le.get("tick_count"),
            "entry_order_id": le.get("entry_order_id"),
            "exit_order_id": le.get("exit_order_id"),
            "cooldown_until_utc": le.get("cooldown_until_utc"),
            "last_exit_reason": le.get("last_exit_reason"),
        },
    }
    if last_price is not None:
        latest_levels["last_price"] = last_price

    return {
        "user_id": sess.user_id,
        "symbol": sess.symbol,
        "mode": sess.mode,
        "lane": lane,
        "state": sess.state,
        "strategy_family": variant.family if variant is not None else None,
        "strategy_label": variant.label if variant is not None else None,
        "thesis": thesis,
        "confidence": conf,
        "conviction": conf,
        "current_position_state": position_state,
        "last_action": last_action or pe.get("last_exit_reason") or le.get("last_exit_reason") or sess.state,
        "runtime_seconds": runtime_seconds,
        "simulated_pnl_usd": pnl,
        "trade_count": int(trade_count or 0),
        "last_price": last_price,
        "execution_readiness_json": readiness_payload,
        "latest_levels_json": latest_levels,
        "metrics_json": metrics_json,
        "updated_at": datetime.utcnow(),
    }


def upsert_trading_automation_runtime_snapshot(
    db: Session,
    *,
    session_id: int,
    values: dict[str, Any],
) -> TradingAutomationRuntimeSnapshot:
    stmt = pg_insert(TradingAutomationRuntimeSnapshot).values(session_id=int(session_id), **values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["session_id"],
        set_=dict(values),
    )
    db.execute(stmt)
    db.flush()
    return db.query(TradingAutomationRuntimeSnapshot).filter(TradingAutomationRuntimeSnapshot.session_id == int(session_id)).one()


def upsert_trading_automation_session_binding(
    db: Session,
    *,
    session_id: int,
    values: dict[str, Any],
) -> TradingAutomationSessionBinding:
    stmt = pg_insert(TradingAutomationSessionBinding).values(session_id=int(session_id), **values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["session_id"],
        set_=dict(values),
    )
    db.execute(stmt)
    db.flush()
    return db.query(TradingAutomationSessionBinding).filter(TradingAutomationSessionBinding.session_id == int(session_id)).one()


def append_trading_automation_simulated_fill(
    db: Session,
    *,
    session_id: int,
    symbol: str,
    lane: str,
    action: str,
    fill_type: str | None = None,
    side: str | None = None,
    quantity: float | None = None,
    price: float | None = None,
    reference_price: float | None = None,
    fees_usd: float | None = None,
    pnl_usd: float | None = None,
    position_state_before: str | None = None,
    position_state_after: str | None = None,
    reason: str | None = None,
    marker_json: Optional[dict[str, Any]] = None,
) -> TradingAutomationSimulatedFill:
    row = TradingAutomationSimulatedFill(
        session_id=int(session_id),
        symbol=symbol,
        lane=lane,
        action=action,
        fill_type=fill_type,
        side=side,
        quantity=quantity,
        price=price,
        reference_price=reference_price,
        fees_usd=fees_usd,
        pnl_usd=pnl_usd,
        position_state_before=position_state_before,
        position_state_after=position_state_after,
        reason=reason,
        marker_json=dict(marker_json or {}),
    )
    db.add(row)
    db.flush()
    return row


def persist_neural_momentum_tick(
    db: Session,
    *,
    row_dicts: list[dict[str, Any]],
    regime_snapshot: dict[str, Any],
    features: ExecutionReadinessFeatures,
    correlation_id: Optional[str],
    source_node_id: Optional[str],
) -> int:
    """Upsert ``MomentumSymbolViability`` for each computed row; returns rows written."""
    if not _momentum_tables_present(db):
        _log.debug("momentum persistence skipped (tables missing)")
        return 0

    ensure_momentum_strategy_variants(db)

    exec_json = features.to_public_dict()
    now = datetime.utcnow()
    n = 0
    for row in row_dicts:
        fam_id = row.get("family_id")
        fam_ver = int(row.get("family_version") or 1)
        if not fam_id:
            continue
        active_variant = active_variant_for_family(db, str(fam_id))
        if active_variant is not None:
            vid = int(active_variant.id)
        else:
            vid = _variant_id_for_family(db, str(fam_id), fam_ver)
        if vid is None:
            _log.warning("momentum persistence: no variant row for family=%s", fam_id)
            continue

        explain: dict[str, Any] = {
            "rationale": row.get("rationale"),
            "warnings": row.get("warnings") or [],
            "label": row.get("label"),
            "entry_style": row.get("entry_style"),
            "default_stop_logic": row.get("default_stop_logic"),
            "default_exit_logic": row.get("default_exit_logic"),
            "regime_fit": row.get("regime_fit"),
            "freshness_hint": row.get("freshness_hint"),
        }
        evidence_window: dict[str, Any] = {"note": "phase2_placeholder"}

        stmt = pg_insert(MomentumSymbolViability).values(
            symbol=str(row.get("symbol") or ""),
            scope=infer_viability_scope(row.get("symbol"), explicit=row.get("scope")),
            variant_id=vid,
            viability_score=float(row.get("viability") or 0.0),
            paper_eligible=bool(row.get("paper_eligible", True)),
            live_eligible=bool(row.get("live_eligible", False)),
            freshness_ts=now,
            regime_snapshot_json=dict(regime_snapshot),
            execution_readiness_json=dict(exec_json),
            explain_json=explain,
            evidence_window_json=evidence_window,
            source_node_id=source_node_id,
            correlation_id=correlation_id,
            created_at=now,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_momentum_symbol_viability_sym_var",
            set_={
                "viability_score": float(row.get("viability") or 0.0),
                "scope": infer_viability_scope(row.get("symbol"), explicit=row.get("scope")),
                "paper_eligible": bool(row.get("paper_eligible", True)),
                "live_eligible": bool(row.get("live_eligible", False)),
                "freshness_ts": now,
                "regime_snapshot_json": dict(regime_snapshot),
                "execution_readiness_json": dict(exec_json),
                "explain_json": explain,
                "evidence_window_json": evidence_window,
                "source_node_id": source_node_id,
                "correlation_id": correlation_id,
                "updated_at": now,
            },
        )
        db.execute(stmt)
        n += 1
    return n


def create_trading_automation_session(
    db: Session,
    *,
    user_id: Optional[int] = None,
    venue: str = "coinbase",
    execution_family: str = "coinbase_spot",
    mode: str = "paper",
    symbol: str = "",
    variant_id: int = 0,
    state: str = "idle",
    risk_snapshot_json: Optional[dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
    source_node_id: Optional[str] = None,
    source_paper_session_id: Optional[int] = None,
) -> TradingAutomationSession:
    """Minimal session constructor for tests / future runner (no FSM logic)."""
    sess = TradingAutomationSession(
        user_id=user_id,
        venue=venue,
        execution_family=execution_family,
        mode=mode,
        symbol=symbol,
        variant_id=variant_id,
        state=state,
        risk_snapshot_json=dict(risk_snapshot_json or {}),
        correlation_id=correlation_id,
        source_node_id=source_node_id,
        source_paper_session_id=source_paper_session_id,
        started_at=datetime.utcnow(),
    )
    db.add(sess)
    db.flush()
    try:
        variant = (
            db.query(MomentumStrategyVariant)
            .filter(MomentumStrategyVariant.id == int(variant_id))
            .one_or_none()
        )
        binding = default_session_binding(
            venue=venue,
            mode=mode,
            execution_family=execution_family,
        )
        upsert_trading_automation_session_binding(db, session_id=int(sess.id), values=binding)
        snap_values = build_runtime_snapshot_values(sess, variant=variant, trade_count=0)
        upsert_trading_automation_runtime_snapshot(db, session_id=int(sess.id), values=snap_values)
    except Exception:
        _log.warning("autopilot runtime bootstrap skipped for session %s", sess.id, exc_info=True)
    return sess


def append_trading_automation_event(
    db: Session,
    session_id: int,
    event_type: str,
    payload_json: dict[str, Any],
    *,
    correlation_id: Optional[str] = None,
    source_node_id: Optional[str] = None,
) -> TradingAutomationEvent:
    ev = TradingAutomationEvent(
        session_id=session_id,
        ts=datetime.utcnow(),
        event_type=event_type,
        payload_json=dict(payload_json),
        correlation_id=correlation_id,
        source_node_id=source_node_id,
    )
    db.add(ev)
    db.flush()
    return ev

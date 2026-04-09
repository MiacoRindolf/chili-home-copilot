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
    TradingAutomationSession,
)
from .features import ExecutionReadinessFeatures
from .variants import iter_momentum_families

_log = logging.getLogger(__name__)


def _momentum_tables_present(db: Session) -> bool:
    try:
        bind = db.get_bind()
        names = set(sa_inspect(bind).get_table_names())
    except Exception:
        return False
    return "momentum_strategy_variants" in names and "momentum_symbol_viability" in names


def ensure_momentum_strategy_variants(db: Session) -> None:
    """Upsert registry rows from ``variants.iter_momentum_families`` (idempotent)."""
    now = datetime.utcnow()
    for fam in iter_momentum_families():
        stmt = pg_insert(MomentumStrategyVariant).values(
            family=fam.family_id,
            variant_key=fam.family_id,
            version=int(fam.version),
            label=fam.label,
            params_json={},
            is_active=True,
            execution_family="coinbase_spot",
            created_at=now,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_momentum_strategy_variant_fkv",
            set_={
                "label": fam.label,
                "is_active": True,
                "updated_at": now,
            },
        )
        db.execute(stmt)


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

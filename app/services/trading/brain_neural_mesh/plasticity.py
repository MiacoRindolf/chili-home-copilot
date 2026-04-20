"""Phase 2C: Hebbian plasticity engine.

Outcome-driven edge-weight updates. On trade close, find the edges in the
activation path that led to the entry signal and reinforce (win) or attenuate
(loss) those specific edges.

Safety model:
- Feature flag ``chili_mesh_plasticity_enabled`` (default False).
- ``chili_mesh_plasticity_dry_run`` (default True) writes audit rows without
  mutating edge weights — use this for the first week of live rollout.
- Per-edge cooldown: at most one applied mutation per edge within the last
  N trade_outcome mutations (default 5).
- Daily |Δw| budget per edge-type (default 0.5): further updates that day are
  logged with ``reason='budget_capped'`` but not applied.
- ``operator_output`` edges do not learn (human authority).

Exposed entry point: ``apply_outcome_plasticity(db, trade_id, ...)``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import (
    BrainActivationPathLog,
    BrainGraphEdge,
    BrainGraphEdgeMutation,
    BrainNodeState,
    Trade,
)

_log = logging.getLogger(__name__)
_LOG_PREFIX = "[mesh.plasticity]"

# Edge-type plasticity scale. operator_output is 0.0: human authority does not
# learn from outcomes.
_EDGE_TYPE_PLASTICITY_SCALE: dict[str, float] = {
    "dataflow": 1.0,
    "evidence": 1.5,
    "veto": 1.5,
    "feedback": 1.0,
    "control": 1.0,
    "operator_output": 0.0,
}

MIN_WEIGHT = 0.05
MAX_WEIGHT = 3.0

# pnl_r = pnl / risked_capital. Noise band around zero produces reward_sign=0
# so neutral outcomes don't move weights.
DEFAULT_MIN_WIN_R = 0.10
DEFAULT_MIN_LOSS_R = 0.10
# Clip the magnitude to avoid extreme pnl events single-handedly dominating.
MAX_PNL_R = 2.0


def compute_plasticity_delta(
    *,
    pnl_r: float,
    edge_weight: float,
    edge_type: str,
    source_confidence: float,
    target_confidence: float,
    learning_rate: float = 0.05,
    min_win_r: float = DEFAULT_MIN_WIN_R,
    min_loss_r: float = DEFAULT_MIN_LOSS_R,
) -> tuple[float, float]:
    """Pure Hebbian-with-reward update. Returns ``(delta_w, new_w)``.

    reward_sign = +1 if pnl_r > min_win_r, -1 if pnl_r < -min_loss_r, else 0
    (noise band → no update). Magnitude is clipped to ±MAX_PNL_R to cap
    individual-trade influence.
    """
    if pnl_r > min_win_r:
        reward_sign = 1.0
    elif pnl_r < -min_loss_r:
        reward_sign = -1.0
    else:
        reward_sign = 0.0

    type_scale = _EDGE_TYPE_PLASTICITY_SCALE.get(edge_type, 1.0)
    if reward_sign == 0.0 or type_scale == 0.0:
        return 0.0, float(edge_weight)

    magnitude = min(abs(float(pnl_r)), MAX_PNL_R)
    src_c = max(0.0, min(1.0, float(source_confidence)))
    tgt_c = max(0.0, min(1.0, float(target_confidence)))

    delta = float(learning_rate) * reward_sign * magnitude * src_c * tgt_c * type_scale
    new_w = max(MIN_WEIGHT, min(MAX_WEIGHT, float(edge_weight) + delta))
    return delta, new_w


def _edge_in_cooldown(db: Session, edge_id: int, cooldown_trades: int) -> bool:
    """Return True if this edge has an applied trade-outcome mutation within the last
    ``cooldown_trades`` distinct trade_ids."""
    if cooldown_trades <= 0:
        return False
    # Most recent N distinct trade_ids in the mutation log (applied, non-dry-run).
    rows = db.execute(
        text(
            """
            SELECT DISTINCT (evidence_ref->>'trade_id')::int AS tid
            FROM brain_graph_edge_mutations
            WHERE reason = 'trade_outcome'
              AND applied = TRUE
              AND dry_run = FALSE
              AND evidence_ref ? 'trade_id'
            ORDER BY tid DESC
            LIMIT :n
            """
        ),
        {"n": cooldown_trades},
    ).fetchall()
    if not rows:
        return False
    recent_ids = [int(r[0]) for r in rows]
    # If any applied non-dry-run mutation on this edge references one of those trade_ids, cooldown.
    count = db.execute(
        text(
            """
            SELECT COUNT(*) FROM brain_graph_edge_mutations
            WHERE edge_id = :eid
              AND reason = 'trade_outcome'
              AND applied = TRUE
              AND dry_run = FALSE
              AND (evidence_ref->>'trade_id')::int = ANY(:ids)
            """
        ),
        {"eid": edge_id, "ids": recent_ids},
    ).scalar()
    return int(count or 0) > 0


def _edge_drift_from_baseline(db: Session, edge_id: int, current_weight: float) -> float:
    """Return |current_weight - baseline_weight| for this edge.

    If no baseline exists (e.g., an edge added after go-live), returns 0.0 so
    the drift check is effectively disabled for that edge.
    """
    baseline = db.execute(
        text(
            "SELECT baseline_weight FROM brain_graph_edge_weight_baseline WHERE edge_id = :eid"
        ),
        {"eid": edge_id},
    ).scalar()
    if baseline is None:
        return 0.0
    return abs(float(current_weight) - float(baseline))


def _daily_budget_used(db: Session, edge_type: str) -> float:
    """Sum of |Δw| applied today for this edge_type (non-dry-run only)."""
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    val = db.execute(
        text(
            """
            SELECT COALESCE(SUM(ABS(new_weight - old_weight)), 0.0)
            FROM brain_graph_edge_mutations m
            JOIN brain_graph_edges e ON e.id = m.edge_id
            WHERE m.applied = TRUE
              AND m.dry_run = FALSE
              AND m.applied_at >= :since
              AND e.edge_type = :etype
            """
        ),
        {"since": since, "etype": edge_type},
    ).scalar()
    return float(val or 0.0)


def _fetch_path_edge_ids(db: Session, correlation_id: str) -> list[int]:
    """Return distinct edge_ids in the propagation path for this correlation_id."""
    rows = (
        db.query(BrainActivationPathLog.edge_id)
        .filter(BrainActivationPathLog.correlation_id == correlation_id)
        .filter(BrainActivationPathLog.edge_id.isnot(None))
        .distinct()
        .all()
    )
    return [int(r[0]) for r in rows if r[0] is not None]


def apply_outcome_plasticity(
    db: Session,
    *,
    trade_id: int,
    pnl: float,
    risked_capital: float,
    correlation_id: Optional[str],
) -> dict[str, int]:
    """Apply Hebbian updates to edges in the entry signal's activation path.

    Returns a summary dict: ``{proposed, applied, skipped_cooldown, skipped_budget,
    skipped_operator, skipped_noop}``.
    Safe no-op when plasticity is disabled or the path log is empty.
    """
    out = {
        "proposed": 0,
        "applied": 0,
        "skipped_cooldown": 0,
        "skipped_budget": 0,
        "skipped_operator": 0,
        "skipped_noop": 0,
        "skipped_drift": 0,
    }
    if not getattr(settings, "chili_mesh_plasticity_enabled", False):
        return out
    if not correlation_id:
        return out
    if risked_capital <= 0.0:
        return out

    dry_run = bool(getattr(settings, "chili_mesh_plasticity_dry_run", True))
    lr = float(getattr(settings, "chili_mesh_plasticity_learning_rate", 0.05))
    daily_budget = float(getattr(settings, "chili_mesh_plasticity_daily_budget", 0.5))
    cooldown_trades = int(getattr(settings, "chili_mesh_plasticity_per_edge_cooldown_trades", 5))
    drift_cap = float(getattr(settings, "chili_mesh_plasticity_drift_cap", 0.0))

    pnl_r = float(pnl) / float(risked_capital)
    edge_ids = _fetch_path_edge_ids(db, correlation_id)
    if not edge_ids:
        return out

    # Budget used so far today per edge_type (cached — no need to re-query each edge).
    budget_used_by_type: dict[str, float] = {}

    for eid in edge_ids:
        edge = db.query(BrainGraphEdge).filter(BrainGraphEdge.id == eid).one_or_none()
        if edge is None:
            continue
        etype = (edge.edge_type or "dataflow")

        # Operator-output edges don't learn.
        if _EDGE_TYPE_PLASTICITY_SCALE.get(etype, 1.0) == 0.0:
            out["skipped_operator"] += 1
            continue

        # Pull node states for confidences
        src_state = (
            db.query(BrainNodeState)
            .filter(BrainNodeState.node_id == edge.source_node_id)
            .one_or_none()
        )
        tgt_state = (
            db.query(BrainNodeState)
            .filter(BrainNodeState.node_id == edge.target_node_id)
            .one_or_none()
        )
        src_c = float(src_state.confidence) if src_state else 0.5
        tgt_c = float(tgt_state.confidence) if tgt_state else 0.5

        delta, new_w = compute_plasticity_delta(
            pnl_r=pnl_r,
            edge_weight=float(edge.weight),
            edge_type=etype,
            source_confidence=src_c,
            target_confidence=tgt_c,
            learning_rate=lr,
        )
        out["proposed"] += 1

        if delta == 0.0:
            out["skipped_noop"] += 1
            continue

        # Drift circuit breaker: if this edge has drifted too far from its
        # pre-live baseline, refuse further mutations (tamper detection /
        # runaway protection).
        if not dry_run and drift_cap > 0.0:
            current_drift = _edge_drift_from_baseline(db, eid, float(edge.weight))
            prospective_drift = _edge_drift_from_baseline(db, eid, new_w)
            if current_drift > drift_cap or prospective_drift > drift_cap:
                _audit(
                    db, edge, new_weight=float(edge.weight), delta=0.0,
                    reason="drift_cap", applied=False, dry_run=False,
                    trade_id=trade_id, pnl=pnl, correlation_id=correlation_id,
                )
                out["skipped_drift"] += 1
                continue

        if not dry_run and _edge_in_cooldown(db, eid, cooldown_trades):
            _audit(
                db, edge, new_weight=float(edge.weight), delta=0.0,
                reason="cooldown", applied=False, dry_run=False,
                trade_id=trade_id, pnl=pnl, correlation_id=correlation_id,
            )
            out["skipped_cooldown"] += 1
            continue

        if not dry_run:
            used = budget_used_by_type.get(etype)
            if used is None:
                used = _daily_budget_used(db, etype)
                budget_used_by_type[etype] = used
            if used + abs(delta) > daily_budget:
                _audit(
                    db, edge, new_weight=float(edge.weight), delta=0.0,
                    reason="budget_capped", applied=False, dry_run=False,
                    trade_id=trade_id, pnl=pnl, correlation_id=correlation_id,
                )
                out["skipped_budget"] += 1
                continue
            budget_used_by_type[etype] = used + abs(delta)

        _audit(
            db, edge, new_weight=new_w, delta=delta,
            reason="trade_outcome", applied=(not dry_run), dry_run=dry_run,
            trade_id=trade_id, pnl=pnl, correlation_id=correlation_id,
        )
        if not dry_run:
            edge.weight = new_w
            edge.updated_at = datetime.now(timezone.utc)
            db.add(edge)
        out["applied"] += 1

    db.commit()
    _log.info(
        "%s plasticity trade=%s corr=%s pnl_r=%.3f summary=%s",
        _LOG_PREFIX, trade_id, correlation_id, pnl_r, out,
    )
    return out


def compute_risked_capital(trade: Trade) -> float:
    """Return 1R (risk per share * quantity) for a trade, or 0 if undefined.

    Uses stop_loss for the 1R calculation; if absent, risk is undefined (returns 0)
    and plasticity will skip this trade.
    """
    try:
        entry = float(trade.entry_price or 0.0)
        qty = float(trade.quantity or 0.0)
        stop = float(trade.stop_loss) if trade.stop_loss is not None else None
    except (TypeError, ValueError):
        return 0.0
    if stop is None or entry <= 0.0 or qty <= 0.0:
        return 0.0
    per_share_risk = abs(entry - stop)
    return per_share_risk * qty


def handle_trade_close_plasticity(db: Session, trade: Trade) -> dict[str, int]:
    """Hook called from close_trade / paper close / autotrader close.

    Idempotent and safe: returns a zero summary when plasticity disabled,
    when the trade has no mesh correlation, or when risked_capital is undefined.
    Never raises to caller.
    """
    empty = {
        "proposed": 0, "applied": 0,
        "skipped_cooldown": 0, "skipped_budget": 0,
        "skipped_operator": 0, "skipped_noop": 0,
    }
    try:
        if trade is None or getattr(trade, "status", None) != "closed":
            return empty
        corr = getattr(trade, "mesh_entry_correlation_id", None)
        if not corr:
            return empty
        risked = compute_risked_capital(trade)
        if risked <= 0.0:
            return empty
        pnl = float(trade.pnl or 0.0)
        return apply_outcome_plasticity(
            db,
            trade_id=int(trade.id),
            pnl=pnl,
            risked_capital=risked,
            correlation_id=corr,
        )
    except Exception as e:
        _log.warning("%s handle_trade_close_plasticity failed: %s", _LOG_PREFIX, e)
        return empty


def _audit(
    db: Session,
    edge: BrainGraphEdge,
    *,
    new_weight: float,
    delta: float,
    reason: str,
    applied: bool,
    dry_run: bool,
    trade_id: int,
    pnl: float,
    correlation_id: Optional[str],
) -> None:
    row = BrainGraphEdgeMutation(
        edge_id=edge.id,
        old_weight=float(edge.weight),
        new_weight=float(new_weight),
        old_min_source_confidence=float(
            getattr(edge, "min_source_confidence", 0.0) or 0.0
        ),
        new_min_source_confidence=float(
            getattr(edge, "min_source_confidence", 0.0) or 0.0
        ),
        reason=reason,
        evidence_ref={
            "trade_id": int(trade_id),
            "pnl": float(pnl),
            "delta": float(delta),
            "edge_source": edge.source_node_id,
            "edge_target": edge.target_node_id,
            "edge_type": edge.edge_type,
        },
        delta_source="hebbian_trade_outcome",
        applied=bool(applied),
        dry_run=bool(dry_run),
        correlation_id=correlation_id,
        applied_at=datetime.now(timezone.utc),
    )
    db.add(row)

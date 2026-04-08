"""Neural-backed viable momentum strategies for operator API (DB + hot BrainNodeState merge)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import BrainNodeState, MomentumStrategyVariant, MomentumSymbolViability, TradingAutomationSession
from ..brain_neural_mesh.schema import mesh_enabled
from ..execution_family_registry import momentum_execution_seam_meta
from .pipeline import VIABILITY_NODE_ID
from .persistence import _variant_id_for_family

_log = logging.getLogger(__name__)


def _momentum_tables_present(db: Session) -> bool:
    try:
        bind = db.get_bind()
        names = set(sa_inspect(bind).get_table_names())
    except Exception:
        return False
    return "momentum_symbol_viability" in names and "momentum_strategy_variants" in names


def _parse_iso_utc(s: Any) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    try:
        # fromisoformat handles ...Z in 3.11+
        t = s.replace("Z", "+00:00")
        return datetime.fromisoformat(t)
    except Exception:
        return None


def _hot_rows_for_symbol(db: Session, symbol: str) -> tuple[list[dict[str, Any]], Optional[datetime]]:
    """Return viability-shaped rows from viability pool local_state if present."""
    sym = symbol.strip().upper()
    st = db.query(BrainNodeState).filter(BrainNodeState.node_id == VIABILITY_NODE_ID).one_or_none()
    if not st or not isinstance(st.local_state, dict):
        return [], None
    ls = st.local_state
    raw = ls.get("viability_rows") or ls.get("top_preview")
    if not isinstance(raw, list):
        return [], None
    last = _parse_iso_utc(ls.get("last_tick_utc"))
    out: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol") or "").strip().upper() != sym:
            continue
        out.append(row)
    return out, last


def _merge_row(
    db: Session,
    symbol: str,
    db_row: MomentumSymbolViability,
    variant: MomentumStrategyVariant,
    hot_by_family: dict[tuple[str, int], dict[str, Any]],
    hot_ts: Optional[datetime],
    live_readiness_overlay: dict[str, Any],
) -> dict[str, Any]:
    """Prefer hot-path row when newer than durable freshness_ts."""
    fam = variant.family
    ver = int(variant.version)
    hot = hot_by_family.get((fam, ver))
    use_hot = False

    def _naive(dt: Optional[datetime]) -> Optional[datetime]:
        if dt is None:
            return None
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    if hot and hot_ts:
        ht = _naive(hot_ts)
        db_ts = _naive(db_row.freshness_ts)
        if ht and db_ts and ht > db_ts:
            use_hot = True

    viability_score = float(db_row.viability_score)
    paper_eligible = bool(db_row.paper_eligible)
    live_eligible = bool(db_row.live_eligible)
    regime = dict(db_row.regime_snapshot_json or {})
    exec_r = dict(db_row.execution_readiness_json or {})
    explain = dict(db_row.explain_json or {})
    evidence = dict(db_row.evidence_window_json or {})
    warnings = list(explain.get("warnings") or [])
    rationale = explain.get("rationale")
    regime_fit = explain.get("regime_fit")
    entry_style = explain.get("entry_style")
    stop_logic = explain.get("default_stop_logic")
    exit_logic = explain.get("default_exit_logic")
    freshness_ts = db_row.freshness_ts
    source = "db"

    if use_hot and hot:
        source = "neural_hot"
        try:
            viability_score = float(hot.get("viability", viability_score))
        except (TypeError, ValueError):
            pass
        paper_eligible = bool(hot.get("paper_eligible", paper_eligible))
        live_eligible = bool(hot.get("live_eligible", live_eligible))
        rationale = hot.get("rationale", rationale)
        regime_fit = hot.get("regime_fit", regime_fit)
        entry_style = hot.get("entry_style", entry_style)
        stop_logic = hot.get("default_stop_logic", stop_logic)
        exit_logic = hot.get("default_exit_logic", exit_logic)
        hw = hot.get("warnings")
        if isinstance(hw, list):
            warnings = list(hw)
        freshness_ts = hot_ts or freshness_ts

    if live_readiness_overlay:
        exec_r = {**exec_r, **live_readiness_overlay}

    return {
        "variant_id": variant.id,
        "family": fam,
        "strategy_family": fam,
        "variant_key": variant.variant_key,
        "label": variant.label,
        "version": ver,
        "viability_score": round(viability_score, 4),
        "paper_eligible": paper_eligible,
        "live_eligible": live_eligible,
        "freshness_ts": freshness_ts.isoformat() if hasattr(freshness_ts, "isoformat") else str(freshness_ts),
        "regime": regime,
        "execution_readiness": exec_r,
        "rationale": rationale,
        "evidence": evidence,
        "warnings": warnings,
        "regime_fit": regime_fit,
        "entry_style": entry_style,
        "stop_logic": stop_logic,
        "exit_logic": exit_logic,
        "execution_family": variant.execution_family or "coinbase_spot",
        "source_layer": source,
        "actions": {
            "can_run_paper": bool(paper_eligible),
            "can_arm_live": bool(live_eligible),
        },
    }


def _hot_index(rows: list[dict[str, Any]]) -> dict[tuple[str, int], dict[str, Any]]:
    idx: dict[tuple[str, int], dict[str, Any]] = {}
    for r in rows:
        fid = str(r.get("family_id") or "")
        try:
            ver = int(r.get("family_version") or 1)
        except (TypeError, ValueError):
            ver = 1
        if fid:
            idx[(fid, ver)] = r
    return idx


def _session_summary(
    db: Session,
    *,
    user_id: Optional[int],
    symbol: str,
    variant_id: int,
) -> dict[str, Any]:
    since = datetime.utcnow() - timedelta(days=7)
    q = db.query(TradingAutomationSession).filter(
        TradingAutomationSession.symbol == symbol,
        TradingAutomationSession.variant_id == variant_id,
        TradingAutomationSession.started_at >= since,
    )
    if user_id is not None:
        q = q.filter(TradingAutomationSession.user_id == user_id)
    rows = q.all()
    paper_n = sum(1 for r in rows if r.mode == "paper")
    live_n = sum(1 for r in rows if r.mode == "live")
    armed = sum(1 for r in rows if r.state in ("armed_pending_runner", "live_arm_pending", "live_confirmed"))
    return {
        "sessions_7d_paper": paper_n,
        "sessions_7d_live": live_n,
        "sessions_7d_armed_or_pending": armed,
    }


def build_viable_strategies_payload(
    db: Session,
    *,
    symbol: str,
    user_id: Optional[int] = None,
    enrich_coinbase: bool = True,
    operator_mode: str = "paper",
) -> dict[str, Any]:
    """Assemble stable JSON for GET /api/trading/momentum/viable."""
    sym = symbol.strip().upper()
    refreshed_at = datetime.utcnow().isoformat()
    warnings: list[str] = []
    strategies: list[dict[str, Any]] = []

    neural_status = {
        "mesh_enabled": bool(mesh_enabled()),
        "momentum_neural_enabled": bool(settings.chili_momentum_neural_enabled),
        "coinbase_adapter_enabled": bool(settings.chili_coinbase_spot_adapter_enabled),
        "execution_seam": momentum_execution_seam_meta(),
    }

    if not sym:
        return {
            "symbol": "",
            "refreshed_at": refreshed_at,
            "source": "none",
            "mode": operator_mode if operator_mode in ("paper", "live") else "paper",
            "strategies": [],
            "warnings": ["missing_symbol"],
            "neural_status": neural_status,
        }

    live_readiness_overlay: dict[str, Any] = {}
    if enrich_coinbase and settings.chili_coinbase_spot_adapter_enabled:
        try:
            from ..venue.readiness_bridge import execution_readiness_meta_from_coinbase

            live_readiness_overlay = execution_readiness_meta_from_coinbase(sym)
        except Exception as e:
            _log.debug("[viable_query] coinbase enrich skipped: %s", e)

    if not _momentum_tables_present(db):
        warnings.append("momentum_tables_missing")
        return {
            "symbol": sym,
            "refreshed_at": refreshed_at,
            "source": "none",
            "mode": operator_mode if operator_mode in ("paper", "live") else "paper",
            "strategies": [],
            "warnings": warnings,
            "neural_status": neural_status,
        }

    hot_rows, hot_ts = _hot_rows_for_symbol(db, sym)
    hot_idx = _hot_index(hot_rows)

    q = (
        db.query(MomentumSymbolViability, MomentumStrategyVariant)
        .join(MomentumStrategyVariant, MomentumStrategyVariant.id == MomentumSymbolViability.variant_id)
        .filter(MomentumSymbolViability.symbol == sym)
        .order_by(MomentumSymbolViability.viability_score.desc())
    )
    pairs = q.all()
    if not pairs and hot_rows:
        # Hot path only (persistence lag or truncate in tests): synthesize rows from variants registry.
        warnings.append("db_rows_missing_using_hot_only")
        from .persistence import ensure_momentum_strategy_variants

        ensure_momentum_strategy_variants(db)
        for r in hot_rows:
            fid = str(r.get("family_id") or "")
            try:
                ver = int(r.get("family_version") or 1)
            except (TypeError, ValueError):
                ver = 1
            vid = _variant_id_for_family(db, fid, ver)
            if vid is None:
                continue
            vrow = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.id == vid).one_or_none()
            if not vrow:
                continue
            fake = MomentumSymbolViability(
                symbol=sym,
                variant_id=vid,
                viability_score=float(r.get("viability") or 0.0),
                paper_eligible=bool(r.get("paper_eligible", True)),
                live_eligible=bool(r.get("live_eligible", False)),
                # Older than hot tick so _merge_row prefers neural hot path.
                freshness_ts=datetime(1970, 1, 1),
                regime_snapshot_json={},
                execution_readiness_json={},
                explain_json={
                    "rationale": r.get("rationale"),
                    "warnings": list(r.get("warnings") or []),
                    "regime_fit": r.get("regime_fit"),
                    "entry_style": r.get("entry_style"),
                    "default_stop_logic": r.get("default_stop_logic"),
                    "default_exit_logic": r.get("default_exit_logic"),
                },
                evidence_window_json={"note": "hot_only"},
            )
            strategies.append(
                _merge_row(db, sym, fake, vrow, hot_idx, hot_ts, live_readiness_overlay)
            )
            s = strategies[-1]
            s["recent_sessions"] = _session_summary(db, user_id=user_id, symbol=sym, variant_id=int(vrow.id))
        return {
            "symbol": sym,
            "refreshed_at": refreshed_at,
            "source": "neural_hot" if strategies else "none",
            "mode": operator_mode if operator_mode in ("paper", "live") else "paper",
            "strategies": strategies,
            "warnings": warnings,
            "neural_status": neural_status,
        }

    source = "db"
    if hot_rows:
        source = "db+neural_hot"

    for ms, variant in pairs:
        row = _merge_row(db, sym, ms, variant, hot_idx, hot_ts, live_readiness_overlay)
        row["recent_sessions"] = _session_summary(db, user_id=user_id, symbol=sym, variant_id=int(variant.id))
        strategies.append(row)

    return {
        "symbol": sym,
        "refreshed_at": refreshed_at,
        "source": source,
        "mode": operator_mode if operator_mode in ("paper", "live") else "paper",
        "strategies": strategies,
        "warnings": warnings,
        "neural_status": neural_status,
    }

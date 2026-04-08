"""Read-model for Trading Brain neural desk: momentum intel, viability, evolution (Phase 10).

Precedence (documented):
- **Hot** ``BrainNodeState.local_state`` wins for latest tick timestamps and in-memory previews.
- **Durable** ``MomentumSymbolViability`` / ``MomentumAutomationOutcome`` win for row counts and history windows.
- Never label stale hot previews as authoritative counts; surface both where they differ.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import (
    BrainNodeState,
    MomentumAutomationOutcome,
    MomentumStrategyVariant,
    MomentumSymbolViability,
)
from ..brain_neural_mesh.schema import mesh_enabled
from ..execution_family_registry import EXECUTION_FAMILY_COINBASE_SPOT, normalize_execution_family
from ..governance import get_kill_switch_status
from .evolution import EVOLUTION_NODE_ID, paper_vs_live_performance_slices
from .feedback_query import momentum_outcomes_table_present

_log = logging.getLogger(__name__)

# Keep aligned with ``pipeline.HUB_NODE_ID`` / ``VIABILITY_NODE_ID`` (avoid importing pipeline here:
# pipeline → mesh → projection → this module would circular-import).
HUB_NODE_ID = "nm_momentum_crypto_intel"
VIABILITY_NODE_ID = "nm_momentum_viability_pool"

MOMENTUM_GRAPH_NODE_IDS: frozenset[str] = frozenset(
    {HUB_NODE_ID, VIABILITY_NODE_ID, EVOLUTION_NODE_ID}
)

_PREVIEW_VERSION = 1
_LIVE_LOW_N = 3


def _brain_ls(db: Session, node_id: str) -> dict[str, Any]:
    st = db.query(BrainNodeState).filter(BrainNodeState.node_id == node_id).one_or_none()
    if not st or not isinstance(st.local_state, dict):
        return {}
    return dict(st.local_state)


def _viability_durable_stats(db: Session) -> dict[str, Any]:
    try:
        total = int(db.query(func.count(MomentumSymbolViability.id)).scalar() or 0)
    except Exception as ex:
        _log.debug("viability count: %s", ex)
        return {"row_count": 0, "error": "query_failed"}
    if total == 0:
        return {
            "row_count": 0,
            "live_eligible_count": 0,
            "paper_only_count": 0,
            "fresh_last_24h_count": 0,
            "top_lines": [],
        }
    live_eligible = int(
        db.query(func.count(MomentumSymbolViability.id))
        .filter(MomentumSymbolViability.live_eligible.is_(True))
        .scalar()
        or 0
    )
    paper_only = int(
        db.query(func.count(MomentumSymbolViability.id))
        .filter(
            MomentumSymbolViability.paper_eligible.is_(True),
            MomentumSymbolViability.live_eligible.is_(False),
        )
        .scalar()
        or 0
    )
    since = datetime.utcnow() - timedelta(hours=24)
    fresh_24h = int(
        db.query(func.count(MomentumSymbolViability.id))
        .filter(MomentumSymbolViability.freshness_ts >= since)
        .scalar()
        or 0
    )
    top = (
        db.query(MomentumSymbolViability)
        .order_by(MomentumSymbolViability.viability_score.desc())
        .limit(5)
        .all()
    )
    top_lines: list[str] = []
    for r in top:
        le = "live" if r.live_eligible else "paper-only"
        top_lines.append(f"{r.symbol} · {float(r.viability_score):.2f} · {le}")
    return {
        "row_count": total,
        "live_eligible_count": live_eligible,
        "paper_only_count": paper_only,
        "fresh_last_24h_count": fresh_24h,
        "top_lines": top_lines,
    }


def _outcome_windows(db: Session, days: int = 30) -> dict[str, Any]:
    out: dict[str, Any] = {
        "table_present": momentum_outcomes_table_present(db),
        "window_days": days,
        "paper": {"n": 0, "mean_return_bps": None},
        "live": {"n": 0, "mean_return_bps": None},
        "mix_top": [],
        "best_variant": None,
        "weakest_variant": None,
    }
    if not out["table_present"]:
        return out
    since = datetime.utcnow() - timedelta(days=max(1, min(days, 120)))
    try:
        for mode in ("paper", "live"):
            rows = (
                db.query(MomentumAutomationOutcome)
                .filter(
                    MomentumAutomationOutcome.mode == mode,
                    MomentumAutomationOutcome.terminal_at >= since,
                    MomentumAutomationOutcome.contributes_to_evolution.is_(True),
                )
                .all()
            )
            n = len(rows)
            wsum = sum(float(r.evidence_weight or 1.0) for r in rows)
            wrb = sum(
                float(r.return_bps) * float(r.evidence_weight or 1.0)
                for r in rows
                if r.return_bps is not None
            )
            mean_bps = (wrb / wsum) if wsum > 0 else None
            out[mode] = {"n": n, "mean_return_bps": round(mean_bps, 2) if mean_bps is not None else None}

        mix_rows = (
            db.query(MomentumAutomationOutcome.outcome_class, func.count(MomentumAutomationOutcome.id))
            .filter(MomentumAutomationOutcome.terminal_at >= since)
            .group_by(MomentumAutomationOutcome.outcome_class)
            .order_by(func.count(MomentumAutomationOutcome.id).desc())
            .limit(6)
            .all()
        )
        out["mix_top"] = [{"outcome_class": str(oc), "n": int(c)} for oc, c in mix_rows]

        vstats = (
            db.query(
                MomentumAutomationOutcome.variant_id,
                func.count().label("cnt"),
                func.avg(MomentumAutomationOutcome.return_bps).label("avg_bps"),
            )
            .filter(
                MomentumAutomationOutcome.terminal_at >= since,
                MomentumAutomationOutcome.return_bps.isnot(None),
            )
            .group_by(MomentumAutomationOutcome.variant_id)
            .having(func.count() >= 1)
            .all()
        )
        if vstats:
            best = max(vstats, key=lambda x: float(x.avg_bps or -1e9))
            worst = min(vstats, key=lambda x: float(x.avg_bps or 1e9))
            vid_b, vid_w = int(best.variant_id), int(worst.variant_id)
            vb = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.id == vid_b).one_or_none()
            vw = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.id == vid_w).one_or_none()
            out["best_variant"] = {
                "variant_id": vid_b,
                "label": vb.label if vb else str(vid_b),
                "family": vb.family if vb else None,
                "avg_return_bps": round(float(best.avg_bps), 2) if best.avg_bps is not None else None,
                "n": int(best.cnt),
            }
            out["weakest_variant"] = {
                "variant_id": vid_w,
                "label": vw.label if vw else str(vid_w),
                "family": vw.family if vw else None,
                "avg_return_bps": round(float(worst.avg_bps), 2) if worst.avg_bps is not None else None,
                "n": int(worst.cnt),
            }
    except Exception as ex:
        _log.debug("outcome_windows: %s", ex)
        out["error"] = str(ex)
    return out


def build_momentum_neural_graph_context(db: Session) -> dict[str, Any]:
    """Single bundle for graph projection + optional brain panel (compact)."""
    mesh_on = mesh_enabled()
    mom_on = bool(settings.chili_momentum_neural_enabled)
    fb_on = bool(settings.chili_momentum_neural_feedback_enabled)
    gov = get_kill_switch_status()

    hub = _brain_ls(db, HUB_NODE_ID)
    pool = _brain_ls(db, VIABILITY_NODE_ID)
    evo = _brain_ls(db, EVOLUTION_NODE_ID)

    durable = _viability_durable_stats(db)
    outcomes = _outcome_windows(db, days=30)

    live_n = int(outcomes.get("live", {}).get("n") or 0)
    paper_n = int(outcomes.get("paper", {}).get("n") or 0)

    badges = {
        "neural_mesh_on": mesh_on,
        "momentum_neural_on": mom_on,
        "feedback_enabled": fb_on,
        "governance_kill_switch": bool(gov.get("active")),
        "live_sample_low": live_n < _LIVE_LOW_N and outcomes["table_present"],
        "outcomes_table_present": outcomes["table_present"],
    }

    top_prev = hub.get("top_preview") if isinstance(hub.get("top_preview"), list) else []
    top_family = None
    if top_prev and isinstance(top_prev[0], dict):
        top_family = top_prev[0].get("family") or top_prev[0].get("strategy_family")

    regime = hub.get("regime") if isinstance(hub.get("regime"), dict) else {}
    session_label = regime.get("session_label") or regime.get("crypto_session")

    hub_ef = hub.get("execution_family")
    intel_execution_family = (
        normalize_execution_family(hub_ef) if isinstance(hub_ef, str) and hub_ef.strip() else EXECUTION_FAMILY_COINBASE_SPOT
    )

    intel_card = {
        "role": "momentum_crypto_intel",
        "title": "Momentum neural (intel)",
        "execution_family": intel_execution_family,
        "neural_active": mesh_on and mom_on,
        "last_tick_utc": hub.get("last_tick_utc"),
        "correlation_id_tail": (str(hub.get("correlation_id") or "")[:12] or None),
        "symbols_evaluated_count": len(hub.get("symbols_evaluated") or []) if isinstance(hub.get("symbols_evaluated"), list) else 0,
        "regime_session_hint": session_label,
        "top_family_hint": top_family,
        "hot_preview_count": min(8, len(top_prev)),
        "subtitle": _subtitle_intel(hub, mesh_on, mom_on),
    }

    pool_rows = pool.get("viability_rows") if isinstance(pool.get("viability_rows"), list) else []
    pool_card = {
        "role": "momentum_viability_pool",
        "title": "Viability pool",
        "hot_last_tick_utc": pool.get("last_tick_utc"),
        "hot_row_count": min(64, len(pool_rows)),
        "durable_row_count": durable.get("row_count", 0),
        "live_eligible_count": durable.get("live_eligible_count", 0),
        "paper_only_count": durable.get("paper_only_count", 0),
        "fresh_last_24h_count": durable.get("fresh_last_24h_count", 0),
        "top_durable_lines": durable.get("top_lines") or [],
        "subtitle": _subtitle_pool(pool, durable),
    }

    ft = evo.get("feedback_trace") if isinstance(evo.get("feedback_trace"), list) else []
    last_fb = evo.get("latest_feedback_at_utc")
    evo_card = {
        "role": "momentum_evolution_trace",
        "title": "Evolution & feedback",
        "latest_feedback_at_utc": last_fb,
        "feedback_trace_tail": len(ft),
        "paper_30d_n": paper_n,
        "live_30d_n": live_n,
        "live_sample_low": live_n < _LIVE_LOW_N,
        "paper_mean_bps_30d": outcomes["paper"].get("mean_return_bps"),
        "live_mean_bps_30d": outcomes["live"].get("mean_return_bps"),
        "mix_top": outcomes.get("mix_top") or [],
        "best_variant": outcomes.get("best_variant"),
        "weakest_variant": outcomes.get("weakest_variant"),
        "feedback_loop_active": fb_on and outcomes["table_present"],
        "subtitle": _subtitle_evo(evo, outcomes, fb_on),
    }

    panel = {
        "headline": _panel_headline(paper_n, live_n, mesh_on, mom_on),
        "badges": badges,
        "paper_vs_live_30d": {
            "paper": outcomes["paper"],
            "live": outcomes["live"],
            "live_sample_caution": live_n < _LIVE_LOW_N,
        },
        "links": {
            "trading_momentum": "/trading#momentum",
            "automation": "/trading/automation",
        },
    }

    return {
        "version": _PREVIEW_VERSION,
        "meta": {
            "momentum_preview_version": _PREVIEW_VERSION,
            "sources": "hot: BrainNodeState; durable: momentum_symbol_viability + momentum_automation_outcomes",
        },
        "badges": badges,
        "nodes": {
            HUB_NODE_ID: intel_card,
            VIABILITY_NODE_ID: pool_card,
            EVOLUTION_NODE_ID: evo_card,
        },
        "momentum_panel": panel,
        "outcomes_window": outcomes,
    }


def _subtitle_intel(hub: dict[str, Any], mesh_on: bool, mom_on: bool) -> str:
    if not mesh_on:
        return "Neural mesh off"
    if not mom_on:
        return "Momentum neural disabled (config)"
    tick = hub.get("last_tick_utc")
    if not tick:
        return "Momentum intel — no tick yet"
    fam = ""
    tp = hub.get("top_preview")
    if isinstance(tp, list) and tp and isinstance(tp[0], dict):
        fam = str(tp[0].get("family") or "")[:24]
    return f"Intel · tick {str(tick)[:19]} · top {fam or '—'}"


def _subtitle_pool(pool: dict[str, Any], durable: dict[str, Any]) -> str:
    hot = pool.get("last_tick_utc")
    n = durable.get("row_count", 0)
    return f"Pool · DB rows {n} · hot tick {str(hot)[:19] if hot else '—'}"


def _subtitle_evo(evo: dict[str, Any], outcomes: dict[str, Any], fb_on: bool) -> str:
    if not fb_on:
        return "Feedback disabled (config)"
    if not outcomes.get("table_present"):
        return "Feedback — outcomes table missing"
    p, l = outcomes.get("paper", {}).get("n", 0), outcomes.get("live", {}).get("n", 0)
    lf = evo.get("latest_feedback_at_utc")
    return f"Evolution · 30d paper {p} / live {l} · last {str(lf)[:19] if lf else '—'}"


def _panel_headline(paper_n: int, live_n: int, mesh_on: bool, mom_on: bool) -> str:
    if not mesh_on:
        return "Neural mesh disabled — momentum desk preview limited."
    if not mom_on:
        return "Momentum neural disabled — intel/viability ticks inactive."
    return f"Momentum neural · 30d outcomes paper {paper_n} · live {live_n}"


def get_momentum_brain_desk_payload(db: Session) -> dict[str, Any]:
    """API wrapper: full context + small evolution slice for one variant (optional)."""
    ctx = build_momentum_neural_graph_context(db)
    return {"ok": True, **ctx}


def get_momentum_evolution_trace_summary(db: Session) -> dict[str, Any]:
    ctx = build_momentum_neural_graph_context(db)
    return {"ok": True, "node": ctx["nodes"].get(EVOLUTION_NODE_ID), "badges": ctx.get("badges")}


def get_momentum_feedback_brain_summary(db: Session) -> dict[str, Any]:
    ctx = build_momentum_neural_graph_context(db)
    return {
        "ok": True,
        "outcomes_window": ctx.get("outcomes_window"),
        "panel": ctx.get("momentum_panel"),
        "badges": ctx.get("badges"),
    }


def get_momentum_variants_brain_summary(db: Session, *, days: int = 14) -> dict[str, Any]:
    """Variants with most viability rows, each with paper vs live outcome slices (Phase 9 helper)."""
    out: dict[str, Any] = {"ok": True, "window_days": days, "variants": []}
    try:
        ranked = (
            db.query(MomentumSymbolViability.variant_id, func.count().label("cnt"))
            .group_by(MomentumSymbolViability.variant_id)
            .order_by(func.count().desc())
            .limit(10)
            .all()
        )
    except Exception:
        return out
    for vid, _cnt in ranked:
        v = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.id == int(vid)).one_or_none()
        if not v:
            continue
        try:
            pv = paper_vs_live_performance_slices(db, variant_id=int(v.id), days=days)
        except Exception:
            continue
        out["variants"].append(
            {
                "variant_id": v.id,
                "family": v.family,
                "label": v.label,
                "paper_vs_live": pv,
            }
        )
    return out

"""Activation hook: refresh momentum intelligence into BrainNodeState."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import BrainActivationEvent
from ..brain_neural_mesh.repository import get_or_create_state
from ..brain_neural_mesh.schema import mesh_enabled

from .context import build_momentum_regime_context
from .evolution import record_evolution_trace
from .features import ExecutionReadinessFeatures
from .telemetry import log_tick
from .variants import iter_momentum_families
from .viability import score_viability
from .viability_scope import VIABILITY_SCOPE_AGGREGATE, VIABILITY_SCOPE_SYMBOL

HUB_NODE_ID = "nm_momentum_crypto_intel"
VIABILITY_NODE_ID = "nm_momentum_viability_pool"

_log = logging.getLogger(__name__)


def maybe_run_momentum_neural_tick(
    db: Session,
    ev: BrainActivationEvent,
    *,
    graph_version: int = 1,
) -> None:
    """Run tick when activation event is a momentum context refresh."""
    if not settings.chili_momentum_neural_enabled:
        return
    if not mesh_enabled():
        return
    pl = ev.payload if isinstance(ev.payload, dict) else {}
    if ev.cause != "momentum_context_refresh" and pl.get("signal_type") != "momentum_context_refresh":
        return
    meta = pl.get("meta") if isinstance(pl.get("meta"), dict) else {}
    run_momentum_neural_tick(
        db,
        meta=meta,
        correlation_id=ev.correlation_id,
        graph_version=graph_version,
    )


def run_momentum_neural_tick(
    db: Session,
    *,
    meta: Optional[dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
    graph_version: int = 1,
) -> dict[str, Any]:
    """Compute regime + family viability; persist on hub and viability pool nodes."""
    _ = graph_version
    meta = dict(meta or {})
    tickers = meta.get("tickers")
    if isinstance(tickers, list) and tickers:
        symbols = [str(t).strip().upper() for t in tickers if t][:32]
        scope = VIABILITY_SCOPE_SYMBOL
    else:
        symbols = ["__aggregate__"]
        scope = VIABILITY_SCOPE_AGGREGATE

    # Phase 6c: optional Hurst proxy from first symbol's recent closes (feeds regime context).
    if symbols and symbols[0].upper() != "__AGGREGATE__":
        try:
            from ..market_data import fetch_ohlcv_df

            from .entry_gates import hurst_proxy_from_closes

            df_h = fetch_ohlcv_df(symbols[0], interval="15m", period="5d")
            if df_h is not None and not df_h.empty and "Close" in df_h.columns:
                meta["hurst_proxy"] = hurst_proxy_from_closes(df_h["Close"])
        except Exception:
            pass

    # Ross momentum-quality (M2): the scanner bridge forwards the RVOL/gap/
    # daily-change/float signals it computed as meta["ross_signals"] instead of
    # discarding them. Rank the batch once here and pass each symbol's [0,1]
    # quality through ctx_meta below so score_viability prefers EXPLOSIVE
    # instruments. Strict no-op when absent.
    _ross_signals = meta.get("ross_signals")
    if isinstance(_ross_signals, dict) and _ross_signals:
        try:
            from .ross_momentum import ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED
            from .ross_momentum import score_universe as _ross_score_universe

            # Liquidity-BIASED weights: prefer movers the lane can actually FILL
            # (dollar turnover -> tighter spread), not only the most explosive
            # names that get spread-gated and only ever watched. Validated on the
            # 11-day previous-days A/B replay: +6 fills, +$914 PnL vs baseline
            # (scripts/_sim_liquidity_selection.py, 2026-06-10).
            meta["ross_scores"] = {
                s: rs.score
                for s, rs in _ross_score_universe(
                    _ross_signals, weights=ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED
                ).items()
            }
        except Exception:
            pass

    # E5: news-catalyst set (EARNINGS + fresh general NEWS headlines) for the catalyst
    # viability tilt. The fresh-news union is what catches Ross's explosive sympathy/
    # theme movers (a low-float small-cap that just printed a hot headline), not just
    # scheduled earnings. Best-effort + cached; empty -> no-op (degrades gracefully
    # without the news/Benzinga feed). (catalyst.py)
    try:
        from .catalyst import all_catalyst_symbols

        _cat = all_catalyst_symbols()
        if _cat:
            # MUST be a list, not a set: meta flows into the brain_node_states
            # local_state JSONB and a set is not JSON-serializable ("Object of type
            # set is not JSON serializable"), which would fail the ENTIRE viability
            # write and leave every symbol stale. (regression guard for #528)
            meta["catalyst_symbols"] = sorted(_cat)
    except Exception:
        pass

    ctx_meta = {
        k: meta[k]
        for k in (
            "spread_regime",
            "fee_burden_regime",
            "liquidity_regime",
            "exhaustion_cooldown",
            "rolling_range_state",
            "breakout_continuity",
            "realized_vol_rank",
            "atr_pct",
            "hurst_proxy",
            "adx",
            "adx_14",
            "ross_scores",
            "catalyst_symbols",
        )
        if k in meta
    }
    ctx = build_momentum_regime_context(
        realized_vol_rank=meta.get("realized_vol_rank"),
        atr_pct=meta.get("atr_pct"),
        meta=ctx_meta,
    )
    feats = ExecutionReadinessFeatures.from_meta(meta)

    rows: list[dict[str, Any]] = []
    for sym in symbols:
        for family in iter_momentum_families():
            vr = score_viability(sym, family, ctx, feats, db=db)
            d = vr.to_public_dict()
            d["scope"] = scope
            d["label"] = family.label
            d["entry_style"] = family.entry_style
            d["default_stop_logic"] = family.default_stop_logic
            d["default_exit_logic"] = family.default_exit_logic
            rows.append(d)

    rows.sort(key=lambda r: r["viability"], reverse=True)
    top = rows[0] if rows else {}

    now = datetime.utcnow().isoformat()
    hub_payload = {
        "momentum_neural_version": 1,
        "last_tick_utc": now,
        "correlation_id": correlation_id,
        "regime": ctx.to_public_dict(),
        "symbols_evaluated": symbols,
        "top_preview": rows[:8],
    }
    viability_payload = {
        "momentum_neural_version": 1,
        "last_tick_utc": now,
        "viability_rows": rows[:64],
        "correlation_id": correlation_id,
    }

    hub = get_or_create_state(db, HUB_NODE_ID)
    hub.local_state = hub_payload
    hub.last_activated_at = datetime.utcnow()
    hub.updated_at = datetime.utcnow()

    pool = get_or_create_state(db, VIABILITY_NODE_ID)
    pool.local_state = viability_payload
    pool.last_activated_at = datetime.utcnow()
    pool.updated_at = datetime.utcnow()

    record_evolution_trace(
        db,
        snapshot={
            "top_family_id": top.get("family_id"),
            "top_viability": top.get("viability"),
            "session_label": ctx.session_label,
        },
    )

    persistence_ok = True
    try:
        from .persistence import persist_neural_momentum_tick

        n = persist_neural_momentum_tick(
            db,
            row_dicts=rows,
            regime_snapshot=ctx.to_public_dict(),
            features=feats,
            correlation_id=correlation_id,
            source_node_id=HUB_NODE_ID,
        )
        if n:
            log_tick("persisted viability rows=%s", n)
    except Exception as e:
        _log.warning("[momentum_neural] viability persistence failed: %s", e)
        persistence_ok = False

    log_tick(
        "tick symbols=%s families=%s top=%s corr=%s",
        len(symbols),
        len(rows) // max(len(symbols), 1),
        top.get("family_id"),
        correlation_id,
    )
    return {"ok": True, "rows": len(rows), "top_family": top.get("family_id"), "persistence_ok": persistence_ok}

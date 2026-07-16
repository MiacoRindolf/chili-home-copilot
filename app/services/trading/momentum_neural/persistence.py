"""Durable PostgreSQL backing for neural momentum (variants + viability + automation audit)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ....models.trading import (
    MomentumStrategyVariant,
    MomentumSymbolViability,
    MomentumViabilityHistory,
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


def _crypto_viability_gate_active() -> bool:
    """AREA D (2026-06-25): True when "-USD" symbols should NOT be persisted into
    momentum_symbol_viability because crypto is not traded.

    Crypto IS considered traded (gate OFF, scoring untouched) when EITHER:
      * the lane is crypto-only (chili_momentum_auto_arm_crypto_only), or
      * crypto live-arm is enabled (chili_momentum_crypto_live_arm_enabled).

    Otherwise (the current equity-only / crypto-not-traded state) the gate is ON and
    -USD rows are skipped at this single persistence chokepoint — downstream of all
    scoring, so equity rows are byte-identical. Master kill-switch
    chili_momentum_crypto_viability_gate_enabled=False disables the gate entirely
    (legacy: persist all symbols). Fail-safe: any error -> gate OFF (persist all)."""
    try:
        from ....config import settings

        if not bool(getattr(settings, "chili_momentum_crypto_viability_gate_enabled", True)):
            return False
        crypto_traded = bool(
            getattr(settings, "chili_momentum_auto_arm_crypto_only", True)
        ) or bool(getattr(settings, "chili_momentum_crypto_live_arm_enabled", False))
        return not crypto_traded
    except Exception:
        return False


def _is_usd_crypto_symbol(symbol: str | None) -> bool:
    return "-USD" in str(symbol or "").upper()


def _viability_history_enabled() -> bool:
    """Replay v3 R1 — gate the momentum_viability_history append (default ON; cheap,
    append-only observability). Fail-safe: any config error => OFF (no append, the
    viability upsert is byte-identical)."""
    try:
        from ....config import settings

        return bool(getattr(settings, "chili_momentum_viability_history_enabled", True))
    except Exception:
        return False


def _coerce_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


# Scanner-result keys carrying RVOL / change (mirror distribution_filters._RVOL_KEYS /
# _CHANGE_KEYS so the history captures the SAME inputs the scorer/floors read).
_HIST_RVOL_KEYS = ("vol_ratio", "rvol", "volume_ratio")
_HIST_CHANGE_KEYS = (
    "daily_change_pct",
    "change_24h",
    "change_pct",
    "todays_change_perc",
    "gap_pct",
)


def _scorer_inputs_for_history(
    symbol: str, row: dict[str, Any], features: ExecutionReadinessFeatures
) -> tuple[Optional[float], Optional[float], Optional[float], Optional[str]]:
    """Extract (rvol, change_pct, spread_bps, blocked_reason) for the history row from
    the SAME sources the live scorer reads — features.meta['ross_signals'][symbol] for
    rvol/change, features.spread_bps for the spread, and the first eligibility-bearing
    warning for the blocked_reason. Pure read; never raises (fail-open to Nones)."""
    rvol: Optional[float] = None
    change_pct: Optional[float] = None
    try:
        meta = getattr(features, "meta", None)
        rsig = meta.get("ross_signals") if isinstance(meta, dict) else None
        sig = rsig.get(symbol) if isinstance(rsig, dict) else None
        if isinstance(sig, dict):
            for k in _HIST_RVOL_KEYS:
                if k in sig:
                    rvol = _coerce_float(sig.get(k))
                    if rvol is not None:
                        break
            for k in _HIST_CHANGE_KEYS:
                if k in sig:
                    change_pct = _coerce_float(sig.get(k))
                    if change_pct is not None:
                        break
    except (AttributeError, TypeError):
        pass
    spread_bps = _coerce_float(getattr(features, "spread_bps", None))
    blocked_reason: Optional[str] = None
    try:
        warnings = row.get("warnings") or []
        for w in warnings:
            wl = str(w).lower()
            if "eligib" in wl or "untradeable" in wl or "not a live setup" in wl or "vetoed" in wl:
                blocked_reason = str(w)[:120]
                break
    except (AttributeError, TypeError):
        pass
    return rvol, change_pct, spread_bps, blocked_reason


def _strategy_variant_key(family_id: str, version: int) -> tuple[str, str, int]:
    return (family_id, family_id, int(version))


def _strategy_variants_by_key(db: Session, families: list[Any]) -> dict[tuple[str, str, int], MomentumStrategyVariant]:
    if not families:
        return {}

    family_ids = sorted({str(fam.family_id) for fam in families})
    versions = sorted({int(fam.version) for fam in families})
    rows = (
        db.query(MomentumStrategyVariant)
        .filter(
            MomentumStrategyVariant.family.in_(family_ids),
            MomentumStrategyVariant.variant_key.in_(family_ids),
            MomentumStrategyVariant.version.in_(versions),
        )
        .all()
    )
    return {
        (str(row.family), str(row.variant_key), int(row.version)): row
        for row in rows
    }


def _momentum_tables_present(db: Session) -> bool:
    try:
        bind = db.get_bind()
        names = set(sa_inspect(bind).get_table_names())
    except Exception:
        return False
    return "momentum_strategy_variants" in names and "momentum_symbol_viability" in names


def ensure_momentum_strategy_variants(db: Session) -> None:
    """Ensure seed registry rows exist and carry runner-consumable params."""
    families = list(iter_momentum_families())
    variants_by_key = _strategy_variants_by_key(db, families)
    for fam in families:
        row = variants_by_key.get(_strategy_variant_key(fam.family_id, int(fam.version)))
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
    decision_packet_id: int | None = None,
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
        decision_packet_id=int(decision_packet_id) if decision_packet_id is not None else None,
    )
    db.add(row)
    db.flush()
    return row


def _resolve_viability_upserts(
    db: Session, row_dicts: list[dict[str, Any]]
) -> list[tuple[str, int, dict[str, Any]]]:
    """Resolve each row to ``(symbol, variant_id, row)`` and return them in a
    DETERMINISTIC ``(symbol, variant_id)`` lock-acquisition order.

    Two momentum ticks fired by DIFFERENT callers — the neural-mesh snapshot
    activation (``maybe_run_momentum_neural_tick``) and a scanner / viability-
    refresh bridge (``_bridge_scanner_to_viability``) — routinely upsert
    OVERLAPPING ``momentum_symbol_viability`` rows. When each acquired the per-row
    unique-index locks in its OWN (viability-ranked) symbol order they formed a
    lock cycle, and Postgres aborted one with ``deadlock detected`` mid-persist
    (observed 25×/48h, 2026-06-10; both parties were this very INSERT…ON CONFLICT).
    Upserting every tick's rows in ONE global ``(symbol, variant_id)`` order means
    concurrent ticks acquire the shared locks in the same sequence, so they
    serialize instead of deadlocking.

    The variant id is resolved ONCE per family here too: the loop used to re-query
    ``active_variant_for_family`` for every row, i.e. ~300 redundant SELECTs per
    32-symbol × 10-family tick, each one extending how long the transaction sat
    idle-in-transaction holding the accumulated viability row locks.
    """
    vid_cache: dict[tuple[str, int], Optional[int]] = {}

    def _vid_for(fam_id: str, fam_ver: int) -> Optional[int]:
        key = (fam_id, fam_ver)
        if key not in vid_cache:
            active_variant = active_variant_for_family(db, fam_id)
            vid_cache[key] = (
                int(active_variant.id)
                if active_variant is not None
                else _variant_id_for_family(db, fam_id, fam_ver)
            )
        return vid_cache[key]

    resolved: list[tuple[str, int, dict[str, Any]]] = []
    for row in row_dicts:
        fam_id = row.get("family_id")
        if not fam_id:
            continue
        fam_ver = int(row.get("family_version") or 1)
        vid = _vid_for(str(fam_id), fam_ver)
        if vid is None:
            _log.warning("momentum persistence: no variant row for family=%s", fam_id)
            continue
        resolved.append((str(row.get("symbol") or ""), int(vid), row))

    resolved.sort(key=lambda t: (t[0], t[1]))
    return resolved


def persist_neural_momentum_tick(
    db: Session,
    *,
    row_dicts: list[dict[str, Any]],
    regime_snapshot: dict[str, Any],
    features: ExecutionReadinessFeatures,
    correlation_id: Optional[str],
    source_node_id: Optional[str],
    observed_at: Optional[datetime] = None,
) -> int:
    """Upsert ``MomentumSymbolViability`` for each computed row; returns rows written."""
    if not _momentum_tables_present(db):
        _log.debug("momentum persistence skipped (tables missing)")
        return 0

    ensure_momentum_strategy_variants(db)

    exec_json = features.to_public_dict()
    now = observed_at or datetime.utcnow()
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    n = 0
    skipped_crypto = 0
    # AREA D — CRYPTO VIABILITY GATE: when crypto is not traded, skip persisting
    # "-USD" symbol rows so they never pollute the equity scoring pool (the auto_arm
    # candidate query already excludes them from arming; this stops them at the source
    # so the viability table stays clean for the day crypto is re-enabled). Computed
    # ONCE per tick (not per row). The "__aggregate__" row is NEVER crypto, so it is
    # always persisted. flag-OFF / crypto-traded = scores all symbols (legacy).
    _gate_crypto = _crypto_viability_gate_active()
    # Replay v3 R1 — buffer the live_eligible TIME-SERIES rows to append (bulk-inserted
    # once after the upsert loop). Built only when the flag is ON; the append itself is
    # fail-open (a history error never blocks the live viability upsert).
    _hist_enabled = _viability_history_enabled()
    _hist_rows: list[dict[str, Any]] = []
    # Deterministic (symbol, variant_id) order — prevents the cross-tick deadlock
    # on the unique index (see _resolve_viability_upserts).
    for symbol, vid, row in _resolve_viability_upserts(db, row_dicts):
        if _gate_crypto and _is_usd_crypto_symbol(symbol):
            skipped_crypto += 1
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
            # LEVER 1: persist the risk-bounded marker so the live_runner sizing path
            # (and operator readouts) can size an extreme-vol / missing-rvol genuine
            # mover DOWN. Absent / False => byte-identical (the lever is OFF or the name
            # is a normal-vol fully-confirmed mover).
            "extreme_vol_risk_bounded": bool(row.get("extreme_vol_risk_bounded", False)),
        }
        evidence_window: dict[str, Any] = {"note": "phase2_placeholder"}

        stmt = pg_insert(MomentumSymbolViability).values(
            symbol=symbol,
            scope=infer_viability_scope(symbol, explicit=row.get("scope")),
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
                "scope": infer_viability_scope(symbol, explicit=row.get("scope")),
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

        # Replay v3 R1 — record the live_eligible value (+ the scorer inputs to recompute
        # / audit it) into the append-only history. Cheap (buffered, one bulk INSERT) and
        # observability-only; the live viability decision above is untouched.
        if _hist_enabled:
            _rvol, _chg, _spread, _blocked = _scorer_inputs_for_history(symbol, row, features)
            _hist_rows.append(
                {
                    "symbol": symbol,
                    "variant_id": vid,
                    "scope": infer_viability_scope(symbol, explicit=row.get("scope")),
                    "observed_at": now,
                    "live_eligible": bool(row.get("live_eligible", False)),
                    "paper_eligible": bool(row.get("paper_eligible", True)),
                    "freshness_ts": now,
                    "viability_score": float(row.get("viability") or 0.0),
                    "rvol": _rvol,
                    "change_pct": _chg,
                    "spread_bps": _spread,
                    "blocked_reason": _blocked,
                    "correlation_id": correlation_id,
                    "source_node_id": source_node_id,
                    "created_at": now,
                }
            )

    # Append the buffered history rows in ONE bulk INSERT. FAIL-OPEN: a history-write
    # error is swallowed (and the SAVEPOINT rolled back so the live viability upserts
    # above survive) — observability must never block the live viability update.
    if _hist_enabled and _hist_rows:
        try:
            with db.begin_nested():
                db.execute(MomentumViabilityHistory.__table__.insert(), _hist_rows)
        except Exception:
            _log.debug(
                "[momentum_neural] viability_history append failed (%d rows) — "
                "fail-open, viability upserts unaffected",
                len(_hist_rows),
                exc_info=True,
            )

    if skipped_crypto:
        _log.debug(
            "[momentum_neural] crypto viability gate skipped %s -USD rows (crypto not traded)",
            skipped_crypto,
        )
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

"""Deployment ladder: paper → tiny → limited → scaled → restricted → disabled.

Durable state in trading_deployment_states. Live sizing enforcement is config-gated.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ...config import settings
from ...models.trading import TradingDeploymentState

def _scope_session(session_id: int) -> tuple[str, str]:
    return "automation_session", f"session:{int(session_id)}"


def _scope_variant(variant_id: int) -> tuple[str, str]:
    return "strategy_variant", f"variant:{int(variant_id)}"


def stage_size_multiplier(stage: str) -> float:
    s = (stage or "paper").lower()
    return {
        "disabled": 0.0,
        "restricted": 0.15,
        "paper": 1.0,
        "tiny": 0.2,
        "limited": 0.45,
        "scaled": 1.0,
    }.get(s, 0.5)


def _promotion_readiness(state: TradingDeploymentState) -> dict[str, Any]:
    reasons: list[str] = []
    dd = state.rolling_drawdown_pct
    slip = state.rolling_slippage_bps
    miss = state.rolling_missed_fill_rate
    exp = state.rolling_expectancy_net
    if exp is not None and exp < 0:
        reasons.append("negative_expectancy")
    if dd is not None and dd >= float(settings.brain_deployment_degrade_drawdown_pct):
        reasons.append("drawdown")
    if slip is not None and slip >= float(settings.brain_deployment_degrade_slippage_bps):
        reasons.append("slippage")
    if miss is not None and miss >= float(settings.brain_deployment_degrade_missed_fill_rate):
        reasons.append("missed_fill")
    return {
        "ok": not reasons,
        "reasons": reasons,
        "metrics": {
            "paper_trade_count": int(state.paper_trade_count or 0),
            "rolling_expectancy_net": exp,
            "rolling_drawdown_pct": dd,
            "rolling_slippage_bps": slip,
            "rolling_missed_fill_rate": miss,
        },
    }


def get_or_create_deployment_state(
    db: Session,
    *,
    scope_type: str,
    scope_key: str,
    user_id: int | None,
    initial_stage: str = "paper",
) -> TradingDeploymentState:
    row = (
        db.query(TradingDeploymentState)
        .filter(
            TradingDeploymentState.scope_type == scope_type,
            TradingDeploymentState.scope_key == scope_key,
        )
        .one_or_none()
    )
    if row:
        return row
    row = TradingDeploymentState(
        scope_type=scope_type,
        scope_key=scope_key,
        user_id=user_id,
        current_stage=initial_stage,
    )
    db.add(row)
    db.flush()
    return row


def sync_initial_stage_from_viability(
    db: Session,
    *,
    session_id: int,
    variant_id: int,
    user_id: int | None,
    paper_eligible: bool,
    live_eligible: bool,
    mode: str,
) -> TradingDeploymentState:
    if not settings.brain_enable_deployment_ladder:
        st = get_or_create_deployment_state(db, scope_type="automation_session", scope_key=f"session:{session_id}", user_id=user_id)
        return st
    _stype, _skey = _scope_session(session_id)
    st = get_or_create_deployment_state(db, scope_type=_stype, scope_key=_skey, user_id=user_id)
    if st.current_stage in ("disabled",):
        return st
    if mode == "live" and live_eligible:
        if st.current_stage in ("paper",) and st.paper_trade_count >= int(settings.brain_deployment_promote_min_paper_trades):
            readiness = _promotion_readiness(st)
            m = dict(st.stage_metrics_json or {})
            m["promotion_readiness"] = readiness
            st.stage_metrics_json = m
            if readiness["ok"]:
                st.current_stage = "limited"
                st.promoted_at = datetime.utcnow()
                st.last_reason_code = "auto_promote_live_eligible"
                st.last_reason_text = None
            else:
                st.last_reason_code = "promotion_gate_blocked"
                st.last_reason_text = ",".join(readiness["reasons"])
    elif paper_eligible:
        if st.current_stage not in ("restricted", "disabled"):
            st.current_stage = "paper"
    else:
        st.current_stage = "restricted"
        st.last_reason_code = "viability_not_eligible"
    st.updated_at = datetime.utcnow()
    db.flush()
    return st


def evaluate_de_escalation(db: Session, state: TradingDeploymentState) -> None:
    """Apply automatic de-escalation from rolling metrics (no promotion logic beyond sync)."""
    if not settings.brain_enable_deployment_ladder:
        return
    dd = state.rolling_drawdown_pct
    slip = state.rolling_slippage_bps
    miss = state.rolling_missed_fill_rate
    exp = state.rolling_expectancy_net

    reasons: list[str] = []
    if dd is not None and dd >= float(settings.brain_deployment_degrade_drawdown_pct):
        reasons.append("drawdown")
    if slip is not None and slip >= float(settings.brain_deployment_degrade_slippage_bps):
        reasons.append("slippage")
    if miss is not None and miss >= float(settings.brain_deployment_degrade_missed_fill_rate):
        reasons.append("missed_fill")
    if exp is not None and exp < 0:
        neg_n = int((state.stage_metrics_json or {}).get("negative_expectancy_streak") or 0) + 1
        m = dict(state.stage_metrics_json or {})
        m["negative_expectancy_streak"] = neg_n
        state.stage_metrics_json = m
        if neg_n >= int(settings.brain_deployment_degrade_negative_expectancy_rolls):
            reasons.append("negative_expectancy")

    if not reasons:
        return

    cur = state.current_stage
    if cur == "scaled":
        state.current_stage = "limited"
    elif cur == "limited":
        state.current_stage = "tiny"
    elif cur == "tiny":
        state.current_stage = "restricted"
    else:
        state.current_stage = "restricted"
    state.degraded_at = datetime.utcnow()
    state.last_reason_code = "auto_deescalate"
    state.last_reason_text = ",".join(reasons)
    state.updated_at = datetime.utcnow()
    db.flush()


def _rolling_average(prev: float | None, n: int, value: float) -> float:
    if prev is None:
        return round(float(value), 4)
    return round((float(prev) * (n - 1) + float(value)) / max(1, n), 4)


def _apply_trade_rollup(
    state: TradingDeploymentState,
    *,
    mode: str,
    realized_pnl_usd: float | None,
    slippage_bps: float | None,
    missed_fill: bool,
    partial_fill: bool,
    cumulative_pnl_usd: float | None,
    derive_cumulative_from_realized: bool,
    drawdown_metric_prefix: str,
) -> None:
    if mode == "live":
        state.live_trade_count = int(state.live_trade_count or 0) + 1
    else:
        state.paper_trade_count = int(state.paper_trade_count or 0) + 1
    n = int(state.paper_trade_count or 0) + int(state.live_trade_count or 0)

    if slippage_bps is not None:
        state.rolling_slippage_bps = _rolling_average(state.rolling_slippage_bps, n, float(slippage_bps))

    state.rolling_missed_fill_rate = _rolling_average(
        state.rolling_missed_fill_rate,
        n,
        1.0 if missed_fill else 0.0,
    )
    state.rolling_partial_fill_rate = _rolling_average(
        state.rolling_partial_fill_rate,
        n,
        1.0 if partial_fill else 0.0,
    )

    if realized_pnl_usd is not None:
        state.rolling_expectancy_net = _rolling_average(
            state.rolling_expectancy_net,
            n,
            float(realized_pnl_usd),
        )

    effective_cumulative = cumulative_pnl_usd
    m = dict(state.stage_metrics_json or {})
    if effective_cumulative is None and derive_cumulative_from_realized and realized_pnl_usd is not None:
        cumulative_key = f"{drawdown_metric_prefix}_pnl_cumulative_usd"
        effective_cumulative = float(m.get(cumulative_key) or 0.0) + float(realized_pnl_usd)
        m[cumulative_key] = round(float(effective_cumulative), 6)

    if effective_cumulative is not None:
        hwm_key = f"{drawdown_metric_prefix}_pnl_hwm_usd"
        last_key = f"{drawdown_metric_prefix}_pnl_last_usd"
        hwm = float(m.get(hwm_key) or 0.0)
        hwm = max(hwm, float(effective_cumulative))
        m[hwm_key] = round(hwm, 6)
        m[last_key] = round(float(effective_cumulative), 6)
        cur = float(effective_cumulative)
        ref_key = f"{drawdown_metric_prefix}_drawdown_ref_risk_usd"
        ref = float(m.get(ref_key) or 0.0)
        if ref <= 0:
            ref = float(getattr(settings, "chili_momentum_risk_max_notional_per_trade_usd", 250.0) or 250.0)
            m[ref_key] = ref
        if hwm > 1e-9 and (not derive_cumulative_from_realized or hwm >= ref):
            state.rolling_drawdown_pct = round(max(0.0, (hwm - cur) / hwm * 100.0), 4)
        else:
            loss_mag = max(0.0, hwm - cur, abs(min(0.0, cur)))
            state.rolling_drawdown_pct = round(min(100.0, loss_mag / max(1.0, ref) * 100.0), 4)
        state.stage_metrics_json = m

    state.updated_at = datetime.utcnow()


def _variant_deescalation_applies(state: TradingDeploymentState, *, mode: str) -> bool:
    stage = (state.current_stage or "paper").lower()
    return mode == "live" or stage in {"tiny", "limited", "scaled"}


def record_trade_outcome_metrics(
    db: Session,
    *,
    session_id: int,
    variant_id: int,
    user_id: int | None,
    mode: str,
    realized_pnl_usd: float | None,
    slippage_bps: float | None,
    missed_fill: bool,
    partial_fill: bool,
    cumulative_session_pnl_usd: float | None = None,
) -> None:
    if not settings.brain_enable_deployment_ladder:
        return
    _stype, _skey = _scope_session(session_id)
    st = get_or_create_deployment_state(db, scope_type=_stype, scope_key=_skey, user_id=user_id)
    _apply_trade_rollup(
        st,
        mode=mode,
        realized_pnl_usd=realized_pnl_usd,
        slippage_bps=slippage_bps,
        missed_fill=missed_fill,
        partial_fill=partial_fill,
        cumulative_pnl_usd=cumulative_session_pnl_usd,
        derive_cumulative_from_realized=False,
        drawdown_metric_prefix="session",
    )
    evaluate_de_escalation(db, st)

    _vtype, _vkey = _scope_variant(variant_id)
    vst = get_or_create_deployment_state(db, scope_type=_vtype, scope_key=_vkey, user_id=user_id)
    _apply_trade_rollup(
        vst,
        mode=mode,
        realized_pnl_usd=realized_pnl_usd,
        slippage_bps=slippage_bps,
        missed_fill=missed_fill,
        partial_fill=partial_fill,
        cumulative_pnl_usd=None,
        derive_cumulative_from_realized=True,
        drawdown_metric_prefix="variant",
    )
    if st.rolling_drawdown_pct is not None:
        vdd = float(vst.rolling_drawdown_pct or 0.0)
        vst.rolling_drawdown_pct = round(max(vdd, float(st.rolling_drawdown_pct)), 4)
    vm = dict(vst.stage_metrics_json or {})
    vm["last_session_scope_key"] = _skey
    vm["last_session_stage"] = st.current_stage
    vm["last_session_drawdown_pct"] = st.rolling_drawdown_pct
    vm["last_session_expectancy_net"] = st.rolling_expectancy_net
    vm["last_session_reason_code"] = st.last_reason_code
    vst.stage_metrics_json = vm
    if _variant_deescalation_applies(vst, mode=mode):
        evaluate_de_escalation(db, vst)
    else:
        db.flush()
    db.flush()

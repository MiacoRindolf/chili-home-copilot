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
            st.current_stage = "limited"
            st.promoted_at = datetime.utcnow()
            st.last_reason_code = "auto_promote_live_eligible"
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
    if mode == "live":
        st.live_trade_count = int(st.live_trade_count or 0) + 1
    else:
        st.paper_trade_count = int(st.paper_trade_count or 0) + 1
    n = st.paper_trade_count + st.live_trade_count
    if slippage_bps is not None:
        prev = st.rolling_slippage_bps or slippage_bps
        st.rolling_slippage_bps = round((prev * (n - 1) + slippage_bps) / max(1, n), 4)
    if missed_fill:
        mf = st.rolling_missed_fill_rate or 0.0
        st.rolling_missed_fill_rate = round((mf * (n - 1) + 1.0) / max(1, n), 4)
    if partial_fill:
        pf = st.rolling_partial_fill_rate or 0.0
        st.rolling_partial_fill_rate = round((pf * (n - 1) + 1.0) / max(1, n), 4)
    if realized_pnl_usd is not None:
        prev_e = st.rolling_expectancy_net
        if prev_e is None:
            st.rolling_expectancy_net = round(realized_pnl_usd, 4)
        else:
            st.rolling_expectancy_net = round((prev_e * (n - 1) + realized_pnl_usd) / max(1, n), 4)

    if cumulative_session_pnl_usd is not None:
        m = dict(st.stage_metrics_json or {})
        hwm = float(m.get("session_pnl_hwm_usd") or 0.0)
        hwm = max(hwm, float(cumulative_session_pnl_usd))
        m["session_pnl_hwm_usd"] = round(hwm, 6)
        m["session_pnl_last_usd"] = round(float(cumulative_session_pnl_usd), 6)
        cur = float(cumulative_session_pnl_usd)
        if hwm > 1e-9:
            st.rolling_drawdown_pct = round(max(0.0, (hwm - cur) / hwm * 100.0), 4)
        else:
            ref = float(m.get("drawdown_ref_risk_usd") or 0.0)
            if ref <= 0:
                ref = float(getattr(settings, "chili_momentum_risk_max_notional_per_trade_usd", 250.0) or 250.0)
                m["drawdown_ref_risk_usd"] = ref
            loss_mag = abs(min(0.0, cur))
            st.rolling_drawdown_pct = round(min(100.0, loss_mag / max(1.0, ref) * 100.0), 4)
        st.stage_metrics_json = m

    st.updated_at = datetime.utcnow()
    evaluate_de_escalation(db, st)

    _vtype, _vkey = _scope_variant(variant_id)
    vst = get_or_create_deployment_state(db, scope_type=_vtype, scope_key=_vkey, user_id=user_id)
    vst.paper_trade_count = st.paper_trade_count
    vst.live_trade_count = st.live_trade_count
    vst.rolling_slippage_bps = st.rolling_slippage_bps
    vst.rolling_expectancy_net = st.rolling_expectancy_net
    vst.rolling_drawdown_pct = st.rolling_drawdown_pct
    vst.stage_metrics_json = dict(st.stage_metrics_json or {})
    vst.updated_at = datetime.utcnow()
    db.flush()

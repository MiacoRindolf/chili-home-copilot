from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models.core import User
from app.models.trading import (
    MomentumStrategyVariant,
    MomentumSymbolViability,
    TradingAutomationSession,
)
from app.services.trading.momentum_neural import automation_query as aq
from app.services.trading.momentum_neural import operator_actions

pytestmark = pytest.mark.usefixtures("stable_non_alpaca_account_identity")


def test_expiry_interleaving_cannot_be_overwritten_by_live_confirm(
    db: Session,
    monkeypatch,
) -> None:
    """Expiry winning during confirm must leave the generation terminal, never queued."""
    suffix = uuid.uuid4().hex[:10]
    user = User(name=f"ArmFence_{suffix}")
    variant = MomentumStrategyVariant(
        family=f"arm_fence_{suffix}",
        variant_key="base",
        version=1,
        label="Arm generation fence",
        params_json={},
        is_active=True,
        execution_family="robinhood_spot",
    )
    db.add_all([user, variant])
    db.flush()

    symbol = f"AF{suffix[:6]}".upper()
    viability = MomentumSymbolViability(
        symbol=symbol,
        scope="symbol",
        variant_id=int(variant.id),
        viability_score=0.95,
        paper_eligible=True,
        live_eligible=True,
        freshness_ts=datetime.utcnow(),
        regime_snapshot_json={},
        execution_readiness_json={},
        explain_json={},
        evidence_window_json={},
    )
    token = f"arm-{suffix}"
    arm = TradingAutomationSession(
        user_id=int(user.id),
        venue="robinhood",
        execution_family="robinhood_spot",
        mode="live",
        symbol=symbol,
        variant_id=int(variant.id),
        state=operator_actions.STATE_LIVE_ARM_PENDING,
        risk_snapshot_json={
            "arm_token": token,
            "expires_at_utc": (datetime.utcnow() + timedelta(minutes=5)).isoformat(),
            "non_alpaca_account_identity": "test-non-alpaca-account-v1",
        },
        allocation_decision_json={},
        correlation_id=f"corr-{suffix}",
        source_node_id="generation_fence_test",
    )
    db.add_all([viability, arm])
    db.flush()

    monkeypatch.setattr(
        operator_actions.settings,
        "chili_momentum_arm_time_viability_refresh_enabled",
        False,
    )
    monkeypatch.setattr(aq, "_tables_present", lambda _db: True)
    # Keep the expiry-side lock independent from the confirm hook below.  The
    # production functions share the same imported advisory-lock implementation.
    monkeypatch.setattr(aq, "_lock_live_symbol_arm", lambda *_a, **_k: True)

    interleaving: dict[str, int] = {}

    def _expire_while_confirm_waits_for_fence(*_args, **_kwargs) -> bool:
        locked_arm = (
            db.query(TradingAutomationSession)
            .filter(TradingAutomationSession.id == int(arm.id))
            .one()
        )
        snap = dict(locked_arm.risk_snapshot_json or {})
        snap["expires_at_utc"] = (
            datetime.utcnow() - timedelta(seconds=1)
        ).isoformat()
        locked_arm.risk_snapshot_json = snap
        flag_modified(locked_arm, "risk_snapshot_json")
        db.flush()
        interleaving["expired"] = aq.expire_stale_live_arm_sessions(
            db,
            user_id=int(user.id),
        )
        return True

    monkeypatch.setattr(
        operator_actions,
        "_lock_live_symbol_arm",
        _expire_while_confirm_waits_for_fence,
    )

    result = operator_actions.confirm_live_arm(
        db,
        user_id=int(user.id),
        arm_token=token,
        confirm=True,
    )

    db.flush()
    db.refresh(arm)
    assert interleaving == {"expired": 1}
    assert result["ok"] is False
    assert result["error"] == "arm_generation_changed"
    assert arm.state == aq.STATE_EXPIRED
    assert arm.state not in {
        operator_actions.STATE_QUEUED,
        operator_actions.STATE_ARMED_PENDING_RUNNER,
    }

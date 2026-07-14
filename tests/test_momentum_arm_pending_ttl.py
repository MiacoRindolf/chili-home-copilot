"""FIX-18 (B1) — ZOMBIE-WALL TTL on the begin_live_arm dedupe.

A transient confirm failure strands a live_arm_pending session; the begin_live_arm dedupe
then returns it as "already active" for hours, blocking re-arm of the SAME symbol (80
zombies/7d, median 6.6h; JEM x3 on 06-30).

Fix (chili_momentum_arm_pending_ttl_enabled, default True): the dedupe treats a
live_arm_pending session OLDER than chili_momentum_arm_pending_ttl_seconds as DEAD —
terminalizes it (live_arm_expired) and allows re-arm. A FRESH pending still dedupes (no
double-arm); a genuinely-active session is never expired.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.config import Settings, settings
from app.models.core import User

pytestmark = pytest.mark.usefixtures("stable_non_alpaca_account_identity")
from app.models.trading import (
    MomentumStrategyVariant,
    TradingAutomationSession,
)
from app.services.trading.momentum_neural.operator_actions import (
    STATE_LIVE_ARM_EXPIRED,
    _arm_pending_ttl_expired,
    begin_live_arm,
)
from app.services.trading.momentum_neural.paper_fsm import (
    STATE_LIVE_ARM_PENDING,
    STATE_WATCHING,
)
from app.services.trading.momentum_neural.persistence import (
    ensure_momentum_strategy_variants,
)


def _variant(db: Session) -> MomentumStrategyVariant:
    ensure_momentum_strategy_variants(db)
    db.commit()
    return (
        db.query(MomentumStrategyVariant)
        .filter(
            MomentumStrategyVariant.family == "impulse_breakout",
            MomentumStrategyVariant.parent_variant_id.is_(None),
        )
        .order_by(MomentumStrategyVariant.version.asc(), MomentumStrategyVariant.id.asc())
        .first()
    )


def _uid(db: Session) -> int:
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    u = User(name=f"ArmTTLTest-{stamp}")
    db.add(u)
    db.commit()
    db.refresh(u)
    return int(u.id)


def _pending_session(
    db: Session, *, uid: int, variant_id: int, symbol: str, age_seconds: float
) -> TradingAutomationSession:
    created = datetime.utcnow() - timedelta(seconds=age_seconds)
    sess = TradingAutomationSession(
        user_id=uid,
        venue="robinhood",
        execution_family="robinhood_spot",
        mode="live",
        symbol=symbol,
        variant_id=variant_id,
        state=STATE_LIVE_ARM_PENDING,
        risk_snapshot_json={"arm_token": "zombie-tok"},
        correlation_id="ttl-test",
        source_node_id="test",
        started_at=created,
        created_at=created,
        updated_at=created,
    )
    db.add(sess)
    db.flush()
    return sess


def test_flag_and_ttl_defaults():
    assert Settings.model_fields["chili_momentum_arm_pending_ttl_enabled"].default is True
    assert Settings.model_fields["chili_momentum_arm_pending_ttl_seconds"].default == 120.0


def test_helper_flags_expired_pending_only(db: Session):
    uid = _uid(db)
    v = _variant(db)
    old = _pending_session(db, uid=uid, variant_id=v.id, symbol="ZMB", age_seconds=600.0)
    fresh = _pending_session(db, uid=uid, variant_id=v.id, symbol="FRSH", age_seconds=5.0)
    assert _arm_pending_ttl_expired(old) is True
    assert _arm_pending_ttl_expired(fresh) is False
    # A non-pending (active) session is NEVER expired, regardless of age.
    old.state = STATE_WATCHING
    assert _arm_pending_ttl_expired(old) is False


def test_stranded_pending_past_ttl_is_terminalized_and_rearm_allowed(db: Session):
    uid = _uid(db)
    v = _variant(db)
    # A zombie pending stranded 10 minutes ago (>> 120s TTL). No viability row is seeded, so
    # the re-arm falls through to viability_not_found AFTER the dedupe terminalizes the zombie
    # — proving the dedupe did NOT return "already active".
    zombie = _pending_session(db, uid=uid, variant_id=v.id, symbol="ZMB", age_seconds=600.0)
    zombie_id = int(zombie.id)

    result = begin_live_arm(
        db, user_id=uid, symbol="ZMB", variant_id=int(v.id), execution_family="robinhood_spot"
    )

    db.refresh(zombie)
    assert zombie.state == STATE_LIVE_ARM_EXPIRED  # old zombie terminalized
    assert zombie.ended_at is not None
    # The dedupe did NOT short-circuit as already-active: it fell through past the zombie.
    assert not result.get("deduped")
    # It proceeded to the real arm flow (which then fails viability lookup, not dedupe).
    assert result.get("ok") is False
    assert result.get("error") == "viability_not_found"
    assert result.get("session_id") != zombie_id or result.get("session_id") is None


def test_fresh_pending_still_dedupes(db: Session):
    uid = _uid(db)
    v = _variant(db)
    fresh = _pending_session(db, uid=uid, variant_id=v.id, symbol="FRSH", age_seconds=5.0)
    fresh_id = int(fresh.id)

    result = begin_live_arm(
        db, user_id=uid, symbol="FRSH", variant_id=int(v.id), execution_family="robinhood_spot"
    )

    # A fresh pending must STILL dedupe (no double-arm) — the zombie fix must not weaken this.
    assert result.get("deduped") is True
    assert result.get("session_id") == fresh_id
    db.refresh(fresh)
    assert fresh.state == STATE_LIVE_ARM_PENDING  # untouched


def test_flag_off_is_legacy_dedupes_even_stale(db: Session, monkeypatch):
    uid = _uid(db)
    v = _variant(db)
    zombie = _pending_session(db, uid=uid, variant_id=v.id, symbol="ZMB2", age_seconds=600.0)
    zombie_id = int(zombie.id)
    monkeypatch.setattr(
        settings, "chili_momentum_arm_pending_ttl_enabled", False, raising=False
    )

    result = begin_live_arm(
        db, user_id=uid, symbol="ZMB2", variant_id=int(v.id), execution_family="robinhood_spot"
    )

    # OFF => legacy: even a 10-minute-old zombie dedupes forever (the bug behavior).
    assert result.get("deduped") is True
    assert result.get("session_id") == zombie_id
    db.refresh(zombie)
    assert zombie.state == STATE_LIVE_ARM_PENDING

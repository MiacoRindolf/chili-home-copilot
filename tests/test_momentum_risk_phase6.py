"""Phase 6: momentum automation risk policy, evaluation, governance hooks, snapshots."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy.orm import Session

from app.config import settings
from app.models.core import User
from app.models.trading import MomentumStrategyVariant, TradingAutomationSession
from app.services.trading.momentum_neural.context import build_momentum_regime_context
from app.services.trading.momentum_neural.features import ExecutionReadinessFeatures
from app.services.trading.momentum_neural.persistence import ensure_momentum_strategy_variants, persist_neural_momentum_tick
from app.services.trading.momentum_neural.viability import score_viability
from app.services.trading.momentum_neural.variants import get_family
from app.services.trading.momentum_neural.automation_query import get_automation_session_detail
from app.services.trading.momentum_neural.operator_actions import create_paper_draft_session
from app.services.trading.momentum_neural.risk_evaluator import evaluate_proposed_momentum_automation
from app.services.trading.momentum_neural.risk_policy import RISK_SNAPSHOT_KEY, resolve_effective_risk_policy


def _seed_live_eligible_row(db: Session, *, symbol: str = "SOL-USD") -> tuple[int, MomentumStrategyVariant]:
    ensure_momentum_strategy_variants(db)
    db.commit()
    fam = get_family("impulse_breakout")
    assert fam is not None
    ctx = build_momentum_regime_context(
        now=datetime(2026, 4, 7, 16, 0, tzinfo=timezone.utc),
        atr_pct=0.02,
        meta={"spread_regime": "normal"},
    )
    feats = ExecutionReadinessFeatures(spread_bps=5.0)
    vr = score_viability(symbol, fam, ctx, feats)
    row = vr.to_public_dict()
    row["label"] = fam.label
    row["entry_style"] = fam.entry_style
    row["default_stop_logic"] = fam.default_stop_logic
    row["default_exit_logic"] = fam.default_exit_logic
    persist_neural_momentum_tick(
        db,
        row_dicts=[row],
        regime_snapshot=ctx.to_public_dict(),
        features=feats,
        correlation_id="op-test",
        source_node_id="nm_momentum_crypto_intel",
    )
    db.commit()
    v = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.family == "impulse_breakout").one()
    return v.id, v


def _uid(db: Session, name_suffix: str) -> int:
    u = User(name=f"RiskPhase6_{name_suffix}")
    db.add(u)
    db.commit()
    db.refresh(u)
    return int(u.id)


def test_resolve_effective_risk_policy_has_version() -> None:
    p = resolve_effective_risk_policy()
    assert p.get("policy_version") == 1
    assert "max_concurrent_sessions" in p


def test_evaluate_live_blocked_when_kill_switch(db: Session) -> None:
    vid, _ = _seed_live_eligible_row(db, symbol="KS1-USD")
    db.commit()
    uid = _uid(db, "ks1")

    with patch("app.services.trading.momentum_neural.risk_evaluator.is_kill_switch_active", return_value=True):
        ev = evaluate_proposed_momentum_automation(
            db,
            user_id=uid,
            symbol="KS1-USD",
            variant_id=vid,
            mode="live",
        )
    assert ev["allowed"] is False
    assert ev["severity"] == "block"
    assert any("Kill switch" in (e or "") for e in ev.get("errors", []))


def test_evaluate_paper_not_blocked_by_kill_switch_default(db: Session) -> None:
    vid, _ = _seed_live_eligible_row(db, symbol="KS2-USD")
    db.commit()
    uid = _uid(db, "ks2")

    with patch("app.services.trading.momentum_neural.risk_evaluator.is_kill_switch_active", return_value=True):
        ev = evaluate_proposed_momentum_automation(
            db,
            user_id=uid,
            symbol="KS2-USD",
            variant_id=vid,
            mode="paper",
        )
    assert ev["allowed"] is True


def test_concurrency_blocks_second_paper_draft(monkeypatch, db: Session) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_max_concurrent_sessions", 1)
    vid, _ = _seed_live_eligible_row(db, symbol="CC1-USD")
    _seed_live_eligible_row(db, symbol="CC2-USD")
    db.commit()
    uid = _uid(db, "cc")

    r1 = create_paper_draft_session(
        db, user_id=uid, symbol="CC1-USD", variant_id=vid, execution_family="coinbase_spot"
    )
    assert r1["ok"] is True
    db.flush()
    r2 = create_paper_draft_session(
        db, user_id=uid, symbol="CC2-USD", variant_id=vid, execution_family="coinbase_spot"
    )
    assert r2["ok"] is False
    assert r2.get("error") == "risk_blocked"


def test_paper_draft_persists_momentum_risk_snapshot(db: Session) -> None:
    vid, _ = _seed_live_eligible_row(db, symbol="SNP-USD")
    db.commit()
    uid = _uid(db, "snp")

    r = create_paper_draft_session(
        db, user_id=uid, symbol="SNP-USD", variant_id=vid, execution_family="coinbase_spot"
    )
    assert r["ok"] is True
    db.flush()
    sess = db.query(TradingAutomationSession).filter(TradingAutomationSession.id == r["session_id"]).one()
    snap = sess.risk_snapshot_json
    assert isinstance(snap, dict)
    assert RISK_SNAPSHOT_KEY in snap
    assert snap[RISK_SNAPSHOT_KEY].get("evaluated_at_utc")


def test_get_risk_policy_route(client) -> None:
    r = client.get("/api/trading/momentum/risk/policy")
    assert r.status_code == 200
    assert r.json().get("policy_version") == 1


def test_get_risk_evaluate_route_paired(paired_client, db: Session) -> None:
    vid, _ = _seed_live_eligible_row(db, symbol="REV-USD")
    db.commit()
    c, _user = paired_client
    r = c.get(
        "/api/trading/momentum/risk/evaluate",
        params={"symbol": "REV-USD", "variant_id": vid, "mode": "paper"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "allowed" in body
    assert "checks" in body


def test_confirm_live_arm_blocked_if_kill_switch_after_arm(paired_client, db: Session, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.operator_readiness.get_all_broker_statuses",
        lambda: {
            "robinhood": {"connected": False},
            "coinbase": {"connected": True, "configured": True},
            "metamask": {"connected": False},
        },
    )
    vid, _ = _seed_live_eligible_row(db, symbol="CFK-USD")
    db.commit()
    c, _user = paired_client
    with patch("app.services.trading.momentum_neural.risk_evaluator.is_kill_switch_active", return_value=False):
        r1 = c.post(
            "/api/trading/momentum/arm-live",
            json={"symbol": "CFK-USD", "variant_id": vid},
        )
    assert r1.status_code == 200
    tok = r1.json()["arm_token"]
    with patch("app.services.trading.momentum_neural.risk_evaluator.is_kill_switch_active", return_value=True):
        r2 = c.post(
            "/api/trading/momentum/confirm-live-arm",
            json={"arm_token": tok, "confirm": True},
        )
    assert r2.status_code == 400
    detail = r2.json().get("detail")
    assert isinstance(detail, dict)
    assert detail.get("error") == "risk_blocked"
    assert "risk_evaluation" in detail


def test_session_detail_includes_risk_status(db: Session) -> None:
    vid, _ = _seed_live_eligible_row(db, symbol="DTL-USD")
    db.commit()
    uid = _uid(db, "dtl")
    r = create_paper_draft_session(
        db, user_id=uid, symbol="DTL-USD", variant_id=vid, execution_family="coinbase_spot"
    )
    assert r["ok"] is True
    db.commit()
    d = get_automation_session_detail(db, user_id=uid, session_id=r["session_id"])
    assert d is not None
    rs = d["session"]["risk_status"]
    assert rs.get("severity") in ("ok", "warn", "block")
    assert "governance" in d and "risk_policy_summary" in d

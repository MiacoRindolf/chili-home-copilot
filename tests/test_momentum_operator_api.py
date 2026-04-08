"""Phase 4: momentum operator API + viable query (PostgreSQL)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.trading import MomentumSymbolViability, MomentumStrategyVariant, TradingAutomationSession
from app.services.trading.momentum_neural.context import build_momentum_regime_context
from app.services.trading.momentum_neural.features import ExecutionReadinessFeatures
from app.services.trading.momentum_neural.persistence import ensure_momentum_strategy_variants, persist_neural_momentum_tick
from app.services.trading.momentum_neural.viability import score_viability
from app.services.trading.momentum_neural.variants import get_family
from app.services.trading.momentum_neural.viable_query import build_viable_strategies_payload


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


def test_viable_payload_shape_empty(db: Session) -> None:
    ensure_momentum_strategy_variants(db)
    db.commit()
    p = build_viable_strategies_payload(db, symbol="ZZZ-USD", user_id=1, enrich_coinbase=False)
    assert p["symbol"] == "ZZZ-USD"
    assert p["strategies"] == []
    assert "neural_status" in p


def test_viable_payload_after_persist(db: Session) -> None:
    vid, _ = _seed_live_eligible_row(db, symbol="SOL-USD")
    p = build_viable_strategies_payload(db, symbol="SOL-USD", user_id=1, enrich_coinbase=False)
    assert len(p["strategies"]) >= 1
    s0 = next(s for s in p["strategies"] if s["variant_id"] == vid)
    assert "viability_score" in s0
    assert "paper_eligible" in s0
    assert "live_eligible" in s0
    assert "execution_readiness" in s0
    assert "regime" in s0
    assert "rationale" in s0
    assert "actions" in s0
    assert "can_run_paper" in s0["actions"]
    assert "can_arm_live" in s0["actions"]


def test_get_viable_http(client, db: Session) -> None:
    _seed_live_eligible_row(db, symbol="SOL-USD")
    r = client.get("/api/trading/momentum/viable", params={"symbol": "SOL-USD"})
    assert r.status_code == 200
    data = r.json()
    assert data["symbol"] == "SOL-USD"
    assert isinstance(data["strategies"], list)


def test_refresh_guest_forbidden(client) -> None:
    r = client.post(
        "/api/trading/momentum/refresh",
        json={"symbol": "BTC-USD", "execution_family": "coinbase_spot"},
    )
    assert r.status_code == 403


def test_refresh_paired_accepts_or_disabled(paired_client, db: Session) -> None:
    c, _user = paired_client
    r = c.post(
        "/api/trading/momentum/refresh",
        json={"symbol": "BTC-USD", "execution_family": "coinbase_spot"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "accepted" in body
    if body["accepted"]:
        assert body.get("correlation_id")


def test_arm_live_not_eligible_forbidden(paired_client, db: Session) -> None:
    ensure_momentum_strategy_variants(db)
    db.commit()
    v = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.family == "impulse_breakout").one()
    ms = MomentumSymbolViability(
        symbol="X-USD",
        variant_id=v.id,
        viability_score=0.5,
        paper_eligible=True,
        live_eligible=False,
        freshness_ts=datetime.utcnow(),
        regime_snapshot_json={},
        execution_readiness_json={},
        explain_json={"warnings": []},
        evidence_window_json={},
    )
    db.add(ms)
    db.commit()

    c, _u = paired_client
    r = c.post(
        "/api/trading/momentum/arm-live",
        json={"symbol": "X-USD", "variant_id": v.id},
    )
    assert r.status_code == 403


def test_arm_then_confirm_creates_armed_session(paired_client, db: Session) -> None:
    vid, _ = _seed_live_eligible_row(db, symbol="ARM-USD")
    c, user = paired_client
    r1 = c.post(
        "/api/trading/momentum/arm-live",
        json={"symbol": "ARM-USD", "variant_id": vid},
    )
    assert r1.status_code == 200
    tok = r1.json()["arm_token"]
    assert tok

    r2 = c.post(
        "/api/trading/momentum/confirm-live-arm",
        json={"arm_token": tok, "confirm": True},
    )
    assert r2.status_code == 200
    assert r2.json().get("state") == "armed_pending_runner"

    sess = (
        db.query(TradingAutomationSession)
        .filter(TradingAutomationSession.user_id == user.id, TradingAutomationSession.symbol == "ARM-USD")
        .order_by(TradingAutomationSession.id.desc())
        .first()
    )
    assert sess is not None
    assert sess.state == "armed_pending_runner"


def test_confirm_without_arm_fails(paired_client) -> None:
    c, _ = paired_client
    r = c.post(
        "/api/trading/momentum/confirm-live-arm",
        json={"arm_token": "00000000-0000-0000-0000-000000000000", "confirm": True},
    )
    assert r.status_code == 400


def test_confirm_requires_explicit_true(paired_client, db: Session) -> None:
    vid, _ = _seed_live_eligible_row(db, symbol="CFM-USD")
    c, _u = paired_client
    r1 = c.post(
        "/api/trading/momentum/arm-live",
        json={"symbol": "CFM-USD", "variant_id": vid},
    )
    assert r1.status_code == 200
    tok = r1.json()["arm_token"]
    r2 = c.post(
        "/api/trading/momentum/confirm-live-arm",
        json={"arm_token": tok, "confirm": False},
    )
    assert r2.status_code == 400


def test_run_paper_creates_draft(paired_client, db: Session) -> None:
    vid, _ = _seed_live_eligible_row(db, symbol="PAP-USD")
    c, user = paired_client
    r = c.post(
        "/api/trading/momentum/run-paper",
        json={"symbol": "PAP-USD", "variant_id": vid},
    )
    assert r.status_code == 200
    j = r.json()
    assert j.get("state") == "draft"
    assert "risk_evaluation" in j
    assert j["risk_evaluation"].get("severity") in ("ok", "warn", "block")
    sid = j.get("session_id")
    assert sid
    sess = db.query(TradingAutomationSession).filter(TradingAutomationSession.id == sid).one()
    assert sess.user_id == user.id
    assert sess.mode == "paper"

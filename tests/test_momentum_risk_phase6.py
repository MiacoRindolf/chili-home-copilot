"""Phase 6: momentum automation risk policy, evaluation, governance hooks, snapshots."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy.orm import Session

from app.config import settings
from app.models.core import User
from app.models.trading import MomentumStrategyVariant, MomentumSymbolViability, TradingAutomationSession
from app.services.trading.momentum_neural.context import build_momentum_regime_context
from app.services.trading.momentum_neural.features import ExecutionReadinessFeatures
from app.services.trading.momentum_neural.persistence import ensure_momentum_strategy_variants, persist_neural_momentum_tick
from app.services.trading.momentum_neural.viability import score_viability
from app.services.trading.momentum_neural.variants import get_family
from app.services.trading.momentum_neural.automation_query import get_automation_session_detail
from app.services.trading.momentum_neural.operator_actions import create_paper_draft_session
from app.services.trading.momentum_neural.live_fsm import STATE_LIVE_COOLDOWN, STATE_WATCHING_LIVE
from app.services.trading.momentum_neural.risk_evaluator import (
    count_concurrent_automation_sessions,
    evaluate_proposed_momentum_automation,
    _ross_lane_universe_check,
)
from app.services.trading.momentum_neural.risk_policy import (
    RISK_SNAPSHOT_KEY,
    build_session_risk_snapshot,
    policy_float_cap,
    policy_int_cap,
    resolve_effective_risk_policy,
)


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
    v = (
        db.query(MomentumStrategyVariant)
        .filter(MomentumStrategyVariant.family == "impulse_breakout")
        .order_by(MomentumStrategyVariant.id.asc())
        .first()
    )
    assert v is not None
    return v.id, v


def _uid(db: Session, name_suffix: str) -> int:
    u = User(name=f"RiskPhase6_{name_suffix}")
    db.add(u)
    db.commit()
    db.refresh(u)
    return int(u.id)


def _seed_equity_live_eligible_row(
    db: Session,
    *,
    symbol: str,
    ross_signal: dict,
) -> tuple[int, MomentumStrategyVariant]:
    ensure_momentum_strategy_variants(db)
    db.commit()
    v = (
        db.query(MomentumStrategyVariant)
        .filter(MomentumStrategyVariant.family == "impulse_breakout")
        .order_by(MomentumStrategyVariant.id.asc())
        .first()
    )
    assert v is not None
    now = datetime.utcnow()
    db.add(
        MomentumSymbolViability(
            symbol=symbol,
            scope="symbol",
            variant_id=int(v.id),
            viability_score=0.73,
            paper_eligible=True,
            live_eligible=True,
            freshness_ts=now,
            regime_snapshot_json={},
            execution_readiness_json={
                "spread_bps": 5.0,
                "extra": {"ross_signals": {symbol: dict(ross_signal)}},
            },
            explain_json={},
            evidence_window_json={},
            source_node_id="test_generic_viability",
            correlation_id="risk-ross-universe-test",
            created_at=now,
            updated_at=now,
        )
    )
    db.commit()
    return int(v.id), v


def _check_by_id(evaluation: dict, check_id: str) -> dict:
    for check in evaluation.get("checks", []):
        if check.get("id") == check_id:
            return check
    raise AssertionError(f"missing check {check_id}")


def test_ross_lane_universe_check_blocks_broad_equity_without_db() -> None:
    via = SimpleNamespace(
        execution_readiness_json={
            "extra": {
                "ross_signals": {
                    "AAPL": {
                        "ticker": "AAPL",
                        "price": 295.64,
                        "todays_change_perc": 11.0,
                        "volume": 10_000_000,
                        "rvol": 8.0,
                        "source": "generic equity momentum",
                    }
                }
            }
        }
    )

    ok, reason, detail = _ross_lane_universe_check("AAPL", via)

    assert ok is False
    assert reason == "ross_universe_price_above_profile"
    assert detail["price"] == 295.64


def test_ross_lane_universe_check_allows_smallcap_without_db() -> None:
    via = SimpleNamespace(
        execution_readiness_json={
            "extra": {
                "ross_signals": {
                    "JEM": {
                        "ticker": "JEM",
                        "price": 6.16,
                        "todays_change_perc": 12.1,
                        "volume": 300_000,
                        "source": "tape_delta_ignite",
                        "signal_type": "tape_delta_ignite",
                    }
                }
            }
        }
    )

    ok, reason, detail = _ross_lane_universe_check("JEM", via)

    assert ok is True
    assert reason == "ross_universe_profile_ok"
    assert detail["price"] == 6.16


def test_resolve_effective_risk_policy_has_version() -> None:
    p = resolve_effective_risk_policy()
    assert p.get("policy_version") == 1
    assert "max_concurrent_sessions" in p


def test_policy_cap_readers_preserve_zero_values() -> None:
    caps = {
        "max_notional_per_trade_usd": 0.0,
        "max_loss_per_trade_usd": 0.0,
        "cooldown_after_stopout_seconds": 0,
    }

    assert policy_float_cap(caps, "max_notional_per_trade_usd", 500.0) == 0.0
    assert policy_float_cap(caps, "max_loss_per_trade_usd", 50.0) == 0.0
    assert policy_int_cap(caps, "cooldown_after_stopout_seconds", 300) == 0
    assert policy_float_cap({}, "max_notional_per_trade_usd", 500.0) == 500.0
    assert policy_int_cap({}, "cooldown_after_stopout_seconds", 300) == 300


def test_session_risk_snapshot_preserves_zero_operator_caps() -> None:
    snap = build_session_risk_snapshot(
        policy_full={
            "resolved_at_utc": "2026-06-04T00:00:00+00:00",
            "max_hold_seconds": 86_400,
            "cooldown_after_stopout_seconds": 0,
            "max_notional_per_trade_usd": 0.0,
            "max_loss_per_trade_usd": 0.0,
        },
        evaluation={
            "evaluated_at_utc": "2026-06-04T00:00:00+00:00",
            "allowed": True,
            "severity": "ok",
            "checks": [],
            "warnings": [],
            "errors": [],
        },
        viability_brief=None,
        readiness_subset=None,
    )

    caps = snap["momentum_policy_caps"]
    assert caps["max_notional_per_trade_usd"] == 0.0
    assert caps["max_loss_per_trade_usd"] == 0.0
    assert caps["cooldown_after_stopout_seconds"] == 0


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


def test_ross_equity_lane_blocks_broad_live_eligible_equity(monkeypatch, db: Session) -> None:
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_crypto_only", False, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_equity_only", False, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_ross_equity_universe_required", True, raising=False)
    vid, _ = _seed_equity_live_eligible_row(
        db,
        symbol="AAPL",
        ross_signal={
            "ticker": "AAPL",
            "price": 295.64,
            "todays_change_perc": 11.0,
            "volume": 10_000_000,
            "rvol": 8.0,
            "source": "ross scanner momentum",
        },
    )
    uid = _uid(db, "ross_blocks_aapl")

    ev = evaluate_proposed_momentum_automation(
        db,
        user_id=uid,
        symbol="AAPL",
        variant_id=vid,
        mode="live",
        execution_family="robinhood_agentic_mcp",
    )

    assert ev["allowed"] is False
    check = _check_by_id(ev, "ross_equity_universe")
    assert check["severity"] == "block"
    assert check["detail"]["reason"] == "ross_universe_price_above_profile"
    assert check["message"] == "Ross equity lane blocks broad/mega-cap equity candidate."


def test_ross_equity_lane_blocks_faded_smallcap_with_precise_message(monkeypatch, db: Session) -> None:
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_crypto_only", False, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_equity_only", False, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_ross_equity_universe_required", True, raising=False)
    vid, _ = _seed_equity_live_eligible_row(
        db,
        symbol="AHMA",
        ross_signal={
            "ticker": "AHMA",
            "price": 2.06,
            "todays_change_perc": -5.9,
            "volume": 8_650_534,
            "source": "tape_delta_ignite",
        },
    )
    uid = _uid(db, "ross_blocks_faded_ahma")

    ev = evaluate_proposed_momentum_automation(
        db,
        user_id=uid,
        symbol="AHMA",
        variant_id=vid,
        mode="live",
        execution_family="robinhood_agentic_mcp",
    )

    assert ev["allowed"] is False
    check = _check_by_id(ev, "ross_equity_universe")
    assert check["severity"] == "block"
    assert check["detail"]["reason"] == "ross_universe_change_below_profile"
    assert check["message"] == "Ross equity lane blocks faded/thin small-cap candidate below profile."


def test_ross_equity_lane_allows_profile_proven_smallcap(monkeypatch, db: Session) -> None:
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_crypto_only", False, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_equity_only", False, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_ross_equity_universe_required", True, raising=False)
    vid, _ = _seed_equity_live_eligible_row(
        db,
        symbol="JEM",
        ross_signal={
            "ticker": "JEM",
            "price": 6.16,
            "todays_change_perc": 12.1,
            "volume": 300_000,
            "source": "tape_delta_ignite",
            "signal_type": "tape_delta_ignite",
        },
    )
    uid = _uid(db, "ross_allows_jem")

    ev = evaluate_proposed_momentum_automation(
        db,
        user_id=uid,
        symbol="JEM",
        variant_id=vid,
        mode="live",
        execution_family="robinhood_agentic_mcp",
    )

    check = _check_by_id(ev, "ross_equity_universe")
    assert check["ok"] is True
    assert check["detail"]["reason"] == "ross_universe_profile_ok"


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


def test_decoupled_live_concurrency_ignores_expired_watchers_and_cooldown(
    monkeypatch,
    db: Session,
) -> None:
    monkeypatch.setattr(settings, "chili_momentum_decouple_watching_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_max_watch_seconds", 60)
    vid, _ = _seed_live_eligible_row(db, symbol="JEM-USD")
    uid = _uid(db, f"decoupled_live_slots_{datetime.utcnow().timestamp()}")
    old = datetime.utcnow() - timedelta(minutes=5)

    for idx in range(3):
        db.add(
            TradingAutomationSession(
                user_id=uid,
                symbol=f"OLD{idx}-USD",
                mode="live",
                variant_id=vid,
                state=STATE_WATCHING_LIVE,
                execution_family="coinbase_spot",
                started_at=old,
                risk_snapshot_json={
                    "expires_at_utc": (old + timedelta(seconds=30)).isoformat(),
                },
            )
        )
    db.add(
        TradingAutomationSession(
            user_id=uid,
            symbol="DONE-USD",
            mode="live",
            variant_id=vid,
            state=STATE_LIVE_COOLDOWN,
            execution_family="coinbase_spot",
            started_at=datetime.utcnow(),
        )
    )
    db.commit()

    assert count_concurrent_automation_sessions(db, user_id=uid, mode="live") == 0


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
    # Coinbase live-readiness also requires verified TRADE scope (sell-scope
    # preflight) and the spot adapter enabled, so the broker gate passes and the
    # request reaches the kill-switch risk check. docs/DESIGN/MOMENTUM_LANE.md
    monkeypatch.setattr("app.services.coinbase_service.can_trade", lambda: True)
    monkeypatch.setattr(settings, "chili_coinbase_spot_adapter_enabled", True)
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


# ── execution_family alignment is symbol-routed, not variant-locked (E-phase) ──
def _alignment_check(ev: dict) -> dict | None:
    return next((c for c in ev["checks"] if c["id"] == "execution_family_variant_alignment"), None)


def test_equity_symbol_aligns_with_robinhood_spot(db: Session) -> None:
    """An equity routes to robinhood_spot; arming it on robinhood_spot must pass the
    alignment check even though every strategy variant is a coinbase_spot template."""
    vid, _ = _seed_live_eligible_row(db)
    uid = _uid(db, "rh_align")
    ev = evaluate_proposed_momentum_automation(
        db, user_id=uid, symbol="AAPL", variant_id=vid, mode="live", execution_family="robinhood_spot"
    )
    chk = _alignment_check(ev)
    assert chk is not None and chk["ok"] is True, chk
    assert chk["detail"]["symbol_resolved"] == "robinhood_spot"
    assert chk["detail"]["variant_execution_family"] == "coinbase_spot"


def test_equity_symbol_via_coinbase_is_blocked(db: Session) -> None:
    """Mis-routing an equity to coinbase_spot must BLOCK (the old variant-only check
    would have ALLOWED this because the variant template is coinbase_spot)."""
    vid, _ = _seed_live_eligible_row(db)
    uid = _uid(db, "rh_misroute")
    ev = evaluate_proposed_momentum_automation(
        db, user_id=uid, symbol="AAPL", variant_id=vid, mode="live", execution_family="coinbase_spot"
    )
    chk = _alignment_check(ev)
    assert chk is not None and chk["ok"] is False and chk["severity"] == "block", chk


def test_crypto_symbol_aligns_with_coinbase_spot(db: Session) -> None:
    """Crypto path is unchanged: BASE-USD routes to coinbase_spot and aligns."""
    vid, _ = _seed_live_eligible_row(db)
    uid = _uid(db, "cb_align")
    ev = evaluate_proposed_momentum_automation(
        db, user_id=uid, symbol="SOL-USD", variant_id=vid, mode="live", execution_family="coinbase_spot"
    )
    chk = _alignment_check(ev)
    assert chk is not None and chk["ok"] is True, chk
    assert chk["detail"]["symbol_resolved"] == "coinbase_spot"

"""Phase 6: momentum automation risk policy, evaluation, governance hooks, snapshots."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
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

pytestmark = pytest.mark.usefixtures("stable_non_alpaca_account_identity")
from app.services.trading.momentum_neural.risk_evaluator import evaluate_proposed_momentum_automation
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
    v = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.family == "impulse_breakout").one()
    return v.id, v


def _seed_equity_live_row(
    db: Session,
    *,
    symbol: str,
    signal: dict,
) -> tuple[int, MomentumStrategyVariant]:
    ensure_momentum_strategy_variants(db)
    db.commit()
    v = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.family == "impulse_breakout").one()
    row = MomentumSymbolViability(
        symbol=symbol,
        scope="symbol",
        variant_id=v.id,
        viability_score=0.91,
        paper_eligible=True,
        live_eligible=True,
        freshness_ts=datetime.utcnow(),
        regime_snapshot_json={"regime": "test"},
        execution_readiness_json={
            "spread_bps": 5.0,
            "extra": {"ross_signals": {symbol.upper(): signal}},
        },
        explain_json={},
        evidence_window_json={},
        source_node_id="test",
        correlation_id=f"risk-ross-{symbol}",
    )
    db.add(row)
    db.commit()
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


def test_live_equity_risk_blocks_non_ross_universe_broad_cap(monkeypatch, db: Session) -> None:
    monkeypatch.setattr(settings, "chili_momentum_ross_equity_universe_required", True, raising=False)
    vid, _ = _seed_equity_live_row(
        db,
        symbol="AAPL",
        signal={"price": 185.0, "daily_change_pct": 8.0, "dollar_volume": 50_000_000.0},
    )
    uid = _uid(db, "ross_broad")

    ev = evaluate_proposed_momentum_automation(
        db,
        user_id=uid,
        symbol="AAPL",
        variant_id=vid,
        mode="live",
        execution_family="robinhood_spot",
    )
    check = next(c for c in ev["checks"] if c["id"] == "ross_equity_universe")

    assert ev["allowed"] is False
    assert check["ok"] is False
    assert check["severity"] == "block"
    assert check["detail"]["reason"] == "ross_universe_price_above_profile"


def test_live_equity_risk_accepts_ross_universe_profile(monkeypatch, db: Session) -> None:
    monkeypatch.setattr(settings, "chili_momentum_ross_equity_universe_required", True, raising=False)
    vid, _ = _seed_equity_live_row(
        db,
        symbol="MOVE",
        signal={"price": 4.25, "todays_change_perc": 18.0, "volume": 400_000},
    )
    uid = _uid(db, "ross_profile")

    ev = evaluate_proposed_momentum_automation(
        db,
        user_id=uid,
        symbol="MOVE",
        variant_id=vid,
        mode="live",
        execution_family="robinhood_spot",
    )
    check = next(c for c in ev["checks"] if c["id"] == "ross_equity_universe")

    assert check["ok"] is True
    assert check["detail"]["reason"] == "ross_universe_profile_ok"


def test_alpaca_arm_defers_legacy_session_and_hypothetical_aggregate_risk(
    monkeypatch, db: Session
) -> None:
    import app.services.trading.governance as governance
    import app.services.trading.momentum_neural.risk_evaluator as risk_evaluator
    import app.services.trading.momentum_neural.risk_policy as risk_policy

    monkeypatch.setattr(settings, "chili_momentum_ross_equity_universe_required", True, raising=False)
    monkeypatch.setattr(
        settings, "chili_momentum_max_aggregate_risk_pct_of_equity", 0.03, raising=False
    )
    vid, _ = _seed_equity_live_row(
        db,
        symbol="MOVE",
        signal={"price": 4.25, "todays_change_perc": 18.0, "volume": 400_000},
    )
    uid = _uid(db, "aggregate_alpaca")
    legacy_loss_calls: list[tuple[float, str | None]] = []

    def _legacy_loss_spy(fixed: float, family: str | None = None) -> float:
        legacy_loss_calls.append((float(fixed), family))
        return 100.0

    monkeypatch.setattr(
        risk_policy,
        "_account_equity_usd",
        lambda *_args, **_kwargs: 100_000.0,
    )
    monkeypatch.setattr(
        governance,
        "_peek_broker_breach",
        lambda *_args, **_kwargs: (
            False,
            {"family": "alpaca_spot", "realized": 0.0, "cap": 5_000.0},
        ),
    )
    monkeypatch.setattr(
        risk_evaluator,
        "alpaca_paper_arm_resource_capacity",
        lambda *_args, **_kwargs: {
            "available": True,
            "risk_usd": 0.0,
            "field_size": 8,
            "watching": 5,
            "capacity": 8,
            "headroom": 3,
            "provenance": {
                "authority": "resource_only_watch_fanout",
                "financial_authority": "final_adaptive_reservation",
            },
        },
    )
    monkeypatch.setattr(
        risk_evaluator,
        "count_concurrent_automation_sessions",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy session count must not authorize an Alpaca watcher")
        ),
    )
    monkeypatch.setattr(
        risk_evaluator,
        "count_open_positions",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("position slots must not authorize an Alpaca watcher")
        ),
    )
    monkeypatch.setattr(
        risk_evaluator,
        "aggregate_open_risk_usd",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("arm time has no exact candidate R")
        ),
    )
    monkeypatch.setattr(
        risk_policy,
        "equity_relative_loss_cap",
        _legacy_loss_spy,
    )

    ev = evaluate_proposed_momentum_automation(
        db,
        user_id=uid,
        symbol="MOVE",
        variant_id=vid,
        mode="live",
        execution_family="alpaca_spot",
    )
    by_id = {check["id"]: check for check in ev["checks"]}

    assert by_id["max_concurrent_sessions"]["ok"] is True
    assert by_id["max_concurrent_sessions"]["detail"]["bypassed"] is True
    assert by_id["max_concurrent_live_sessions"]["ok"] is True
    assert by_id["alpaca_paper_watch_resource_capacity"]["ok"] is True
    aggregate = by_id["aggregate_open_risk_cap"]
    assert aggregate["ok"] is True
    assert aggregate["detail"]["bypassed"] is True
    assert aggregate["detail"]["candidate_risk_usd"] is None
    assert aggregate["detail"]["authority"] == "final_adaptive_reservation"
    assert all(family != "alpaca_spot" for _fixed, family in legacy_loss_calls)
    assert (
        ev["effective_policy_summary"]["new_risk_concurrency_authority"]
        == "final_adaptive_reservation"
    )


def test_alpaca_arm_still_fails_closed_when_watcher_resources_are_full(
    monkeypatch, db: Session
) -> None:
    import app.services.trading.governance as governance
    import app.services.trading.momentum_neural.risk_evaluator as risk_evaluator
    import app.services.trading.momentum_neural.risk_policy as risk_policy

    monkeypatch.setattr(
        settings,
        "chili_momentum_ross_equity_universe_required",
        True,
        raising=False,
    )
    vid, _ = _seed_equity_live_row(
        db,
        symbol="FULL",
        signal={"price": 4.25, "todays_change_perc": 18.0, "volume": 400_000},
    )
    uid = _uid(db, "alpaca_watch_full")
    monkeypatch.setattr(
        risk_policy,
        "_account_equity_usd",
        lambda *_args, **_kwargs: 100_000.0,
    )
    monkeypatch.setattr(
        governance,
        "_peek_broker_breach",
        lambda *_args, **_kwargs: (
            False,
            {"family": "alpaca_spot", "realized": 0.0, "cap": 5_000.0},
        ),
    )
    monkeypatch.setattr(
        risk_evaluator,
        "alpaca_paper_arm_resource_capacity",
        lambda *_args, **_kwargs: {
            "available": False,
            "risk_usd": 0.0,
            "field_size": 9,
            "watching": 9,
            "capacity": 9,
            "headroom": 0,
            "provenance": {
                "authority": "resource_only_watch_fanout",
                "financial_authority": "final_adaptive_reservation",
            },
        },
    )

    ev = evaluate_proposed_momentum_automation(
        db,
        user_id=uid,
        symbol="FULL",
        variant_id=vid,
        mode="live",
        execution_family="alpaca_spot",
    )
    check = next(
        row
        for row in ev["checks"]
        if row["id"] == "alpaca_paper_watch_resource_capacity"
    )

    assert check["ok"] is False
    assert check["severity"] == "block"
    assert check["detail"]["risk_usd"] == 0.0
    assert ev["allowed"] is False


def test_selected_direct_alpaca_route_rejects_stale_robinhood_family(
    monkeypatch, db: Session
) -> None:
    import app.services.trading.governance as governance
    import app.services.trading.momentum_neural.risk_evaluator as risk_evaluator
    import app.services.trading.momentum_neural.risk_policy as risk_policy

    monkeypatch.setattr(
        settings,
        "chili_momentum_equity_execution_via_alpaca_paper",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "chili_momentum_ross_equity_universe_required",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        risk_evaluator,
        "resolve_execution_family_for_symbol",
        lambda _symbol, *, mode="live": "alpaca_spot",
    )
    vid, _ = _seed_equity_live_row(
        db,
        symbol="ROUT",
        signal={"price": 4.25, "todays_change_perc": 18.0, "volume": 400_000},
    )
    uid = _uid(db, "alpaca_exact_route")
    monkeypatch.setattr(
        risk_policy,
        "_account_equity_usd",
        lambda *_args, **_kwargs: 100_000.0,
    )
    monkeypatch.setattr(
        governance,
        "_peek_broker_breach",
        lambda *_args, **_kwargs: (
            False,
            {"family": "robinhood_spot", "realized": 0.0, "cap": 5_000.0},
        ),
    )

    ev = evaluate_proposed_momentum_automation(
        db,
        user_id=uid,
        symbol="ROUT",
        variant_id=vid,
        mode="live",
        execution_family="robinhood_spot",
    )
    check = next(
        row
        for row in ev["checks"]
        if row["id"] == "execution_family_variant_alignment"
    )

    assert check["ok"] is False
    assert check["severity"] == "block"
    assert check["detail"]["symbol_resolved"] == "alpaca_spot"
    assert check["detail"]["direct_alpaca_route_required"] is True


def test_alpaca_risk_gate_uses_broker_daily_stop_when_generic_flag_is_off(
    monkeypatch, db: Session
) -> None:
    import app.services.trading.governance as governance
    import app.services.trading.momentum_neural.risk_policy as risk_policy

    monkeypatch.setattr(settings, "chili_per_broker_daily_loss_enabled", False)
    monkeypatch.setattr(
        settings, "chili_momentum_ross_equity_universe_required", True, raising=False
    )
    vid, _ = _seed_equity_live_row(
        db,
        symbol="MOVE",
        signal={"price": 4.25, "todays_change_perc": 18.0, "volume": 400_000},
    )
    uid = _uid(db, "alpaca_daily_stop_flag_off")
    calls: list[tuple[str, int | None]] = []

    def _peek(_db, family, *, user_id=None):
        calls.append((family, user_id))
        return True, {
            "family": family,
            "realized": -300.0,
            "cap": 250.0,
            "source": "alpaca_momentum_fixed_usd_clamp",
            "data_source": "alpaca_account_equity_delta",
        }

    monkeypatch.setattr(governance, "_peek_broker_breach", _peek)
    monkeypatch.setattr(risk_policy, "_account_equity_usd", lambda *a, **k: 100_000.0)

    ev = evaluate_proposed_momentum_automation(
        db,
        user_id=uid,
        symbol="MOVE",
        variant_id=vid,
        mode="live",
        execution_family="alpaca_spot",
    )
    check = next(c for c in ev["checks"] if c["id"] == "global_daily_loss_cap")

    assert calls == [("alpaca_spot", uid)]
    assert check["ok"] is False
    assert check["severity"] == "block"
    assert check["detail"]["data_source"] == "alpaca_account_equity_delta"


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
    import app.services.trading.momentum_neural.risk_policy as risk_policy

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
    # This test isolates the confirm-time kill-switch transition.  Give the
    # begin-arm aggregate-risk gate a deterministic, readable account equity;
    # unknown broker equity is intentionally fail-closed in production.
    monkeypatch.setattr(risk_policy, "_account_equity_usd", lambda *a, **k: 100_000.0)
    vid, _ = _seed_live_eligible_row(db, symbol="CFK-USD")
    db.commit()
    c, _user = paired_client
    with patch("app.services.trading.momentum_neural.risk_evaluator.is_kill_switch_active", return_value=False):
        r1 = c.post(
            "/api/trading/momentum/arm-live",
            json={"symbol": "CFK-USD", "variant_id": vid},
        )
    assert r1.status_code == 200, r1.json()
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

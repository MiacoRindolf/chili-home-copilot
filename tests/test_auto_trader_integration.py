"""Integration-style test for AutoTrader v1 paper path (DB + orchestrator)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy.exc import PendingRollbackError

from app import models
from app.models.trading import AutoTraderRun, BreakoutAlert, PaperTrade, ScanPattern
from app.services.trading import auto_trader as at_mod


def test_run_auto_trader_tick_paper_creates_paper_trade(db, monkeypatch):
    u = models.User(name="at_int_u")
    db.add(u)
    db.flush()

    alert = BreakoutAlert(
        ticker="ATST",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.5,
        price_at_alert=40.0,
        entry_price=40.0,
        stop_loss=38.0,
        target_price=45.0,
        user_id=u.id,
        scan_pattern_id=None,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)

    cfg = SimpleNamespace(
        chili_autotrader_enabled=True,
        chili_autotrader_live_enabled=False,
        chili_autotrader_user_id=u.id,
        brain_default_user_id=u.id,
        chili_autotrader_llm_revalidation_enabled=False,
        chili_autotrader_rth_only=False,
        chili_autotrader_confidence_floor=0.5,
        chili_autotrader_min_projected_profit_pct=0.0,
        chili_autotrader_max_symbol_price_usd=500.0,
        chili_autotrader_max_entry_slippage_pct=5.0,
        chili_autotrader_daily_loss_cap_usd=500.0,
        chili_autotrader_max_concurrent=5,
        chili_autotrader_per_trade_notional_usd=0.0,
        chili_autotrader_per_trade_risk_pct=1.0,
        chili_autotrader_synergy_enabled=False,
        chili_autotrader_synergy_scale_notional_usd=0.0,
        chili_autotrader_assumed_capital_usd=100_000.0,
    )
    monkeypatch.setattr(at_mod, "settings", cfg)
    monkeypatch.setattr(
        at_mod,
        "effective_autotrader_runtime",
        lambda _db: {
            "tick_allowed": True,
            "paused": False,
            "live_orders_effective": False,
            "live_orders_env": False,
            "desk_live_override": False,
            "monitor_entries_allowed": True,
            "payload": {},
        },
    )

    with patch.object(at_mod, "_current_price", return_value=40.0):
        from app.services.trading import auto_trader_rules as rules_mod
        monkeypatch.setattr(
            rules_mod,
            "resolve_effective_capital",
            lambda *a, **k: (100_000.0, "test_equity"),
        )
        monkeypatch.setattr(
            rules_mod,
            "resolve_brain_risk_context",
            lambda *a, **k: {"dial_value": 1.0, "source": "test"},
        )
        with patch(
            "app.services.trading.portfolio_risk.check_new_trade_allowed",
            return_value=(True, "ok"),
        ):
            out = at_mod.run_auto_trader_tick(db)

    assert out.get("ok") is True
    assert out.get("placed", 0) >= 1
    run = db.query(AutoTraderRun).filter(AutoTraderRun.breakout_alert_id == alert.id).first()
    assert run is not None
    assert run.decision == "placed"
    pt = (
        db.query(PaperTrade)
        .filter(PaperTrade.user_id == u.id, PaperTrade.ticker == "ATST")
        .first()
    )
    assert pt is not None
    sj = pt.signal_json or {}
    assert sj.get("auto_trader_v1") is True


def test_run_auto_trader_tick_matches_null_user_alerts(db, monkeypatch):
    """Production imminent alerts are written with user_id=NULL (system-scope).
    The entry tick must still process them when an autotrader user is configured."""
    u = models.User(name="at_int_null_u")
    db.add(u)
    db.flush()

    alert = BreakoutAlert(
        ticker="NULLU",
        asset_type="stock",
        alert_tier="pattern_imminent",
        score_at_alert=0.6,
        price_at_alert=50.0,
        entry_price=50.0,
        stop_loss=47.0,
        target_price=60.0,
        user_id=None,  # system-scope, matches production
        scan_pattern_id=None,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)

    cfg = SimpleNamespace(
        chili_autotrader_enabled=True,
        chili_autotrader_live_enabled=False,
        chili_autotrader_user_id=u.id,
        brain_default_user_id=u.id,
        chili_autotrader_llm_revalidation_enabled=False,
        chili_autotrader_rth_only=False,
        chili_autotrader_confidence_floor=0.5,
        chili_autotrader_min_projected_profit_pct=0.0,
        chili_autotrader_max_symbol_price_usd=500.0,
        chili_autotrader_max_entry_slippage_pct=5.0,
        chili_autotrader_daily_loss_cap_usd=500.0,
        chili_autotrader_max_concurrent=5,
        chili_autotrader_per_trade_notional_usd=0.0,
        chili_autotrader_per_trade_risk_pct=1.0,
        chili_autotrader_synergy_enabled=False,
        chili_autotrader_synergy_scale_notional_usd=0.0,
        chili_autotrader_assumed_capital_usd=100_000.0,
    )
    monkeypatch.setattr(at_mod, "settings", cfg)
    monkeypatch.setattr(
        at_mod,
        "effective_autotrader_runtime",
        lambda _db: {
            "tick_allowed": True,
            "paused": False,
            "live_orders_effective": False,
            "live_orders_env": False,
            "desk_live_override": False,
            "monitor_entries_allowed": True,
            "payload": {},
        },
    )

    with patch.object(at_mod, "_current_price", return_value=50.0):
        from app.services.trading import auto_trader_rules as rules_mod
        monkeypatch.setattr(
            rules_mod,
            "resolve_effective_capital",
            lambda *a, **k: (100_000.0, "test_equity"),
        )
        monkeypatch.setattr(
            rules_mod,
            "resolve_brain_risk_context",
            lambda *a, **k: {"dial_value": 1.0, "source": "test"},
        )
        with patch(
            "app.services.trading.portfolio_risk.check_new_trade_allowed",
            return_value=(True, "ok"),
        ):
            out = at_mod.run_auto_trader_tick(db)

    assert out.get("ok") is True
    # The null-user alert must have been picked up (either placed or audited).
    run = db.query(AutoTraderRun).filter(AutoTraderRun.breakout_alert_id == alert.id).first()
    assert run is not None, "null-user alert was not processed"


def test_run_auto_trader_tick_audits_exception_context(db, monkeypatch):
    u = models.User(name="at_int_error_u")
    pat = ScanPattern(
        id=585,
        name="error pattern",
        rules_json={},
        active=True,
        lifecycle_stage="promoted",
    )
    db.add(pat)
    db.add(u)
    db.flush()

    alert = BreakoutAlert(
        ticker="ERRQ",
        asset_type="crypto",
        alert_tier="pattern_imminent",
        score_at_alert=0.75,
        price_at_alert=10.0,
        entry_price=10.0,
        stop_loss=9.0,
        target_price=12.0,
        user_id=None,
        scan_pattern_id=585,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)

    cfg = SimpleNamespace(
        chili_autotrader_enabled=True,
        chili_autotrader_live_enabled=False,
        chili_autotrader_user_id=u.id,
        brain_default_user_id=u.id,
        chili_autotrader_synergy_enabled=False,
    )
    monkeypatch.setattr(at_mod, "settings", cfg)
    monkeypatch.setattr(
        at_mod,
        "effective_autotrader_runtime",
        lambda _db: {
            "tick_allowed": True,
            "paused": False,
            "live_orders_effective": False,
            "live_orders_env": False,
            "desk_live_override": False,
            "monitor_entries_allowed": True,
            "payload": {},
        },
    )

    exc_msg = (
        "Query.filter() being called on a Query which already has LIMIT or "
        "OFFSET applied.  Call filter() before limit() or offset() are applied."
    )
    with patch.object(at_mod, "_process_one_alert", side_effect=RuntimeError(exc_msg)):
        out = at_mod.run_auto_trader_tick(db)

    assert out.get("ok") is True
    assert out.get("processed") == 1
    run = (
        db.query(AutoTraderRun)
        .filter(AutoTraderRun.breakout_alert_id == alert.id)
        .one()
    )
    assert run.decision == "error"
    snap = run.rule_snapshot or {}
    assert snap["autotrader_exception"] is True
    assert snap["error_phase"] == "process_alert"
    assert snap["error_type"] == "RuntimeError"
    assert snap["error_classification"] == "query_filter_after_limit"
    assert snap["alert_ticker"] == "ERRQ"
    assert snap["alert_asset_type"] == "crypto"
    assert snap["alert_scan_pattern_id"] == 585
    assert snap["error_frames"]


class _AuditRecoverySession:
    def __init__(self, *, active: bool = True, fail_first_commit: bool = False):
        self.is_active = active
        self.fail_first_commit = fail_first_commit
        self.rows = []
        self.rollback_count = 0
        self.commit_count = 0

    def rollback(self):
        self.rollback_count += 1
        self.is_active = True

    def add(self, row):
        self.rows.append(row)

    def commit(self):
        self.commit_count += 1
        if self.fail_first_commit and self.commit_count == 1:
            raise PendingRollbackError("test pending rollback")


def _audit_recovery_alert() -> BreakoutAlert:
    alert = BreakoutAlert(ticker="AUDR", scan_pattern_id=586, entry_price=10.0)
    alert.id = 123
    return alert


def test_audit_recovers_inactive_session_before_write():
    db = _AuditRecoverySession(active=False)

    at_mod._audit(
        db,
        user_id=1,
        alert=_audit_recovery_alert(),
        decision="skipped",
        reason="test_inactive_session_recovery",
        rule_snapshot={"source": "test"},
    )

    assert db.rollback_count == 1
    assert db.commit_count == 1
    run = db.rows[0]
    assert run.decision == "skipped"
    assert run.reason == "test_inactive_session_recovery"
    assert run.rule_snapshot["source"] == "test"
    assert run.rule_snapshot["audit_session_recovered"] is True
    assert run.rule_snapshot["audit_session_recovery_reason"] == "inactive_transaction"


def test_audit_retries_pending_rollback_commit_once():
    db = _AuditRecoverySession(active=True, fail_first_commit=True)

    at_mod._audit(
        db,
        user_id=1,
        alert=_audit_recovery_alert(),
        decision="skipped",
        reason="test_pending_rollback_retry",
        rule_snapshot={"source": "test"},
    )

    assert db.rollback_count == 1
    assert db.commit_count == 2
    run = db.rows[-1]
    assert run.decision == "skipped"
    assert run.reason == "test_pending_rollback_retry"
    assert run.rule_snapshot["source"] == "test"
    assert run.rule_snapshot["audit_session_recovered"] is True
    assert run.rule_snapshot["audit_session_recovery_reason"] == "pending_rollback_retry"

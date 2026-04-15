"""Assign scan pattern to open trade (Monitor attribution + synthetic BreakoutAlert)."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.models.trading import BreakoutAlert, PatternMonitorDecision, ScanPattern, Trade
from app.services import trading_service as ts
from app.services.trading.pattern_adjustment_advisor import AdjustmentRecommendation
from app.services.trading.pattern_position_monitor import run_pattern_position_monitor_for_trades


def _crypto_pattern(db):
    sp = ScanPattern(
        name="Test crypto RSI",
        rules_json={"conditions": [{"indicator": "rsi_14", "op": "<", "value": 30}]},
        origin="test",
        asset_class="crypto",
    )
    db.add(sp)
    db.commit()
    db.refresh(sp)
    return sp


def _stock_pattern(db):
    sp = ScanPattern(
        name="Test stock RSI",
        rules_json={"conditions": [{"indicator": "rsi_14", "op": "<", "value": 30}]},
        origin="test",
        asset_class="stock",
    )
    db.add(sp)
    db.commit()
    db.refresh(sp)
    return sp


class TestAssignScanPatternService:
    def test_assign_creates_breakout_alert_and_fks(self, db):
        sp = _crypto_pattern(db)
        trade = ts.create_trade(
            db,
            user_id=1,
            ticker="BTC-USD",
            direction="long",
            entry_price=100.0,
            quantity=1.0,
        )
        assert trade.related_alert_id is None

        t2, err = ts.assign_scan_pattern_to_trade(db, trade.id, 1, sp.id)
        assert err is None
        assert t2.scan_pattern_id == sp.id
        assert t2.related_alert_id is not None

        alert = db.query(BreakoutAlert).filter(BreakoutAlert.id == t2.related_alert_id).first()
        assert alert is not None
        assert alert.scan_pattern_id == sp.id
        assert alert.alert_tier == "user_assigned"
        assert alert.ticker == "BTC-USD"

    def test_assign_asset_mismatch_crypto_pattern_on_stock(self, db):
        sp = _crypto_pattern(db)
        trade = ts.create_trade(
            db,
            user_id=1,
            ticker="AAPL",
            direction="long",
            entry_price=150.0,
            quantity=1.0,
        )
        t2, err = ts.assign_scan_pattern_to_trade(db, trade.id, 1, sp.id)
        assert t2 is None
        assert err == "asset_mismatch"

    def test_assign_invalid_pattern_no_conditions(self, db):
        sp = ScanPattern(
            name="Empty rules",
            rules_json={"conditions": []},
            origin="test",
            asset_class="all",
        )
        db.add(sp)
        db.commit()
        db.refresh(sp)
        trade = ts.create_trade(
            db,
            user_id=1,
            ticker="ETH-USD",
            direction="long",
            entry_price=1.0,
            quantity=1.0,
        )
        t2, err = ts.assign_scan_pattern_to_trade(db, trade.id, 1, sp.id)
        assert t2 is None
        assert err == "pattern_invalid"

    def test_clear_assignment_nulls_fks(self, db):
        sp = _crypto_pattern(db)
        trade = ts.create_trade(
            db,
            user_id=1,
            ticker="SOL-USD",
            direction="long",
            entry_price=50.0,
            quantity=1.0,
        )
        t2, err = ts.assign_scan_pattern_to_trade(db, trade.id, 1, sp.id)
        assert err is None
        t3, err2 = ts.assign_scan_pattern_to_trade(db, trade.id, 1, None)
        assert err2 is None
        assert t3.scan_pattern_id is None
        assert t3.related_alert_id is None


class TestAssignScanPatternMonitor:
    """Monitor evaluates assigned trades (not skipped at alert gate)."""

    def test_skipped_without_related_alert(self, db):
        trade = ts.create_trade(
            db,
            user_id=1,
            ticker="ZK-USD",
            direction="long",
            entry_price=1.0,
            quantity=1.0,
        )
        summary = run_pattern_position_monitor_for_trades(
            db, [trade], dry_run=True, event_driven=False,
        )
        assert summary["evaluated"] == 1
        assert summary["skipped"] == 1

    def test_not_skipped_after_assign_with_mocks(self, db):
        sp = _crypto_pattern(db)
        trade = ts.create_trade(
            db,
            user_id=1,
            ticker="ZK-USD",
            direction="long",
            entry_price=100.0,
            quantity=1.0,
        )
        t2, err = ts.assign_scan_pattern_to_trade(db, trade.id, 1, sp.id)
        assert err is None
        db.refresh(t2)
        trade_fresh = db.query(Trade).filter(Trade.id == t2.id).first()

        fake_snap = {"rsi": {"value": 25.0}}
        fake_quote = {"price": 100.0}

        hold_rec = AdjustmentRecommendation(action="hold", confidence=1.0, reasoning="test")
        with patch(
            "app.services.trading.pattern_position_monitor.get_indicator_snapshot",
            return_value=fake_snap,
        ):
            with patch(
                "app.services.trading.pattern_position_monitor.fetch_quote",
                return_value=fake_quote,
            ):
                with patch(
                    "app.services.trading.pattern_adjustment_advisor.get_adjustment",
                    return_value=hold_rec,
                ):
                    summary = run_pattern_position_monitor_for_trades(
                        db, [trade_fresh], dry_run=True, event_driven=False,
                    )
        assert summary["evaluated"] == 1
        assert summary["skipped"] == 0


class TestAssignScanPatternAPI:
    def test_assign_pattern_endpoint(self, db, paired_client):
        client, user = paired_client
        sp = _stock_pattern(db)
        trade = ts.create_trade(
            db,
            user_id=user.id,
            ticker="MSFT",
            direction="long",
            entry_price=300.0,
            quantity=1.0,
        )
        res = client.post(
            f"/api/trading/trades/{trade.id}/assign-pattern",
            json={"scan_pattern_id": sp.id},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["ok"] is True
        assert data["scan_pattern_id"] == sp.id
        assert data["related_alert_id"] is not None

        row = db.query(Trade).filter(Trade.id == trade.id).first()
        assert row.scan_pattern_id == sp.id


class TestAttachBreakoutAlertToTrade:
    def test_attach_links_trade_and_fills_stops(self, db):
        sp = _stock_pattern(db)
        user_id = 1
        alert = BreakoutAlert(
            ticker="AIFF",
            asset_type="stock",
            alert_tier="pattern_imminent",
            score_at_alert=0.55,
            price_at_alert=2.5,
            stop_loss=1.4,
            target_price=2.57,
            user_id=user_id,
            scan_pattern_id=sp.id,
        )
        db.add(alert)
        db.commit()
        db.refresh(alert)

        trade = ts.create_trade(
            db,
            user_id=user_id,
            ticker="AIFF",
            direction="long",
            entry_price=2.57,
            quantity=300.0,
        )
        assert trade.related_alert_id is None

        t2, err = ts.attach_breakout_alert_to_open_trade(db, alert.id, user_id)
        assert err is None
        assert t2.related_alert_id == alert.id
        assert t2.scan_pattern_id == sp.id
        assert t2.stop_loss == 1.4
        assert t2.take_profit == 2.57

    def test_attach_forbidden_wrong_user(self, db):
        sp = _stock_pattern(db)
        alert = BreakoutAlert(
            ticker="XOM",
            asset_type="stock",
            alert_tier="pattern_imminent",
            score_at_alert=0.5,
            price_at_alert=100.0,
            user_id=99,
            scan_pattern_id=sp.id,
        )
        db.add(alert)
        db.commit()
        db.refresh(alert)
        ts.create_trade(db, user_id=1, ticker="XOM", direction="long", entry_price=100.0, quantity=1.0)
        t2, err = ts.attach_breakout_alert_to_open_trade(db, alert.id, 1)
        assert t2 is None
        assert err == "forbidden"

    def test_attach_no_open_trade(self, db):
        sp = _stock_pattern(db)
        alert = BreakoutAlert(
            ticker="ZZZ",
            asset_type="stock",
            alert_tier="pattern_imminent",
            score_at_alert=0.5,
            price_at_alert=10.0,
            user_id=1,
            scan_pattern_id=sp.id,
        )
        db.add(alert)
        db.commit()
        db.refresh(alert)
        t2, err = ts.attach_breakout_alert_to_open_trade(db, alert.id, 1)
        assert t2 is None
        assert err == "no_open_trade"

    def test_attach_api_endpoint(self, db, paired_client):
        client, user = paired_client
        sp = _stock_pattern(db)
        alert = BreakoutAlert(
            ticker="LINKME",
            asset_type="stock",
            alert_tier="pattern_imminent",
            score_at_alert=0.5,
            price_at_alert=50.0,
            user_id=user.id,
            scan_pattern_id=sp.id,
        )
        db.add(alert)
        db.commit()
        db.refresh(alert)
        trade = ts.create_trade(
            db,
            user_id=user.id,
            ticker="LINKME",
            direction="long",
            entry_price=50.0,
            quantity=2.0,
        )
        res = client.post(f"/api/trading/breakout-alerts/{alert.id}/attach-to-open-trade", json={})
        assert res.status_code == 200
        data = res.json()
        assert data["ok"] is True
        assert data["related_alert_id"] == alert.id
        row = db.query(Trade).filter(Trade.id == trade.id).first()
        assert row is not None
        assert row.related_alert_id == alert.id


class TestPlanLevelsPatternMonitor:
    """Plan-only trades (stop/target, no related_alert_id) get monitor decisions."""

    def test_plan_levels_writes_decision(self, db, paired_client):
        _client, user = paired_client
        trade = ts.create_trade(
            db,
            user_id=user.id,
            ticker="PLAN-USD",
            direction="long",
            entry_price=100.0,
            quantity=1.0,
        )
        t2, err = ts.apply_trade_plan_levels(
            db, trade.id, user.id, stop_loss=90.0, take_profit=110.0
        )
        assert err is None
        db.refresh(t2)
        assert t2.related_alert_id is None

        with patch(
            "app.services.trading.pattern_position_monitor.fetch_quote",
            return_value={"price": 95.0},
        ):
            summary = run_pattern_position_monitor_for_trades(
                db, [t2], dry_run=True, event_driven=False,
            )
        assert summary["evaluated"] == 1
        decs = (
            db.query(PatternMonitorDecision)
            .filter(PatternMonitorDecision.trade_id == t2.id)
            .all()
        )
        assert len(decs) >= 1
        assert decs[0].decision_source == "plan_levels"
        assert decs[0].health_score is not None

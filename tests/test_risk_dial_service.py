"""Phase I - DB integration tests for ``risk_dial_service``.

Exercises mode gating, row writes, JSON payload shape, summary
shape, and ``get_latest_dial`` read path.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.services.trading.risk_dial_model import RiskDialConfig
from app.services.trading.risk_dial_service import (
    dial_state_summary,
    get_latest_dial,
    mode_is_active,
    mode_is_authoritative,
    resolve_dial,
)


def _cleanup(db) -> None:
    db.execute(text(
        "DELETE FROM trading_risk_dial_state WHERE source LIKE 'phi_test_%'"
    ))
    db.commit()


def _force_mode(monkeypatch, mode: str) -> None:
    monkeypatch.setattr(
        "app.services.trading.risk_dial_service.settings.brain_risk_dial_mode",
        mode,
        raising=False,
    )


class TestModeGate:
    def test_off_mode_is_noop(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "off")
        assert mode_is_active() is False
        assert mode_is_authoritative() is False
        res = resolve_dial(
            db,
            user_id=None,
            regime="risk_on",
            drawdown_pct=0.0,
            source="phi_test_off",
        )
        assert res is None
        count = db.execute(text(
            "SELECT COUNT(*) FROM trading_risk_dial_state WHERE source = 'phi_test_off'"
        )).scalar_one()
        assert count == 0

    def test_shadow_mode_writes_one_row(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "shadow")
        assert mode_is_active() is True
        res = resolve_dial(
            db,
            user_id=42,
            regime="cautious",
            drawdown_pct=5.0,
            source="phi_test_shadow",
            reason="scheduled_sweep",
        )
        assert res is not None
        assert res.mode == "shadow"
        assert res.dial_value > 0
        count = db.execute(text(
            "SELECT COUNT(*) FROM trading_risk_dial_state WHERE source = 'phi_test_shadow'"
        )).scalar_one()
        assert count == 1

    def test_unknown_mode_coerced_to_off(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "gibberish")
        res = resolve_dial(
            db,
            user_id=None,
            regime="risk_on",
            source="phi_test_unknown",
        )
        assert res is None


class TestRowContents:
    def test_row_captures_reasoning_and_regime(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "shadow")
        res = resolve_dial(
            db,
            user_id=7,
            regime="risk_off",
            drawdown_pct=15.0,  # beyond default trigger of 10% -> floor
            source="phi_test_contents",
        )
        assert res is not None
        row = db.execute(text("""
            SELECT user_id, dial_value, regime, source, mode,
                   payload_json->>'drawdown_multiplier',
                   payload_json->>'regime_default',
                   payload_json->>'capped_at_ceiling'
            FROM trading_risk_dial_state WHERE id = :id
        """), {"id": res.log_id}).fetchone()
        assert row is not None
        assert row[0] == 7
        assert float(row[1]) > 0
        assert row[2] == "risk_off"
        assert row[3] == "phi_test_contents"
        assert row[4] == "shadow"
        # drawdown floor 0.5 for 15% DD (beyond 10% trigger).
        assert float(row[5]) == pytest.approx(0.5)
        assert float(row[6]) == pytest.approx(0.3)  # default_risk_off
        assert row[7] == "false"

    def test_override_rejected_is_recorded(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "shadow")
        # Ceiling=1.5, regime_default risk_on=1.0 -> override=2.0 is > 1.5 -> reject
        res = resolve_dial(
            db,
            user_id=None,
            regime="risk_on",
            drawdown_pct=0.0,
            user_override_multiplier=2.0,
            source="phi_test_override",
        )
        assert res is not None
        assert res.override_rejected is True
        row = db.execute(text("""
            SELECT payload_json->>'override_rejected'
            FROM trading_risk_dial_state WHERE id = :id
        """), {"id": res.log_id}).fetchone()
        assert row[0] == "true"


class TestLatestDialRead:
    def test_get_latest_returns_most_recent(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "shadow")
        r1 = resolve_dial(
            db, user_id=99, regime="risk_on", source="phi_test_latest",
        )
        assert r1 is not None
        import time
        time.sleep(0.01)  # ensure observed_at differs
        r2 = resolve_dial(
            db, user_id=99, regime="risk_off", source="phi_test_latest",
        )
        assert r2 is not None
        latest = get_latest_dial(db, user_id=99, default=1.0)
        assert latest == pytest.approx(r2.dial_value)

    def test_get_latest_returns_default_when_no_rows(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "shadow")
        v = get_latest_dial(db, user_id=-12345, default=0.9)
        assert v == pytest.approx(0.9)

    def test_get_latest_returns_default_when_off(self, db, monkeypatch):
        _force_mode(monkeypatch, "off")
        v = get_latest_dial(db, user_id=99, default=1.25)
        assert v == pytest.approx(1.25)


class TestSummary:
    def test_summary_frozen_shape(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "shadow")
        resolve_dial(
            db, user_id=1, regime="risk_on", source="phi_test_sum",
        )
        resolve_dial(
            db, user_id=1, regime="cautious", drawdown_pct=12.0,
            source="phi_test_sum",
        )
        summary = dial_state_summary(db, lookback_hours=24)
        assert set(summary.keys()) == {
            "mode", "lookback_hours", "dial_events_total",
            "by_regime", "by_source", "by_dial_bucket",
            "mean_dial_value", "latest_dial",
            "override_rejected_count", "capped_at_ceiling_count",
        }
        assert summary["mode"] == "shadow"
        assert summary["lookback_hours"] == 24
        assert summary["dial_events_total"] >= 2
        assert set(summary["by_dial_bucket"].keys()) == {
            "under_0_5", "0_5_to_0_8", "0_8_to_1_0", "1_0_to_1_2", "over_1_2",
        }
        assert summary["by_source"].get("phi_test_sum", 0) >= 2
        assert summary["latest_dial"] is not None


class TestCustomConfig:
    def test_custom_config_overrides_settings(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "shadow")
        cfg = RiskDialConfig(
            default_risk_on=0.8,
            default_cautious=0.5,
            default_risk_off=0.2,
            drawdown_floor=0.4,
            drawdown_trigger_pct=8.0,
            ceiling=1.0,
        )
        res = resolve_dial(
            db,
            user_id=None,
            regime="risk_on",
            drawdown_pct=0.0,
            source="phi_test_custom",
            config=cfg,
        )
        assert res is not None
        assert res.dial_value == pytest.approx(0.8)

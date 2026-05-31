from __future__ import annotations

from types import SimpleNamespace

from app.services.trading.edge_reliability import (
    _recent_recert_rescue_blocker_exists,
)


class _Query:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, value):
        assert value == 20
        return self

    def all(self):
        return self._rows


def _db_with_payloads(*payloads):
    rows = [SimpleNamespace(payload=payload) for payload in payloads]
    return SimpleNamespace(query=lambda model: _Query(rows))


def test_recent_recert_rescue_blocker_blocks_completion_action(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "brain_work_cash_deployment_noop_cooldown_minutes", 360)
    db = _db_with_payloads(
        {
            "scan_pattern_id": 1260,
            "recommended_next_action": "complete_oos_recert_and_quality_refresh",
            "recert_rescue_status": "soft_blocked",
        }
    )

    assert _recent_recert_rescue_blocker_exists(db, scan_pattern_id=1260) is True


def test_recent_recert_rescue_blocker_blocks_open_backtest_reason(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "brain_work_cash_deployment_noop_cooldown_minutes", 360)
    db = _db_with_payloads(
        {
            "scan_pattern_id": 1256,
            "recommended_next_action": "run_recert_backtest_refresh_keep_live_blocked",
            "recert_backtest_refresh": {
                "reason": "recert_backtest_refresh_already_open",
                "requested": False,
            },
        }
    )

    assert _recent_recert_rescue_blocker_exists(db, scan_pattern_id=1256) is True


def test_recent_recert_rescue_blocker_allows_useful_refresh(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "brain_work_cash_deployment_noop_cooldown_minutes", 360)
    db = _db_with_payloads(
        {
            "scan_pattern_id": 1256,
            "recommended_next_action": "run_recert_backtest_refresh_keep_live_blocked",
            "recert_backtest_refresh": {
                "reason": "positive_edge_supply_needs_asset_sliced_oos_refresh",
                "requested": True,
            },
        }
    )

    assert _recent_recert_rescue_blocker_exists(db, scan_pattern_id=1256) is False

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from app.config import settings
from app.models.trading import AutoTraderRun, BreakoutAlert
from app.services.trading import runtime_status


class _FakeQuery:
    def __init__(self, session: "_FakeSession", model: object):
        self.session = session
        self.model = model

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def outerjoin(self, *args, **kwargs):
        return self

    def with_entities(self, *args, **kwargs):
        return self

    def first(self):
        if self.model is AutoTraderRun:
            rows = self.session.runs
            return max(rows, key=lambda row: (row.created_at, row.id or 0), default=None)
        if self.model is BreakoutAlert:
            rows = [a for a in self.session.alerts if a.alert_tier == "pattern_imminent"]
            return max(rows, key=lambda row: (row.alerted_at, row.id or 0), default=None)
        return None

    def one(self):
        latest_run = max(
            self.session.runs,
            key=lambda row: (row.created_at, row.id or 0),
            default=None,
        )
        run_alert_ids = {
            run.breakout_alert_id
            for run in self.session.runs
            if getattr(run, "breakout_alert_id", None) is not None
        }
        threshold = runtime_status._autotrader_stale_threshold_seconds()
        stale_cutoff = datetime.utcnow() - timedelta(seconds=threshold)
        alerts = [
            alert
            for alert in self.session.alerts
            if alert.alert_tier == "pattern_imminent"
            and alert.id not in run_alert_ids
            and (
                latest_run is None
                or alert.alerted_at > latest_run.created_at
            )
        ]
        return (
            len(alerts),
            sum(1 for alert in alerts if alert.asset_type == "stock"),
            sum(1 for alert in alerts if alert.asset_type == "crypto"),
            sum(1 for alert in alerts if alert.alerted_at <= stale_cutoff),
            max((alert.id for alert in alerts), default=None),
            max((alert.alerted_at for alert in alerts), default=None),
        )


class _FakeSession:
    def __init__(self, *, runs: list[SimpleNamespace], alerts: list[SimpleNamespace]):
        self.runs = runs
        self.alerts = alerts

    def query(self, model: object):
        return _FakeQuery(self, model)


def _run(**overrides) -> SimpleNamespace:
    defaults = {
        "id": 1,
        "breakout_alert_id": None,
        "ticker": "OLD",
        "decision": "skipped",
        "reason": "unit_test_seed",
        "created_at": datetime.utcnow() - timedelta(minutes=10),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _alert(**overrides) -> SimpleNamespace:
    defaults = {
        "id": 10,
        "ticker": "ROSS",
        "asset_type": "stock",
        "alert_tier": "pattern_imminent",
        "score_at_alert": 0.9,
        "price_at_alert": 8.5,
        "alerted_at": datetime.utcnow() - timedelta(minutes=5),
        "outcome": "pending",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _enable_autotrader_health(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_autotrader_enabled", True)
    monkeypatch.setattr(settings, "chili_autotrader_monitor_interval_seconds", 60)
    monkeypatch.setattr(settings, "chili_autotrader_stale_candidate_sweep_interval_seconds", 60)
    monkeypatch.setattr(settings, "chili_autotrader_fresh_candidate_fastlane_max_age_seconds", 30)
    monkeypatch.setattr(settings, "chili_autotrader_stock_candidate_max_age_minutes", 30)
    monkeypatch.setattr(settings, "chili_autotrader_non_stock_candidate_max_age_minutes", 30)


def test_autotrader_status_flags_stale_unprocessed_backlog(monkeypatch) -> None:
    _enable_autotrader_health(monkeypatch)
    now = datetime.utcnow()
    db = _FakeSession(
        runs=[_run(id=1, created_at=now - timedelta(minutes=10))],
        alerts=[_alert(id=2, alerted_at=now - timedelta(minutes=5))],
    )

    surface = runtime_status.autotrader_status(db)

    assert surface["state"] == "error"
    assert surface["unprocessed_alerts_after_last_run"] == 1
    assert surface["unprocessed_stock_alerts_after_last_run"] == 1
    assert surface["stale_unprocessed_alerts"] == 1
    assert surface["stale_threshold_seconds"] == 180


def test_autotrader_status_allows_fresh_unprocessed_backlog(monkeypatch) -> None:
    _enable_autotrader_health(monkeypatch)
    now = datetime.utcnow()
    db = _FakeSession(
        runs=[_run(id=1, created_at=now - timedelta(minutes=1))],
        alerts=[_alert(id=2, alerted_at=now - timedelta(seconds=20))],
    )

    surface = runtime_status.autotrader_status(db)

    assert surface["state"] == "ok"
    assert surface["unprocessed_alerts_after_last_run"] == 1
    assert surface["stale_unprocessed_alerts"] == 0


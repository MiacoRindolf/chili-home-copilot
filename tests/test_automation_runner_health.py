from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.config import settings
from app.services.trading.momentum_neural import automation_query as aq
from app.services.trading.momentum_neural import lane_health as lh


def _live_session(
    *,
    execution_family: str = "alpaca_spot",
    symbol: str = "ACTU",
) -> SimpleNamespace:
    return SimpleNamespace(
        mode="live",
        execution_family=execution_family,
        symbol=symbol,
        risk_snapshot_json={},
    )


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _enable_event_driver(monkeypatch) -> None:
    monkeypatch.setattr(
        settings,
        "chili_momentum_live_runner_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "chili_momentum_live_runner_scheduler_enabled",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "chili_momentum_live_runner_loop_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "chili_autopilot_price_bus_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        aq,
        "get_kill_switch_status",
        lambda: {"active": False},
    )


def test_neural_config_reports_exclusive_event_driver(monkeypatch) -> None:
    _enable_event_driver(monkeypatch)

    strip = aq.neural_config_strip()

    assert strip["live_runner_enabled"] is True
    assert strip["live_runner_scheduler_enabled"] is False
    assert strip["live_runner_loop_enabled"] is True
    assert strip["live_runner_driver_mode"] == "event_loop"
    assert strip["live_runner_driver_enabled"] is True
    assert strip["live_runner_driver_blocked_reason"] is None
    assert strip["autopilot_price_bus_enabled"] is True


def test_event_loop_health_uses_durable_heartbeat_and_session_family(
    monkeypatch,
) -> None:
    _enable_event_driver(monkeypatch)
    heartbeat_at = _utcnow_naive() - timedelta(seconds=2)
    monkeypatch.setattr(lh, "live_loop_stale_seconds", lambda: 75.0)
    monkeypatch.setattr(
        lh,
        "_latest_live_loop_heartbeat_status",
        lambda _db, *, stale_seconds: {
            "ok": True,
            "heartbeat_at": heartbeat_at,
            "stale_seconds": stale_seconds,
        },
    )
    readiness_calls: list[tuple[str, str | None]] = []

    def _readiness(*, execution_family: str, symbol: str | None = None):
        readiness_calls.append((execution_family, symbol))
        return {
            "broker_ready_for_live": True,
            "runnable_live_now": True,
        }

    monkeypatch.setattr(aq, "build_momentum_operator_readiness", _readiness)

    health = aq._runner_health_for_mode(
        MagicMock(),
        mode="live",
        sess=_live_session(),
    )

    assert health["blocked_reason"] is None
    assert health["scheduler_enabled"] is True
    assert health["driver_mode"] == "event_loop"
    assert health["legacy_batch_scheduler_enabled"] is False
    assert health["event_loop_enabled"] is True
    assert health["last_tick_source"] == "live_loop_heartbeat"
    assert health["live_loop_heartbeat_utc"] == heartbeat_at.isoformat()
    assert health["scheduler_heartbeat_utc"] is None
    assert health["execution_family"] == "alpaca_spot"
    assert readiness_calls == [("alpaca_spot", "ACTU")]


def test_event_loop_driver_conflict_fails_before_health_or_broker_probe(
    monkeypatch,
) -> None:
    _enable_event_driver(monkeypatch)
    monkeypatch.setattr(
        settings,
        "chili_momentum_live_runner_scheduler_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        aq,
        "build_momentum_operator_readiness",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("broker probe")),
    )
    monkeypatch.setattr(
        lh,
        "_latest_live_loop_heartbeat_status",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("heartbeat probe")
        ),
    )

    health = aq._runner_health_for_mode(
        MagicMock(),
        mode="live",
        sess=_live_session(),
    )

    assert (
        health["blocked_reason"]
        == "live_runner_batch_and_event_loop_both_enabled"
    )
    assert health["driver_mode"] is None
    assert health["driver_enabled"] is False
    assert health["scheduler_enabled"] is False


def test_event_loop_without_price_bus_is_not_an_operational_driver(
    monkeypatch,
) -> None:
    _enable_event_driver(monkeypatch)
    monkeypatch.setattr(
        settings,
        "chili_autopilot_price_bus_enabled",
        False,
        raising=False,
    )

    health = aq._runner_health_for_mode(
        MagicMock(),
        mode="live",
        sess=_live_session(),
    )

    assert (
        health["blocked_reason"]
        == "live_runner_event_loop_price_bus_disabled"
    )
    assert health["driver_mode"] is None
    assert health["driver_enabled"] is False
    assert health["scheduler_enabled"] is False


def test_event_loop_missing_and_stale_heartbeat_are_explicit(monkeypatch) -> None:
    _enable_event_driver(monkeypatch)
    monkeypatch.setattr(lh, "live_loop_stale_seconds", lambda: 60.0)
    monkeypatch.setattr(
        aq,
        "build_momentum_operator_readiness",
        lambda **_kwargs: {
            "broker_ready_for_live": True,
            "runnable_live_now": True,
        },
    )
    truth = {
        "ok": False,
        "reason": "live_runner_loop_heartbeat_missing",
    }
    monkeypatch.setattr(
        lh,
        "_latest_live_loop_heartbeat_status",
        lambda _db, *, stale_seconds: dict(truth),
    )

    missing = aq._runner_health_for_mode(
        MagicMock(),
        mode="live",
        sess=_live_session(),
    )
    assert missing["blocked_reason"] == "live_runner_loop_heartbeat_missing"

    truth.clear()
    truth.update(
        {
            "ok": True,
            "heartbeat_at": _utcnow_naive() - timedelta(seconds=61),
        }
    )
    stale = aq._runner_health_for_mode(
        MagicMock(),
        mode="live",
        sess=_live_session(),
    )
    assert stale["blocked_reason"] == "live_runner_loop_heartbeat_stale"
    assert stale["live_loop_stale_seconds"] == 60.0


def test_scheduled_live_health_uses_actual_non_coinbase_family(monkeypatch) -> None:
    monkeypatch.setattr(
        settings,
        "chili_momentum_live_runner_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "chili_momentum_live_runner_scheduler_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "chili_momentum_live_runner_loop_enabled",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        aq,
        "get_kill_switch_status",
        lambda: {"active": False},
    )
    monkeypatch.setattr(
        aq,
        "_latest_scheduler_heartbeat_at",
        lambda _db: _utcnow_naive(),
    )
    calls: list[tuple[str, str | None]] = []

    def _readiness(*, execution_family: str, symbol: str | None = None):
        calls.append((execution_family, symbol))
        return {
            "broker_ready_for_live": True,
            "runnable_live_now": True,
        }

    monkeypatch.setattr(aq, "build_momentum_operator_readiness", _readiness)

    health = aq._runner_health_for_mode(
        MagicMock(),
        mode="live",
        sess=_live_session(
            execution_family="robinhood_agentic_mcp",
            symbol="VEEE",
        ),
    )

    assert health["blocked_reason"] is None
    assert health["driver_mode"] == "scheduled_auto_arm"
    assert health["execution_family"] == "robinhood_agentic_mcp"
    assert calls == [("robinhood_agentic_mcp", "VEEE")]

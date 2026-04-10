"""Tests for trading debt batch: scheduler guard, alert tier propagation, public_api."""
from __future__ import annotations

import logging

import pytest


def test_run_scheduler_job_guarded_ok(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    from app.services.trading_scheduler import run_scheduler_job_guarded

    def _ok() -> None:
        pass

    run_scheduler_job_guarded("unit_test_ok", _ok)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("job_id=unit_test_ok" in m and "phase=start" in m for m in msgs)
    assert any("job_id=unit_test_ok" in m and "phase=ok" in m and "duration_ms=" in m for m in msgs)


def test_run_scheduler_job_guarded_swallows_and_logs_exception(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR)

    from app.services.trading_scheduler import run_scheduler_job_guarded

    def _boom() -> None:
        raise ValueError("expected failure")

    run_scheduler_job_guarded("unit_test_fail", _boom)
    assert any(
        r.levelno >= logging.ERROR and "job_id=unit_test_fail" in r.getMessage() and "phase=fail" in r.getMessage()
        for r in caplog.records
    )


def test_dispatch_alert_passes_classified_tier_to_send_sms(db, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_send(message: str, tier: str = "A") -> bool:
        captured["tier"] = tier
        captured["message"] = message
        return True

    monkeypatch.setattr("app.services.sms_service.send_sms", fake_send)
    monkeypatch.setattr("app.services.sms_service.is_configured", lambda: True)
    monkeypatch.setattr("app.routers.trading._broadcast_alert_sync", lambda _p: None)

    from app.services.trading.alerts import PATTERN_BREAKOUT_IMMINENT, dispatch_alert

    dispatch_alert(
        db,
        user_id=1,
        alert_type=PATTERN_BREAKOUT_IMMINENT,
        ticker="SPY",
        message="unit test alert",
        scan_pattern_id=99,
        confidence=0.0,
        skip_throttle=True,
    )
    assert captured.get("tier") == "B"


def test_trading_public_api_weekly_performance_review() -> None:
    from app.services.trading import public_api

    assert callable(public_api.weekly_performance_review)


def test_trading_public_api_prediction_surface() -> None:
    from app.services.trading import public_api

    assert callable(public_api.compute_prediction)
    assert callable(public_api.predict_direction)
    assert callable(public_api.predict_confidence)
    assert callable(public_api.get_current_predictions)
    assert callable(public_api.refresh_promoted_prediction_cache)


def test_dispatch_alert_passes_tier_a_for_target_hit(db, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_send(message: str, tier: str = "A") -> bool:
        captured["tier"] = tier
        captured["message"] = message
        return True

    monkeypatch.setattr("app.services.sms_service.send_sms", fake_send)
    monkeypatch.setattr("app.services.sms_service.is_configured", lambda: True)
    monkeypatch.setattr("app.routers.trading._broadcast_alert_sync", lambda _p: None)

    from app.services.trading.alerts import TARGET_HIT, dispatch_alert

    dispatch_alert(
        db,
        user_id=1,
        alert_type=TARGET_HIT,
        ticker="SPY",
        message="unit test target hit",
        skip_throttle=True,
    )
    assert captured.get("tier") == "A"


def test_send_sms_uses_telegram_and_skips_carrier_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    monkeypatch.setattr("app.services.sms_service.settings.alerts_enabled", True)
    monkeypatch.setattr("app.services.sms_service.settings.sms_phone", "5551234567")
    monkeypatch.setattr("app.services.sms_service._has_telegram", lambda: True)
    monkeypatch.setattr(
        "app.services.sms_service._should_send",
        lambda channel, tier: channel in {"telegram", "sms"},
    )
    monkeypatch.setattr(
        "app.services.sms_service._send_via_telegram",
        lambda message: calls.append("telegram") or True,
    )
    monkeypatch.setattr("app.services.sms_service._has_discord", lambda: False)
    monkeypatch.setattr("app.services.sms_service._has_twilio", lambda: True)
    monkeypatch.setattr(
        "app.services.sms_service._send_via_twilio",
        lambda message: calls.append("twilio") or True,
    )
    monkeypatch.setattr("app.services.sms_service._has_email_gateway", lambda: True)
    monkeypatch.setattr(
        "app.services.sms_service._send_via_email_gateway",
        lambda message: calls.append("email_gateway") or True,
    )

    from app.services.sms_service import send_sms

    assert send_sms("telegram only", tier="A") is True
    assert calls == ["telegram"]

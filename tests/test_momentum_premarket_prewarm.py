from __future__ import annotations

from datetime import datetime, timezone

from app.config import Settings, settings
from app.services.trading.momentum_neural.market_profile import is_data_session_now
from app.services import trading_scheduler


def test_equity_data_session_prewarms_from_0400_et(monkeypatch):
    """Selection/tape warmup starts at the exchange 04:00 ET data window."""
    monkeypatch.setattr(settings, "chili_momentum_premarket_start_et", "07:00", raising=False)
    monkeypatch.setattr(settings, "chili_momentum_early_premarket_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_selection_prep_lead_min", 60, raising=False)

    assert is_data_session_now("CANF", now=datetime(2026, 7, 1, 7, 59, tzinfo=timezone.utc)) is False
    assert is_data_session_now("CANF", now=datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc)) is True


def test_equity_data_session_can_start_30_min_before_0400_et(monkeypatch):
    """Operator can run CHILI from 03:30 ET so tape/universe is warm before 04:00 ET."""
    monkeypatch.setattr(settings, "chili_momentum_premarket_start_et", "04:00", raising=False)
    monkeypatch.setattr(settings, "chili_momentum_early_premarket_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_selection_prep_lead_min", 30, raising=False)

    assert is_data_session_now("CANF", now=datetime(2026, 7, 1, 7, 29, tzinfo=timezone.utc)) is False
    assert is_data_session_now("CANF", now=datetime(2026, 7, 1, 7, 30, tzinfo=timezone.utc)) is True


def test_default_selection_prep_lead_is_30_market_minutes():
    """Default prewarm lead is 03:30 ET for a 04:00 ET premarket-entry window."""
    assert Settings.model_fields["chili_momentum_selection_prep_lead_min"].default == 30


def test_momentum_prewarm_jobs_have_no_startup_delay(monkeypatch):
    """Selector/tape prewarm should run immediately at scheduler start, not 35-50s later."""
    monkeypatch.setattr(settings, "chili_momentum_tape_delta_min_seconds", 5.0, raising=False)

    assert trading_scheduler._momentum_event_startup_delay_seconds(settings) == 0.0

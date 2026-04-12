"""Phase 12: momentum-related Settings fields are declared (no getattr-only surprises)."""

from __future__ import annotations

from app.config import settings


def test_momentum_and_coinbase_flags_exist_on_settings() -> None:
    """Typed access used by momentum_neural and scheduler after closeout."""
    assert hasattr(settings, "chili_momentum_neural_enabled")
    assert hasattr(settings, "chili_momentum_neural_feedback_enabled")
    assert hasattr(settings, "chili_coinbase_spot_adapter_enabled")
    assert hasattr(settings, "chili_coinbase_ws_enabled")
    assert hasattr(settings, "chili_coinbase_strict_freshness")
    assert hasattr(settings, "chili_coinbase_market_data_max_age_sec")
    assert hasattr(settings, "chili_trading_automation_hud_enabled")
    assert hasattr(settings, "chili_momentum_paper_runner_enabled")
    assert hasattr(settings, "chili_momentum_paper_runner_scheduler_enabled")
    assert hasattr(settings, "chili_momentum_paper_runner_dev_tick_enabled")
    assert hasattr(settings, "chili_momentum_paper_runner_scheduler_interval_minutes")
    assert hasattr(settings, "chili_momentum_live_runner_enabled")
    assert hasattr(settings, "chili_momentum_live_runner_scheduler_enabled")
    assert hasattr(settings, "chili_momentum_live_runner_dev_tick_enabled")
    assert hasattr(settings, "chili_momentum_live_runner_scheduler_interval_minutes")
    assert hasattr(settings, "chili_momentum_risk_max_daily_loss_usd")


def test_scheduler_interval_defaults_conservative() -> None:
    assert int(settings.chili_momentum_paper_runner_scheduler_interval_minutes) >= 2
    assert int(settings.chili_momentum_live_runner_scheduler_interval_minutes) >= 2

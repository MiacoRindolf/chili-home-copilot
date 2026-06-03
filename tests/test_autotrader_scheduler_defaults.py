from app.config import (
    AUTOTRADER_SCHEDULER_TICK_INTERVAL_DEFAULT_SECONDS,
    Settings,
)


def test_autotrader_scheduler_defaults_avoid_skip_storm() -> None:
    assert AUTOTRADER_SCHEDULER_TICK_INTERVAL_DEFAULT_SECONDS == 60
    assert Settings.model_fields["chili_autotrader_tick_interval_seconds"].default == 60
    assert Settings.model_fields["chili_autotrader_monitor_interval_seconds"].default == 60

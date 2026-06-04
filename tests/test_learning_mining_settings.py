from app.config import Settings
from app.services.trading import learning


def test_mining_promotion_controls_read_operator_settings(monkeypatch) -> None:
    monkeypatch.setenv("BRAIN_MINING_MIN_SAMPLES", "37")
    monkeypatch.setenv("BRAIN_MINING_MIN_WIN_RATE", "0.64")
    monkeypatch.setenv("BRAIN_MINING_EMIT_SCAN_PATTERNS", "false")
    monkeypatch.setenv("BRAIN_MINING_USE_V2_PROMOTION", "false")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    controls = learning._mining_promotion_controls(settings)

    assert settings.brain_mining_min_samples == 37
    assert settings.brain_mining_min_win_rate == 0.64
    assert settings.brain_mining_emit_scan_patterns is False
    assert settings.brain_mining_use_v2_promotion is False
    assert controls.min_samples == 37
    assert controls.min_win_rate == 0.64
    assert controls.emit_scan_patterns is False
    assert controls.use_v2_promotion is False

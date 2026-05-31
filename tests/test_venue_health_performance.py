from __future__ import annotations

from app.services.trading.venue import venue_health


class _NoExecuteDb:
    def execute(self, *_args, **_kwargs):  # pragma: no cover - assertion guard
        raise AssertionError("disabled venue health should not query the DB")


def test_cached_settings_object_still_reads_live_threshold_attrs(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(venue_health, "_settings_obj", None)
    monkeypatch.setattr(settings, "chili_venue_health_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_venue_health_min_samples", 7, raising=False)

    assert venue_health._is_enabled() is True
    assert venue_health._resolve_thresholds()["min_samples"] == 7

    monkeypatch.setattr(settings, "chili_venue_health_enabled", False, raising=False)
    monkeypatch.setattr(settings, "chili_venue_health_min_samples", 11, raising=False)

    assert venue_health._is_enabled() is False
    assert venue_health._resolve_thresholds()["min_samples"] == 11


def test_summarize_disabled_reads_enabled_once(monkeypatch) -> None:
    calls = 0

    def _disabled() -> bool:
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setattr(
        venue_health,
        "_resolve_thresholds",
        lambda: {
            "window_sec": 300,
            "min_samples": 5,
            "ack_to_fill_p95_ms": 5000,
            "submit_to_ack_p95_ms": 3000,
            "error_rate": 0.10,
            "auto_switch_to_paper": False,
        },
    )
    monkeypatch.setattr(venue_health, "_is_enabled", _disabled)

    summary = venue_health.summarize_venue(_NoExecuteDb(), venue="coinbase")

    assert summary["status"] == "disabled"
    assert calls == 1

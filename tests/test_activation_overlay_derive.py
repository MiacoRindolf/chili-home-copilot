"""Pure tests for live-overlay hot/pulse derivation from activation waves."""

from __future__ import annotations

from app.services.trading.brain_neural_mesh.waves import (
    derive_overlay_hot_pulse_from_waves,
    group_activation_events_into_waves,
)


def test_derive_overlay_empty_waves() -> None:
    hot, pulse, lw = derive_overlay_hot_pulse_from_waves([], {})
    assert hot == []
    assert pulse == []
    assert lw is None


def test_derive_overlay_multi_hot_same_wave() -> None:
    events = [
        {
            "source_node_id": "a",
            "correlation_id": "c1",
            "created_at": "2026-01-01T12:00:02Z",
        },
        {
            "source_node_id": "b",
            "correlation_id": "c1",
            "created_at": "2026-01-01T12:00:02Z",
        },
    ]
    waves = group_activation_events_into_waves(events, time_window_sec=2.0)
    out = {"a": ["x", "y"], "b": ["z"]}
    hot, pulse, lw = derive_overlay_hot_pulse_from_waves(waves, out)
    assert set(hot) == {"a", "b"}
    assert lw is not None
    assert lw.get("correlation_id") == "c1"
    assert "a->x" in pulse
    assert "a->y" in pulse
    assert "b->z" in pulse


def test_derive_overlay_uses_newest_wave_only() -> None:
    events = [
        {"source_node_id": "old", "correlation_id": "w1", "created_at": "2026-01-01T12:00:00Z"},
        {"source_node_id": "new1", "correlation_id": "w2", "created_at": "2026-01-01T12:00:05Z"},
        {"source_node_id": "new2", "correlation_id": "w2", "created_at": "2026-01-01T12:00:05Z"},
    ]
    waves = group_activation_events_into_waves(events, time_window_sec=2.0)
    hot, _, lw = derive_overlay_hot_pulse_from_waves(waves, {})
    assert set(hot) == {"new1", "new2"}
    assert lw and lw.get("correlation_id") == "w2"

"""Pure tests for neural graph layout helpers."""

from __future__ import annotations

import math

import pytest

from app.services.trading.brain_neural_mesh.layout_neural_graph import (
    CORE_RING_RADIUS,
    CX,
    CY,
    MARGIN,
    OBSERVER_RADIAL_OUTSET,
    VIEWPORT_H,
    VIEWPORT_W,
    compute_neural_positions,
    truncate_neural_label,
)


def test_truncate_neural_label() -> None:
    assert truncate_neural_label("short", 20) == "short"
    assert truncate_neural_label("x" * 25, 20).endswith("\u2026")
    assert len(truncate_neural_label("abcdefghijklmnopqrs", 10)) <= 10


def test_layout_positions_within_viewport() -> None:
    nodes = [
        {"id": "a", "layer": 1, "is_observer": False},
        {"id": "b", "layer": 2, "is_observer": False},
        {"id": "c", "layer": 7, "is_observer": False},
        {"id": "nm_event_bus", "layer": 3, "is_observer": False},
        {"id": "nm_working_memory", "layer": 3, "is_observer": False},
        {"id": "nm_regime", "layer": 3, "is_observer": False},
        {"id": "nm_contradiction", "layer": 3, "is_observer": False},
        {"id": "obs1", "layer": 6, "is_observer": True},
        {"id": "core6", "layer": 6, "is_observer": False},
    ]
    pos, meta = compute_neural_positions(nodes)
    assert meta["bounds"]["min_x"] >= MARGIN * 0.5
    assert meta["bounds"]["max_x"] <= VIEWPORT_W - MARGIN * 0.5
    assert meta["bounds"]["min_y"] >= MARGIN * 0.5
    assert meta["bounds"]["max_y"] <= VIEWPORT_H - MARGIN * 0.5
    for _nid, (x, y) in pos.items():
        assert MARGIN * 0.2 <= x <= VIEWPORT_W - MARGIN * 0.2
        assert MARGIN * 0.2 <= y <= VIEWPORT_H - MARGIN * 0.2


def test_observer_farther_from_center_than_core_same_layer() -> None:
    nodes = [
        {"id": "core_a", "layer": 6, "is_observer": False},
        {"id": "core_b", "layer": 6, "is_observer": False},
        {"id": "obs_x", "layer": 6, "is_observer": True},
    ]
    pos, _meta = compute_neural_positions(nodes)
    dc = math.hypot(pos["core_a"][0] - CX, pos["core_a"][1] - CY)
    do = math.hypot(pos["obs_x"][0] - CX, pos["obs_x"][1] - CY)
    assert do > dc + OBSERVER_RADIAL_OUTSET * 0.5


def test_layout_deterministic() -> None:
    nodes = [
        {"id": "z", "layer": 1, "is_observer": False},
        {"id": "a", "layer": 1, "is_observer": False},
    ]
    p1, m1 = compute_neural_positions(nodes)
    p2, m2 = compute_neural_positions(nodes)
    assert p1 == p2
    assert m1["bounds"] == m2["bounds"]


@pytest.mark.usefixtures("db")
def test_projection_has_layout_meta(db) -> None:
    from app.models.trading import BrainGraphNode
    from app.services.trading.brain_neural_mesh.projection import build_neural_graph_projection

    if db.query(BrainGraphNode).count() == 0:
        pytest.skip("no mesh nodes")
    data = build_neural_graph_projection(db)
    assert data["meta"].get("layout_version") == 2
    assert "viewport" in data["meta"]
    assert data["meta"]["viewport"]["w"] == VIEWPORT_W
    assert "bounds" in data["meta"]
    b = data["meta"]["bounds"]
    assert b["min_x"] < b["max_x"]
    assert "ring_radii_draw" in data["meta"]
    assert "layer_ring_cues" in data["meta"]
    assert any(n.get("label_short") for n in data["nodes"])

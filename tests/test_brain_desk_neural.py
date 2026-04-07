"""Trading Brain desk: neural-first boot, graph config truth, activation waves."""

from __future__ import annotations

from unittest.mock import patch

from app.services.trading.brain_neural_mesh.schema import desk_graph_boot_config
from app.services.trading.brain_neural_mesh.waves import (
    edge_pulse_keys_for_sources,
    group_activation_events_into_waves,
)


def test_desk_graph_boot_config_legacy_when_mesh_off() -> None:
    with patch("app.services.trading.brain_neural_mesh.schema.mesh_enabled", return_value=False):
        with patch("app.services.trading.brain_neural_mesh.schema.effective_graph_mode", return_value="legacy"):
            d = desk_graph_boot_config()
    assert d["mesh_enabled"] is False
    assert d["effective_graph_mode"] == "legacy"
    assert d["desk_boot"] == "api"
    assert d["recommended_graph_url"] == "/api/trading/brain/graph"
    assert d["legacy_graph_url"] == "/api/brain/trading/network-graph"
    assert d["silent_legacy_fallback"] is True


def test_desk_graph_boot_config_neural_no_silent_legacy() -> None:
    with patch("app.services.trading.brain_neural_mesh.schema.mesh_enabled", return_value=True):
        with patch("app.services.trading.brain_neural_mesh.schema.effective_graph_mode", return_value="neural"):
            d = desk_graph_boot_config()
    assert d["mesh_enabled"] is True
    assert d["effective_graph_mode"] == "neural"
    assert d["silent_legacy_fallback"] is False


def test_waves_same_correlation_id_single_wave() -> None:
    evs = [
        {
            "id": 1,
            "source_node_id": "a",
            "correlation_id": "c1",
            "created_at": "2026-01-01T12:00:00",
        },
        {
            "id": 2,
            "source_node_id": "b",
            "correlation_id": "c1",
            "created_at": "2026-01-01T12:00:05",
        },
    ]
    waves = group_activation_events_into_waves(evs, time_window_sec=2.0)
    assert len(waves) == 1
    assert set(waves[0]["source_node_ids"]) == {"a", "b"}
    assert waves[0]["correlation_id"] == "c1"


def test_waves_time_window_groups_null_correlation() -> None:
    evs = [
        {"id": 1, "source_node_id": "x", "correlation_id": None, "created_at": "2026-01-01T12:00:00"},
        {"id": 2, "source_node_id": "y", "correlation_id": None, "created_at": "2026-01-01T12:00:01"},
        {"id": 3, "source_node_id": "z", "correlation_id": None, "created_at": "2026-01-01T12:00:10"},
    ]
    waves = group_activation_events_into_waves(evs, time_window_sec=2.0)
    assert len(waves) == 2
    newest = waves[0]
    assert set(newest["source_node_ids"]) == {"z"}
    older = waves[1]
    assert set(older["source_node_ids"]) == {"x", "y"}


def test_edge_pulse_keys_multiple_edges() -> None:
    out = {"a": ["b", "c"], "b": ["d"]}
    keys = edge_pulse_keys_for_sources(["a", "b"], out)
    assert set(keys) == {"a->b", "a->c", "b->d"}


def test_api_graph_config_truth_fields(paired_client) -> None:
    client, _user = paired_client
    r = client.get("/api/trading/brain/graph/config")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    for k in (
        "mesh_enabled",
        "effective_graph_mode",
        "desk_boot",
        "recommended_graph_url",
        "legacy_graph_url",
        "silent_legacy_fallback",
        "trading_brain_graph_mode_setting",
    ):
        assert k in d
    assert d["desk_boot"] == "api"


def test_brain_page_api_boot_not_inline_legacy_blob(paired_client) -> None:
    client, _user = paired_client
    r = client.get("/brain")
    assert r.status_code == 200
    text = r.text
    assert "trading-brain-network-data" not in text
    assert "__CHILI_TBN_DESK__" in text


def test_activations_includes_waves(paired_client) -> None:
    client, _user = paired_client
    r = client.get("/api/trading/brain/graph/activations?limit=5")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert "waves" in d
    assert isinstance(d["waves"], list)


def test_legacy_network_graph_api_still_ok(paired_client) -> None:
    client, _user = paired_client
    r = client.get("/api/brain/trading/network-graph")
    assert r.status_code == 200
    d = r.json()
    assert d.get("ok") is True
    assert d.get("nodes")
    root_ids = [n["id"] for n in d["nodes"] if n.get("tier") == "root"]
    assert root_ids

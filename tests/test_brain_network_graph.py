"""Structural sanity for the Trading Brain Network graph JSON (no DB)."""

from __future__ import annotations

from app.services.trading.brain_network_graph import get_trading_brain_network_graph


def test_trading_brain_network_graph_structure() -> None:
    data = get_trading_brain_network_graph()
    assert data.get("ok") is True
    nodes = data.get("nodes") or []
    edges = data.get("edges") or []
    assert len(nodes) > 0
    assert len(edges) > 0
    ids = {n["id"] for n in nodes if isinstance(n, dict) and "id" in n}
    for e in edges:
        assert e.get("from") in ids, f"missing from-node: {e!r}"
        assert e.get("to") in ids, f"missing to-node: {e!r}"
    meta = data.get("meta") or {}
    assert int(meta.get("graph_version", 0)) >= 9
    assert meta.get("architecture_source") == "learning_cycle_architecture"
    for n in nodes:
        if not isinstance(n, dict):
            continue
        assert "description" in n
        assert str(n.get("description", "")).strip()
        assert "remarks" in n
        assert str(n.get("remarks", "")).strip()
        assert isinstance(n.get("inputs"), list)
        assert isinstance(n.get("outputs"), list)
        for item in n["inputs"] + n["outputs"]:
            assert isinstance(item, str)
        assert "code_snippet" in n
        assert isinstance(n.get("code_snippet"), str)
        assert str(n.get("code_snippet", "")).strip()

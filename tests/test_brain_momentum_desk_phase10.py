"""Phase 10: neural brain desk momentum visibility (read-model only, not learning-cycle)."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.models.trading import BrainGraphNode
from app.services.trading.brain_neural_mesh.projection import (
    NEURAL_PROJECTION_SCHEMA_VERSION,
    build_neural_graph_projection,
    build_node_detail,
)
from app.services.trading.momentum_neural.brain_desk_summary import (
    build_momentum_neural_graph_context,
    get_momentum_brain_desk_payload,
)
from app.services.trading.momentum_neural.evolution import EVOLUTION_NODE_ID
from app.services.trading.momentum_neural.pipeline import HUB_NODE_ID, VIABILITY_NODE_ID


@pytest.mark.usefixtures("db")
def test_neural_projection_includes_momentum_desk_meta_and_node_previews(db: Session) -> None:
    if db.query(BrainGraphNode).count() == 0:
        pytest.skip("no mesh nodes")
    data = build_neural_graph_projection(db)
    assert data["ok"] is True
    assert data["meta"]["view"] == "neural"
    assert data["meta"].get("projection_schema_version") == NEURAL_PROJECTION_SCHEMA_VERSION
    md = data["meta"].get("momentum_desk")
    assert isinstance(md, dict)
    assert "headline" in md
    assert "badges" in md
    pv = md.get("paper_vs_live_30d")
    assert isinstance(pv, dict)
    assert "paper" in pv and "live" in pv
    assert "n" in pv["paper"] and "mean_return_bps" in pv["paper"]
    assert "n" in pv["live"] and "mean_return_bps" in pv["live"]
    assert "live_sample_caution" in pv

    by_id = {n["id"]: n for n in data["nodes"]}
    for nid in (HUB_NODE_ID, VIABILITY_NODE_ID, EVOLUTION_NODE_ID):
        assert nid in by_id, f"expected momentum neural node {nid} in graph"
        mdn = by_id[nid].get("momentum_desk")
        assert isinstance(mdn, dict), f"node {nid} should expose momentum_desk preview"
        assert mdn.get("subtitle")
        assert mdn.get("role")


@pytest.mark.usefixtures("db")
def test_build_node_detail_momentum_desk_card_not_learning_cycle(db: Session) -> None:
    hub = db.query(BrainGraphNode).filter(BrainGraphNode.id == HUB_NODE_ID).one_or_none()
    if hub is None:
        pytest.skip("momentum hub node not seeded")
    detail = build_node_detail(db, HUB_NODE_ID)
    assert detail is not None
    card = detail.get("momentum_desk_card")
    assert isinstance(card, dict)
    assert card.get("role") == "momentum_crypto_intel"
    assert "subtitle" in card


@pytest.mark.usefixtures("db")
def test_momentum_brain_desk_payload_paper_live_separation(db: Session) -> None:
    p = get_momentum_brain_desk_payload(db)
    assert p["ok"] is True
    panel = p.get("momentum_panel") or {}
    pv = panel.get("paper_vs_live_30d") or {}
    assert pv.get("paper", {}).get("n", -1) >= 0
    assert pv.get("live", {}).get("n", -1) >= 0
    assert "live_sample_caution" in pv
    assert "badges" in p
    assert "outcomes_window" in p
    ow = p["outcomes_window"]
    assert "paper" in ow and "live" in ow
    if ow.get("table_present"):
        assert "mix_top" in ow


@pytest.mark.usefixtures("db")
def test_build_momentum_graph_context_empty_graceful(db: Session) -> None:
    ctx = build_momentum_neural_graph_context(db)
    assert ctx.get("version", 0) >= 1
    nodes = ctx.get("nodes") or {}
    assert HUB_NODE_ID in nodes
    assert isinstance(nodes[HUB_NODE_ID], dict)
    assert nodes[HUB_NODE_ID].get("role") == "momentum_crypto_intel"


def test_phase10_read_model_files_do_not_import_learning_service() -> None:
    """Guardrail: brain desk / projection must not import learning.py."""
    from pathlib import Path

    import app.services.trading.brain_neural_mesh.projection as proj
    import app.services.trading.momentum_neural.brain_desk_summary as bds

    for path in (Path(bds.__file__), Path(proj.__file__)):
        text = path.read_text(encoding="utf-8")
        assert "from ..learning import" not in text
        assert "from app.services.trading.learning" not in text
        assert "import app.services.trading.learning" not in text

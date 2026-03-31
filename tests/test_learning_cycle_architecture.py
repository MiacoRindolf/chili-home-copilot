"""Canonical learning-cycle spec: uniqueness, graph sync, no drift in run_learning_cycle."""

from __future__ import annotations

import re
from pathlib import Path

from app.services.trading.brain_network_graph import get_trading_brain_network_graph
from app.services.trading.learning_cycle_architecture import (
    TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS,
    get_cycle_step,
)


def test_cycle_cluster_and_step_ids_unique() -> None:
    seen: set[tuple[str, str]] = set()
    for c in TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS:
        for s in c.steps:
            key = (c.id, s.sid)
            assert key not in seen, f"duplicate step key: {key}"
            seen.add(key)
        assert get_cycle_step(c.id, c.steps[0].sid).label


def test_graph_node_count_matches_architecture() -> None:
    data = get_trading_brain_network_graph()
    clusters = TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS
    n_steps = sum(len(c.steps) for c in clusters)
    expected_nodes = 1 + len(clusters) + n_steps
    assert len(data["nodes"]) == expected_nodes


def test_run_learning_cycle_no_literal_current_step_assignments() -> None:
    """Forbid `_learning_status["current_step"] = "..."` in learning.py (use architecture helpers)."""
    path = Path(__file__).resolve().parents[1] / "app" / "services" / "trading" / "learning.py"
    text = path.read_text(encoding="utf-8")
    # Allow only clearing to empty string in finally
    bad = re.findall(
        r'_learning_status\s*\[\s*["\']current_step["\']\s*\]\s*=\s*("[^"]*"|\'[^\']*\')',
        text,
    )
    allowed_empty = {'""', "''"}
    suspicious = [b for b in bad if b not in allowed_empty]
    assert not suspicious, (
        "Use apply_learning_cycle_step_status / _progress from learning_cycle_architecture; "
        f"found literal assignments: {suspicious}"
    )

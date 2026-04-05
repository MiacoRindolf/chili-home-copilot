"""Canonical learning-cycle spec: uniqueness, graph sync, no drift in run_learning_cycle."""

from __future__ import annotations

import re
from pathlib import Path

from app.services.trading.brain_network_graph import get_trading_brain_network_graph
from app.services.trading.learning_cycle_architecture import (
    TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS,
    TRADING_BRAIN_ROOT_METADATA,
    apply_learning_cycle_step_status,
    apply_learning_cycle_step_status_progress,
    get_cycle_step,
)


def _assert_io_tuple(t: tuple[str, ...]) -> None:
    assert isinstance(t, tuple)
    for x in t:
        assert isinstance(x, str)


def test_cycle_cluster_and_step_ids_unique() -> None:
    seen: set[tuple[str, str]] = set()
    assert TRADING_BRAIN_ROOT_METADATA.description.strip()
    assert TRADING_BRAIN_ROOT_METADATA.remarks.strip()
    _assert_io_tuple(TRADING_BRAIN_ROOT_METADATA.inputs)
    _assert_io_tuple(TRADING_BRAIN_ROOT_METADATA.outputs)
    for c in TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS:
        assert c.description.strip()
        assert c.remarks.strip()
        _assert_io_tuple(c.inputs)
        _assert_io_tuple(c.outputs)
        for s in c.steps:
            key = (c.id, s.sid)
            assert key not in seen, f"duplicate step key: {key}"
            seen.add(key)
            assert s.description.strip()
            assert s.remarks.strip()
            _assert_io_tuple(s.inputs)
            _assert_io_tuple(s.outputs)
        assert get_cycle_step(c.id, c.steps[0].sid).label


def test_graph_node_count_matches_architecture() -> None:
    data = get_trading_brain_network_graph()
    clusters = TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS
    n_steps = sum(len(c.steps) for c in clusters)
    expected_nodes = 1 + len(clusters) + n_steps
    assert len(data["nodes"]) == expected_nodes


def test_snapshot_learning_for_brain_worker_status_file_has_stable_keys() -> None:
    from app.services.trading.learning import (
        _BRAIN_WORKER_STATUS_LEARNING_KEYS,
        snapshot_learning_for_brain_worker_status_file,
    )

    snap = snapshot_learning_for_brain_worker_status_file()
    assert set(snap.keys()) == set(_BRAIN_WORKER_STATUS_LEARNING_KEYS)


def test_apply_learning_cycle_step_status_sets_graph_node_fields() -> None:
    st: dict = {}
    apply_learning_cycle_step_status(st, "c_discovery", "mine")
    assert st["graph_node_id"] == "s_c_discovery_mine"
    assert st["current_cluster_id"] == "c_discovery"
    assert st["current_step_sid"] == "mine"
    ci = next(i for i, c in enumerate(TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS) if c.id == "c_discovery")
    assert st["current_cluster_index"] == ci
    mine_i = next(
        i for i, s in enumerate(TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS[ci].steps) if s.sid == "mine"
    )
    assert st["current_step_index"] == mine_i

    st2: dict = {}
    apply_learning_cycle_step_status_progress(st2, "c_state", "snapshots_daily", 3, 100)
    assert st2["graph_node_id"] == "s_c_state_snapshots_daily"
    assert st2["current_cluster_id"] == "c_state"
    assert st2["current_step_sid"] == "snapshots_daily"
    assert st2["current_step"] == "Taking daily market snapshots (3/100)"


def test_apply_learning_cycle_step_status_preceded_by_graph_node_comment() -> None:
    """Each apply_learning_cycle_step_status in learning.py must be preceded by # graph-node: cid/sid."""
    path = Path(__file__).resolve().parents[1] / "app" / "services" / "trading" / "learning.py"
    lines = path.read_text(encoding="utf-8").splitlines()
    apply_re = re.compile(
        r"apply_learning_cycle_step_status\s*\(\s*_learning_status\s*,\s*\"([^\"]+)\"\s*,\s*\"([^\"]+)\"\s*\)"
    )
    graph_re = re.compile(r"^\s*#\s*graph-node:\s*([\w_]+)/([\w_]+)")
    for i, line in enumerate(lines):
        m = apply_re.search(line)
        if not m:
            continue
        prev = lines[i - 1] if i > 0 else ""
        gm = graph_re.match(prev)
        assert gm is not None, f"line {i + 1}: expected # graph-node: cluster/step above apply call"
        assert gm.group(1) == m.group(1) and gm.group(2) == m.group(2), (
            f"line {i + 1}: graph-node {gm.group(1)}/{gm.group(2)} does not match "
            f"apply_learning_cycle_step_status({m.group(1)}, {m.group(2)})"
        )


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

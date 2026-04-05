"""Learning live snapshot in PostgreSQL for cross-process Brain UI (Network graph)."""

from __future__ import annotations

import copy

from app.services import brain_worker_signals as bws
from app.services.trading import learning as learning_mod


def test_get_learning_status_merges_learning_live_from_db(db) -> None:
    """When in-memory status is idle, DB row should still supply graph_node_id / running."""
    saved = copy.deepcopy(learning_mod._learning_status)
    try:
        payload = {
            "running": True,
            "phase": "mining",
            "current_step": "Mining patterns",
            "graph_node_id": "s_c_discovery_mine",
            "current_cluster_id": "c_discovery",
            "current_step_sid": "mine",
            "current_cluster_index": 2,
            "current_step_index": 0,
            "steps_completed": 5,
            "total_steps": 24,
            "started_at": "2020-01-01T00:00:00",
            "elapsed_s": 1.0,
        }
        bws.persist_learning_live_json(db, payload)

        learning_mod._learning_status["running"] = False
        learning_mod._learning_status["graph_node_id"] = ""
        learning_mod._learning_status["current_cluster_id"] = ""
        learning_mod._learning_status["current_step_sid"] = ""
        learning_mod._learning_status["current_cluster_index"] = -1
        learning_mod._learning_status["current_step_index"] = -1

        st = learning_mod.get_learning_status()
        assert st["graph_node_id"] == "s_c_discovery_mine"
        assert st["running"] is True
        assert st["current_cluster_id"] == "c_discovery"
    finally:
        learning_mod._learning_status.clear()
        learning_mod._learning_status.update(saved)

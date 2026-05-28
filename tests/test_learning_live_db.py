"""Learning live snapshot in PostgreSQL for cross-process Brain UI (Network graph)."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

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
            "graph_node_id": "nm_lc_mine",
            "current_cluster_id": "c_discovery",
            "current_step_sid": "mine",
            "current_cluster_index": 2,
            "current_step_index": 0,
            "nodes_completed": 5,
            "total_nodes": 28,
            "clusters_completed": 1,
            "total_clusters": 11,
            "started_at": datetime.now(timezone.utc).isoformat(),
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
        assert st["graph_node_id"] == "nm_lc_mine"
        assert st["running"] is True
        assert st["current_cluster_id"] == "c_discovery"
    finally:
        learning_mod._learning_status.clear()
        learning_mod._learning_status.update(saved)


def test_get_learning_status_ignores_stale_learning_live_from_db(db) -> None:
    """A crashed worker must not make the Brain UI/gates think learning is still live."""
    saved = copy.deepcopy(learning_mod._learning_status)
    try:
        payload = {
            "running": True,
            "phase": "mining",
            "current_step": "Mining patterns",
            "graph_node_id": "nm_lc_mine",
            "current_cluster_id": "c_discovery",
            "current_step_sid": "mine",
            "current_cluster_index": 2,
            "current_step_index": 0,
            "nodes_completed": 5,
            "total_nodes": 28,
            "clusters_completed": 1,
            "total_clusters": 11,
            "started_at": (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat(),
            "elapsed_s": 14400.0,
        }
        bws.persist_learning_live_json(db, payload)

        learning_mod._learning_status["running"] = False
        learning_mod._learning_status["phase"] = "idle"
        learning_mod._learning_status["graph_node_id"] = ""
        learning_mod._learning_status["current_cluster_id"] = ""
        learning_mod._learning_status["current_step_sid"] = ""
        learning_mod._learning_status["current_cluster_index"] = -1
        learning_mod._learning_status["current_step_index"] = -1

        st = learning_mod.get_learning_status()
        assert st["running"] is False
        assert st["phase"] == "idle"
        assert st["graph_node_id"] == ""
        assert st["learning_live_stale"] is True
        assert st["learning_live_stale_reason"].startswith("stale_learning_live_age_s=")
    finally:
        learning_mod._learning_status.clear()
        learning_mod._learning_status.update(saved)

"""Regression: conftest self-heals a wiped/degraded brain-graph seed at bootstrap.

The neural-mesh seed (``brain_graph_nodes`` / ``brain_graph_edges``, migration 086 +
successors) is excluded from per-test truncation, but an external collision (a second
pytest / replay process sharing the same ``*_test`` sink) can wipe the seed rows while
``schema_version`` still records the seed migrations. In that state any still-pending
graph migration fails at ``db``-fixture bootstrap with ForeignKeyViolation on
``brain_graph_edges.target_node_id`` (nm_action_signals) and every DB test in the
session ERRORs (observed 2026-07-31 as 18 passed + 5 errors on
``test_pullback_scalp_enable.py``). ``_repair_wiped_neural_mesh_seed`` detects the
state, forgets every graph-touching migration, and lets ``run_migrations`` replay
them to rebuild the canonical topology.
"""
from __future__ import annotations

import sys

from sqlalchemy import text

from app.db import engine
from app.migrations import run_migrations


def _conftest():
    return sys.modules["tests.conftest"]


def _static_ids_present(db) -> int:
    return int(
        db.execute(
            text("SELECT COUNT(*) FROM brain_graph_nodes WHERE id = ANY(:ids)"),
            {"ids": _conftest()._expected_static_mesh_node_ids()},
        ).scalar()
        or 0
    )


def test_expected_static_ids_all_exist_after_bootstrap(db):
    """Canary: every id in the detection set exists in a canonically migrated DB.

    If a future migration renames/removes a static node without updating
    ``seed_graph.py`` (or the exclusion list in
    ``_expected_static_mesh_node_ids``), the repair would re-fire every
    bootstrap. This test makes that drift loud instead of silent.
    """
    expected = _conftest()._expected_static_mesh_node_ids()
    assert _static_ids_present(db) == len(expected)


def test_repair_noop_on_healthy_graph(db):
    """A healthy seed must not be touched: no schema_version rows forgotten."""
    before_versions = int(db.execute(text("SELECT COUNT(*) FROM schema_version")).scalar())
    before_nodes = int(db.execute(text("SELECT COUNT(*) FROM brain_graph_nodes")).scalar())
    db.commit()

    _conftest()._repair_wiped_neural_mesh_seed()

    after_versions = int(db.execute(text("SELECT COUNT(*) FROM schema_version")).scalar())
    after_nodes = int(db.execute(text("SELECT COUNT(*) FROM brain_graph_nodes")).scalar())
    assert after_versions == before_versions
    assert after_nodes == before_nodes


def test_repair_rebuilds_wiped_seed_and_replays_pending_graph_migration(db):
    """Poison the sink exactly like the observed collision, then heal it.

    Wipe the graph while ``schema_version`` keeps the seed record, and leave one
    edge-inserting graph migration (125_mesh_reactive_sensors: sensors →
    nm_action_signals) pending. Without the repair, ``run_migrations`` raises
    ForeignKeyViolation here; with it, the mesh is rebuilt canonically.
    """
    db.commit()  # release the fixture session's snapshot before DDL below
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE brain_graph_nodes CASCADE"))
        conn.execute(
            text(
                "DELETE FROM schema_version "
                "WHERE version_id = '125_mesh_reactive_sensors'"
            )
        )

    _conftest()._repair_wiped_neural_mesh_seed()
    run_migrations(engine)

    expected = _conftest()._expected_static_mesh_node_ids()
    with engine.connect() as conn:
        present = int(
            conn.execute(
                text("SELECT COUNT(*) FROM brain_graph_nodes WHERE id = ANY(:ids)"),
                {"ids": expected},
            ).scalar()
            or 0
        )
        assert present == len(expected)
        # The exact FK pair that crashed the fixture is back: the pending
        # migration's sensor edge into nm_action_signals.
        sensor_edge = conn.execute(
            text(
                "SELECT COUNT(*) FROM brain_graph_edges "
                "WHERE source_node_id = 'nm_stop_eval' "
                "AND target_node_id = 'nm_action_signals'"
            )
        ).scalar()
        assert int(sensor_edge or 0) >= 1
        # The real-world partial-degradation ids (observed missing in a warm
        # chili_test on 2026-08-01) are restored too.
        degraded_pair = conn.execute(
            text(
                "SELECT COUNT(*) FROM brain_graph_nodes "
                "WHERE id IN ('nm_imminent_eval', 'nm_trade_context')"
            )
        ).scalar()
        assert int(degraded_pair or 0) == 2
        # And the migration ledger is whole again.
        replayed = conn.execute(
            text(
                "SELECT COUNT(*) FROM schema_version "
                "WHERE version_id IN ('086_trading_brain_neural_mesh', "
                "'125_mesh_reactive_sensors')"
            )
        ).scalar()
        assert int(replayed or 0) == 2

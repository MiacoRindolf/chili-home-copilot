"""Background runner UI copy: mesh-native progress."""

from __future__ import annotations

from app.services.trading.brain_runner_display import (
    runner_active_full_plain,
    runner_active_secondary_details,
    runner_clusters_suffix,
    runner_idle_caption,
    runner_phase_primary,
)


def test_runner_phase_primary_shows_nodes() -> None:
    s = runner_phase_primary(4, 24)
    assert s == "Nodes 4/24"


def test_runner_clusters_suffix() -> None:
    assert runner_clusters_suffix(2, 10) == "Clusters 2/10"


def test_runner_idle_caption() -> None:
    s = runner_idle_caption()
    assert "idle" in s.lower() or "waiting" in s.lower()


def test_runner_active_full_plain_mesh_progress() -> None:
    s = runner_active_full_plain(4, 24, clusters_completed=1, total_clusters=10)
    assert "Nodes 4/24" in s
    assert "Clusters 1/10" in s


def test_runner_active_full_plain_with_extras() -> None:
    s = runner_active_full_plain(4, 24, clusters_completed=1, total_clusters=10, tickers_processed=30, elapsed_s=12.4)
    assert "Nodes 4/24" in s
    assert "Clusters 1/10" in s
    assert "30 scored" in s
    assert "12s elapsed" in s


def test_runner_active_secondary_details_only_tail() -> None:
    assert runner_active_secondary_details() == ""
    assert "scored" in runner_active_secondary_details(tickers_processed=1)
    assert "s elapsed" in runner_active_secondary_details(elapsed_s=3.2)

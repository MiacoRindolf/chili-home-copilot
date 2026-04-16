"""Pure strings for Trading Brain background runner UI (mesh-native progress)."""

from __future__ import annotations


def runner_idle_caption() -> str:
    """Shown when no learning cycle is actively running."""
    return "Runner idle · waiting for the next scheduled cycle"


def runner_phase_primary(nodes_completed: int, total_nodes: int) -> str:
    return f"Nodes {int(nodes_completed)}/{int(total_nodes)}"


def runner_clusters_suffix(clusters_completed: int, total_clusters: int) -> str:
    return f"Clusters {int(clusters_completed)}/{int(total_clusters)}"


def runner_active_secondary_details(
    *,
    tickers_processed: int = 0,
    elapsed_s: float | None = None,
) -> str:
    """Trailing details after the phase line (plain text, joined with middle dot)."""
    parts: list[str] = []
    if tickers_processed > 0:
        parts.append(f"{int(tickers_processed)} scored")
    if elapsed_s is not None and elapsed_s >= 0:
        parts.append(f"{int(round(float(elapsed_s)))}s elapsed")
    return " · ".join(parts)


def runner_active_full_plain(
    nodes_completed: int,
    total_nodes: int,
    *,
    clusters_completed: int = 0,
    total_clusters: int = 0,
    tickers_processed: int = 0,
    elapsed_s: float | None = None,
) -> str:
    """Single-line plain fallback / screen readers (no HTML)."""
    core = f"{runner_phase_primary(nodes_completed, total_nodes)} ({runner_clusters_suffix(clusters_completed, total_clusters)})"
    tail = runner_active_secondary_details(tickers_processed=tickers_processed, elapsed_s=elapsed_s)
    return core + (" · " + tail if tail else "")

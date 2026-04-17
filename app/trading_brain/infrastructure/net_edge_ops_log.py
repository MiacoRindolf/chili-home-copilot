"""Phase E: bounded one-line ops log for the NetEdgeRanker rollout.

Mirrors the shape of ``prediction_ops_log.py`` so the same grep/soak discipline
applies. A single INFO line per decision, fixed field order, fixed enums, no
ticker lists or blobs inline.

Release blocker (mirrors prediction-mirror contract):
    Any line with ``mode=authoritative`` while ``brain_net_edge_ranker_mode``
    is not ``authoritative`` is a deploy blocker.
"""

from __future__ import annotations

CHILI_NET_EDGE_OPS_PREFIX = "[net_edge_ops]"

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_COMPARE = "compare"
MODE_AUTHORITATIVE = "authoritative"

READ_NA = "na"
READ_SHADOW = "shadow"
READ_COMPARE_OK = "compare_ok"
READ_COMPARE_DISAGREE = "compare_disagree"
READ_AUTHORITATIVE = "authoritative"
READ_COLD_START = "cold_start"
READ_ERROR = "error"


def format_net_edge_ops_line(
    *,
    mode: str,
    read: str,
    decision_id: str,
    pattern_id: int | None,
    asset_class: str | None,
    regime: str | None,
    net_edge: float | None,
    heuristic_score: float | None,
    disagree: bool,
    sample_pct: float,
) -> str:
    """Return a single bounded INFO line; no ticker lists or blobs."""
    pid = "none" if pattern_id is None else str(int(pattern_id))
    ac = asset_class or "none"
    rg = regime or "none"
    ne = "none" if net_edge is None else f"{float(net_edge):.6f}"
    hs = "none" if heuristic_score is None else f"{float(heuristic_score):.6f}"
    dg = "true" if bool(disagree) else "false"
    sp = f"{float(sample_pct):.3f}"
    did = (decision_id or "none")[:24]
    return (
        f"{CHILI_NET_EDGE_OPS_PREFIX} mode={mode} read={read} "
        f"decision_id={did} pattern_id={pid} asset_class={ac} regime={rg} "
        f"net_edge={ne} heuristic_score={hs} disagree={dg} sample_pct={sp}"
    )

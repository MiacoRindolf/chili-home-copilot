"""Phase B: bounded one-line ops log for the ExitEngine unification rollout.

Mirrors the shape of ``net_edge_ops_log.py`` and ``prediction_ops_log.py`` so
the same grep/soak discipline applies. A single INFO line per per-bar parity
check, fixed field order, fixed enums, no ticker lists or blobs inline.

Release blocker (mirrors prediction-mirror + NetEdgeRanker contract):
    Any line with ``mode=authoritative`` while ``brain_exit_engine_mode``
    is not ``authoritative`` is a deploy blocker.

The canonical evaluator is shadow-only until a later cutover phase. An
``authoritative`` line in logs from the current phase implies a leak.
"""

from __future__ import annotations

CHILI_EXIT_ENGINE_OPS_PREFIX = "[exit_engine_ops]"

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_COMPARE = "compare"
MODE_AUTHORITATIVE = "authoritative"

SOURCE_BACKTEST = "backtest"
SOURCE_LIVE = "live"

ACTION_HOLD = "hold"
ACTION_EXIT_STOP = "exit_stop"
ACTION_EXIT_TARGET = "exit_target"
ACTION_EXIT_TRAIL = "exit_trail"
ACTION_EXIT_BOS = "exit_bos"
ACTION_EXIT_TIME_DECAY = "exit_time_decay"
ACTION_PARTIAL = "partial"


def format_exit_engine_ops_line(
    *,
    mode: str,
    source: str,
    position_id: int | None,
    ticker: str,
    legacy_action: str,
    canonical_action: str,
    agree: bool,
    config_hash: str | None,
    sample_pct: float,
) -> str:
    """Return a single bounded INFO line; no raw price blobs, no provenance dumps."""
    pid = "none" if position_id is None else str(int(position_id))
    ch = (config_hash or "none")[:16]
    tk = (ticker or "none")[:24]
    la = (legacy_action or "none")[:20]
    ca = (canonical_action or "none")[:20]
    ag = "true" if bool(agree) else "false"
    sp = f"{float(sample_pct):.3f}"
    return (
        f"{CHILI_EXIT_ENGINE_OPS_PREFIX} mode={mode} source={source} "
        f"position_id={pid} ticker={tk} "
        f"legacy_action={la} canonical_action={ca} agree={ag} "
        f"config_hash={ch} sample_pct={sp}"
    )

"""Phase C: bounded one-line ops log for the PIT hygiene audit shadow rollout.

Mirrors ``ledger_ops_log.py`` / ``net_edge_ops_log.py`` so the same grep/soak
discipline applies. A single INFO line per pattern audit, fixed field order,
fixed enums, no raw provenance.

Release blocker (mirrors prediction-mirror + NetEdgeRanker + ExitEngine +
Ledger contract): any line with ``mode=authoritative`` while
``brain_pit_audit_mode`` is not ``authoritative`` is a deploy blocker.

During Phase C the auditor is shadow-only; any ``authoritative`` line in logs
implies a cutover leak into a non-authoritative deploy.
"""

from __future__ import annotations

CHILI_PIT_OPS_PREFIX = "[pit_ops]"

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_COMPARE = "compare"
MODE_AUTHORITATIVE = "authoritative"


def _sanitize_name(name: str | None, max_len: int = 60) -> str:
    if not name:
        return "none"
    s = str(name).replace('"', "'").replace("\n", " ").replace("\r", " ")
    return s[:max_len]


def format_pit_ops_line(
    *,
    mode: str,
    pattern_id: int | None,
    name: str | None,
    lifecycle: str | None,
    pit_count: int,
    non_pit_count: int,
    unknown_count: int,
    agree: bool,
) -> str:
    """Return a single bounded INFO line; no raw field lists (use log.debug for those)."""
    pid = "none" if pattern_id is None else str(int(pattern_id))
    lc = (lifecycle or "none")[:20]
    return (
        f"{CHILI_PIT_OPS_PREFIX} mode={mode} pattern_id={pid} "
        f'name="{_sanitize_name(name)}" lifecycle={lc} '
        f"pit={int(pit_count)} non_pit={int(non_pit_count)} "
        f"unknown={int(unknown_count)} "
        f"agree={'true' if agree else 'false'}"
    )

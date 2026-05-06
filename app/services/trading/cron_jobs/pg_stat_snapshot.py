"""5-minute cron: snapshot pg_stat_activity for forensics.

Per f-leak-4 Phase 2 deferred Open Q: when the next leak surfaces,
the operator should have a forensic trail of which sessions held
which queries for how long. This cron writes one snapshot file per
tick to ``scripts/_pg_stat_log/<UTC iso>.txt``, keyed on the chili-*
application_name filter.

Author: 2026-05-06 (f-add-pg-stat-snapshot-logger).
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Snapshots land in scripts/_pg_stat_log/ at the repo root. Each tick
# writes one file <ISO timestamp>.txt with the top-30 chili sessions.
# A separate retention policy (or operator cleanup) handles disk usage.
_SNAPSHOT_DIR = Path(__file__).resolve().parents[4] / "scripts" / "_pg_stat_log"


def run_pg_stat_snapshot(db: "Session") -> dict[str, Any]:
    """Write one ``pg_stat_activity`` snapshot to disk.

    Returns ``{"path": <str>, "rows_captured": <int>}`` for log
    visibility. Failures swallowed at the cron-job boundary -- a
    snapshot miss is observability loss, not a brain-correctness issue.
    """
    from sqlalchemy import text

    rows = db.execute(text("""
        SELECT
            pid,
            COALESCE(application_name, '') AS app,
            COALESCE(state, '') AS state,
            COALESCE(wait_event_type, '') AS wait_event_type,
            COALESCE(wait_event, '') AS wait_event,
            EXTRACT(EPOCH FROM (NOW() - state_change))::int AS held_s,
            LEFT(COALESCE(query, ''), 200) AS q
        FROM pg_stat_activity
        WHERE application_name LIKE 'chili%'
          AND state IS NOT NULL
        ORDER BY held_s DESC NULLS LAST
        LIMIT 30
    """)).fetchall()

    _SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    iso = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    path = _SNAPSHOT_DIR / f"{iso}.txt"
    with path.open("w", encoding="utf-8") as f:
        f.write(f"# pg_stat_activity snapshot {iso}\n")
        f.write(f"# rows={len(rows)} (chili-* apps with non-null state, top 30 by held_s)\n")
        f.write("---\n")
        for r in rows:
            f.write(
                f"pid={r.pid} app={r.app!r} state={r.state!r} "
                f"wait={r.wait_event_type}/{r.wait_event} "
                f"held_s={r.held_s} query={r.q!r}\n"
            )
    return {"path": str(path), "rows_captured": len(rows)}

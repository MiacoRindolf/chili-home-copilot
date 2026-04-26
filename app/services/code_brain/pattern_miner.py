"""Code Brain pattern miner — extracts deterministic templates from history.

This is the brain's *learning* loop, the analog of trading's
``services/trading/learning.py:run_learning_cycle()``. It runs INFREQUENTLY
(default every 6 hours, never more often than once an hour) — emphatically
NOT on every cycle. The trading brain gets away with a 13-step learning
loop because it operates on tens of thousands of bars; the code brain has
far less data and must be conservative about over-fitting.

Inputs:
  * ``llm_call_log``           — successful (success=t, weak_response=f)
                                 prompt+completion pairs
  * ``coding_agent_suggestion`` — diffs that were generated for tasks
  * ``code_decision_router_log`` — which decisions paid off (outcome='applied')

Outputs:
  * Inserts/updates ``code_patterns`` rows. Each pattern is a confidence-
    weighted template that the decision router will prefer over the LLM.

Phase 1 (this file): skeleton with a simple bucketing miner that groups
diffs by *target file glob + verb keywords* (e.g. "add field" + "*.py" in
``app/schemas/``). The bucket bumps confidence each time it sees a
successful match. Real templating logic is Phase 2.

Phase 2 (later): build parameterized diff templates from clusters,
verify them against held-out tasks, gate by promotion rules.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import runtime_state

logger = logging.getLogger(__name__)


# Verb tokens we look for in task briefs to bucket diffs by intent.
_VERB_KEYWORDS = (
    "add", "rename", "remove", "delete", "fix", "wire", "extend",
    "refactor", "introduce", "support", "expose", "log", "audit",
    "migrate", "deprecate", "guard", "validate", "test",
)


@dataclass
class _Bucket:
    archetype: str
    file_glob: str
    keywords: tuple[str, ...]
    success_count: int
    failure_count: int
    sample_llm_call_ids: list[int]


def _file_path_to_glob(path: str) -> str:
    """Generalize a concrete path into a coarse glob.

    Examples:
      app/services/coding_task/agent_suggest.py → app/services/**/*.py
      app/schemas/coding_task.py                → app/schemas/*.py
      docs/CHILI_DISPATCH_RUNBOOK.md            → docs/*.md
    """
    if not path or "/" not in path:
        return path or "*"
    parts = path.split("/")
    last = parts[-1]
    ext = "*"
    if "." in last:
        ext = "*" + last[last.rfind("."):]
    if len(parts) >= 3:
        return f"{parts[0]}/{parts[1]}/**/{ext}"
    return f"{parts[0]}/{ext}"


def _extract_verbs(brief: str) -> tuple[str, ...]:
    if not brief:
        return ()
    lc = brief.lower()
    found = sorted({v for v in _VERB_KEYWORDS if v in lc})
    return tuple(found[:4])  # cap to keep keys stable


def _diff_files(diffs_json: Optional[str]) -> list[str]:
    """Extract target file paths from a JSON-encoded list of unified diffs."""
    if not diffs_json:
        return []
    try:
        diffs = json.loads(diffs_json)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(diffs, list):
        return []
    out: list[str] = []
    for d in diffs:
        if not isinstance(d, str):
            continue
        m = re.search(r"^\+\+\+\s+b/([^\s\n]+)", d, re.MULTILINE)
        if m:
            out.append(m.group(1))
            continue
        m = re.search(r"^---\s+a/([^\s\n]+)", d, re.MULTILINE)
        if m:
            out.append(m.group(1))
    return out


def mine_recent(
    db: Session,
    *,
    lookback_hours: int = 24,
    min_observations: int = 3,
    min_success_rate: float = 0.6,
) -> dict[str, Any]:
    """Mine the last ``lookback_hours`` of activity into ``code_patterns`` rows.

    A bucket is upgraded to a pattern when:
      * it was observed at least ``min_observations`` times
      * the observed success rate (decision_router outcome='applied'
        OR coding_agent_suggestion that produced ≥1 valid diff)
        exceeds ``min_success_rate``

    Confidence on the resulting pattern starts at min(success_rate, 0.85)
    so a brand-new pattern can never beat a hand-tuned threshold without
    additional evidence.

    Returns a small report dict for the status endpoint.
    """
    state = runtime_state.get_state(db)
    if state.mode == "paused":
        logger.info("[code_brain.pattern_miner] mode=paused; skipping mining run")
        return {"skipped": True, "reason": "mode=paused"}

    rows = db.execute(
        text(
            "SELECT s.id, s.task_id, s.diffs_json, s.model, t.title, t.brief "
            "FROM coding_agent_suggestion s "
            "LEFT JOIN plan_tasks t ON t.id = s.task_id "
            "WHERE s.created_at > NOW() - (:h || ' hours')::interval "
            "  AND s.model <> 'error' "
            "  AND COALESCE(s.diffs_json, '[]') <> '[]' "
            "ORDER BY s.id DESC "
            "LIMIT 2000"
        ),
        {"h": int(lookback_hours)},
    ).fetchall()

    buckets: dict[tuple[str, str, tuple[str, ...]], _Bucket] = {}
    for sid, task_id, diffs_json, model, title, brief in rows or []:
        files = _diff_files(diffs_json)
        if not files:
            continue
        verbs = _extract_verbs(f"{title or ''}\n{brief or ''}")
        for f in files:
            glob = _file_path_to_glob(f)
            archetype = f"{verbs[0] if verbs else 'edit'}_{glob.split('/')[-1]}"
            key = (archetype, glob, verbs)
            b = buckets.get(key)
            if b is None:
                b = _Bucket(
                    archetype=archetype,
                    file_glob=glob,
                    keywords=verbs,
                    success_count=0,
                    failure_count=0,
                    sample_llm_call_ids=[],
                )
                buckets[key] = b
            b.success_count += 1
            if len(b.sample_llm_call_ids) < 5:
                b.sample_llm_call_ids.append(int(sid))

    # Cross-reference with decision-router outcomes when available
    # (early-life: most rows won't have outcome yet; that's fine).
    failed_rows = db.execute(
        text(
            "SELECT task_id FROM code_decision_router_log "
            "WHERE decided_at > NOW() - (:h || ' hours')::interval "
            "  AND outcome IN ('failed', 'escalated')"
        ),
        {"h": int(lookback_hours)},
    ).fetchall()
    failed_task_ids = {int(r[0]) for r in failed_rows or [] if r[0] is not None}
    if failed_task_ids:
        logger.info(
            "[code_brain.pattern_miner] %d failed tasks observed in window",
            len(failed_task_ids),
        )

    candidates: list[_Bucket] = []
    for b in buckets.values():
        observations = b.success_count + b.failure_count
        if observations < min_observations:
            continue
        success_rate = b.success_count / max(1, observations)
        if success_rate < min_success_rate:
            continue
        candidates.append(b)

    inserted = 0
    updated = 0
    for b in candidates:
        confidence = min(b.success_count / max(1, b.success_count + b.failure_count), 0.85)
        name = f"{b.archetype}::{b.file_glob}"
        existing = db.execute(
            text("SELECT id, success_count, failure_count, confidence FROM code_patterns WHERE name = :n"),
            {"n": name},
        ).fetchone()
        if existing:
            new_succ = int(existing[1] or 0) + b.success_count
            new_fail = int(existing[2] or 0) + b.failure_count
            new_conf = min(new_succ / max(1, new_succ + new_fail), 0.95)
            db.execute(
                text(
                    "UPDATE code_patterns "
                    "SET success_count = :s, failure_count = :f, "
                    "    confidence = :c, "
                    "    mined_from_llm_call_ids = CAST(:ids AS jsonb), "
                    "    updated_at = NOW() "
                    "WHERE id = :id"
                ),
                {
                    "s": new_succ,
                    "f": new_fail,
                    "c": Decimal(str(round(new_conf, 4))),
                    "ids": json.dumps(b.sample_llm_call_ids),
                    "id": int(existing[0]),
                },
            )
            updated += 1
        else:
            db.execute(
                text(
                    "INSERT INTO code_patterns "
                    "(name, description, brief_keywords, file_glob_pattern, "
                    " diff_archetype, template_body, template_params, "
                    " confidence, success_count, failure_count, "
                    " mined_from_llm_call_ids) "
                    "VALUES (:n, :d, CAST(:kw AS jsonb), :glob, :arch, "
                    "        :tmpl, CAST(:tp AS jsonb), :c, :s, :f, "
                    "        CAST(:ids AS jsonb))"
                ),
                {
                    "n": name,
                    "d": (
                        f"Auto-mined {b.archetype} on {b.file_glob} "
                        f"({b.success_count} successful observations)"
                    ),
                    "kw": json.dumps(list(b.keywords)),
                    "glob": b.file_glob,
                    "arch": b.archetype,
                    "tmpl": None,  # Phase 2 fills this in
                    "tp": json.dumps([]),
                    "c": Decimal(str(round(confidence, 4))),
                    "s": b.success_count,
                    "f": b.failure_count,
                    "ids": json.dumps(b.sample_llm_call_ids),
                },
            )
            inserted += 1
    db.commit()

    runtime_state.mark_pattern_mining_complete(db)

    report = {
        "skipped": False,
        "lookback_hours": lookback_hours,
        "buckets_observed": len(buckets),
        "patterns_inserted": inserted,
        "patterns_updated": updated,
        "min_observations": min_observations,
        "min_success_rate": min_success_rate,
    }
    logger.info("[code_brain.pattern_miner] %s", report)
    return report

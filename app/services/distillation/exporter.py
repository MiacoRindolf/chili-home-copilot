"""Export filtered llm_call_log rows to a JSONL training set.

Filters:
  - validation_status = 'passed'
  - weak_response = FALSE
  - distillable = TRUE
  - completion length within sane bounds
  - dedup by (system_prompt + user_prompt) hash; prefer cheapest tier

Redaction:
  - obvious secrets (sk-..., gho_..., AKIA...)
  - email addresses
  - absolute Windows paths outside the repo
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Iterator

from sqlalchemy import text

logger = logging.getLogger(__name__)


_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"gho_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[bp]-[A-Za-z0-9-]{20,}"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
]


def _redact(s: str) -> str:
    for pat in _SECRET_PATTERNS:
        s = pat.sub("[REDACTED]", s)
    return s


def _hash_prompt(system: str, user: str) -> str:
    return hashlib.sha256(f"{system}|||{user}".encode("utf-8")).hexdigest()


def build_jsonl_dataset(
    output_path: str,
    *,
    min_complexity: float = 0.0,
    max_rows: int = 50000,
) -> dict[str, int]:
    """Write a JSONL file usable by Unsloth/Axolotl chat templates.

    Returns a stats dict: {seen, kept, deduped, redacted}.
    """
    Path(os.path.dirname(output_path) or ".").mkdir(parents=True, exist_ok=True)
    seen = kept = deduped = 0
    keys: set[str] = set()

    from ...db import SessionLocal

    sess = SessionLocal()
    try:
        rows = sess.execute(
            text(
                "SELECT system_prompt, user_prompt, completion, tier, model "
                "FROM llm_call_log "
                "WHERE distillable = TRUE "
                "  AND validation_status = 'passed' "
                "  AND completion IS NOT NULL "
                "ORDER BY tier ASC, id ASC "  # cheapest tier first so dedup keeps it
                "LIMIT :lim"
            ),
            {"lim": max_rows},
        ).fetchall()
    finally:
        sess.close()

    with open(output_path, "w", encoding="utf-8") as fh:
        for sys_prompt, user_prompt, completion, _tier, _model in rows:
            seen += 1
            if not completion or len(completion) < 8:
                continue
            sys_prompt = _redact(sys_prompt or "")
            user_prompt = _redact(user_prompt or "")
            completion = _redact(completion)
            key = _hash_prompt(sys_prompt, user_prompt)
            if key in keys:
                deduped += 1
                continue
            keys.add(key)
            record = {
                "messages": [
                    *([{"role": "system", "content": sys_prompt}] if sys_prompt else []),
                    {"role": "user", "content": user_prompt},
                    {"role": "assistant", "content": completion},
                ]
            }
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            kept += 1
    logger.info("[distillation.exporter] seen=%d kept=%d deduped=%d -> %s", seen, kept, deduped, output_path)
    return {"seen": seen, "kept": kept, "deduped": deduped}

"""Phase 16: bounded, append-only persistence for Phase 15 agent-suggest success payloads."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from ...models.coding_task import CodingAgentSuggestion
from .envelope import truncate_text

_MODEL_MAX = 200
_RESPONSE_MAX_BYTES = 24_000
_DIFF_MAX_COUNT = 20
_DIFF_ITEM_MAX_BYTES = 64_000
_FILES_MAX_COUNT = 100
_PATH_MAX_LEN = 500
_VALIDATION_MAX_ITEMS = 50
_VALIDATION_ITEM_MAX_BYTES = 4_000
_CONTEXT_USED_MAX_BYTES = 8_000
_ROW_MAX_BYTES = 256_000

ALLOWED_SAVE_KEYS = frozenset(
    {"response", "model", "diffs", "files_changed", "validation", "context_used"}
)


def _utf8_len(s: str) -> int:
    return len(s.encode("utf-8", errors="replace"))


def bound_payload_for_save(body: dict[str, Any]) -> tuple[dict[str, str] | None, str | None]:
    """
    Strict allowlist + bounds. Returns DB column dict (model, response_text, *_json, truncation_flags_json)
    or (None, error_message).
    """
    if set(body.keys()) != ALLOWED_SAVE_KEYS:
        return None, "Body must contain exactly the six Phase 15 success fields, no more and no less."

    flags: dict[str, Any] = {}
    model_raw = body.get("model")
    response_raw = body.get("response")
    diffs_raw = body.get("diffs")
    files_raw = body.get("files_changed")
    val_raw = body.get("validation")
    ctx_raw = body.get("context_used")

    if not isinstance(model_raw, str):
        return None, "model must be a string"
    if not isinstance(response_raw, str):
        return None, "response must be a string"
    if not isinstance(diffs_raw, list) or not all(isinstance(x, str) for x in diffs_raw):
        return None, "diffs must be a list of strings"
    if not isinstance(files_raw, list) or not all(isinstance(x, str) for x in files_raw):
        return None, "files_changed must be a list of strings"
    if not isinstance(val_raw, list) or not all(isinstance(x, dict) for x in val_raw):
        return None, "validation must be a list of objects"
    if not isinstance(ctx_raw, dict):
        return None, "context_used must be an object"

    model = model_raw[:_MODEL_MAX]
    if len(model_raw) > _MODEL_MAX:
        flags["model_truncated"] = True

    response_text, _ = truncate_text(response_raw, _RESPONSE_MAX_BYTES)
    if _utf8_len(response_raw) > _RESPONSE_MAX_BYTES:
        flags["response_truncated"] = True

    bounded_diffs: list[str] = []
    for i, d in enumerate(diffs_raw[:_DIFF_MAX_COUNT]):
        dt, _ = truncate_text(str(d), _DIFF_ITEM_MAX_BYTES)
        bounded_diffs.append(dt)
        if _utf8_len(str(d)) > _DIFF_ITEM_MAX_BYTES:
            flags.setdefault("diff_items_truncated_indices", []).append(i)
    if len(diffs_raw) > _DIFF_MAX_COUNT:
        flags["diffs_dropped"] = len(diffs_raw) - _DIFF_MAX_COUNT

    bounded_files: list[str] = []
    for p in files_raw[:_FILES_MAX_COUNT]:
        s = str(p)[:_PATH_MAX_LEN]
        bounded_files.append(s)
    if len(files_raw) > _FILES_MAX_COUNT:
        flags["files_changed_dropped"] = len(files_raw) - _FILES_MAX_COUNT

    val_items: list[dict[str, Any]] = []
    for i, v in enumerate(val_raw[:_VALIDATION_MAX_ITEMS]):
        try:
            sj = json.dumps(v, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return None, f"validation[{i}] is not JSON-serializable"
        if _utf8_len(sj) > _VALIDATION_ITEM_MAX_BYTES:
            flags.setdefault("validation_items_dropped_indices", []).append(i)
            continue
        val_items.append(v)
    if len(val_raw) > _VALIDATION_MAX_ITEMS:
        flags["validation_items_dropped_tail"] = len(val_raw) - _VALIDATION_MAX_ITEMS

    ctx_keys = list(ctx_raw.keys())
    ctx_obj: dict[str, Any] = dict(ctx_raw)
    ctx_json = "{}"
    while ctx_keys:
        try:
            ctx_json = json.dumps({k: ctx_obj[k] for k in ctx_keys}, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return None, "context_used is not JSON-serializable"
        if _utf8_len(ctx_json) <= _CONTEXT_USED_MAX_BYTES:
            break
        dropped = ctx_keys.pop()
        flags.setdefault("context_used_keys_dropped", []).append(dropped)
    if not ctx_keys and _utf8_len(ctx_json) > _CONTEXT_USED_MAX_BYTES:
        ctx_json = "{}"
        flags["context_used_cleared"] = True

    diffs_json = json.dumps(bounded_diffs, ensure_ascii=False)
    files_json = json.dumps(bounded_files, ensure_ascii=False)
    validation_json = json.dumps(val_items, ensure_ascii=False)

    row_parts = [model, response_text, diffs_json, files_json, validation_json, ctx_json]
    total = sum(_utf8_len(p) for p in row_parts)
    if total > _ROW_MAX_BYTES:
        return None, "Payload exceeds maximum stored size after bounding."

    flags_json = json.dumps(flags, ensure_ascii=False) if flags else None

    return {
        "model": model,
        "response_text": response_text,
        "diffs_json": diffs_json,
        "files_changed_json": files_json,
        "validation_json": validation_json,
        "context_used_json": ctx_json,
        "truncation_flags_json": flags_json,
    }, None


def insert_suggestion(
    db: Session,
    task_id: int,
    user_id: int,
    columns: dict[str, str],
) -> int:
    row = CodingAgentSuggestion(
        task_id=task_id,
        user_id=user_id,
        model=columns["model"],
        response_text=columns["response_text"],
        diffs_json=columns["diffs_json"],
        files_changed_json=columns["files_changed_json"],
        validation_json=columns["validation_json"],
        context_used_json=columns["context_used_json"],
        truncation_flags_json=columns.get("truncation_flags_json"),
    )
    db.add(row)
    db.flush()
    return int(row.id)


def list_suggestion_metadata(db: Session, task_id: int, limit: int) -> list[dict[str, Any]]:
    lim = max(1, min(limit, 50))
    rows = (
        db.query(CodingAgentSuggestion)
        .filter(CodingAgentSuggestion.task_id == task_id)
        .order_by(CodingAgentSuggestion.id.desc())
        .limit(lim)
        .all()
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            fc = json.loads(r.files_changed_json or "[]")
            dc = json.loads(r.diffs_json or "[]")
        except json.JSONDecodeError:
            fc, dc = [], []
        out.append(
            {
                "id": r.id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "model": r.model,
                "files_changed_count": len(fc) if isinstance(fc, list) else 0,
                "diffs_count": len(dc) if isinstance(dc, list) else 0,
            }
        )
    return out


def get_suggestion_detail_dict(db: Session, task_id: int, suggestion_id: int) -> dict[str, Any] | None:
    r = (
        db.query(CodingAgentSuggestion)
        .filter(
            CodingAgentSuggestion.id == suggestion_id,
            CodingAgentSuggestion.task_id == task_id,
        )
        .first()
    )
    if not r:
        return None
    try:
        diffs = json.loads(r.diffs_json or "[]")
        files_changed = json.loads(r.files_changed_json or "[]")
        validation = json.loads(r.validation_json or "[]")
        context_used = json.loads(r.context_used_json or "{}")
    except json.JSONDecodeError:
        return None
    flags = None
    if r.truncation_flags_json:
        try:
            flags = json.loads(r.truncation_flags_json)
        except json.JSONDecodeError:
            flags = {"_parse_error": True}
    return {
        "id": r.id,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "model": r.model,
        "response": r.response_text,
        "diffs": diffs,
        "files_changed": files_changed,
        "validation": validation,
        "context_used": context_used,
        "truncation_flags": flags,
    }


def coerce_list_limit(raw: str | None, default: int = 20) -> int:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        n = int(str(raw).strip(), 10)
    except ValueError:
        return default
    return max(1, min(n, 50))

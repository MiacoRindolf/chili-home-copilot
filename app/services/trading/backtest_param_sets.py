"""Canonical backtest param/provenance blobs with hash-based deduplication.

See docs/TRADING_SELECTIVE_NORMALIZATION.md for rationale and rollout.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...models.trading import BacktestParamSet, BacktestResult

logger = logging.getLogger(__name__)


def canonicalize_json_value(value: Any) -> Any:
    """Recursively sort dict keys for stable hashing; preserve list order.

    Non-dict/list scalars are returned as-is. Sets are not supported (treated via default=str at dump).
    """
    if isinstance(value, dict):
        return {str(k): canonicalize_json_value(value[k]) for k in sorted(value.keys(), key=lambda x: str(x))}
    if isinstance(value, list):
        return [canonicalize_json_value(v) for v in value]
    if isinstance(value, tuple):
        return [canonicalize_json_value(v) for v in value]
    return value


def canonical_params_dict(params_obj: dict[str, Any]) -> dict[str, Any]:
    """Return a canonical dict suitable for JSONB storage and hashing."""
    canon = canonicalize_json_value(params_obj)
    if not isinstance(canon, dict):
        return {"_root": canon}
    return canon


def param_hash_sha256(canon: dict[str, Any]) -> str:
    """Stable SHA-256 hex digest of canonical JSON (not for cryptography — dedupe only)."""
    payload = json.dumps(
        canon,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_or_create_backtest_param_set(db: Session, params_obj: dict[str, Any]) -> int | None:
    """Insert or reuse a ``BacktestParamSet`` row; return id, or None if params empty.

    Uses a savepoint so a concurrent unique-key conflict does not abort the outer transaction.
    """
    if not params_obj:
        return None
    canon = canonical_params_dict(params_obj)
    h = param_hash_sha256(canon)
    row = db.query(BacktestParamSet).filter(BacktestParamSet.param_hash == h).one_or_none()
    if row is not None:
        return int(row.id)
    new_row = BacktestParamSet(param_hash=h, params_json=canon)
    try:
        with db.begin_nested():
            db.add(new_row)
            db.flush()
        return int(new_row.id)
    except IntegrityError:
        row = db.query(BacktestParamSet).filter(BacktestParamSet.param_hash == h).one_or_none()
        if row is None:
            logger.warning("[backtest_param_sets] race lost and row missing for hash=%s…", h[:12])
            return None
        return int(row.id)


def materialize_backtest_params(db: Session, bt: BacktestResult) -> dict[str, Any]:
    """Resolve params for API / consumers: prefer denormalized ``params``, else param set JSON."""
    raw = bt.params
    if raw is not None:
        if isinstance(raw, str):
            try:
                out = json.loads(raw)
                return out if isinstance(out, dict) else {}
            except (json.JSONDecodeError, TypeError, ValueError):
                return {}
        if isinstance(raw, dict):
            return dict(raw)
    ps_id = getattr(bt, "param_set_id", None)
    if ps_id is not None:
        ps = db.get(BacktestParamSet, int(ps_id))
        if ps is not None and ps.params_json is not None:
            pj = ps.params_json
            return dict(pj) if isinstance(pj, dict) else {}
    return {}

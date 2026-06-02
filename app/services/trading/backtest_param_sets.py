"""Canonical backtest param/provenance blobs with hash-based deduplication.

See docs/TRADING_SELECTIVE_NORMALIZATION.md for rationale and rollout.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from decimal import Decimal
from numbers import Integral, Real
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...models.trading import BacktestParamSet, BacktestResult

logger = logging.getLogger(__name__)


def canonicalize_json_value(value: Any) -> Any:
    """Return a stable, JSONB-safe value for hashing and storage.

    PostgreSQL JSONB rejects IEEE non-finite values such as NaN and Infinity.
    Those can leak out of OOS and calibration math, so normalize them to null
    before the queue attempts to persist evidence fingerprints.
    """
    if isinstance(value, dict):
        return {str(k): canonicalize_json_value(value[k]) for k in sorted(value.keys(), key=lambda x: str(x))}
    if isinstance(value, list):
        return [canonicalize_json_value(v) for v in value]
    if isinstance(value, tuple):
        return [canonicalize_json_value(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return [canonicalize_json_value(v) for v in sorted(value, key=lambda x: str(x))]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Real):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    if isinstance(value, Decimal):
        return float(value) if value.is_finite() else None
    return str(value)


def canonical_params_dict(params_obj: dict[str, Any]) -> dict[str, Any]:
    """Return a canonical dict suitable for JSONB storage and hashing."""
    canon = canonicalize_json_value(params_obj)
    if not isinstance(canon, dict):
        return {"_root": canon}
    return canon


def param_hash_sha256(canon: dict[str, Any]) -> str:
    """Stable SHA-256 hex digest of canonical JSON (not for cryptography — dedupe only)."""
    safe_canon = canonical_params_dict(canon)
    payload = json.dumps(
        safe_canon,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _id_from_row(row: Any) -> int | None:
    if row is None:
        return None
    if isinstance(row, (tuple, list)):
        raw = row[0] if row else None
    else:
        raw = getattr(row, "id", None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def get_or_create_backtest_param_set(db: Session, params_obj: dict[str, Any]) -> int | None:
    """Insert or reuse a ``BacktestParamSet`` row; return id, or None if params empty.

    Uses a savepoint so a concurrent unique-key conflict does not abort the outer transaction.
    """
    if not params_obj:
        return None
    canon = canonical_params_dict(params_obj)
    h = param_hash_sha256(canon)
    row = db.query(BacktestParamSet.id).filter(BacktestParamSet.param_hash == h).one_or_none()
    row_id = _id_from_row(row)
    if row_id is not None:
        return row_id
    new_row = BacktestParamSet(param_hash=h, params_json=canon)
    try:
        with db.begin_nested():
            db.add(new_row)
            db.flush()
        return int(new_row.id)
    except IntegrityError:
        row = db.query(BacktestParamSet.id).filter(BacktestParamSet.param_hash == h).one_or_none()
        row_id = _id_from_row(row)
        if row_id is None:
            logger.warning("[backtest_param_sets] race lost and row missing for hash=%s…", h[:12])
            return None
        return row_id


def _params_json_from_row(row: Any) -> Any:
    if row is None:
        return None
    if isinstance(row, (tuple, list)):
        return row[0] if row else None
    return getattr(row, "params_json", None)


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
        row = (
            db.query(BacktestParamSet.params_json)
            .filter(BacktestParamSet.id == int(ps_id))
            .first()
        )
        pj = _params_json_from_row(row)
        if pj is not None:
            return dict(pj) if isinstance(pj, dict) else {}
    return {}

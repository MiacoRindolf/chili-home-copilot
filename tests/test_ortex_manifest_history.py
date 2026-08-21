"""Task #10 (2026-08-21) — EXACT-manifest history: ang in-flight na desisyon ay
nagba-bind pa rin sa batch nito matapos umikot ang hub manifest.

Ang 08-18 autopsy: 116/134 place attempts ang namatay sa reference mismatch
dahil ang manifest ay umiikot kada field-prep. Ang lunas ay retention, HINDI
relaxation — parehong 5-field equality. PURE (mock hub row).
Runnable: pytest tests/test_ortex_manifest_history.py -v
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.trading.momentum_neural.pipeline import (
    ORTEX_SQUEEZE_BATCH_STATUS_HISTORY_KEY,
    ORTEX_SQUEEZE_BATCH_STATUS_KEY,
    _ORTEX_BATCH_HISTORY_MAX,
    _ORTEX_SQUEEZE_BATCH_REF_SCHEMA,
    resolve_ortex_batch_manifest_from_hub,
)

_PIPE = "app.services.trading.momentum_neural.pipeline"


def _manifest(sha: str, at: str) -> dict:
    return {
        "batch_sha256": sha,
        "decision_at": at,
        "complete": True,
        "quota_policy_sha256": "qp-1",
        "members_sha256": "mem-1",
        "payload": f"batch-{sha}",
    }


def _reference(sha: str, at: str) -> dict:
    return {
        "schema_version": _ORTEX_SQUEEZE_BATCH_REF_SCHEMA,
        "batch_sha256": sha,
        "decision_at": at,
        "complete": True,
        "quota_policy_sha256": "qp-1",
        "members_sha256": "mem-1",
    }


def _db_with_hub(local_state: dict):
    hub = SimpleNamespace(local_state=local_state)
    db = MagicMock()
    (db.query.return_value.populate_existing.return_value
       .filter.return_value.one_or_none.return_value) = hub
    return db


def test_current_manifest_exact_match_unchanged():
    db = _db_with_hub({ORTEX_SQUEEZE_BATCH_STATUS_KEY: _manifest("A", "t1")})
    manifest, reason = resolve_ortex_batch_manifest_from_hub(
        db, batch_reference=_reference("A", "t1"))
    assert reason is None and manifest["payload"] == "batch-A"


def test_rotated_manifest_found_in_history():
    """ANG AUTOPSY CASE: current ay B na, pero ang desisyon ay laban sa A —
    matatagpuan sa history nang EKSAKTO at nagba-bind."""
    db = _db_with_hub({
        ORTEX_SQUEEZE_BATCH_STATUS_KEY: _manifest("B", "t2"),
        ORTEX_SQUEEZE_BATCH_STATUS_HISTORY_KEY: [_manifest("A", "t1")],
    })
    manifest, reason = resolve_ortex_batch_manifest_from_hub(
        db, batch_reference=_reference("A", "t1"))
    assert reason is None and manifest["payload"] == "batch-A"


def test_absent_from_history_still_mismatches():
    db = _db_with_hub({
        ORTEX_SQUEEZE_BATCH_STATUS_KEY: _manifest("B", "t2"),
        ORTEX_SQUEEZE_BATCH_STATUS_HISTORY_KEY: [_manifest("C", "t0")],
    })
    manifest, reason = resolve_ortex_batch_manifest_from_hub(
        db, batch_reference=_reference("A", "t1"))
    assert manifest is None
    assert reason == "ortex_batch_manifest_reference_mismatch"


def test_partial_field_match_never_binds():
    """Ang equality ay 5-field pa rin — kahit sha lang ang tumugma, hindi
    nagba-bind (walang relaxation)."""
    stale = _manifest("A", "IBANG-decision-at")
    db = _db_with_hub({
        ORTEX_SQUEEZE_BATCH_STATUS_KEY: _manifest("B", "t2"),
        ORTEX_SQUEEZE_BATCH_STATUS_HISTORY_KEY: [stale],
    })
    manifest, reason = resolve_ortex_batch_manifest_from_hub(
        db, batch_reference=_reference("A", "t1"))
    assert manifest is None
    assert reason == "ortex_batch_manifest_reference_mismatch"


def test_history_cap_constant_bounded():
    assert 1 <= _ORTEX_BATCH_HISTORY_MAX <= 32

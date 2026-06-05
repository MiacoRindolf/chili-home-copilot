"""The recert/exit producer work loop must not re-emit every sweep just because
the evidence-fingerprint suffix rotates. A fingerprint-independent prefix cooldown
caps a single (pattern, asset, event_type) to ~one emission per window.
"""
from __future__ import annotations

from app.services.trading.edge_reliability import (
    RECERT_RESCUE_REFRESH,
    emit_targeted_profitability_work,
)


def test_rotating_fingerprint_is_throttled(db):
    pid = 999001
    e1 = emit_targeted_profitability_work(
        db,
        event_type=RECERT_RESCUE_REFRESH,
        scan_pattern_id=pid,
        source="test",
        asset_class="crypto",
        evidence_fingerprint="fpAAAAAAAA",
        payload={"recommended_work_event": "recert_rescue_refresh"},
    )
    db.flush()
    assert e1 is not None  # first emission goes through

    # next producer sweep: SAME pattern/asset/event, DIFFERENT fingerprint
    e2 = emit_targeted_profitability_work(
        db,
        event_type=RECERT_RESCUE_REFRESH,
        scan_pattern_id=pid,
        source="test",
        asset_class="crypto",
        evidence_fingerprint="fpBBBBBBBB",
        payload={"recommended_work_event": "recert_rescue_refresh"},
    )
    assert e2 is None  # throttled by the fingerprint-independent prefix guard


def test_distinct_patterns_not_throttled(db):
    e1 = emit_targeted_profitability_work(
        db,
        event_type=RECERT_RESCUE_REFRESH,
        scan_pattern_id=999002,
        source="test",
        asset_class="crypto",
        evidence_fingerprint="fpA",
    )
    db.flush()
    e2 = emit_targeted_profitability_work(
        db,
        event_type=RECERT_RESCUE_REFRESH,
        scan_pattern_id=999003,  # different pattern -> different prefix
        source="test",
        asset_class="crypto",
        evidence_fingerprint="fpB",
    )
    assert e1 is not None and e2 is not None

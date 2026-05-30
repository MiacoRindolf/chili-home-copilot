from __future__ import annotations

from datetime import datetime, timedelta

from app.models.trading import BrainWorkEvent, ScanPattern
from app.services.trading.edge_reliability import (
    EXIT_VARIANT_REFRESH,
    emit_targeted_profitability_work,
)


def _pattern(db, **kwargs) -> ScanPattern:
    pat = ScanPattern(
        name=kwargs.pop("name", "edge targeted exit variant pattern"),
        rules_json={},
        origin="test",
        asset_class=kwargs.pop("asset_class", "stocks"),
        timeframe="1d",
        active=True,
        lifecycle_stage=kwargs.pop("lifecycle_stage", "promoted"),
        **kwargs,
    )
    db.add(pat)
    db.flush()
    return pat


def test_targeted_exit_variant_skips_recent_noop_same_evidence(db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "brain_work_cash_deployment_noop_cooldown_minutes", 360)
    pat = _pattern(db, name="edge targeted noop exit")
    db.add(
        BrainWorkEvent(
            domain="trading",
            event_type="exit_variant_diagnostic",
            event_kind="outcome",
            dedupe_key=f"exit-noop:{pat.id}",
            status="done",
            payload={
                "scan_pattern_id": pat.id,
                "evidence_fingerprint": "same-fp",
                "created_count": 0,
                "skip_reason": "non_positive_quality_evidence_no_exit_variant_birth",
            },
            created_at=datetime.utcnow(),
        )
    )
    db.commit()

    event_id = emit_targeted_profitability_work(
        db,
        event_type=EXIT_VARIANT_REFRESH,
        scan_pattern_id=pat.id,
        source="edge_reliability_snapshot",
        asset_class="stock",
        evidence_fingerprint="same-fp",
    )

    assert event_id is None
    assert (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.event_kind == "work")
        .filter(BrainWorkEvent.event_type == EXIT_VARIANT_REFRESH)
        .count()
        == 0
    )


def test_targeted_exit_variant_allows_after_noop_cooldown(db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "brain_work_cash_deployment_noop_cooldown_minutes", 60)
    pat = _pattern(db, name="edge targeted old noop exit")
    db.add(
        BrainWorkEvent(
            domain="trading",
            event_type="exit_variant_diagnostic",
            event_kind="outcome",
            dedupe_key=f"exit-old-noop:{pat.id}",
            status="done",
            payload={
                "scan_pattern_id": pat.id,
                "evidence_fingerprint": "old-fp",
                "created_count": 0,
                "skip_reason": "duplicate_learned_exit_label",
            },
            created_at=datetime.utcnow() - timedelta(minutes=61),
        )
    )
    db.commit()

    event_id = emit_targeted_profitability_work(
        db,
        event_type=EXIT_VARIANT_REFRESH,
        scan_pattern_id=pat.id,
        source="edge_reliability_snapshot",
        asset_class="stock",
        evidence_fingerprint="new-fp",
    )

    assert event_id is not None

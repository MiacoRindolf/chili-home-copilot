from __future__ import annotations

from app.services.trading.promotion_evidence_audit import (
    PROMOTION_EVIDENCE_FEATURE_SCHEMA_VERSION,
    PROMOTED_PATTERN_AUDIT_EVIDENCE_SOURCE,
    _promotion_audit_classifier_rows,
    classify_promotion_evidence_rows,
)


def _row(**overrides):
    row = {
        "evidence_source": "backtest",
        "broker_truth_state": "accepted",
        "quarantine_state": "clear",
        "provenance_status": "complete",
        "code_version": "abc123",
        "feature_schema_version": PROMOTION_EVIDENCE_FEATURE_SCHEMA_VERSION,
        "backtest_result_id": 42,
        "family_trial_burden": 7,
        "research_integrity": True,
        "row_id": "row-1",
        "scan_pattern_id": 585,
        "evidence_timestamp": "2026-06-01T14:30:00Z",
        "horizon": "close_to_close_event_day",
        "return_unit": "pct",
        "realized_return_pct": 2.5,
    }
    row.update(overrides)
    return row


def test_classifier_counts_complete_rows_as_eligible() -> None:
    packet = classify_promotion_evidence_rows(
        [_row(row_id="row-1"), _row(row_id="row-2", family_trial_burden=9)]
    )

    assert packet["raw_rows"] == 2
    assert packet["eligible_rows"] == 2
    assert packet["excluded_rows_by_reason"] == {}
    assert packet["required_metadata_missing"] == []
    assert packet["code_version"] == "abc123"
    assert packet["backtest_result_id"] == 42
    assert packet["family_trial_burden"] == 9
    assert all(row["eligible"] for row in packet["rows"])
    assert packet["rows"][0]["realized_return"] == 2.5
    assert packet["raw_row_refs"] == ["row-1", "row-2"]
    assert packet["eligible_row_refs"] == ["row-1", "row-2"]
    assert packet["excluded_row_refs_by_reason"] == {}


def test_classifier_excludes_missing_metadata_and_quarantined_rows() -> None:
    packet = classify_promotion_evidence_rows(
        [
            _row(
                row_id="row-1",
                code_version=None,
                feature_schema_version=None,
                backtest_result_id=None,
                quarantine_state="active",
                provenance_status="missing",
                research_integrity=False,
            )
        ],
        code_version=None,
        feature_schema_version=None,
        backtest_result_id=None,
    )

    assert packet["raw_rows"] == 1
    assert packet["eligible_rows"] == 0
    assert set(packet["required_metadata_missing"]) == {
        "code_version",
        "feature_schema_version",
        "backtest_result_id",
    }
    reasons = packet["rows"][0]["excluded_reasons"]
    assert "missing_code_version" in reasons
    assert "missing_feature_schema_version" in reasons
    assert "missing_backtest_result_id" in reasons
    assert "quarantine_not_clear" in reasons
    assert "provenance_not_accepted" in reasons
    assert "research_integrity_failed" in reasons


def test_classifier_live_fallback_requires_broker_truth_and_no_blockers() -> None:
    packet = classify_promotion_evidence_rows(
        [
            _row(
                evidence_source="live_fallback",
                broker_truth_state="unknown",
                missing_stop=True,
                qty_drift=True,
            )
        ]
    )

    assert packet["eligible_rows"] == 0
    reasons = packet["rows"][0]["excluded_reasons"]
    assert "broker_truth_not_accepted" in reasons
    assert "missing_stop" in reasons
    assert "qty_drift" in reasons


def test_classifier_blocks_mixed_source_assumptions() -> None:
    packet = classify_promotion_evidence_rows(
        [
            _row(row_id="row-1", evidence_source="backtest", code_version="abc123"),
            _row(row_id="row-2", evidence_source="paper", code_version="def456"),
        ]
    )

    assert packet["eligible_rows"] == 0
    assert "mixed_evidence_source" in packet["warnings"]
    assert "mixed_code_version" in packet["warnings"]
    assert packet["excluded_rows_by_reason"]["mixed_evidence_source"] == 2
    assert packet["excluded_rows_by_reason"]["mixed_code_version"] == 2


def test_classifier_keeps_family_trial_burden_from_excluded_rows() -> None:
    packet = classify_promotion_evidence_rows(
        [
            _row(row_id="row-1", family_trial_burden=3),
            _row(row_id="row-2", family_trial_burden=21, quarantine_state="active"),
        ],
        family_trial_burden=5,
    )

    assert packet["raw_rows"] == 2
    assert packet["eligible_rows"] == 1
    assert packet["family_trial_burden"] == 21
    assert packet["excluded_rows_by_reason"]["quarantine_not_clear"] == 1


def test_classifier_excludes_missing_identity_horizon_and_return_shape() -> None:
    packet = classify_promotion_evidence_rows(
        [
            _row(
                row_id=None,
                scan_pattern_id=None,
                evidence_timestamp=None,
                horizon=None,
                return_unit=None,
                realized_return_pct=None,
            )
        ]
    )

    assert packet["eligible_rows"] == 0
    assert packet["raw_row_refs"] == ["row_index:0"]
    assert packet["rows"][0]["row_ref"] == "row_index:0"
    reasons = packet["rows"][0]["excluded_reasons"]
    assert "missing_row_id" in reasons
    assert "missing_scan_pattern_id" in reasons
    assert "missing_evidence_timestamp" in reasons
    assert "missing_horizon" in reasons
    assert "missing_return_unit" in reasons
    assert "missing_realized_return" in reasons
    assert packet["excluded_row_refs_by_reason"]["missing_row_id"] == ["row_index:0"]


def test_classifier_excludes_nonfinite_and_mixed_horizon_return_rows() -> None:
    packet = classify_promotion_evidence_rows(
        [
            _row(row_id="row-1", horizon="day", realized_return_pct="NaN"),
            _row(row_id="row-2", horizon="week", return_unit="fraction"),
        ]
    )

    assert packet["eligible_rows"] == 0
    assert "mixed_horizon" in packet["warnings"]
    assert "mixed_return_unit" in packet["warnings"]
    assert "nonfinite_realized_return" in packet["rows"][0]["excluded_reasons"]
    assert "mixed_horizon" in packet["rows"][1]["excluded_reasons"]
    assert "mixed_return_unit" in packet["rows"][1]["excluded_reasons"]
    assert packet["excluded_row_refs_by_reason"]["nonfinite_realized_return"] == ["row-1"]
    assert packet["excluded_row_refs_by_reason"]["mixed_horizon"] == ["row-1", "row-2"]
    assert packet["excluded_row_refs_by_reason"]["mixed_return_unit"] == [
        "row-1",
        "row-2",
    ]


def test_audit_classifier_rows_remain_ineligible_until_metadata_is_proven() -> None:
    rows = _promotion_audit_classifier_rows([{"id": 585}, {"id": 999}])

    packet = classify_promotion_evidence_rows(
        rows,
        evidence_source=PROMOTED_PATTERN_AUDIT_EVIDENCE_SOURCE,
        broker_truth_state="not_applicable",
        feature_schema_version=PROMOTION_EVIDENCE_FEATURE_SCHEMA_VERSION,
    )

    assert packet["raw_rows"] == 2
    assert packet["eligible_rows"] == 0
    assert packet["evidence_source"] == PROMOTED_PATTERN_AUDIT_EVIDENCE_SOURCE
    assert packet["raw_row_refs"] == ["scan_pattern:585", "scan_pattern:999"]
    assert packet["rows"][0]["scan_pattern_id"] == 585
    assert set(packet["required_metadata_missing"]) == {
        "code_version",
        "backtest_result_id",
    }
    reasons = packet["rows"][0]["excluded_reasons"]
    assert "missing_code_version" in reasons
    assert "missing_backtest_result_id" in reasons
    assert "provenance_not_accepted" in reasons
    assert "quarantine_not_clear" in reasons

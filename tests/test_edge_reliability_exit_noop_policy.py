from __future__ import annotations

from app.services.trading.edge_reliability import (
    _exit_noop_blocks_refresh,
    _non_positive_exit_noop_blocks_weak_request,
    _repeated_non_positive_exit_noop_blocks_refresh,
)


def test_non_positive_exit_noop_only_blocks_same_evidence():
    payload = {
        "evidence_fingerprint": "old-fp",
        "created_count": 0,
        "skip_reason": "non_positive_quality_evidence_no_exit_variant_birth",
    }

    assert _exit_noop_blocks_refresh(payload, evidence_fingerprint="old-fp") is True
    assert _exit_noop_blocks_refresh(payload, evidence_fingerprint="new-fp") is False


def test_structural_exit_noop_blocks_new_evidence():
    payload = {
        "evidence_fingerprint": "old-fp",
        "created_count": 0,
        "skip_reason": "duplicate_learned_exit_label",
    }

    assert _exit_noop_blocks_refresh(payload, evidence_fingerprint="new-fp") is True


def test_successful_exit_variant_diagnostic_never_blocks_refresh():
    payload = {
        "evidence_fingerprint": "same-fp",
        "created_count": 1,
        "skip_reason": "duplicate_learned_exit_label",
    }

    assert _exit_noop_blocks_refresh(payload, evidence_fingerprint="same-fp") is False


def test_recent_non_positive_exit_noop_blocks_weak_request():
    diagnostic = {
        "evidence_fingerprint": "old-fp",
        "created_count": 0,
        "skip_reason": "non_positive_quality_evidence_no_exit_variant_birth",
    }

    assert (
        _non_positive_exit_noop_blocks_weak_request(
            diagnostic,
            request_payload={"expected_evidence_value": 0.0, "calibrated_ev_pct": -1.2},
        )
        is True
    )


def test_recent_non_positive_exit_noop_allows_positive_request():
    diagnostic = {
        "evidence_fingerprint": "old-fp",
        "created_count": 0,
        "skip_reason": "non_positive_quality_evidence_no_exit_variant_birth",
    }

    assert (
        _non_positive_exit_noop_blocks_weak_request(
            diagnostic,
            request_payload={"expected_evidence_value": 0.3},
        )
        is False
    )


def test_repeated_non_positive_exit_noops_block_weak_refresh():
    diagnostics = [
        {
            "evidence_fingerprint": f"old-fp-{idx}",
            "created_count": 0,
            "skip_reason": "non_positive_quality_evidence_no_exit_variant_birth",
        }
        for idx in range(3)
    ]

    assert (
        _repeated_non_positive_exit_noop_blocks_refresh(
            diagnostics,
            request_payload={"expected_evidence_value": 0.0, "calibrated_ev_pct": 0.0},
        )
        is True
    )


def test_repeated_non_positive_exit_noops_allow_positive_refresh():
    diagnostics = [
        {
            "evidence_fingerprint": f"old-fp-{idx}",
            "created_count": 0,
            "skip_reason": "non_positive_quality_evidence_no_exit_variant_birth",
        }
        for idx in range(3)
    ]

    assert (
        _repeated_non_positive_exit_noop_blocks_refresh(
            diagnostics,
            request_payload={"expected_evidence_value": 0.75},
        )
        is False
    )

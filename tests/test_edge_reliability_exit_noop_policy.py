from __future__ import annotations

from app.services.trading.edge_reliability import _exit_noop_blocks_refresh


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

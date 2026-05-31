"""Shared exit-variant refresh no-op policy."""
from __future__ import annotations

from typing import Any

STRUCTURAL_EXIT_NOOP_REASONS = frozenset(
    {
        "duplicate_learned_exit_label",
        "missing_parent_payoff_geometry",
        "no_loss_report",
        "no_parent_returns",
        "parent_missing_or_inactive",
        "max_active_variants",
        "non_positive_parent_realized_avg",
        "learned_target_not_tighter_than_static",
        "learned_stop_not_tighter_than_static",
    }
)
STRUCTURAL_EXIT_NOOP_PREFIXES = (
    "edge_debt_too_negative_for_exit_child:",
    "insufficient_parent_payoff_samples:",
    "reward_risk_below_floor:",
)
NON_POSITIVE_EXIT_NOOP_REASONS = frozenset(
    {
        "negative_ev_no_exit_variant_birth",
        "non_positive_quality_evidence_no_exit_variant_birth",
    }
)
REPEATED_NON_POSITIVE_EXIT_NOOP_MIN_FINGERPRINTS = 3


def _token(value: object) -> str:
    return str(value or "").strip().lower()


def structural_exit_noop_reason(reason: Any) -> bool:
    value = _token(reason)
    return value in STRUCTURAL_EXIT_NOOP_REASONS or any(
        value.startswith(prefix) for prefix in STRUCTURAL_EXIT_NOOP_PREFIXES
    )


def non_positive_exit_noop_reason(reason: Any) -> bool:
    return _token(reason) in NON_POSITIVE_EXIT_NOOP_REASONS


def exit_noop_blocks_refresh(
    payload: dict[str, Any],
    *,
    evidence_fingerprint: str | None,
) -> bool:
    try:
        created_count = int(payload.get("created_count"))
    except (TypeError, ValueError):
        return False
    if created_count != 0:
        return False
    fingerprint = str(evidence_fingerprint or "")
    if fingerprint and str(payload.get("evidence_fingerprint") or "") == fingerprint:
        return True
    return structural_exit_noop_reason(payload.get("skip_reason"))


def same_evidence_exit_noop_blocks_refresh(
    payload: dict[str, Any],
    *,
    evidence_fingerprint: str | None,
) -> bool:
    try:
        created_count = int(payload.get("created_count"))
    except (TypeError, ValueError):
        return False
    if created_count != 0:
        return False
    fingerprint = str(evidence_fingerprint or "")
    return bool(fingerprint) and str(payload.get("evidence_fingerprint") or "") == fingerprint


def payload_has_positive_exit_evidence(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    for key in (
        "expected_evidence_value",
        "calibrated_ev_after_cost_pct",
        "calibrated_ev_pct",
        "expected_net_pct",
    ):
        try:
            value = float(payload.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0.0:
            return True
    return False


def non_positive_exit_noop_blocks_weak_request(
    diagnostic_payload: dict[str, Any],
    *,
    request_payload: dict[str, Any] | None,
) -> bool:
    try:
        created_count = int(diagnostic_payload.get("created_count"))
    except (TypeError, ValueError):
        return False
    if created_count != 0:
        return False
    if not non_positive_exit_noop_reason(diagnostic_payload.get("skip_reason")):
        return False
    return not payload_has_positive_exit_evidence(request_payload)


def repeated_non_positive_exit_noop_blocks_refresh(
    diagnostic_payloads: list[dict[str, Any]],
    *,
    request_payload: dict[str, Any] | None,
) -> bool:
    non_positive_fingerprints: set[str] = set()
    for idx, row_payload in enumerate(diagnostic_payloads):
        try:
            created_count = int(row_payload.get("created_count"))
        except (TypeError, ValueError):
            created_count = -1
        if (
            created_count == 0
            and non_positive_exit_noop_reason(row_payload.get("skip_reason"))
        ):
            fingerprint = str(row_payload.get("evidence_fingerprint") or "").strip()
            non_positive_fingerprints.add(fingerprint or f"row:{idx}")
    return (
        len(non_positive_fingerprints)
        >= REPEATED_NON_POSITIVE_EXIT_NOOP_MIN_FINGERPRINTS
        and not payload_has_positive_exit_evidence(request_payload)
    )

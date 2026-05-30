"""Fail-closed manifest validation for governed trading evidence consumers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "CURRENT_EVIDENCE_TARGET_BRANCH",
    "CURRENT_EVIDENCE_TARGET_HEAD",
    "CURRENT_V14_VALIDATOR_ARTIFACT",
    "CURRENT_V14_VALIDATOR_SHA256",
    "CURRENT_V34_INDEX_ARTIFACT",
    "CURRENT_V34_INDEX_SHA256",
    "ManifestValidationResult",
    "validate_consumer_manifest",
    "validate_manifest",
]


CURRENT_EVIDENCE_TARGET_BRANCH = "codex/brain-work-done-marker-recovery"
CURRENT_EVIDENCE_TARGET_REMOTE_REF = "origin/codex/brain-work-done-marker-recovery"
CURRENT_EVIDENCE_TARGET_HEAD = "b8bee4bd26cb17cd679b74c31de98bcf4a50218f"
PREVIOUS_INCIDENT_HEAD = "8426169fbef6da11331a0bcd95ca77ca9f08da07"
MERGED_PR_122_HEAD = "3ba5aacaeca3c2433e3bf5ec09d6c483e6bc2984"
ORIGIN_MAIN_AT_V34_BINDING = "5e3c38f3241356c8ae41ed13b5d0162bb9dd92dc"
MERGE_BASE_WITH_ORIGIN_MAIN = MERGED_PR_122_HEAD
LATEST_REQUIRED_CONTAINMENT_FLOOR_UTC = "2026-05-30T06:27:14Z"

CURRENT_V34_INDEX_ARTIFACT = (
    "project_ws/MLOps/OUT/20260530-062300Z-"
    "mlops-governed-evidence-blocker-index-v34.json"
)
CURRENT_V34_INDEX_SHA256 = (
    "38F0054EC81B15E2D68C04DE5DC669BA9908F23CEF9555A5A29EE0E4E28A8591"
)
CURRENT_V14_VALIDATOR_ARTIFACT = (
    "project_ws/MLOps/OUT/20260530-062300Z-"
    "pm065-v34-consumer-manifest-validator-spec.json"
)
CURRENT_V14_VALIDATOR_SHA256 = (
    "0731E02A26B4464AEDDC928F1E05C111F45E59A810DE81CFA22B914077D96CF4"
)

PRELIMINARY_0629_V34_INDEX_SHA256 = (
    "34BA67001A45DAD356A643B12DCEDB18B3A21E192898E381E70D2A5CA44CF390"
)
PRELIMINARY_0629_V14_VALIDATOR_SHA256 = (
    "C612691ACDE01910214260FD16D72974FFBE10FB27FD4277E9CA30AE491C1D21"
)

REQUIRED_MANIFEST_FIELDS = frozenset(
    {
        "manifest_schema",
        "manifest_generated_utc",
        "consumer_name",
        "evidence_target_branch",
        "evidence_target_head",
        "mlops_blocker_index_artifact",
        "mlops_blocker_index_sha256",
        "mlops_validator_spec_artifact",
        "mlops_validator_spec_sha256",
        "source_artifacts",
        "excluded_commit_ids",
        "excluded_remote_refs",
        "excluded_branch_refs",
        "excluded_pr_refs",
        "excluded_check_run_refs",
        "excluded_runtime_windows",
        "required_fail_closed_exclusions",
        "consumer_surfaces_fail_closed",
        "non_clearance_assertions",
        "latest_required_containment_floor_utc",
        "checks_run_or_skipped",
        "remaining_risks",
        "safety_constraints",
    }
)

REQUIRED_FAIL_CLOSED_EXCLUSIONS = frozenset(
    {
        "post0546_branch_push_stream_still_evidence_only",
        "post0614_current_branch_head_b8bee4b_not_clean_source_lineage",
        "post0614_b8bee4b_no_github_checks_not_ci_evidence",
        "post0614_b8bee4b_no_branch_workflow_not_release_readiness",
        "post0614_b8bee4b_nonmain_ahead7_behind19_not_pr_readiness",
        "post0614_b8bee4b_autotrader_backtest_queue_brain_work_scope_requires_clean_review",
        "post0614_b8bee4b_target_local_tests_not_clean_verification",
        "post0614_b8bee4b_branch_head_stream_requires_v34_manifest_binding",
        "post0616_active_target_after_b8bee4b_control_plane_failure",
        "dirty_shared_bind_mount_not_reproducible_source_state",
        "pm065_source_lineage_not_satisfied_without_manifest_exact_hash_binding",
    }
)

REQUIRED_CONSUMER_SURFACES = frozenset(
    {
        "labels",
        "training",
        "evaluation",
        "backtests",
        "transaction_cost_analysis",
        "pnl_statistics",
        "calibration",
        "threshold_tuning",
        "model_health",
        "release_readiness",
        "pr_readiness",
        "source_lineage",
        "runtime_provenance",
        "broker_truth_claims",
        "evidence_promotion",
        "model_promotion",
    }
)

REQUIRED_NON_CLEARANCE_FALSE = frozenset(
    {
        "approved_to_push",
        "approved_to_merge",
        "approved_to_deploy",
        "approved_for_release",
        "approved_for_model_promotion",
        "approved_for_evidence_promotion",
        "approved_for_live_trading_behavior_change",
        "approved_to_reset_breakers",
        "approved_to_mutate_broker_or_database_state",
    }
)

REQUIRED_SOURCE_ARTIFACTS = (
    (
        "project_ws/DevOps/OUT/20260530-061700Z-devops-release-watch-b8bee-nonmain-block.md",
        "376EBC4BD7891558F067254B62285D7781A8521B1FEDE640073E95326FDE326D",
    ),
    (
        "project_ws/Risk/OUT/20260530-062100Z-devops-post0614-b8bee-branch-head-risk-disposition.md",
        "24317D2EF95843E025D32603A9431733F6D0BF7DD2FBF79E0B9D784FB1D80532",
    ),
    (
        "project_ws/PM/IN/20260530-062101Z-from-Risk-to-PM-urgent-b8bee-branch-head-classification.md",
        "81EFBB2B994328C2620C02D4412EAEB1E36ADB190C82E276AA8847D1C4B42EE9",
    ),
    (
        "project_ws/PM/OUT/20260530-062000Z-pm-b8bee-branch-head-disposition.md",
        "34F3A334EFD95B0089A20EFF374A63904DB4444C9BA726A5D3F8722D3B719F96",
    ),
    (
        "project_ws/DS/OUT/20260530-061900Z-ds-v33-consumer-binding-implementation-brief.md",
        "518946D0F60CF0A715699027F9F54FD79298EF11C9C871DC0A3A30C969713128",
    ),
    (
        "project_ws/MLOps/IN/20260530-062400Z-from-DS-to-MLOps-urgent-post0614-b8bee-successor-evidence-governance.md",
        "335BFDDCEAE4AB916C0492F6C3D9814B3F2F9A3358D0635766EE12679B94BD7B",
    ),
    (
        "project_ws/PM/BRANCHES.md",
        "685BC8691A111EBD5EDEED30DB5178E396FF99B148F3B2A9EA4BC88F99E02D20",
    ),
    (
        "project_ws/PM/PR_BLOCKERS.md",
        "57B09DB31170BE137F77C27C70CE1E6933AF65F745EA98490DDF7FDC95AF75C2",
    ),
    (
        "project_ws/MLOps/OUT/20260530-060400Z-mlops-governed-evidence-blocker-index-v33.json",
        "B6D7634D1FF83959E919CE66AB287B31F2F619906E79432E6E032F844F700E61",
    ),
    (
        "project_ws/MLOps/OUT/20260530-060400Z-pm065-v33-consumer-manifest-validator-spec.json",
        "731C5F0B8526685F2BC567E5CDA4BBD99AA89815D6A26644857921BBDF4467AA",
    ),
)

REQUIRED_EXCLUDED_REFERENCES = {
    "excluded_commit_ids": frozenset(
        {MERGED_PR_122_HEAD, PREVIOUS_INCIDENT_HEAD, CURRENT_EVIDENCE_TARGET_HEAD}
    ),
    "excluded_remote_refs": frozenset({CURRENT_EVIDENCE_TARGET_REMOTE_REF}),
    "excluded_branch_refs": frozenset({CURRENT_EVIDENCE_TARGET_BRANCH}),
    "excluded_pr_refs": frozenset({"PR #122"}),
    "excluded_check_run_refs": frozenset(
        {
            "GitHub check-runs count 0 for b8bee4bd26cb17cd679b74c31de98bcf4a50218f",
            "GitHub status contexts total 0 and pending for b8bee4bd26cb17cd679b74c31de98bcf4a50218f",
        }
    ),
    "excluded_runtime_windows": frozenset(
        {
            "post-2026-05-30T05:46:00Z branch-push stream",
            "post-2026-05-30T06:14:00Z b8bee4b current branch-head stream",
            "post-2026-05-30T06:27:14Z active-target containment floor",
        }
    ),
}

REQUIRED_SAFETY_CONSTRAINTS = frozenset(
    {
        "broker_apis_authoritative",
        "deterministic_safety_gates_preserved",
        "db_tests_must_use_test_suffix",
        "no_magic_number_thresholds",
        "kill_switches_preserved",
        "drawdown_breakers_preserved",
        "trading_trades_compatibility_view_preserved",
        "position_identity_constraints_preserved",
    }
)


@dataclass(frozen=True)
class ManifestValidationResult:
    accepted: bool
    status: str
    errors: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "status": self.status,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def validate_consumer_manifest(manifest: Any) -> ManifestValidationResult:
    """Validate a consumer manifest against the current v34/v14 fail-closed gate."""

    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(manifest, dict):
        return ManifestValidationResult(
            accepted=False,
            status="FAIL_CLOSED",
            errors=("manifest_not_object",),
        )

    _require_fields(manifest, errors)
    _require_exact_value(manifest, errors, "evidence_target_branch", CURRENT_EVIDENCE_TARGET_BRANCH)
    _require_exact_value(manifest, errors, "evidence_target_head", CURRENT_EVIDENCE_TARGET_HEAD)
    _require_exact_value(
        manifest,
        errors,
        "latest_required_containment_floor_utc",
        LATEST_REQUIRED_CONTAINMENT_FLOOR_UTC,
    )
    _require_current_v34_v14_binding(manifest, errors)
    _require_source_artifacts(manifest, errors)
    _require_set_field(
        manifest,
        errors,
        "required_fail_closed_exclusions",
        REQUIRED_FAIL_CLOSED_EXCLUSIONS,
        "missing_fail_closed_exclusion",
    )
    _require_set_field(
        manifest,
        errors,
        "consumer_surfaces_fail_closed",
        REQUIRED_CONSUMER_SURFACES,
        "missing_consumer_surface",
    )
    for field_name, expected_values in REQUIRED_EXCLUDED_REFERENCES.items():
        _require_set_field(
            manifest,
            errors,
            field_name,
            expected_values,
            f"missing_{field_name[:-1] if field_name.endswith('s') else field_name}",
        )
    _require_non_clearance(manifest, errors)
    _require_set_field(
        manifest,
        errors,
        "safety_constraints",
        REQUIRED_SAFETY_CONSTRAINTS,
        "missing_safety_constraint",
    )

    if errors:
        return ManifestValidationResult(
            accepted=False,
            status="FAIL_CLOSED",
            errors=tuple(errors),
            warnings=tuple(warnings),
        )

    return ManifestValidationResult(
        accepted=True,
        status="EVIDENCE_GOVERNED_NON_CLEARANCE",
        errors=(),
        warnings=tuple(warnings),
    )


validate_manifest = validate_consumer_manifest


def _normalize_sha(value: Any) -> str:
    return str(value or "").strip().upper()


def _require_fields(manifest: dict[str, Any], errors: list[str]) -> None:
    for field_name in sorted(REQUIRED_MANIFEST_FIELDS):
        if field_name not in manifest:
            errors.append(f"missing_required_field:{field_name}")


def _require_exact_value(
    manifest: dict[str, Any], errors: list[str], field_name: str, expected: Any
) -> None:
    if manifest.get(field_name) != expected:
        errors.append(f"exact_value_mismatch:{field_name}")


def _require_current_v34_v14_binding(
    manifest: dict[str, Any], errors: list[str]
) -> None:
    blocker_sha = _normalize_sha(manifest.get("mlops_blocker_index_sha256"))
    validator_sha = _normalize_sha(manifest.get("mlops_validator_spec_sha256"))
    if blocker_sha == PRELIMINARY_0629_V34_INDEX_SHA256:
        errors.append("preliminary_0629_v34_index_hash_not_accepted")
    if validator_sha == PRELIMINARY_0629_V14_VALIDATOR_SHA256:
        errors.append("preliminary_0629_v14_validator_hash_not_accepted")
    if manifest.get("mlops_blocker_index_artifact") != CURRENT_V34_INDEX_ARTIFACT:
        errors.append("current_v34_index_artifact_required")
    if blocker_sha != CURRENT_V34_INDEX_SHA256:
        errors.append("current_v34_index_sha_required")
    if manifest.get("mlops_validator_spec_artifact") != CURRENT_V14_VALIDATOR_ARTIFACT:
        errors.append("current_v14_validator_artifact_required")
    if validator_sha != CURRENT_V14_VALIDATOR_SHA256:
        errors.append("current_v14_validator_sha_required")


def _require_source_artifacts(manifest: dict[str, Any], errors: list[str]) -> None:
    source_artifacts = manifest.get("source_artifacts")
    if not isinstance(source_artifacts, list):
        errors.append("source_artifacts_not_list")
        return
    by_path: dict[str, str] = {}
    for item in source_artifacts:
        if isinstance(item, dict):
            by_path[str(item.get("artifact") or "")] = _normalize_sha(item.get("sha256"))
    for artifact, sha256 in REQUIRED_SOURCE_ARTIFACTS:
        if by_path.get(artifact) != sha256:
            errors.append(f"source_artifact_exact_hash_required:{artifact}")


def _require_set_field(
    manifest: dict[str, Any],
    errors: list[str],
    field_name: str,
    required: frozenset[str],
    error_prefix: str,
) -> None:
    values = manifest.get(field_name)
    if not isinstance(values, list):
        errors.append(f"{field_name}_not_list")
        return
    observed = {str(value) for value in values}
    for value in sorted(required - observed):
        errors.append(f"{error_prefix}:{value}")


def _require_non_clearance(manifest: dict[str, Any], errors: list[str]) -> None:
    assertions = manifest.get("non_clearance_assertions")
    if not isinstance(assertions, dict):
        errors.append("non_clearance_assertions_not_object")
        return
    for name in sorted(REQUIRED_NON_CLEARANCE_FALSE):
        if name not in assertions:
            errors.append(f"missing_non_clearance_assertion:{name}")
        elif assertions[name] is not False:
            errors.append(f"non_clearance_assertion_must_be_false:{name}")

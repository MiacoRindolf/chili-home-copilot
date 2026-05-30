from __future__ import annotations

import json
import subprocess
import sys

from app.services.trading.evidence_consumer_manifest import (
    CURRENT_EVIDENCE_TARGET_BRANCH,
    CURRENT_V15_VALIDATOR_ARTIFACT,
    CURRENT_V15_VALIDATOR_SHA256,
    CURRENT_V35_INDEX_ARTIFACT,
    CURRENT_V35_INDEX_SHA256,
    LEGACY_V14_VALIDATOR_ARTIFACT,
    LEGACY_V14_VALIDATOR_SHA256,
    LEGACY_V34_INDEX_ARTIFACT,
    LEGACY_V34_INDEX_SHA256,
    ManifestBindingPolicy,
    PRELIMINARY_0629_V14_VALIDATOR_SHA256,
    PRELIMINARY_0629_V34_INDEX_SHA256,
    REQUIRED_SAFETY_CONSTRAINTS,
    V33_POLICY,
    V34_POLICY,
    V40_CURRENT_BRANCH_HEAD,
    V40_POLICY,
    V39_CURRENT_BRANCH_HEAD,
    V39_POLICY,
    V38_CURRENT_BRANCH_HEAD,
    V38_POLICY,
    V37_CURRENT_BRANCH_HEAD,
    V37_POLICY,
    V37_PREDECESSOR_4C68_HEAD,
    V36_CURRENT_BRANCH_HEAD,
    V36_POLICY,
    V36_PREDECESSOR_571_HEAD,
    V36_PREDECESSOR_EFE9_HEAD,
    V35_CURRENT_BRANCH_HEAD,
    V35_INTERMEDIATE_HEAD_AFTER_B8BEE,
    V35_POLICY,
    validate_consumer_manifest,
)


def _valid_manifest(
    policy: ManifestBindingPolicy,
    *,
    target_head: str | None = None,
    ancestor_heads: list[str] | None = None,
) -> dict[str, object]:
    head = target_head or sorted(policy.accepted_target_heads)[0]
    if policy is V40_POLICY and ancestor_heads is None:
        ancestor_heads = [V39_CURRENT_BRANCH_HEAD]
    elif policy is V39_POLICY and ancestor_heads is None:
        ancestor_heads = [V37_CURRENT_BRANCH_HEAD]
    elif policy is V38_POLICY and ancestor_heads is None:
        ancestor_heads = [V37_CURRENT_BRANCH_HEAD]
    elif policy is V37_POLICY and ancestor_heads is None:
        ancestor_heads = [V37_PREDECESSOR_4C68_HEAD, V36_CURRENT_BRANCH_HEAD]
    elif policy is V36_POLICY and ancestor_heads is None:
        ancestor_heads = [V36_PREDECESSOR_EFE9_HEAD, V36_PREDECESSOR_571_HEAD]
    manifest = {
        "manifest_schema": f"sswe.consumer_manifest.{policy.name}",
        "manifest_generated_utc": "2026-05-30T06:52:00Z",
        "consumer_name": "unit-test-consumer",
        "evidence_target_branch": CURRENT_EVIDENCE_TARGET_BRANCH,
        "evidence_target_head": head,
        "mlops_blocker_index_artifact": policy.blocker_index_artifact,
        "mlops_blocker_index_sha256": policy.blocker_index_sha256,
        "mlops_validator_spec_artifact": policy.validator_spec_artifact,
        "mlops_validator_spec_sha256": policy.validator_spec_sha256,
        "source_artifacts": [
            {"artifact": artifact, "sha256": sha256}
            for artifact, sha256 in policy.required_source_artifacts
        ],
        "required_fail_closed_exclusions": list(
            policy.required_fail_closed_exclusions
        ),
        "required_consumer_surfaces_fail_closed": list(
            policy.required_consumer_surfaces
        ),
        "non_clearance_assertions": {
            name: False for name in policy.required_non_clearance_false
        },
        "latest_required_containment_floor_utc": (
            policy.latest_required_containment_floor_utc
        ),
        "checks_run_or_skipped": [
            "No production database, broker API, release, deploy, restart, or migration action."
        ],
        "remaining_risks": [
            "Governed branch-head evidence remains evidence-only non-clearance."
        ],
        "safety_constraints": list(REQUIRED_SAFETY_CONSTRAINTS),
    }
    if ancestor_heads:
        manifest["evidence_target_ancestor_heads"] = ancestor_heads
    for field_name, values in policy.required_excluded_references.items():
        manifest[field_name] = list(values)
    return manifest


def _error_set(manifest: dict[str, object]) -> set[str]:
    return set(validate_consumer_manifest(manifest).errors)


def test_current_v40_v20_manifest_passes_only_as_non_clearance() -> None:
    result = validate_consumer_manifest(
        _valid_manifest(V40_POLICY, target_head=V40_CURRENT_BRANCH_HEAD)
    )

    assert result.accepted is True
    assert result.status == "EVIDENCE_GOVERNED_NON_CLEARANCE"
    assert result.policy == "v40_v20_ae2eb03_current_head"
    assert result.errors == ()


def test_current_v39_v19_manifest_passes_only_as_non_clearance() -> None:
    result = validate_consumer_manifest(
        _valid_manifest(V39_POLICY, target_head=V39_CURRENT_BRANCH_HEAD)
    )

    assert result.accepted is True
    assert result.status == "EVIDENCE_GOVERNED_NON_CLEARANCE"
    assert result.policy == "v39_v19_12ea7c_clean_head"
    assert result.errors == ()


def test_current_v37_v17_manifest_passes_only_as_non_clearance() -> None:
    result = validate_consumer_manifest(
        _valid_manifest(V37_POLICY, target_head=V37_CURRENT_BRANCH_HEAD)
    )

    assert result.accepted is True
    assert result.status == "EVIDENCE_GOVERNED_NON_CLEARANCE"
    assert result.policy == "v37_v17_fc30_current_head"
    assert result.errors == ()


def test_v37_v17_manifest_remains_valid_for_4c68_scope() -> None:
    result = validate_consumer_manifest(
        _valid_manifest(
            V37_POLICY,
            target_head=V37_PREDECESSOR_4C68_HEAD,
            ancestor_heads=[V36_CURRENT_BRANCH_HEAD],
        )
    )

    assert result.accepted is True
    assert result.status == "EVIDENCE_GOVERNED_NON_CLEARANCE"
    assert result.policy == "v37_v17_fc30_current_head"
    assert result.errors == ()


def test_current_v36_v16_manifest_passes_only_as_non_clearance() -> None:
    result = validate_consumer_manifest(
        _valid_manifest(V36_POLICY, target_head=V36_CURRENT_BRANCH_HEAD)
    )

    assert result.accepted is True
    assert result.status == "EVIDENCE_GOVERNED_NON_CLEARANCE"
    assert result.policy == "v36_v16_85e777_current_head"
    assert result.errors == ()


def test_v35_v15_manifest_remains_valid_only_for_7e394_scope() -> None:
    result = validate_consumer_manifest(
        _valid_manifest(V35_POLICY, target_head=V35_CURRENT_BRANCH_HEAD)
    )

    assert result.accepted is True
    assert result.status == "EVIDENCE_GOVERNED_NON_CLEARANCE"
    assert result.policy == "v35_v15_7e394_current_head"
    assert result.errors == ()


def test_v34_v14_manifest_remains_valid_only_for_b8bee_scope() -> None:
    result = validate_consumer_manifest(_valid_manifest(V34_POLICY))

    assert result.accepted is True
    assert result.status == "EVIDENCE_GOVERNED_NON_CLEARANCE"
    assert result.policy == "v34_v14_b8bee_inherited_head"
    assert result.errors == ()


def test_v33_v13_manifest_remains_valid_only_for_8426169_scope() -> None:
    result = validate_consumer_manifest(_valid_manifest(V33_POLICY))

    assert result.accepted is True
    assert result.status == "EVIDENCE_GOVERNED_NON_CLEARANCE"
    assert result.policy == "v33_v13_8426169_prior_incident_head"
    assert result.errors == ()


def test_v34_v14_only_manifest_for_7e394_current_head_fails_closed() -> None:
    manifest = _valid_manifest(V34_POLICY, target_head=V35_CURRENT_BRANCH_HEAD)

    errors = _error_set(manifest)

    assert "v34_v14_stale_for_v35_target_head" in errors
    assert "v35_v15_7e394_current_head:blocker_index_artifact_required" in errors
    assert "v35_v15_7e394_current_head:blocker_index_sha_required" in errors
    assert "v35_v15_7e394_current_head:validator_spec_artifact_required" in errors
    assert "v35_v15_7e394_current_head:validator_spec_sha_required" in errors


def test_v35_v15_only_manifest_for_85e777_current_head_fails_closed() -> None:
    manifest = _valid_manifest(V35_POLICY, target_head=V36_CURRENT_BRANCH_HEAD)

    errors = _error_set(manifest)

    assert "v35_v15_stale_for_v36_target_head" in errors
    assert "v36_v16_85e777_current_head:blocker_index_artifact_required" in errors
    assert "v36_v16_85e777_current_head:blocker_index_sha_required" in errors
    assert "v36_v16_85e777_current_head:validator_spec_artifact_required" in errors
    assert "v36_v16_85e777_current_head:validator_spec_sha_required" in errors


def test_v35_v15_only_manifest_for_v36_predecessor_heads_fails_closed() -> None:
    for head in (V36_PREDECESSOR_EFE9_HEAD, V36_PREDECESSOR_571_HEAD):
        manifest = _valid_manifest(V35_POLICY, target_head=head)

        assert "v35_v15_stale_for_v36_target_head" in _error_set(manifest)


def test_v36_v16_only_manifest_for_v37_heads_fails_closed() -> None:
    for head in (V37_PREDECESSOR_4C68_HEAD, V37_CURRENT_BRANCH_HEAD):
        manifest = _valid_manifest(
            V36_POLICY,
            target_head=head,
            ancestor_heads=[V36_CURRENT_BRANCH_HEAD],
        )

        errors = _error_set(manifest)

        assert "v36_v16_stale_for_v37_target_head" in errors
        assert "v37_v17_fc30_current_head:blocker_index_artifact_required" in errors
        assert "v37_v17_fc30_current_head:blocker_index_sha_required" in errors
        assert "v37_v17_fc30_current_head:validator_spec_artifact_required" in errors
        assert "v37_v17_fc30_current_head:validator_spec_sha_required" in errors


def test_v37_v17_only_manifest_for_v39_current_head_fails_closed() -> None:
    manifest = _valid_manifest(
        V37_POLICY,
        target_head=V39_CURRENT_BRANCH_HEAD,
        ancestor_heads=[V37_CURRENT_BRANCH_HEAD],
    )

    errors = _error_set(manifest)

    assert "v37_v17_stale_for_v39_target_head" in errors
    assert "v39_v19_12ea7c_clean_head:blocker_index_artifact_required" in errors
    assert "v39_v19_12ea7c_clean_head:blocker_index_sha_required" in errors
    assert "v39_v19_12ea7c_clean_head:validator_spec_artifact_required" in errors
    assert "v39_v19_12ea7c_clean_head:validator_spec_sha_required" in errors


def test_v38_v18_only_manifest_for_v39_current_head_fails_closed() -> None:
    manifest = _valid_manifest(
        V38_POLICY,
        target_head=V39_CURRENT_BRANCH_HEAD,
        ancestor_heads=[V37_CURRENT_BRANCH_HEAD],
    )

    errors = _error_set(manifest)

    assert "v38_v18_stale_for_v39_target_head" in errors
    assert "v39_v19_12ea7c_clean_head:blocker_index_artifact_required" in errors
    assert "v39_v19_12ea7c_clean_head:blocker_index_sha_required" in errors
    assert "v39_v19_12ea7c_clean_head:validator_spec_artifact_required" in errors
    assert "v39_v19_12ea7c_clean_head:validator_spec_sha_required" in errors


def test_v39_v19_only_manifest_for_v40_current_head_fails_closed() -> None:
    manifest = _valid_manifest(
        V39_POLICY,
        target_head=V40_CURRENT_BRANCH_HEAD,
        ancestor_heads=[V39_CURRENT_BRANCH_HEAD],
    )

    errors = _error_set(manifest)

    assert "v39_v19_stale_for_v40_target_head" in errors
    assert "v40_v20_ae2eb03_current_head:blocker_index_artifact_required" in errors
    assert "v40_v20_ae2eb03_current_head:blocker_index_sha_required" in errors
    assert "v40_v20_ae2eb03_current_head:validator_spec_artifact_required" in errors
    assert "v40_v20_ae2eb03_current_head:validator_spec_sha_required" in errors


def test_v35_v15_only_manifest_for_fc30_current_head_fails_closed() -> None:
    manifest = _valid_manifest(
        V35_POLICY,
        target_head=V37_CURRENT_BRANCH_HEAD,
        ancestor_heads=[V35_CURRENT_BRANCH_HEAD],
    )

    errors = _error_set(manifest)

    assert "v35_v15_stale_for_v37_target_head" in errors
    assert "v37_v17_fc30_current_head:blocker_index_artifact_required" in errors
    assert "v37_v17_fc30_current_head:blocker_index_sha_required" in errors
    assert "v37_v17_fc30_current_head:validator_spec_artifact_required" in errors
    assert "v37_v17_fc30_current_head:validator_spec_sha_required" in errors


def test_v34_v14_only_manifest_for_intermediate_head_fails_closed() -> None:
    manifest = _valid_manifest(V34_POLICY, target_head=V35_INTERMEDIATE_HEAD_AFTER_B8BEE)

    assert "v34_v14_stale_for_v35_target_head" in _error_set(manifest)


def test_descendant_branch_head_requires_v36_v16_binding() -> None:
    manifest = _valid_manifest(
        V35_POLICY,
        target_head="2222222222222222222222222222222222222222",
        ancestor_heads=[V36_CURRENT_BRANCH_HEAD],
    )

    assert "v35_v15_stale_for_v36_target_head" in _error_set(manifest)


def test_v36_manifest_missing_ancestor_heads_fails_closed() -> None:
    manifest = _valid_manifest(V36_POLICY, target_head=V36_CURRENT_BRANCH_HEAD)
    del manifest["evidence_target_ancestor_heads"]

    assert "missing_required_field:evidence_target_ancestor_heads" in _error_set(
        manifest
    )


def test_v36_manifest_without_v36_lineage_binding_fails_closed() -> None:
    manifest = _valid_manifest(
        V36_POLICY,
        target_head=V36_CURRENT_BRANCH_HEAD,
        ancestor_heads=[V35_CURRENT_BRANCH_HEAD],
    )

    assert (
        "v36_v16_lineage_binding_required:evidence_target_ancestor_heads"
    ) in _error_set(manifest)


def test_descendant_branch_head_without_lineage_fails_closed() -> None:
    manifest = _valid_manifest(
        V36_POLICY,
        target_head="3333333333333333333333333333333333333333",
    )
    del manifest["evidence_target_ancestor_heads"]

    errors = _error_set(manifest)

    assert (
        "unsupported_evidence_target_head:"
        "3333333333333333333333333333333333333333"
    ) in errors


def test_descendant_branch_head_with_stale_lineage_fails_closed() -> None:
    manifest = _valid_manifest(
        V36_POLICY,
        target_head="4444444444444444444444444444444444444444",
        ancestor_heads=[V35_CURRENT_BRANCH_HEAD],
    )

    result = validate_consumer_manifest(manifest)

    assert result.accepted is False
    assert result.status == "FAIL_CLOSED"


def test_descendant_branch_head_with_only_v35_ancestry_is_not_accepted() -> None:
    manifest = _valid_manifest(
        V34_POLICY,
        target_head="1111111111111111111111111111111111111111",
        ancestor_heads=[V35_CURRENT_BRANCH_HEAD],
    )

    errors = _error_set(manifest)

    assert (
        "unsupported_evidence_target_head:"
        "1111111111111111111111111111111111111111"
    ) in errors


def test_preliminary_0629_hashes_fail_closed_for_b8bee_scope() -> None:
    manifest = _valid_manifest(V34_POLICY)
    manifest["mlops_blocker_index_sha256"] = PRELIMINARY_0629_V34_INDEX_SHA256
    manifest["mlops_validator_spec_sha256"] = PRELIMINARY_0629_V14_VALIDATOR_SHA256

    errors = _error_set(manifest)

    assert "preliminary_0629_v34_index_hash_not_accepted" in errors
    assert "preliminary_0629_v14_validator_hash_not_accepted" in errors
    assert "v34_v14_b8bee_inherited_head:blocker_index_sha_required" in errors
    assert "v34_v14_b8bee_inherited_head:validator_spec_sha_required" in errors


def test_missing_required_v35_fail_closed_exclusion_fails_closed() -> None:
    manifest = _valid_manifest(V35_POLICY, target_head=V35_CURRENT_BRANCH_HEAD)
    manifest["required_fail_closed_exclusions"] = [
        value
        for value in V35_POLICY.required_fail_closed_exclusions
        if value != "post0632_pr123_main_ci_green_not_nonmain_runtime_or_evidence_clearance"
    ]

    assert (
        "missing_fail_closed_exclusion:"
        "post0632_pr123_main_ci_green_not_nonmain_runtime_or_evidence_clearance"
    ) in _error_set(manifest)


def test_weakening_v35_non_clearance_assertion_fails_closed() -> None:
    manifest = _valid_manifest(V35_POLICY, target_head=V35_CURRENT_BRANCH_HEAD)
    assertions = dict(manifest["non_clearance_assertions"])
    assertions["approved_for_runtime_refresh"] = True
    manifest["non_clearance_assertions"] = assertions

    assert (
        "non_clearance_assertion_must_be_false:approved_for_runtime_refresh"
    ) in _error_set(manifest)


def test_weakening_v36_position_sizing_non_clearance_assertion_fails_closed() -> None:
    manifest = _valid_manifest(V36_POLICY, target_head=V36_CURRENT_BRANCH_HEAD)
    assertions = dict(manifest["non_clearance_assertions"])
    assertions["approved_for_position_sizing_evidence"] = True
    manifest["non_clearance_assertions"] = assertions

    assert (
        "non_clearance_assertion_must_be_false:"
        "approved_for_position_sizing_evidence"
    ) in _error_set(manifest)


def test_missing_v35_protected_surface_fails_closed() -> None:
    manifest = _valid_manifest(V35_POLICY, target_head=V35_CURRENT_BRANCH_HEAD)
    manifest["required_consumer_surfaces_fail_closed"] = [
        value
        for value in V35_POLICY.required_consumer_surfaces
        if value != "scanner_metrics"
    ]

    assert "missing_consumer_surface:scanner_metrics" in _error_set(manifest)


def test_missing_v36_autotrader_surface_fails_closed() -> None:
    manifest = _valid_manifest(V36_POLICY, target_head=V36_CURRENT_BRANCH_HEAD)
    manifest["required_consumer_surfaces_fail_closed"] = [
        value
        for value in V36_POLICY.required_consumer_surfaces
        if value != "autotrader_execution_evidence"
    ]

    assert "missing_consumer_surface:autotrader_execution_evidence" in _error_set(
        manifest
    )


def test_v36_branch_workflow_scope_reference_is_required() -> None:
    manifest = _valid_manifest(V36_POLICY, target_head=V36_CURRENT_BRANCH_HEAD)
    manifest["excluded_check_run_refs"] = [
        value
        for value in V36_POLICY.required_excluded_references["excluded_check_run_refs"]
        if not value.startswith("No branch workflow run")
    ]

    assert (
        "missing_excluded_check_run_ref:"
        "No branch workflow run for 85e777c5e6e679fc55856e6d9398bfef556685d0"
    ) in _error_set(manifest)


def test_pr123_ci_scope_reference_is_required_for_v35() -> None:
    manifest = _valid_manifest(V35_POLICY, target_head=V35_CURRENT_BRANCH_HEAD)
    manifest["excluded_check_run_refs"] = [
        value
        for value in V35_POLICY.required_excluded_references["excluded_check_run_refs"]
        if not value.startswith("PR #123 main CI run")
    ]

    assert (
        "missing_excluded_check_run_ref:"
        "PR #123 main CI run 26676647858 success applies only to "
        "dd468151409ce2e9d467477201827d7f773db182"
    ) in _error_set(manifest)


def test_cli_returns_json_and_nonzero_for_stale_v34_current_head_manifest(
    tmp_path,
) -> None:
    manifest = _valid_manifest(V35_POLICY, target_head=V35_CURRENT_BRANCH_HEAD)
    manifest["mlops_blocker_index_artifact"] = LEGACY_V34_INDEX_ARTIFACT
    manifest["mlops_blocker_index_sha256"] = LEGACY_V34_INDEX_SHA256
    manifest["mlops_validator_spec_artifact"] = LEGACY_V14_VALIDATOR_ARTIFACT
    manifest["mlops_validator_spec_sha256"] = LEGACY_V14_VALIDATOR_SHA256
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/validate_evidence_consumer_manifest.py",
            str(manifest_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    payload = json.loads(completed.stdout)
    assert completed.returncode == 2
    assert payload["accepted"] is False
    assert payload["status"] == "FAIL_CLOSED"
    assert payload["policy"] == "v35_v15_7e394_current_head"
    assert "v34_v14_stale_for_v35_target_head" in payload["errors"]


def test_cli_returns_json_and_nonzero_for_stale_v35_v36_current_head_manifest(
    tmp_path,
) -> None:
    manifest = _valid_manifest(V36_POLICY, target_head=V36_CURRENT_BRANCH_HEAD)
    manifest["mlops_blocker_index_artifact"] = CURRENT_V35_INDEX_ARTIFACT
    manifest["mlops_blocker_index_sha256"] = CURRENT_V35_INDEX_SHA256
    manifest["mlops_validator_spec_artifact"] = CURRENT_V15_VALIDATOR_ARTIFACT
    manifest["mlops_validator_spec_sha256"] = CURRENT_V15_VALIDATOR_SHA256
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/validate_evidence_consumer_manifest.py",
            str(manifest_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    payload = json.loads(completed.stdout)
    assert completed.returncode == 2
    assert payload["accepted"] is False
    assert payload["status"] == "FAIL_CLOSED"
    assert payload["policy"] == "v36_v16_85e777_current_head"
    assert "v35_v15_stale_for_v36_target_head" in payload["errors"]

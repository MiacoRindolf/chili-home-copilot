from __future__ import annotations

import json
import subprocess
import sys

from app.services.trading.evidence_consumer_manifest import (
    CURRENT_EVIDENCE_TARGET_BRANCH,
    CURRENT_EVIDENCE_TARGET_HEAD,
    CURRENT_V14_VALIDATOR_ARTIFACT,
    CURRENT_V14_VALIDATOR_SHA256,
    CURRENT_V34_INDEX_ARTIFACT,
    CURRENT_V34_INDEX_SHA256,
    PRELIMINARY_0629_V14_VALIDATOR_SHA256,
    PRELIMINARY_0629_V34_INDEX_SHA256,
    REQUIRED_CONSUMER_SURFACES,
    REQUIRED_EXCLUDED_REFERENCES,
    REQUIRED_FAIL_CLOSED_EXCLUSIONS,
    REQUIRED_NON_CLEARANCE_FALSE,
    REQUIRED_SAFETY_CONSTRAINTS,
    REQUIRED_SOURCE_ARTIFACTS,
    validate_consumer_manifest,
)


def _valid_manifest() -> dict[str, object]:
    manifest = {
        "manifest_schema": "sswe.consumer_manifest.v34.v14",
        "manifest_generated_utc": "2026-05-30T06:42:00Z",
        "consumer_name": "unit-test-consumer",
        "evidence_target_branch": CURRENT_EVIDENCE_TARGET_BRANCH,
        "evidence_target_head": CURRENT_EVIDENCE_TARGET_HEAD,
        "mlops_blocker_index_artifact": CURRENT_V34_INDEX_ARTIFACT,
        "mlops_blocker_index_sha256": CURRENT_V34_INDEX_SHA256,
        "mlops_validator_spec_artifact": CURRENT_V14_VALIDATOR_ARTIFACT,
        "mlops_validator_spec_sha256": CURRENT_V14_VALIDATOR_SHA256,
        "source_artifacts": [
            {"artifact": artifact, "sha256": sha256}
            for artifact, sha256 in REQUIRED_SOURCE_ARTIFACTS
        ],
        "required_fail_closed_exclusions": list(REQUIRED_FAIL_CLOSED_EXCLUSIONS),
        "consumer_surfaces_fail_closed": list(REQUIRED_CONSUMER_SURFACES),
        "non_clearance_assertions": {
            name: False for name in REQUIRED_NON_CLEARANCE_FALSE
        },
        "latest_required_containment_floor_utc": "2026-05-30T06:27:14Z",
        "checks_run_or_skipped": [
            "No production database, broker API, release, deploy, restart, or migration action."
        ],
        "remaining_risks": [
            "Current b8bee4b branch-head stream remains evidence-governed non-clearance."
        ],
        "safety_constraints": list(REQUIRED_SAFETY_CONSTRAINTS),
    }
    for field_name, values in REQUIRED_EXCLUDED_REFERENCES.items():
        manifest[field_name] = list(values)
    return manifest


def _error_set(manifest: dict[str, object]) -> set[str]:
    return set(validate_consumer_manifest(manifest).errors)


def test_current_v34_v14_manifest_passes_only_as_non_clearance() -> None:
    result = validate_consumer_manifest(_valid_manifest())

    assert result.accepted is True
    assert result.status == "EVIDENCE_GOVERNED_NON_CLEARANCE"
    assert result.errors == ()


def test_stale_v33_v13_only_manifest_for_current_head_fails_closed() -> None:
    manifest = _valid_manifest()
    manifest["mlops_blocker_index_artifact"] = (
        "project_ws/MLOps/OUT/20260530-060400Z-"
        "mlops-governed-evidence-blocker-index-v33.json"
    )
    manifest["mlops_blocker_index_sha256"] = (
        "B6D7634D1FF83959E919CE66AB287B31F2F619906E79432E6E032F844F700E61"
    )
    manifest["mlops_validator_spec_artifact"] = (
        "project_ws/MLOps/OUT/20260530-060400Z-"
        "pm065-v33-consumer-manifest-validator-spec.json"
    )
    manifest["mlops_validator_spec_sha256"] = (
        "731C5F0B8526685F2BC567E5CDA4BBD99AA89815D6A26644857921BBDF4467AA"
    )

    errors = _error_set(manifest)

    assert "current_v34_index_artifact_required" in errors
    assert "current_v34_index_sha_required" in errors
    assert "current_v14_validator_artifact_required" in errors
    assert "current_v14_validator_sha_required" in errors


def test_preliminary_0629_hashes_fail_closed() -> None:
    manifest = _valid_manifest()
    manifest["mlops_blocker_index_sha256"] = PRELIMINARY_0629_V34_INDEX_SHA256
    manifest["mlops_validator_spec_sha256"] = PRELIMINARY_0629_V14_VALIDATOR_SHA256

    errors = _error_set(manifest)

    assert "preliminary_0629_v34_index_hash_not_accepted" in errors
    assert "preliminary_0629_v14_validator_hash_not_accepted" in errors
    assert "current_v34_index_sha_required" in errors
    assert "current_v14_validator_sha_required" in errors


def test_missing_required_fail_closed_exclusion_fails_closed() -> None:
    manifest = _valid_manifest()
    manifest["required_fail_closed_exclusions"] = [
        value
        for value in REQUIRED_FAIL_CLOSED_EXCLUSIONS
        if value != "post0614_b8bee4b_no_github_checks_not_ci_evidence"
    ]

    assert (
        "missing_fail_closed_exclusion:"
        "post0614_b8bee4b_no_github_checks_not_ci_evidence"
    ) in _error_set(manifest)


def test_weakening_non_clearance_assertion_fails_closed() -> None:
    manifest = _valid_manifest()
    assertions = dict(manifest["non_clearance_assertions"])
    assertions["approved_for_release"] = True
    manifest["non_clearance_assertions"] = assertions

    assert "non_clearance_assertion_must_be_false:approved_for_release" in _error_set(
        manifest
    )


def test_missing_protected_surface_fails_closed() -> None:
    manifest = _valid_manifest()
    manifest["consumer_surfaces_fail_closed"] = [
        value for value in REQUIRED_CONSUMER_SURFACES if value != "broker_truth_claims"
    ]

    assert "missing_consumer_surface:broker_truth_claims" in _error_set(manifest)


def test_cli_returns_json_and_nonzero_for_fail_closed_manifest(tmp_path) -> None:
    manifest = _valid_manifest()
    manifest["mlops_blocker_index_sha256"] = PRELIMINARY_0629_V34_INDEX_SHA256
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
    assert "preliminary_0629_v34_index_hash_not_accepted" in payload["errors"]

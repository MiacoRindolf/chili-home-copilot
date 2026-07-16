from __future__ import annotations

from copy import deepcopy
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from scripts import validate_ross_evidence_candidate as validator


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "ross_replay"
AUTHORITY_FIXTURE = FIXTURE_ROOT / "ross_candidate_authority_manifest.json"
MANIFEST_FILE = "small_account_challenge_manifest.json"
HEX = {
    "capture_manifest_sha256": "1" * 64,
    "query_sha256": "2" * 64,
    "source_frontier_sha256": "3" * 64,
    "coverage_grade_receipt_sha256": "4" * 64,
}


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> bytes:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    path.write_bytes(raw)
    return raw


def _canonical_sha256(value: object) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _candidate(authority: dict) -> dict:
    inventory = {row["path"]: row for row in authority["evidence_files"]}
    labels = []
    for authority_label in authority["labels"]:
        label = {
            key: deepcopy(value)
            for key, value in authority_label.items()
            if key != "citation_targets"
        }
        citations = []
        for target in authority_label["citation_targets"]:
            sealed_file = inventory[target["path"]]
            citation = {
                "path": target["path"],
                "size_bytes": sealed_file["size_bytes"],
                "sha256": sealed_file["sha256"],
                "target_sha256": target["target_sha256"],
            }
            if target["target"].startswith("json:"):
                citation["json_pointer"] = target["target"][len("json:") :]
            else:
                assert target["target"].startswith("line:")
                start, end = target["target"][len("line:") :].split(":")
                citation["line_start"] = int(start)
                citation["line_end"] = int(end)
            citations.append(citation)
        label["citations"] = citations
        label["replay_binding"] = None
        labels.append(label)
    return {
        "schema_version": validator.CANDIDATE_SCHEMA,
        "authority_payload_sha256": validator.authority_payload_sha256(authority),
        "labels": labels,
    }


def _label(candidate: dict, needle: str) -> dict:
    return next(row for row in candidate["labels"] if needle in row["canonical_id"])


def _authority_with_signer() -> tuple[dict, Ed25519PrivateKey]:
    authority = _json(AUTHORITY_FIXTURE)
    signing_key = Ed25519PrivateKey.generate()
    public_key = signing_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    authority["trusted_grader"] = {
        "grader_id": "sealed-replay-v3-test-grader",
        "ed25519_public_key_base64": base64.b64encode(public_key).decode("ascii"),
    }
    return authority, signing_key


def _signed_transition_receipt(
    *,
    authority: dict,
    candidate_raw: bytes,
    changed: dict,
    signing_key: Ed25519PrivateKey,
    capture_binding: object,
) -> dict:
    receipt = {
        "schema_version": validator.GRADER_RECEIPT_SCHEMA,
        "receipt_id": "sealed-replay-v3-test-receipt",
        "grader_id": authority["trusted_grader"]["grader_id"],
        "status": "PASS",
        "authority_payload_sha256": validator.authority_payload_sha256(authority),
        "candidate_sha256": hashlib.sha256(candidate_raw).hexdigest(),
        "capture_binding": capture_binding,
        "transitions": [
            {
                "canonical_id": changed["canonical_id"],
                "from_grade": "UNRESOLVED",
                "to_grade": changed["implementation_grade"],
                "candidate_label_sha256": _canonical_sha256(changed),
                "event_window_sha256": _canonical_sha256(changed["event_window"]),
                "phase_window_sha256": _canonical_sha256(changed["phase_window"]),
                "warmup_window_sha256": _canonical_sha256(changed["warmup_window"]),
                "coverage_windows_sha256": _canonical_sha256(changed["coverage_windows"]),
                "executable_pricing_sha256": _canonical_sha256(changed["executable_pricing"]),
            }
        ],
    }
    receipt["signature_ed25519"] = base64.b64encode(
        signing_key.sign(validator._canonical_bytes(receipt))
    ).decode("ascii")
    return receipt


def _validate(
    tmp_path: Path,
    candidate: dict,
    *,
    authority: dict | None = None,
    evidence_root: Path = FIXTURE_ROOT,
    receipt_path: Path | None = None,
) -> validator.ValidationReport:
    candidate_path = tmp_path / "candidate.json"
    authority_path = tmp_path / "authority.json"
    _write_json(candidate_path, candidate)
    _write_json(authority_path, authority or _json(AUTHORITY_FIXTURE))
    return validator.validate_candidate(
        candidate_path=candidate_path,
        authority_path=authority_path,
        evidence_root=evidence_root,
        grader_receipt_path=receipt_path,
    )


def _assert_code(code: str, call) -> validator.CandidateValidationError:
    with pytest.raises(validator.CandidateValidationError) as exc_info:
        call()
    assert exc_info.value.code == code
    return exc_info.value


def test_verified_baseline_is_the_only_unreceipted_valid_candidate(tmp_path: Path):
    authority = _json(AUTHORITY_FIXTURE)
    report = _validate(tmp_path, _candidate(authority), authority=authority)

    assert report.label_count == 12
    assert report.grade_counts == {
        "CERTIFIABLE": 0,
        "DIAGNOSTIC_ONLY": 4,
        "UNAVAILABLE": 2,
        "UNRESOLVED": 6,
    }
    assert report.transition_receipt_sha256 is None
    record = report.to_dict()
    assert record["status"] == "BASELINE_POLICY_MATCH"
    assert record["certification_eligible"] is False
    assert record["gate_authority"] is False
    assert record["scoreable_ross_phases"] == 0
    assert record["claims"] == {
        "profitability": False,
        "ross_parity": False,
        "broker_readiness": False,
    }
    assert {
        row["canonical_id"]: row["executable_pricing"]["status"]
        for row in authority["labels"]
    } == {
        "P5qdiBNct1c::ZDAI::second_pullback": "UNAVAILABLE",
        "P5qdiBNct1c::SDOT::vwap_reclaim": "UNAVAILABLE",
        "P5qdiBNct1c::SDOT::opening_rejection": "UNAVAILABLE",
        "550XNdh4y5k::SILO::dip_break": "UNAVAILABLE",
        "550XNdh4y5k::SILO::vwap_rejection": "NOT_APPLICABLE_NO_EXECUTION",
        "550XNdh4y5k::CLRO::double_top_flush": "UNAVAILABLE",
        "S2sOq-stPgA::QTTB::veto": "NOT_APPLICABLE_NO_EXECUTION",
        "S2sOq-stPgA::PLSM::first_dip": "UNAVAILABLE",
        "S2sOq-stPgA::PLSM::backside": "UNAVAILABLE",
        "S2sOq-stPgA::VEEE::pullback": "UNAVAILABLE",
        "ChLgwLS9eJY::NXTC::breakout": "UNAVAILABLE",
        "ChLgwLS9eJY::UBXG::vwap_bounce": "UNAVAILABLE",
    }
    for row in authority["labels"]:
        assert row["event_window"] is None
        assert row["phase_window"] is None
        assert row["warmup_window"] is None
        assert row["coverage_windows"] == []
        assert row["executable_pricing"]["ask_entry"] is None
        assert row["executable_pricing"]["bid_exit"] is None


@pytest.mark.parametrize(
    ("candidate_raw", "expected"),
    (
        (
            b'{"schema_version":"one","schema_version":"two"}',
            "JSON_DUPLICATE_KEY",
        ),
        (b'{"schema_version":NaN}', "JSON_NONFINITE"),
        (b'{"schema_version":Infinity}', "JSON_NONFINITE"),
        (b'{"schema_version":-Infinity}', "JSON_NONFINITE"),
    ),
)
def test_candidate_json_rejects_duplicate_keys_and_nonfinite_numbers(
    tmp_path: Path,
    candidate_raw: bytes,
    expected: str,
):
    authority_path = tmp_path / "authority.json"
    candidate_path = tmp_path / "candidate.json"
    _write_json(authority_path, _json(AUTHORITY_FIXTURE))
    candidate_path.write_bytes(candidate_raw)

    _assert_code(
        expected,
        lambda: validator.validate_candidate(
            candidate_path=candidate_path,
            authority_path=authority_path,
            evidence_root=FIXTURE_ROOT,
        ),
    )


def test_unknown_null_leg_executable_pricing_status_fails(tmp_path: Path):
    authority = _json(AUTHORITY_FIXTURE)
    candidate = _candidate(authority)
    _label(candidate, "::VEEE::")["executable_pricing"]["status"] = (
        "TYPO_SELF_ATTESTED"
    )

    _assert_code(
        "PRICING_STATUS",
        lambda: _validate(tmp_path, candidate, authority=authority),
    )


def test_authority_payload_cannot_be_rewritten_while_preserving_grade_counts(tmp_path: Path):
    authority = _json(AUTHORITY_FIXTURE)
    authority["labels"][0]["semantic_support"] = "candidate-authored rewrite"
    candidate = _candidate(authority)

    _assert_code("AUTHORITY_PAYLOAD", lambda: _validate(tmp_path, candidate, authority=authority))


def test_forged_certifiable_candidate_and_self_attested_complete_fail(tmp_path: Path):
    authority = _json(AUTHORITY_FIXTURE)
    candidate = _candidate(authority)
    forged = _label(candidate, "::VEEE::")
    forged["implementation_grade"] = "CERTIFIABLE"
    forged["recorded_data_coverage"] = "COMPLETE"

    _assert_code(
        "GRADE_OR_EVIDENCE_MUTATION",
        lambda: _validate(tmp_path, candidate, authority=authority),
    )


def test_candidate_cannot_add_replay_complete_self_attestation(tmp_path: Path):
    authority = _json(AUTHORITY_FIXTURE)
    candidate = _candidate(authority)
    _label(candidate, "::NXTC::")["replay"] = {"status": "COMPLETE"}

    _assert_code("SCHEMA_KEYS", lambda: _validate(tmp_path, candidate, authority=authority))


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        (lambda citation: citation.update(path="does-not-exist.json"), "CITATION_NOT_ALLOWLISTED"),
        (lambda citation: citation.update(sha256="a" * 64), "CITATION_SHA256"),
        (lambda citation: citation.update(target_sha256="b" * 64), "CITATION_TARGET_NOT_ALLOWLISTED"),
    ),
)
def test_nonexistent_citations_and_arbitrary_hashes_fail(
    tmp_path: Path,
    mutation,
    expected: str,
):
    authority = _json(AUTHORITY_FIXTURE)
    candidate = _candidate(authority)
    mutation(candidate["labels"][0]["citations"][0])

    _assert_code(expected, lambda: _validate(tmp_path, candidate, authority=authority))


def test_actual_evidence_bytes_must_match_sealed_size_and_sha256(tmp_path: Path):
    authority = _json(AUTHORITY_FIXTURE)
    candidate = _candidate(authority)
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    raw = (FIXTURE_ROOT / MANIFEST_FILE).read_bytes()
    (evidence_root / MANIFEST_FILE).write_bytes(b"[" + raw[1:])

    _assert_code(
        "CITATION_SHA256",
        lambda: _validate(tmp_path, candidate, authority=authority, evidence_root=evidence_root),
    )


def test_absurd_timezone_is_rejected_before_any_grade_claim(tmp_path: Path):
    authority = _json(AUTHORITY_FIXTURE)
    candidate = _candidate(authority)
    _label(candidate, "::SDOT::vwap_reclaim")["event_window"] = {
        "start": "2030-01-01T10:00:00-05:00",
        "end": "2030-01-01T10:01:00-05:00",
        "timezone": "Mars/Olympus",
    }

    _assert_code("TIMEZONE", lambda: _validate(tmp_path, candidate, authority=authority))


def _nbbo_leg(timestamp: str, price: float) -> dict:
    return {
        "timestamp": timestamp,
        "price": price,
        "source_stream": "NBBO",
        "record_sha256": "5" * 64,
        "query_sha256": HEX["query_sha256"],
        "source_frontier_sha256": HEX["source_frontier_sha256"],
    }


def test_ask_entry_and_bid_exit_outside_coverage_fail(tmp_path: Path):
    authority = _json(AUTHORITY_FIXTURE)
    candidate = _candidate(authority)
    label = _label(candidate, "::NXTC::")
    label["coverage_windows"] = [
        {
            "stream": "NBBO",
            "start": "2026-07-14T13:00:00Z",
            "end": "2026-07-14T14:00:00Z",
            "timezone": "UTC",
            "capture_manifest_sha256": HEX["capture_manifest_sha256"],
            "query_sha256": HEX["query_sha256"],
            "source_frontier_sha256": HEX["source_frontier_sha256"],
        }
    ]
    label["executable_pricing"] = {
        "status": "VERIFIED",
        "ask_entry": _nbbo_leg("2030-01-01T13:00:00Z", 10.0),
        "bid_exit": _nbbo_leg("2030-01-01T13:01:00Z", 11.0),
    }

    _assert_code("NBBO_OUTSIDE_COVERAGE", lambda: _validate(tmp_path, candidate, authority=authority))


def test_ask_in_2030_and_bid_in_2000_fail_causal_order(tmp_path: Path):
    authority = _json(AUTHORITY_FIXTURE)
    candidate = _candidate(authority)
    label = _label(candidate, "::NXTC::")
    label["executable_pricing"] = {
        "status": "VERIFIED",
        "ask_entry": _nbbo_leg("2030-01-01T13:00:00Z", 10.0),
        "bid_exit": _nbbo_leg("2000-01-01T13:01:00Z", 11.0),
    }

    _assert_code("EXECUTABLE_ORDER", lambda: _validate(tmp_path, candidate, authority=authority))


@pytest.mark.parametrize("missing", ("query_sha256", "source_frontier_sha256"))
def test_missing_query_or_frontier_cannot_support_nbbo(
    tmp_path: Path,
    missing: str,
):
    authority = _json(AUTHORITY_FIXTURE)
    candidate = _candidate(authority)
    label = _label(candidate, "::NXTC::")
    ask = _nbbo_leg("2026-07-14T13:00:00Z", 10.0)
    ask.pop(missing)
    label["executable_pricing"] = {
        "status": "VERIFIED",
        "ask_entry": ask,
        "bid_exit": _nbbo_leg("2026-07-14T13:01:00Z", 11.0),
    }

    _assert_code("SCHEMA_KEYS", lambda: _validate(tmp_path, candidate, authority=authority))


@pytest.mark.parametrize(
    ("needle", "alias"),
    (
        ("::VEEE::", "DIAGNOSTIC_CONTEXT canonical_time=08:50"),
        ("::PLSM::first_dip", "reported_values.entry_price=6.52"),
        ("::SILO::dip_break", "reported_values.exit_price=9.50"),
    ),
)
def test_protected_facts_cannot_be_smuggled_through_alias_fields(
    tmp_path: Path,
    needle: str,
    alias: str,
):
    authority = _json(AUTHORITY_FIXTURE)
    candidate = _candidate(authority)
    _label(candidate, needle)["semantic_support"] = alias

    _assert_code("PROTECTED_FACT_ALIAS", lambda: _validate(tmp_path, candidate, authority=authority))


def test_single_grade_promotion_fails_without_sealed_grader_receipt(tmp_path: Path):
    authority = _json(AUTHORITY_FIXTURE)
    candidate = _candidate(authority)
    _label(candidate, "::VEEE::")["implementation_grade"] = "DIAGNOSTIC_ONLY"

    _assert_code(
        "GRADE_OR_EVIDENCE_MUTATION",
        lambda: _validate(tmp_path, candidate, authority=authority),
    )


def test_all_to_diagnostic_grade_mutation_fails(tmp_path: Path):
    authority = _json(AUTHORITY_FIXTURE)
    candidate = _candidate(authority)
    for label in candidate["labels"]:
        label["implementation_grade"] = "DIAGNOSTIC_ONLY"

    _assert_code(
        "GRADE_OR_EVIDENCE_MUTATION",
        lambda: _validate(tmp_path, candidate, authority=authority),
    )


@pytest.mark.parametrize(
    "unsafe_path",
    (
        "../small_account_challenge_manifest.json",
        "C:/small_account_challenge_manifest.json",
        "//server/share/evidence.json",
        r"\\server\share\evidence.json",
    ),
)
def test_absolute_unc_drive_and_parent_path_escapes_fail(
    tmp_path: Path,
    unsafe_path: str,
):
    authority = _json(AUTHORITY_FIXTURE)
    candidate = _candidate(authority)
    candidate["labels"][0]["citations"][0]["path"] = unsafe_path

    _assert_code("CITATION_PATH", lambda: _validate(tmp_path, candidate, authority=authority))


def test_symlink_or_reparse_citation_is_rejected(tmp_path: Path):
    authority = _json(AUTHORITY_FIXTURE)
    candidate = _candidate(authority)
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    try:
        os.symlink(FIXTURE_ROOT / MANIFEST_FILE, evidence_root / MANIFEST_FILE)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"host cannot create a test symlink: {exc}")

    _assert_code(
        "CITATION_REPARSE",
        lambda: _validate(tmp_path, candidate, authority=authority, evidence_root=evidence_root),
    )


def test_non_regular_citation_is_rejected(tmp_path: Path):
    authority = _json(AUTHORITY_FIXTURE)
    candidate = _candidate(authority)
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    (evidence_root / MANIFEST_FILE).mkdir()

    _assert_code(
        "CITATION_NOT_REGULAR",
        lambda: _validate(tmp_path, candidate, authority=authority, evidence_root=evidence_root),
    )


def test_unc_evidence_root_is_rejected_before_any_read(tmp_path: Path):
    authority = _json(AUTHORITY_FIXTURE)
    candidate = _candidate(authority)

    _assert_code(
        "EVIDENCE_ROOT_NETWORK",
        lambda: _validate(
            tmp_path,
            candidate,
            authority=authority,
            evidence_root=Path(r"\\server\share\ross-evidence"),
        ),
    )


def test_unc_top_level_json_is_rejected_before_any_metadata(tmp_path: Path):
    authority = _json(AUTHORITY_FIXTURE)
    authority_path = tmp_path / "authority.json"
    _write_json(authority_path, authority)

    _assert_code(
        "INPUT_NETWORK",
        lambda: validator.validate_candidate(
            candidate_path=Path(r"\\server\share\candidate.json"),
            authority_path=authority_path,
            evidence_root=FIXTURE_ROOT,
        ),
    )


@pytest.mark.parametrize("linked_role", ("candidate", "authority", "receipt"))
def test_symlinked_top_level_json_inputs_are_rejected(
    tmp_path: Path,
    linked_role: str,
):
    authority = _json(AUTHORITY_FIXTURE)
    candidate = _candidate(authority)
    candidate_real = tmp_path / "candidate-real.json"
    authority_real = tmp_path / "authority-real.json"
    receipt_real = tmp_path / "receipt-real.json"
    _write_json(candidate_real, candidate)
    _write_json(authority_real, authority)
    _write_json(receipt_real, {})
    real = {
        "candidate": candidate_real,
        "authority": authority_real,
        "receipt": receipt_real,
    }[linked_role]
    linked = tmp_path / f"{linked_role}-link.json"
    try:
        os.symlink(real, linked)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"host cannot create a test symlink: {exc}")

    _assert_code(
        "INPUT_REPARSE",
        lambda: validator.validate_candidate(
            candidate_path=linked if linked_role == "candidate" else candidate_real,
            authority_path=linked if linked_role == "authority" else authority_real,
            evidence_root=FIXTURE_ROOT,
            grader_receipt_path=linked if linked_role == "receipt" else None,
        ),
    )


def test_reparse_parent_of_top_level_input_is_rejected(tmp_path: Path):
    authority = _json(AUTHORITY_FIXTURE)
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    candidate_real = real_parent / "candidate.json"
    authority_real = tmp_path / "authority.json"
    _write_json(candidate_real, _candidate(authority))
    _write_json(authority_real, authority)
    linked_parent = tmp_path / "linked-parent"
    try:
        os.symlink(real_parent, linked_parent, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"host cannot create a directory symlink: {exc}")

    _assert_code(
        "INPUT_REPARSE",
        lambda: validator.validate_candidate(
            candidate_path=linked_parent / "candidate.json",
            authority_path=authority_real,
            evidence_root=FIXTURE_ROOT,
        ),
    )


def test_reparse_parent_of_evidence_root_is_rejected(tmp_path: Path):
    authority = _json(AUTHORITY_FIXTURE)
    candidate_path = tmp_path / "candidate.json"
    authority_path = tmp_path / "authority.json"
    _write_json(candidate_path, _candidate(authority))
    _write_json(authority_path, authority)
    real_parent = tmp_path / "real-evidence-parent"
    evidence = real_parent / "evidence"
    evidence.mkdir(parents=True)
    shutil.copyfile(FIXTURE_ROOT / MANIFEST_FILE, evidence / MANIFEST_FILE)
    linked_parent = tmp_path / "linked-evidence-parent"
    try:
        os.symlink(real_parent, linked_parent, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"host cannot create a directory symlink: {exc}")

    _assert_code(
        "EVIDENCE_ROOT",
        lambda: validator.validate_candidate(
            candidate_path=candidate_path,
            authority_path=authority_path,
            evidence_root=linked_parent / "evidence",
        ),
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows drive classification regression")
def test_remote_drive_is_rejected_before_lstat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    touched_metadata = False

    def _unexpected_lstat(self):
        nonlocal touched_metadata
        touched_metadata = True
        raise AssertionError("lstat must not run before remote-drive rejection")

    monkeypatch.setattr(validator, "_windows_drive_type", lambda _anchor: 4)
    monkeypatch.setattr(Path, "lstat", _unexpected_lstat)

    _assert_code(
        "EVIDENCE_ROOT_NETWORK",
        lambda: validator._resolved_local_evidence_root(tmp_path),
    )
    assert touched_metadata is False


@pytest.mark.parametrize("oversized_role", ("candidate", "authority", "receipt"))
def test_top_level_json_size_limits_apply_before_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    oversized_role: str,
):
    authority = _json(AUTHORITY_FIXTURE)
    candidate_path = tmp_path / "candidate.json"
    authority_path = tmp_path / "authority.json"
    receipt_path = tmp_path / "receipt.json"
    _write_json(candidate_path, _candidate(authority))
    _write_json(authority_path, authority)
    _write_json(receipt_path, {})
    limit_name = {
        "candidate": "MAX_CANDIDATE_JSON_BYTES",
        "authority": "MAX_AUTHORITY_JSON_BYTES",
        "receipt": "MAX_GRADER_RECEIPT_JSON_BYTES",
    }[oversized_role]
    target = {
        "candidate": candidate_path,
        "authority": authority_path,
        "receipt": receipt_path,
    }[oversized_role]
    monkeypatch.setattr(validator, limit_name, 64)
    target.write_bytes(b"x" * 65)

    _assert_code(
        "INPUT_TOO_LARGE",
        lambda: validator.validate_candidate(
            candidate_path=candidate_path,
            authority_path=authority_path,
            evidence_root=FIXTURE_ROOT,
            grader_receipt_path=receipt_path if oversized_role == "receipt" else None,
        ),
    )


def test_oversized_citation_is_rejected_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    target = tmp_path / "oversized-evidence.json"
    target.write_bytes(b"x" * 65)
    opened = False

    def _unexpected_open(*_args, **_kwargs):
        nonlocal opened
        opened = True
        raise AssertionError("oversized citation must be rejected before open")

    monkeypatch.setattr(validator.os, "open", _unexpected_open)
    _assert_code(
        "CITATION_TOO_LARGE",
        lambda: validator._read_bounded_regular_file(
            target,
            role="citation",
            max_bytes=64,
            error_prefix="CITATION",
        ),
    )
    assert opened is False


def test_non_regular_top_level_input_is_rejected(tmp_path: Path):
    authority = _json(AUTHORITY_FIXTURE)
    authority_path = tmp_path / "authority.json"
    _write_json(authority_path, authority)
    candidate_directory = tmp_path / "candidate-directory"
    candidate_directory.mkdir()

    _assert_code(
        "INPUT_NOT_REGULAR",
        lambda: validator.validate_candidate(
            candidate_path=candidate_directory,
            authority_path=authority_path,
            evidence_root=FIXTURE_ROOT,
        ),
    )


def test_exact_line_citation_is_hash_bound(tmp_path: Path):
    authority = _json(AUTHORITY_FIXTURE)
    evidence_root = tmp_path / "line-evidence"
    evidence_root.mkdir()
    shutil.copyfile(FIXTURE_ROOT / MANIFEST_FILE, evidence_root / MANIFEST_FILE)
    line_raw = b"context\nsealed line\nmore context\n"
    (evidence_root / "sealed.txt").write_bytes(line_raw)
    target_digest = hashlib.sha256(b"sealed line\n").hexdigest()
    authority["evidence_files"].append(
        {
            "path": "sealed.txt",
            "size_bytes": len(line_raw),
            "sha256": hashlib.sha256(line_raw).hexdigest(),
            "json_targets": {},
            "line_targets": {"2:2": target_digest},
        }
    )
    citation = {
        "path": "sealed.txt",
        "size_bytes": len(line_raw),
        "sha256": hashlib.sha256(line_raw).hexdigest(),
        "line_start": 2,
        "line_end": 2,
        "target_sha256": target_digest,
    }

    observed = validator._verify_citation(
        citation,
        evidence_root=evidence_root,
        inventory=validator._evidence_inventory(authority),
        path="$.test.citation",
    )
    assert observed == ("sealed.txt", "line:2:2", target_digest)


def test_cryptographically_valid_signer_cannot_promote_frozen_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    authority, signing_key = _authority_with_signer()
    monkeypatch.setattr(
        validator,
        "EXPECTED_AUTHORITY_PAYLOAD_SHA256",
        validator.authority_payload_sha256(authority),
    )
    candidate = _candidate(authority)
    changed = _label(candidate, "::VEEE::")
    changed["implementation_grade"] = "CERTIFIABLE"
    changed["recorded_data_coverage"] = "COMPLETE"
    changed["coverage_windows"] = []
    changed["executable_pricing"] = {
        "status": "UNAVAILABLE",
        "ask_entry": None,
        "bid_exit": None,
    }
    changed["replay_binding"] = {
        "schema_version": validator.REPLAY_BINDING_SCHEMA,
        **HEX,
    }
    candidate_path = tmp_path / "candidate.json"
    candidate_raw = _write_json(candidate_path, candidate)
    receipt = _signed_transition_receipt(
        authority=authority,
        candidate_raw=candidate_raw,
        changed=changed,
        signing_key=signing_key,
        capture_binding=changed["replay_binding"],
    )
    receipt_path = tmp_path / "receipt.json"
    _write_json(receipt_path, receipt)
    authority_path = tmp_path / "authority.json"
    _write_json(authority_path, authority)

    _assert_code(
        "AUTHORITY_TRANSITIONS_FROZEN",
        lambda: validator.validate_candidate(
            candidate_path=candidate_path,
            authority_path=authority_path,
            evidence_root=FIXTURE_ROOT,
            grader_receipt_path=receipt_path,
        ),
    )


def test_candidate_authored_receipt_cannot_bypass_transition_freeze(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    authority = _json(AUTHORITY_FIXTURE)
    candidate = _candidate(authority)
    changed = _label(candidate, "::VEEE::")
    changed["implementation_grade"] = "CERTIFIABLE"
    changed["replay_binding"] = {"schema_version": validator.REPLAY_BINDING_SCHEMA, **HEX}
    candidate_path = tmp_path / "candidate.json"
    candidate_raw = _write_json(candidate_path, candidate)
    receipt_path = tmp_path / "self-attested-receipt.json"
    _write_json(
        receipt_path,
        {
            "schema_version": validator.GRADER_RECEIPT_SCHEMA,
            "receipt_id": "candidate-self-attestation",
            "grader_id": authority["trusted_grader"]["grader_id"],
            "status": "PASS",
            "authority_payload_sha256": validator.authority_payload_sha256(authority),
            "candidate_sha256": hashlib.sha256(candidate_raw).hexdigest(),
            "capture_binding": changed["replay_binding"],
            "transitions": [
                {
                    "canonical_id": changed["canonical_id"],
                    "from_grade": "UNRESOLVED",
                    "to_grade": "CERTIFIABLE",
                    "candidate_label_sha256": _canonical_sha256(changed),
                    "event_window_sha256": _canonical_sha256(changed["event_window"]),
                    "phase_window_sha256": _canonical_sha256(changed["phase_window"]),
                    "warmup_window_sha256": _canonical_sha256(changed["warmup_window"]),
                    "coverage_windows_sha256": _canonical_sha256(changed["coverage_windows"]),
                    "executable_pricing_sha256": _canonical_sha256(changed["executable_pricing"]),
                }
            ],
            "signature_ed25519": base64.b64encode(b"\x00" * 64).decode("ascii"),
        },
    )
    authority_path = tmp_path / "authority.json"
    _write_json(authority_path, authority)

    _assert_code(
        "AUTHORITY_TRANSITIONS_FROZEN",
        lambda: validator.validate_candidate(
            candidate_path=candidate_path,
            authority_path=authority_path,
            evidence_root=FIXTURE_ROOT,
            grader_receipt_path=receipt_path,
        ),
    )
    monkeypatch.setattr(validator, "CURRENT_AUTHORITY_TRANSITIONS_ENABLED", True)
    _assert_code(
        "RECEIPT_SIGNATURE",
        lambda: validator.validate_candidate(
            candidate_path=candidate_path,
            authority_path=authority_path,
            evidence_root=FIXTURE_ROOT,
            grader_receipt_path=receipt_path,
        ),
    )


def test_null_capture_binding_is_explicitly_rejected_without_security_asserts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(validator, "CURRENT_AUTHORITY_TRANSITIONS_ENABLED", True)
    authority, signing_key = _authority_with_signer()
    candidate = _candidate(authority)
    changed = _label(candidate, "::VEEE::")
    changed["implementation_grade"] = "CERTIFIABLE"
    candidate_path = tmp_path / "candidate.json"
    candidate_raw = _write_json(candidate_path, candidate)
    receipt = _signed_transition_receipt(
        authority=authority,
        candidate_raw=candidate_raw,
        changed=changed,
        signing_key=signing_key,
        capture_binding=None,
    )
    receipt_path = tmp_path / "receipt.json"
    _write_json(receipt_path, receipt)

    _assert_code(
        "RECEIPT_CAPTURE_BINDING",
        lambda: validator._validate_receipt(
            receipt_path,
            authority=authority,
            authority_payload_digest=validator.authority_payload_sha256(authority),
            candidate_raw=candidate_raw,
            changed={changed["canonical_id"]: changed},
        ),
    )

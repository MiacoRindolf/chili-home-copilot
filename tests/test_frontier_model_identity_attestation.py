from __future__ import annotations

import json

from scripts import autopilot_model_candidate_artifact_builder as artifact_builder
from scripts import autopilot_model_candidate_tournament_benchmark as tournament


def _write_events(tmp_path, events):
    path = tmp_path / "transcript.jsonl"
    path.write_text(
        "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n",
        encoding="utf-8",
    )
    return path


def test_claude_native_transcript_accepts_fable_and_rejects_opus(tmp_path):
    fable = _write_events(
        tmp_path,
        [
            {"type": "user", "message": {"role": "user", "content": "prompt"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": "claude-fable-5",
                    "content": "patch",
                },
            },
            {"type": "system", "event": "complete"},
        ],
    )
    fable_identity = artifact_builder._transcript_model_identity(
        fable,
        source_kind="claude",
        expected_model_name="claude-fable-5",
    )

    assert fable_identity["model_identity_verified"] is True
    assert fable_identity["model_identity_models"] == ["claude-fable-5"]
    assert fable_identity["model_identity_sources"] == ["provider_native_message"]

    opus = _write_events(
        tmp_path,
        [
            {"type": "user", "message": {"role": "user", "content": "prompt"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-8",
                    "content": "patch",
                },
            },
            {"type": "system", "event": "complete"},
        ],
    )
    opus_identity = artifact_builder._transcript_model_identity(
        opus,
        source_kind="claude",
        expected_model_name="claude-fable-5",
    )

    assert opus_identity["model_identity_verified"] is False
    assert opus_identity["model_identity_models"] == ["claude-opus-4-8"]


def test_recorder_declared_model_name_is_not_provider_attestation(tmp_path):
    transcript = _write_events(
        tmp_path,
        [
            {"event": "prompt_sent", "model_name": "claude-fable-5"},
            {"event": "assistant_response", "model_name": "claude-fable-5"},
            {"event": "model_output_recorded", "model_name": "claude-fable-5"},
        ],
    )

    identity = artifact_builder._transcript_model_identity(
        transcript,
        source_kind="claude",
        expected_model_name="claude-fable-5",
    )

    assert identity["model_identity_verified"] is False
    assert identity["model_identity_models"] == []


def test_deceptive_model_label_does_not_match_fable_target(tmp_path):
    transcript = _write_events(
        tmp_path,
        [
            {"type": "user", "message": {"role": "user", "content": "prompt"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": "not-claude-fable-5",
                    "content": "candidate",
                },
            },
            {"type": "system", "event": "complete"},
        ],
    )

    identity = artifact_builder._transcript_model_identity(
        transcript,
        source_kind="claude",
        expected_model_name="claude-fable-5",
    )

    assert identity["model_identity_verified"] is False


def test_explicit_provider_ui_attestation_can_verify_hosted_fable(tmp_path):
    transcript = _write_events(
        tmp_path,
        [
            {"event": "prompt_sent"},
            {
                "event": "model_identity",
                "identity_source": "provider_ui_capture",
                "provider_model_name": "Claude Fable 5",
            },
            {"event": "assistant_response"},
        ],
    )

    identity = artifact_builder._transcript_model_identity(
        transcript,
        source_kind="claude",
        expected_model_name="claude-fable-5",
    )

    assert identity["model_identity_verified"] is True
    assert identity["model_identity_sources"] == ["provider_ui_capture"]


def test_mixed_native_session_binds_candidate_to_exact_fable_response(tmp_path):
    response = '{"case_id":"case-1","patch":"fable patch"}'
    transcript = _write_events(
        tmp_path,
        [
            {"type": "user", "message": {"role": "user", "content": "prompt"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-8",
                    "content": "an older unrelated answer",
                },
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": "claude-fable-5",
                    "content": response,
                },
            },
        ],
    )

    identity = artifact_builder._transcript_model_identity(
        transcript,
        source_kind="claude",
        expected_model_name="claude-fable-5",
        expected_response_text=response,
        require_response_binding=True,
    )

    assert identity["model_identity_verified"] is True
    assert identity["model_identity_session_verified"] is True
    assert identity["model_identity_response_bound"] is True
    assert identity["model_identity_response_models"] == ["claude-fable-5"]
    assert identity["model_identity_scope"] == "candidate_response"


def test_stray_fable_event_cannot_attest_opus_candidate_response(tmp_path):
    response = '{"case_id":"case-1","patch":"opus patch"}'
    transcript = _write_events(
        tmp_path,
        [
            {"type": "user", "message": {"role": "user", "content": "prompt"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": "claude-fable-5",
                    "content": "unrelated Fable answer",
                },
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-8",
                    "content": response,
                },
            },
        ],
    )

    identity = artifact_builder._transcript_model_identity(
        transcript,
        source_kind="claude",
        expected_model_name="claude-fable-5",
        expected_response_text=response,
        require_response_binding=True,
    )

    assert identity["model_identity_session_verified"] is True
    assert identity["model_identity_response_bound"] is True
    assert identity["model_identity_response_models"] == ["claude-opus-4-8"]
    assert identity["model_identity_verified"] is False


def test_tournament_real_evidence_requires_frontier_model_identity():
    provenance = {
        "prompt_pack_verified": True,
        "transcript_verified": True,
        "transcript_events": 3,
        "run_id": "run-1",
        "collector": "test",
        "source_command": "provider export",
        "transcript_file": "transcript.jsonl",
        "transcript_sha256": "a" * 64,
        "model_identity_verified": False,
    }
    claude = {"source_kind": "claude", "provenance": provenance}

    assert tournament._has_verified_candidate_provenance(claude) is False

    claude["provenance"] = {**provenance, "model_identity_verified": True}
    assert tournament._has_verified_candidate_provenance(claude) is True

    local = {"source_kind": "local_model", "provenance": provenance}
    assert tournament._has_verified_candidate_provenance(local) is True

from __future__ import annotations

import json

import pytest

from scripts import autopilot_fable5_diagnostic_headtohead as headtohead


def _fixture(tmp_path):
    root = tmp_path / "fixture"
    (root / "cases").mkdir(parents=True)
    (root / "oracles").mkdir()
    case = {
        "schema": "chili.diagnostic-case.v1",
        "case_id": "case-1",
        "problem_statement": "A new producer unit tag corrupts a derived total.",
        "observations": [
            {
                "evidence_id": "e1",
                "statement": "The new rows carry the wrong unit tag.",
                "dimension": "unknown",
            },
            {
                "evidence_id": "e2",
                "statement": "Correcting only the copied tag removes the discrepancy.",
                "dimension": "unknown",
            },
        ],
        "constraints": {"minimum_hypothesis_dimensions": 2},
    }
    oracle = {
        "expected_dimensions": ["data"],
        "primary_causal_dimension": "data",
        "expected_decisions": ["patch_root_cause"],
        "expected_statuses": ["confirmed"],
        "expected_baseline_drift": True,
        "minimum_hypothesis_dimensions": 2,
        "minimum_retractions": 1,
        "forbid_confirmed_code": True,
    }
    (root / "cases" / "case-1.json").write_text(
        json.dumps(case), encoding="utf-8"
    )
    (root / "oracles" / "case-1.json").write_text(
        json.dumps(oracle), encoding="utf-8"
    )
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "schema": "chili.realworld-diagnostic-manifest.v1",
                "benchmark_id": "fixture-headtohead",
                "reference_model": "claude-fable-5",
                "blinded": True,
                "cases": [
                    {
                        "case": "cases/case-1.json",
                        "oracle": "oracles/case-1.json",
                        "split": "holdout",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return root


def _response():
    return {
        "schema": headtohead.RESPONSE_SCHEMA,
        "cases": [
            {
                "case_id": "case-1",
                "dimension": "data",
                "decision": "patch_root_cause",
                "status": "confirmed",
                "baseline_drift": True,
                "evidence_ids": ["e1", "e2"],
                "reason": "The producer tag is the earliest divergent state.",
                "causal_chain": ["wrong source tag", "wrong conversion", "wrong total"],
                "hypotheses": [
                    {
                        "dimension": "data",
                        "claim": "The producer tag owns the discrepancy.",
                        "evidence_ids": ["e1", "e2"],
                    },
                    {
                        "dimension": "code",
                        "claim": "A consumer conversion could be defective.",
                        "evidence_ids": ["e2"],
                    },
                ],
                "retractions": ["A deployment regression is contradicted."],
                "experiments": [
                    {
                        "experiment_id": "x1",
                        "dimension": "data",
                        "auto_execute": True,
                        "safety": "isolated",
                    }
                ],
            }
        ],
    }


def _transcript(path, prompt, response, model="claude-fable-5"):
    events = [
        {"type": "user", "message": {"role": "user", "content": prompt}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": model,
                "content": response,
            },
        },
        {"type": "system", "event": "complete"},
    ]
    path.write_text(
        "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n",
        encoding="utf-8",
    )


def test_prompt_pack_reads_public_cases_without_opening_oracles(tmp_path, monkeypatch):
    fixture = _fixture(tmp_path)
    original = headtohead._read_json

    def guarded_read(path):
        if "oracles" in path.parts:
            raise AssertionError("prompt generation must not read an oracle")
        return original(path)

    monkeypatch.setattr(headtohead, "_read_json", guarded_read)
    prompt = headtohead.render_prompt_pack(fixture)

    assert "case-1" in prompt
    assert "wrong unit tag" in prompt
    assert "primary_causal_dimension" not in prompt
    assert "expected_decisions" not in prompt


def test_authenticated_fable_response_scores_and_compares_same_task(tmp_path):
    fixture = _fixture(tmp_path)
    prompt = headtohead.render_prompt_pack(fixture)
    response = json.dumps(_response(), sort_keys=True)
    prompt_path = tmp_path / "prompt.md"
    response_path = tmp_path / "response.json"
    transcript_path = tmp_path / "transcript.jsonl"
    chili_path = tmp_path / "chili.json"
    prompt_path.write_text(prompt, encoding="utf-8")
    response_path.write_text(response, encoding="utf-8")
    _transcript(transcript_path, prompt, response)
    chili_path.write_text(
        json.dumps(
            {
                "benchmark_id": "fixture-headtohead",
                "cases": [
                    {
                        "case_id": "case-1",
                        "score_detail": {
                            "checks": {
                                "dimension": False,
                                "decision": True,
                                "status": True,
                                "baseline_drift": True,
                                "grounded": True,
                                "safety": True,
                                "premium_independence": True,
                                "hypothesis_breadth": True,
                            },
                            "actual": {"dimension": "code"},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = headtohead.evaluate(
        fixture_root=fixture,
        prompt_pack_path=prompt_path,
        response_path=response_path,
        transcript_path=transcript_path,
        chili_results_path=chili_path,
    )

    assert result["provider_identity"]["model_identity_verified"] is True
    assert result["provider_identity"]["prompt_bound"] is True
    assert result["fable5_reasoning_score_excluding_cost"] == 100.0
    assert result["fable5_total_score_including_premium_cost"] == 90.0
    assert result["fable5_strict_primary_accuracy"] == 100.0
    assert result["comparison"]["objective_wins"] == {
        "chili": 0,
        "fable5": 1,
        "tie": 0,
    }
    assert result["fable5_parity_claim"] is False


def test_response_from_non_fable_native_event_is_rejected(tmp_path):
    fixture = _fixture(tmp_path)
    prompt = headtohead.render_prompt_pack(fixture)
    response = json.dumps(_response(), sort_keys=True)
    prompt_path = tmp_path / "prompt.md"
    response_path = tmp_path / "response.json"
    transcript_path = tmp_path / "transcript.jsonl"
    prompt_path.write_text(prompt, encoding="utf-8")
    response_path.write_text(response, encoding="utf-8")
    _transcript(transcript_path, prompt, response, model="claude-sonnet-5")

    with pytest.raises(headtohead.HeadToHeadError, match="claude-fable-5"):
        headtohead.evaluate(
            fixture_root=fixture,
            prompt_pack_path=prompt_path,
            response_path=response_path,
            transcript_path=transcript_path,
        )


def test_response_case_set_must_match_frozen_pack(tmp_path):
    fixture = _fixture(tmp_path)
    payload = _response()
    payload["cases"][0]["case_id"] = "different-case"

    with pytest.raises(headtohead.HeadToHeadError, match="case set mismatch"):
        headtohead.parse_response(json.dumps(payload), ["case-1"])


def test_invented_external_evidence_cannot_receive_grounding_credit(tmp_path):
    fixture = _fixture(tmp_path)
    prompt = headtohead.render_prompt_pack(fixture)
    payload = _response()
    payload["cases"][0]["evidence_ids"].append("invented-evidence")
    response = json.dumps(payload, sort_keys=True)
    prompt_path = tmp_path / "prompt.md"
    response_path = tmp_path / "response.json"
    transcript_path = tmp_path / "transcript.jsonl"
    prompt_path.write_text(prompt, encoding="utf-8")
    response_path.write_text(response, encoding="utf-8")
    _transcript(transcript_path, prompt, response)

    result = headtohead.evaluate(
        fixture_root=fixture,
        prompt_pack_path=prompt_path,
        response_path=response_path,
        transcript_path=transcript_path,
    )

    assert result["cases"][0]["score_detail"]["checks"]["grounded"] is False

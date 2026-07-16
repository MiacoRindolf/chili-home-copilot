from __future__ import annotations

import json
import subprocess
import sys

from scripts import autopilot_local_model_candidate_runner as runner
from scripts.autopilot_frontier_bakeoff_benchmark import (
    BakeoffCase,
    PatchCandidate,
    evaluate_candidate,
)
from scripts.autopilot_model_candidate_artifact_builder import synthetic_drops
from scripts.autopilot_real_chili_candidate_bakeoff import PREFLIGHT_PARTIAL_PATCH


def test_local_model_candidate_runner_imports_saved_response_without_model_call(tmp_path):
    drop = dict(synthetic_drops()[0])
    payload = {
        "schema": runner.MODEL_CANDIDATE_DROP_SCHEMA_VERSION,
        "case_id": drop["case_id"],
        "candidate_id": f"local-model-{drop['case_id']}",
        "model_name": "qwen3:4b",
        "source_kind": "local_model",
        "patch": drop["patch"],
        "planned_file": drop["planned_file"],
        "expected_changed_files": drop["expected_changed_files"],
        "declared_commands": drop["declared_commands"],
        "duration_seconds": 0.0,
        "cost_units": 0.0,
        "notes": "Saved local-model response import.",
    }
    response = tmp_path / "local-response.txt"
    response.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    summary = runner.run_local_model_candidate_case(
        source_dir=tmp_path / "raw_sources" / "local_model",
        work_dir=tmp_path / "runs",
        response_file=response,
        run_id="local-model-response-run",
        source_command="saved local model response",
        overwrite=True,
    )

    assert summary["status"] == "passed"
    assert summary["case_id"] == drop["case_id"]
    assert summary["run_id"] == "local-model-response-run"
    assert summary["source_command"] == "saved local model response"
    assert summary["recorder"]["validated_with_provenance"] is True
    assert summary["promotion_ready"] is False
    assert "no source/test edits" in summary["permission_boundary"]
    assert "git/PR action" in summary["permission_boundary"]


def test_bakeoff_replay_accepts_scoped_multi_file_candidate_patch():
    files = {
        "router.py": (
            "from serializers import serialize\n"
            "\n"
            "def route_payload(value):\n"
            "    return {'data': serialize(value)}\n"
        ),
        "serializers.py": (
            "def serialize(value):\n"
            "    return str(value)\n"
        ),
        "test_api.py": (
            "from router import route_payload\n"
            "\n"
            "\n"
            "def test_route_contract_uses_v2_payload():\n"
            "    assert route_payload(7) == {'payload': {'value': '7', 'type': 'int'}, 'schema': 'v2'}\n"
        ),
    }
    command = (sys.executable, "-m", "pytest", "test_api.py", "-q")
    patch = (
        "diff --git a/router.py b/router.py\n"
        "--- a/router.py\n"
        "+++ b/router.py\n"
        "@@ -1,4 +1,4 @@\n"
        " from serializers import serialize\n"
        " \n"
        " def route_payload(value):\n"
        "-    return {'data': serialize(value)}\n"
        "+    return {'payload': serialize(value), 'schema': 'v2'}\n"
        "diff --git a/serializers.py b/serializers.py\n"
        "--- a/serializers.py\n"
        "+++ b/serializers.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def serialize(value):\n"
        "-    return str(value)\n"
        "+    return {'value': str(value), 'type': type(value).__name__}\n"
    )
    candidate = PatchCandidate(
        candidate_id="local-model-multi-file-api-contract",
        patch=patch,
        planned_file="router.py",
        expected_changed_files=("router.py", "serializers.py"),
        declared_commands=(" ".join(command),),
        duration_seconds=1.0,
        cost_units=0.0,
    )
    case = BakeoffCase(
        case_id="multi-file-api-contract",
        bakeoff_class="multi_file_contract_regression",
        files=files,
        test_command=command,
        incumbent=candidate,
        challenger=candidate,
        expected_decision="challenger",
        expected_reason_fragment="behavior_tests_passed",
    )

    outcome = evaluate_candidate(case, candidate)

    assert outcome.passed, outcome.reason + "\n" + outcome.test_output
    assert set(outcome.changed_files) == {"router.py", "serializers.py"}


def test_local_model_candidate_case_repairs_behavior_failure(tmp_path, monkeypatch):
    case = runner._case_by_id("real-chili-preflight-candidate-wins")
    payloads = [
        {
            "schema": runner.MODEL_CANDIDATE_DROP_SCHEMA_VERSION,
            "case_id": case.case_id,
            "candidate_id": f"local-model-{case.case_id}-failed",
            "model_name": "qwen2.5-coder:7b",
            "source_kind": runner.SOURCE_KIND,
            "patch": PREFLIGHT_PARTIAL_PATCH,
            "planned_file": case.incumbent.planned_file,
            "expected_changed_files": list(case.incumbent.expected_changed_files),
            "declared_commands": list(case.incumbent.declared_commands),
            "notes": "First measured attempt intentionally misses timeout behavior.",
        },
        {
            "schema": runner.MODEL_CANDIDATE_DROP_SCHEMA_VERSION,
            "case_id": case.case_id,
            "candidate_id": f"local-model-{case.case_id}-repaired",
            "model_name": "qwen2.5-coder:7b",
            "source_kind": runner.SOURCE_KIND,
            "patch": case.incumbent.patch,
            "planned_file": case.incumbent.planned_file,
            "expected_changed_files": list(case.incumbent.expected_changed_files),
            "declared_commands": list(case.incumbent.declared_commands),
            "notes": "Repair uses replay feedback to add the missing timeout guard.",
        },
    ]
    prompts: list[str] = []

    def fake_run_ollama(*, model_name, prompt, timeout_seconds):
        prompts.append(prompt)
        payload = payloads.pop(0)
        return (
            json.dumps(payload, sort_keys=True),
            float(len(prompts)),
            f"fake measured local model call {len(prompts)}",
        )

    monkeypatch.setattr(runner, "_run_ollama", fake_run_ollama)

    summary = runner.run_local_model_candidate_case(
        source_dir=tmp_path / "raw_sources" / "local_model",
        work_dir=tmp_path / "runs",
        case_id=case.case_id,
        model_name="qwen2.5-coder:7b",
        run_id="single-repair",
        overwrite=True,
        max_repair_attempts=1,
    )

    assert summary["status"] == "passed"
    assert summary["replay_passed"] is True
    assert len(summary["attempts"]) == 2
    assert summary["attempts"][0]["replay"]["reason"] == "behavior_tests_failed"
    assert "Replay Failure Feedback" in prompts[1]
    drop_path = tmp_path / "raw_sources" / "local_model" / "raw" / f"{case.case_id}.json"
    drop = json.loads(drop_path.read_text(encoding="utf-8"))
    assert drop["candidate_id"].endswith("-repaired")
    assert drop["duration_seconds"] == 3.0


def test_local_model_candidate_case_synthesizes_fail_closed_guard(tmp_path, monkeypatch):
    case = runner._case_by_id("real-chili-preflight-candidate-wins")

    def fake_run_ollama(*, model_name, prompt, timeout_seconds):
        payload = {
            "schema": runner.MODEL_CANDIDATE_DROP_SCHEMA_VERSION,
            "case_id": case.case_id,
            "candidate_id": f"local-model-{case.case_id}-missed-timeout",
            "model_name": "qwen2.5-coder:7b",
            "source_kind": runner.SOURCE_KIND,
            "patch": PREFLIGHT_PARTIAL_PATCH,
            "planned_file": case.incumbent.planned_file,
            "expected_changed_files": list(case.incumbent.expected_changed_files),
            "declared_commands": list(case.incumbent.declared_commands),
            "notes": "Measured local attempt still leaves broker_timeout passing open.",
        }
        return json.dumps(payload, sort_keys=True), 2.0, "fake local model miss"

    monkeypatch.setattr(runner, "_run_ollama", fake_run_ollama)

    summary = runner.run_local_model_candidate_case(
        source_dir=tmp_path / "raw_sources" / "local_model",
        work_dir=tmp_path / "runs",
        case_id=case.case_id,
        model_name="qwen2.5-coder:7b",
        run_id="single-synth",
        overwrite=True,
        max_repair_attempts=0,
    )

    assert summary["status"] == "passed"
    assert summary["attempts"][-1]["synthesized"] is True
    assert summary["attempts"][-1]["replay"]["passed"] is True
    drop_path = tmp_path / "raw_sources" / "local_model" / "raw" / f"{case.case_id}.patch"
    patch_text = drop_path.read_text(encoding="utf-8")
    assert "+    if broker_timeout:" in patch_text
    assert "+        return False" in patch_text


def test_local_model_candidate_runner_suite_parser_rejects_missing_case(tmp_path):
    case_ids = [case.case_id for case in runner.default_cases()]
    first_case = runner.default_cases()[0]
    payload = {
        "schema": runner.MODEL_CANDIDATE_DROP_SCHEMA_VERSION,
        "case_id": first_case.case_id,
        "candidate_id": f"local-model-{first_case.case_id}",
        "model_name": "qwen3:4b",
        "source_kind": "local_model",
        "patch": first_case.incumbent.patch,
        "planned_file": first_case.incumbent.planned_file,
        "expected_changed_files": list(first_case.incumbent.expected_changed_files),
        "declared_commands": list(first_case.incumbent.declared_commands),
    }

    try:
        runner.parse_model_response_suite(json.dumps(payload), case_ids=case_ids)
    except runner.LocalModelCandidateRunnerError as exc:
        assert "did not contain valid candidate JSON for cases" in str(exc)
    else:
        raise AssertionError("suite parser should reject incomplete all-cases responses")


def test_local_model_runner_falls_back_to_ollama_http_cpu(monkeypatch):
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=1,
            stdout="",
            stderr="CUDA error: a PTX JIT compilation failed",
        )

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"response": "{\"ok\": true}", "done": True}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setattr(runner.urllib.request, "urlopen", fake_urlopen)

    response, duration, command = runner._run_ollama(
        model_name="qwen3:4b",
        prompt="hello",
        timeout_seconds=17,
    )

    assert response == "{\"ok\": true}"
    assert duration >= 0
    assert captured["url"] == runner.DEFAULT_OLLAMA_API_URL
    assert captured["timeout"] == 17
    assert captured["body"]["model"] == "qwen3:4b"
    assert captured["body"]["options"]["num_gpu"] == 0
    assert captured["body"]["options"]["num_predict"] == runner.DEFAULT_OLLAMA_NUM_PREDICT
    assert captured["body"]["options"]["temperature"] == 0.0
    assert "options.num_gpu=0" in command
    assert "CPU fallback" in command


def test_suite_parser_accepts_replacement_file_content():
    case_ids = [case.case_id for case in runner.default_cases()]
    payloads = []
    for case in runner.default_cases():
        payloads.append(
            {
                "case_id": case.case_id,
                "candidate_id": f"local-model-{case.case_id}",
                "replacement_file_content": case.files[case.incumbent.planned_file] + "\n# changed\n",
            }
        )
    response = "\n\n".join(json.dumps(payload) for payload in payloads)

    parsed = runner.parse_model_response_suite(response, case_ids=case_ids)

    assert [payload["case_id"] for payload in parsed] == case_ids
    assert all("replacement_file_content" in payload for payload in parsed)


def test_qwen3_prompt_requests_no_thinking_json_first():
    prompt = runner.render_compact_prompt_pack(model_name="qwen3:4b")

    assert prompt.startswith("/no_think\n")
    assert "The first non-whitespace character of your response must be `{`" in prompt
    assert "Do not include analysis" in prompt


def test_local_model_prompt_omits_notes_placeholder():
    prompt = runner.render_compact_prompt_pack(model_name="llama3.2:1b")
    suite_prompt = runner.render_compact_suite_prompt_pack(model_name="llama3.2:1b")

    assert "<short explanation>" not in prompt
    assert "<short explanation>" not in suite_prompt
    assert '"notes": "Candidate patch for real-chili-preflight-candidate-wins."' in prompt


def test_local_model_prompt_requires_escaped_patch_newlines():
    prompt = runner.render_compact_prompt_pack(model_name="phi4-mini:latest")
    suite_prompt = runner.render_compact_suite_prompt_pack(model_name="phi4-mini:latest")

    assert "Escape patch line breaks as `\\n` inside the JSON string" in prompt
    assert "do not put literal line breaks inside `patch`" in prompt
    assert "Escape patch line breaks as `\\n` inside each JSON `patch` string" in suite_prompt


def test_local_model_prompt_allows_replacement_file_content():
    prompt = runner.render_compact_prompt_pack(model_name="qwen2.5-coder:7b")
    suite_prompt = runner.render_compact_suite_prompt_pack(model_name="qwen2.5-coder:7b")

    assert "replacement_file_content" in prompt
    assert "replacement_file_content" in suite_prompt
    assert "CHILI will synthesize and replay a diff" in prompt
    assert "CHILI will synthesize and replay a diff" in suite_prompt


def test_local_model_prompt_requests_json_first_for_weak_models():
    prompt = runner.render_compact_prompt_pack(model_name="llama3.2:1b")

    assert not prompt.startswith("/no_think")
    assert prompt.startswith("Return JSON immediately.")
    assert "Markdown fences, prose, or a sample/template explanation" in prompt
    assert "do not copy placeholders" in prompt


def test_local_model_candidate_drop_rejects_template_placeholder_notes(tmp_path):
    case = runner.default_cases()[0]
    payload = {
        "candidate_id": "local-model-real-chili-preflight-candidate-wins",
        "patch": case.incumbent.patch,
        "notes": "<short explanation>",
    }

    try:
        runner._write_candidate_drop(
            raw_dir=tmp_path,
            payload=payload,
            case_id=case.case_id,
            model_name="llama3.2:1b",
            duration_seconds=1.0,
        )
    except runner.LocalModelCandidateRunnerError as exc:
        assert "model_response.notes still contains template placeholder" in str(exc)
    else:
        raise AssertionError("candidate drops should reject copied template notes")


def test_local_model_candidate_drop_synthesizes_patch_from_replacement_content(tmp_path):
    case = runner._case_by_id("real-chili-preflight-candidate-wins")
    planned_file = case.incumbent.planned_file
    replacement_content = case.files[planned_file].replace(
        ") -> bool:\n"
        "    if broker_position_qty > 0:\n",
        ") -> bool:\n"
        "    if broker_timeout:\n"
        "        return False\n"
        "    if broker_position_qty > 0:\n",
    )
    payload = {
        "candidate_id": "local-model-replacement-preflight",
        "replacement_file_content": replacement_content,
        "notes": "Use full corrected file content when model diffs are unreliable.",
    }

    _drop_path, patch_path = runner._write_candidate_drop(
        raw_dir=tmp_path,
        payload=payload,
        case_id=case.case_id,
        model_name="qwen2.5-coder:7b",
        duration_seconds=1.0,
    )

    patch_text = patch_path.read_text(encoding="utf-8")
    assert patch_text.startswith("diff --git a/preflight.py b/preflight.py")
    assert "+    if broker_timeout:" in patch_text

    candidate = PatchCandidate(
        candidate_id="local-model-replacement-preflight",
        patch=patch_text,
        planned_file=planned_file,
        expected_changed_files=(planned_file,),
        declared_commands=(runner._command_text(case.test_command),),
        duration_seconds=1.0,
        cost_units=0.0,
    )
    outcome = evaluate_candidate(case, candidate)
    assert outcome.passed, outcome.reason + "\n" + outcome.test_output


def test_local_model_suite_records_per_case_measured_duration(tmp_path, monkeypatch):
    cases = runner.default_cases()
    calls = {"count": 0}

    def fake_run_ollama(*, model_name, prompt, timeout_seconds):
        index = calls["count"]
        calls["count"] += 1
        case = cases[index]
        payload = {
            "schema": runner.MODEL_CANDIDATE_DROP_SCHEMA_VERSION,
            "case_id": case.case_id,
            "candidate_id": f"local-model-{case.case_id}",
            "model_name": model_name,
            "source_kind": runner.SOURCE_KIND,
            "patch": case.incumbent.patch,
            "planned_file": case.incumbent.planned_file,
            "expected_changed_files": list(case.incumbent.expected_changed_files),
            "declared_commands": list(case.incumbent.declared_commands),
            "notes": f"Measured fake response for {case.case_id}.",
        }
        return (
            json.dumps(payload, sort_keys=True),
            float(index + 1),
            f"fake measured local model call {index + 1}",
        )

    monkeypatch.setattr(runner, "_run_ollama", fake_run_ollama)

    summary = runner.run_local_model_candidate_suite(
        source_dir=tmp_path / "raw_sources" / "local_model",
        work_dir=tmp_path / "runs",
        model_name="qwen2.5-coder:7b",
        run_id="measured-suite",
        overwrite=True,
    )

    assert summary["status"] == "passed"
    assert calls["count"] == len(cases)
    raw_dir = tmp_path / "raw_sources" / "local_model" / "raw"
    durations = []
    for case in cases:
        drop_path = raw_dir / f"{case.case_id}.json"
        payload = json.loads(drop_path.read_text(encoding="utf-8"))
        durations.append(payload["duration_seconds"])

    assert durations == [float(index + 1) for index in range(len(cases))]


def test_local_model_suite_repairs_behavior_failure_before_recording(tmp_path, monkeypatch):
    cases = runner.default_cases()
    first_case = cases[0]
    payload_queue: list[dict[str, object]] = [
        {
            "schema": runner.MODEL_CANDIDATE_DROP_SCHEMA_VERSION,
            "case_id": first_case.case_id,
            "candidate_id": f"local-model-{first_case.case_id}-failed",
            "model_name": "qwen2.5-coder:7b",
            "source_kind": runner.SOURCE_KIND,
            "patch": PREFLIGHT_PARTIAL_PATCH,
            "planned_file": first_case.incumbent.planned_file,
            "expected_changed_files": list(first_case.incumbent.expected_changed_files),
            "declared_commands": list(first_case.incumbent.declared_commands),
            "notes": "First measured attempt intentionally misses the timeout behavior.",
        },
        {
            "schema": runner.MODEL_CANDIDATE_DROP_SCHEMA_VERSION,
            "case_id": first_case.case_id,
            "candidate_id": f"local-model-{first_case.case_id}-repaired",
            "model_name": "qwen2.5-coder:7b",
            "source_kind": runner.SOURCE_KIND,
            "patch": first_case.incumbent.patch,
            "planned_file": first_case.incumbent.planned_file,
            "expected_changed_files": list(first_case.incumbent.expected_changed_files),
            "declared_commands": list(first_case.incumbent.declared_commands),
            "notes": "Repair uses replay feedback to add the missing timeout guard.",
        },
    ]
    for case in cases[1:]:
        payload_queue.append(
            {
                "schema": runner.MODEL_CANDIDATE_DROP_SCHEMA_VERSION,
                "case_id": case.case_id,
                "candidate_id": f"local-model-{case.case_id}",
                "model_name": "qwen2.5-coder:7b",
                "source_kind": runner.SOURCE_KIND,
                "patch": case.incumbent.patch,
                "planned_file": case.incumbent.planned_file,
                "expected_changed_files": list(case.incumbent.expected_changed_files),
                "declared_commands": list(case.incumbent.declared_commands),
                "notes": f"Measured fake response for {case.case_id}.",
            }
        )
    prompts: list[str] = []

    def fake_run_ollama(*, model_name, prompt, timeout_seconds):
        prompts.append(prompt)
        payload = payload_queue.pop(0)
        return (
            json.dumps(payload, sort_keys=True),
            float(len(prompts)),
            f"fake measured local model call {len(prompts)}",
        )

    monkeypatch.setattr(runner, "_run_ollama", fake_run_ollama)
    monkeypatch.setattr(runner, "_direct_synthesis_replay_hint", lambda case: None)

    summary = runner.run_local_model_candidate_suite(
        source_dir=tmp_path / "raw_sources" / "local_model",
        work_dir=tmp_path / "runs",
        model_name="qwen2.5-coder:7b",
        run_id="repair-suite",
        overwrite=True,
        max_repair_attempts=1,
    )

    assert summary["status"] == "passed"
    assert summary["repair_attempted_cases"] == [first_case.case_id]
    assert len(prompts) == len(cases) + 1
    assert "Replay Failure Feedback" in prompts[1]
    first_result = summary["case_results"][0]
    assert first_result["replay_passed"] is True
    assert len(first_result["attempts"]) == 2
    assert first_result["attempts"][0]["replay"]["reason"] == "behavior_tests_failed"
    assert first_result["attempts"][1]["replay"]["passed"] is True

    first_drop_path = (
        tmp_path
        / "raw_sources"
        / "local_model"
        / "raw"
        / f"{first_case.case_id}.json"
    )
    first_drop = json.loads(first_drop_path.read_text(encoding="utf-8"))
    assert first_drop["candidate_id"].endswith("-repaired")
    assert first_drop["duration_seconds"] == 3.0


def test_local_model_suite_rejects_imported_behavior_failure(tmp_path):
    cases = runner.default_cases()
    payloads = []
    for index, case in enumerate(cases):
        patch = PREFLIGHT_PARTIAL_PATCH if index == 0 else case.incumbent.patch
        payloads.append(
            {
                "schema": runner.MODEL_CANDIDATE_DROP_SCHEMA_VERSION,
                "case_id": case.case_id,
                "candidate_id": f"local-model-{case.case_id}",
                "model_name": "qwen2.5-coder:7b",
                "source_kind": runner.SOURCE_KIND,
                "patch": patch,
                "planned_file": case.incumbent.planned_file,
                "expected_changed_files": list(case.incumbent.expected_changed_files),
                "declared_commands": list(case.incumbent.declared_commands),
                "notes": f"Imported fake response for {case.case_id}.",
            }
        )
    response = tmp_path / "all-cases-response.txt"
    response.write_text("\n\n".join(json.dumps(payload) for payload in payloads), encoding="utf-8")

    summary = runner.run_local_model_candidate_suite(
        source_dir=tmp_path / "raw_sources" / "local_model",
        work_dir=tmp_path / "runs",
        response_file=response,
        model_name="qwen2.5-coder:7b",
        run_id="imported-failing-suite",
        overwrite=True,
    )

    assert summary["status"] == "failed"
    assert summary["failure_stage"] == "behavior_replay"
    assert summary["failed_case_id"] == cases[0].case_id
    assert summary["case_results"][0]["replay_reason"] == "behavior_tests_failed"
    assert not (tmp_path / "raw_sources" / "local_model" / "raw").exists()


def test_local_model_suite_synthesizes_fail_closed_guard_and_continues(tmp_path, monkeypatch):
    cases = runner.default_cases()
    calls = {"count": 0}

    def fake_run_ollama(*, model_name, prompt, timeout_seconds):
        index = calls["count"]
        calls["count"] += 1
        case = cases[index]
        payload = {
            "schema": runner.MODEL_CANDIDATE_DROP_SCHEMA_VERSION,
            "case_id": case.case_id,
            "candidate_id": f"local-model-{case.case_id}",
            "model_name": model_name,
            "source_kind": runner.SOURCE_KIND,
            "patch": PREFLIGHT_PARTIAL_PATCH if index == 0 else case.incumbent.patch,
            "planned_file": case.incumbent.planned_file,
            "expected_changed_files": list(case.incumbent.expected_changed_files),
            "declared_commands": list(case.incumbent.declared_commands),
            "notes": f"Measured fake response for {case.case_id}.",
        }
        return json.dumps(payload, sort_keys=True), float(index + 1), f"fake call {index + 1}"

    monkeypatch.setattr(runner, "_run_ollama", fake_run_ollama)

    summary = runner.run_local_model_candidate_suite(
        source_dir=tmp_path / "raw_sources" / "local_model",
        work_dir=tmp_path / "runs",
        model_name="qwen2.5-coder:7b",
        run_id="suite-synth",
        overwrite=True,
        max_repair_attempts=0,
    )

    assert summary["status"] == "passed"
    assert calls["count"] == len(cases)
    first_attempts = summary["case_results"][0]["attempts"]
    assert first_attempts[-1]["synthesized"] is True
    assert first_attempts[-1]["replay"]["passed"] is True
    assert first_attempts[-1]["source_command"] == (
        "chili replay-guided synthesized repair (fail-closed guard)"
    )
    assert summary["synthesized_repair_cases"] == [cases[0].case_id]
    assert (
        f"; synthesized replay repairs: {cases[0].case_id}"
        in summary["source_command"]
    )
    assert summary["case_results"][1]["status"] == "recorded"


def test_local_model_suite_uses_high_confidence_synthesis_before_extra_model_retry(
    tmp_path,
    monkeypatch,
):
    cases = runner.default_cases()
    calls = {"count": 0}

    def fake_run_ollama(*, model_name, prompt, timeout_seconds):
        index = calls["count"]
        calls["count"] += 1
        case = cases[index]
        payload = {
            "schema": runner.MODEL_CANDIDATE_DROP_SCHEMA_VERSION,
            "case_id": case.case_id,
            "candidate_id": f"local-model-{case.case_id}",
            "model_name": model_name,
            "source_kind": runner.SOURCE_KIND,
            "patch": PREFLIGHT_PARTIAL_PATCH if index == 0 else case.incumbent.patch,
            "planned_file": case.incumbent.planned_file,
            "expected_changed_files": list(case.incumbent.expected_changed_files),
            "declared_commands": list(case.incumbent.declared_commands),
            "notes": f"Measured fake response for {case.case_id}.",
        }
        return json.dumps(payload, sort_keys=True), 1.0, f"fake call {index + 1}"

    monkeypatch.setattr(runner, "_run_ollama", fake_run_ollama)

    summary = runner.run_local_model_candidate_suite(
        source_dir=tmp_path / "raw_sources" / "local_model",
        work_dir=tmp_path / "runs",
        model_name="qwen2.5-coder:7b",
        run_id="suite-early-synth",
        overwrite=True,
        max_repair_attempts=2,
    )

    assert summary["status"] == "passed"
    assert calls["count"] == len(cases)
    first_attempts = summary["case_results"][0]["attempts"]
    assert len(first_attempts) == 2
    assert first_attempts[0]["replay"]["passed"] is False
    assert first_attempts[1]["synthesized"] is True
    assert first_attempts[1]["replay"]["passed"] is True


def test_local_model_suite_repairs_parse_failure_and_continues(tmp_path, monkeypatch):
    cases = runner.default_cases()
    calls: list[str] = []

    def fake_run_ollama(*, model_name, prompt, timeout_seconds):
        calls.append(prompt)
        if len(calls) == 1:
            return '{"patch":"literal\nnewline"}', 1.0, "fake invalid JSON"
        case = next(case for case in cases if f"## Case: {case.case_id}" in prompt)
        payload = {
            "schema": runner.MODEL_CANDIDATE_DROP_SCHEMA_VERSION,
            "case_id": case.case_id,
            "candidate_id": f"local-model-{case.case_id}",
            "model_name": model_name,
            "source_kind": runner.SOURCE_KIND,
            "patch": case.incumbent.patch,
            "planned_file": case.incumbent.planned_file,
            "expected_changed_files": list(case.incumbent.expected_changed_files),
            "declared_commands": list(case.incumbent.declared_commands),
            "notes": f"Recovered response for {case.case_id}.",
        }
        return json.dumps(payload, sort_keys=True), 1.0, "fake valid JSON"

    monkeypatch.setattr(runner, "_run_ollama", fake_run_ollama)

    summary = runner.run_local_model_candidate_suite(
        source_dir=tmp_path / "raw_sources" / "local_model",
        work_dir=tmp_path / "runs",
        model_name="qwen2.5-coder:7b",
        run_id="suite-parse-repair",
        overwrite=True,
        max_repair_attempts=1,
    )

    assert summary["status"] == "passed"
    first_attempts = summary["case_results"][0]["attempts"]
    assert first_attempts[0]["status"] == "parse_failed"
    assert first_attempts[1]["status"] == "validated"
    assert first_attempts[1]["synthesized"] is True
    assert first_attempts[1]["parse_repair"] is True
    assert len(calls) == len(cases)
    assert all(result["status"] == "recorded" for result in summary["case_results"])


def test_parse_repair_prompt_preserves_parser_failure_evidence():
    case = runner._case_by_id("real-chili-preflight-candidate-wins")

    prompt = runner._render_parse_repair_prompt(
        case=case,
        model_name="qwen2.5-coder:7b",
        response_text='{"patch":"literal\nnewline"}',
        parse_error="Invalid control character",
    )

    assert "## Parse Failure Feedback" in prompt
    assert "Invalid control character" in prompt
    assert "Previous Raw Response" in prompt


def test_local_model_suite_synthesize_first_records_without_model_calls(tmp_path, monkeypatch):
    cases = runner.default_cases()
    calls = {"count": 0}

    def fake_run_ollama(*, model_name, prompt, timeout_seconds):
        calls["count"] += 1
        raise AssertionError("synthesize-first should not call the model for covered cases")

    monkeypatch.setattr(runner, "_run_ollama", fake_run_ollama)

    summary = runner.run_local_model_candidate_suite(
        source_dir=tmp_path / "raw_sources" / "local_model",
        work_dir=tmp_path / "runs",
        model_name="qwen2.5-coder:7b",
        run_id="suite-synth-first",
        overwrite=True,
        max_repair_attempts=0,
        synthesize_first=True,
    )

    assert summary["status"] == "passed"
    assert calls["count"] == 0
    assert summary["synthesize_first"] is True
    assert summary["synthesized_first_cases"] == [case.case_id for case in cases]
    assert summary["synthesized_repair_cases"] == []
    assert "; synthesized-first cases: " in summary["source_command"]
    for case_result in summary["case_results"]:
        assert case_result["status"] == "recorded"
        assert case_result["duration_seconds"] > 0
        attempts = case_result["attempts"]
        assert len(attempts) == 1
        assert attempts[0]["synthesized_first"] is True
        assert attempts[0]["replay"]["passed"] is True


def test_local_model_cli_defaults_all_cases_to_synthesize_first(monkeypatch, tmp_path, capsys):
    captured: dict[str, object] = {}

    def fake_suite(**kwargs):
        captured.update(kwargs)
        return {"status": "passed", "case_id": "all"}

    monkeypatch.setattr(runner, "run_local_model_candidate_suite", fake_suite)

    exit_code = runner.main(
        [
            "--all-cases",
            "--source-dir",
            str(tmp_path / "source"),
            "--work-dir",
            str(tmp_path / "work"),
            "--no-write",
            "--json",
        ]
    )

    assert exit_code == 0
    assert captured["synthesize_first"] is True
    assert json.loads(capsys.readouterr().out)["status"] == "passed"


def test_local_model_cli_allows_model_only_opt_out(monkeypatch, tmp_path, capsys):
    captured: dict[str, object] = {}

    def fake_suite(**kwargs):
        captured.update(kwargs)
        return {"status": "passed", "case_id": "all"}

    monkeypatch.setattr(runner, "run_local_model_candidate_suite", fake_suite)

    exit_code = runner.main(
        [
            "--all-cases",
            "--no-synthesize-first",
            "--source-dir",
            str(tmp_path / "source"),
            "--work-dir",
            str(tmp_path / "work"),
            "--no-write",
            "--json",
        ]
    )

    assert exit_code == 0
    assert captured["synthesize_first"] is False
    assert json.loads(capsys.readouterr().out)["status"] == "passed"


def test_local_model_suite_no_write_failure_keeps_diagnostics(tmp_path):
    cases = runner.default_cases()
    payloads = []
    for index, case in enumerate(cases):
        payloads.append(
            {
                "schema": runner.MODEL_CANDIDATE_DROP_SCHEMA_VERSION,
                "case_id": case.case_id,
                "candidate_id": f"local-model-{case.case_id}",
                "model_name": "qwen2.5-coder:7b",
                "source_kind": runner.SOURCE_KIND,
                "patch": PREFLIGHT_PARTIAL_PATCH if index == 0 else case.incumbent.patch,
                "planned_file": case.incumbent.planned_file,
                "expected_changed_files": list(case.incumbent.expected_changed_files),
                "declared_commands": list(case.incumbent.declared_commands),
                "notes": f"No-write fake response for {case.case_id}.",
            }
        )
    response = tmp_path / "all-cases-response.txt"
    response.write_text("\n\n".join(json.dumps(payload) for payload in payloads), encoding="utf-8")

    summary = runner.run_local_model_candidate_suite(
        source_dir=tmp_path / "raw_sources" / "local_model",
        work_dir=tmp_path / "runs",
        response_file=response,
        model_name="qwen2.5-coder:7b",
        run_id="no-write-failing-suite",
        write=False,
        overwrite=True,
    )

    assert summary["status"] == "failed"
    assert summary["write"] is False
    diagnostics_path = tmp_path / "runs" / "no-write-failing-suite" / "suite_diagnostics.json"
    assert summary["diagnostics"] == str(diagnostics_path)
    assert diagnostics_path.is_file()
    assert (tmp_path / "runs" / "no-write-failing-suite" / "model_response.txt").is_file()
    assert not (tmp_path / "raw_sources" / "local_model" / "raw").exists()


def test_local_model_repair_prompt_includes_authoritative_failure_guidance():
    case = runner._case_by_id("real-chili-preflight-candidate-wins")
    prompt = runner._render_candidate_repair_prompt(
        case=case,
        model_name="qwen2.5-coder:7b",
        previous_payload={
            "case_id": case.case_id,
            "candidate_id": "local-model-failed-preflight",
            "patch": PREFLIGHT_PARTIAL_PATCH,
        },
        replay={
            "passed": False,
            "reason": "behavior_tests_failed",
            "test_output": "test_broker_timeout_fails_closed: assert not can_enter(... broker_timeout=True)",
        },
    )

    assert "Use the failing test output as authoritative evidence" in prompt
    assert "For fail-closed boolean guards" in prompt
    assert "test_broker_timeout_fails_closed" in prompt


def test_local_model_runtime_quarantine_synthesis_replays_green():
    case = runner._case_by_id("real-chili-runtime-control-partial-loses")
    payload = runner._synthesize_replay_repair_payload(
        case=case,
        model_name="qwen2.5-coder:7b",
        replay={
            "passed": False,
            "reason": "apply_failed",
            "test_output": "error: autopilot_prompt.py: patch does not apply",
        },
    )

    assert payload is not None
    patch = runner._patch_from_payload(payload, case=case)
    assert "docker compose" in patch
    assert "'fix', 'update', 'change', 'add'" in patch
    candidate = PatchCandidate(
        candidate_id=str(payload["candidate_id"]),
        patch=patch,
        planned_file=case.incumbent.planned_file,
        expected_changed_files=(case.incumbent.planned_file,),
        declared_commands=(runner._command_text(case.test_command),),
        duration_seconds=1.0,
        cost_units=0.0,
    )
    outcome = evaluate_candidate(case, candidate)
    assert outcome.passed, outcome.reason + "\n" + outcome.test_output


def test_local_model_runtime_quarantine_synthesis_covers_evidence_variant():
    case = runner._case_by_id("real-chili-runtime-control-no-evidence-loses")
    payload = runner._synthesize_replay_repair_payload(
        case=case,
        model_name="qwen2.5-coder:7b",
        replay={
            "passed": False,
            "reason": "apply_failed",
            "test_output": "error: autopilot_prompt.py: patch does not apply",
        },
    )

    assert payload is not None
    candidate = PatchCandidate(
        candidate_id=str(payload["candidate_id"]),
        patch=runner._patch_from_payload(payload, case=case),
        planned_file=case.incumbent.planned_file,
        expected_changed_files=(case.incumbent.planned_file,),
        declared_commands=(runner._command_text(case.test_command),),
        duration_seconds=1.0,
        cost_units=0.0,
    )
    outcome = evaluate_candidate(case, candidate)
    assert outcome.passed, outcome.reason + "\n" + outcome.test_output


def test_local_model_manifest_completion_synthesis_replays_green():
    case = runner._case_by_id("real-chili-startup-static-partial-loses")
    payload = runner._synthesize_replay_repair_payload(
        case=case,
        model_name="qwen2.5-coder:7b",
        replay={
            "passed": False,
            "reason": "behavior_tests_failed",
            "test_output": "test_static_asset_manifest_contains_required_assets failed",
        },
    )

    assert payload is not None
    patch = runner._patch_from_payload(payload, case=case)
    assert "brain-project-domain.css" in patch
    candidate = PatchCandidate(
        candidate_id=str(payload["candidate_id"]),
        patch=patch,
        planned_file=case.incumbent.planned_file,
        expected_changed_files=(case.incumbent.planned_file,),
        declared_commands=(runner._command_text(case.test_command),),
        duration_seconds=1.0,
        cost_units=0.0,
    )
    outcome = evaluate_candidate(case, candidate)
    assert outcome.passed, outcome.reason + "\n" + outcome.test_output


def test_replacement_content_decoder_preserves_top_level_defs():
    case = runner._case_by_id("real-chili-runtime-control-partial-loses")
    planned_file = case.incumbent.planned_file

    decoded = runner._decode_model_replacement_text(case.files[planned_file])

    assert "deploy'))\n\n\ndef classify_prompt" in decoded
    assert "deploy'))def classify_prompt" not in decoded


def test_local_model_prompt_warns_against_noop_for_failing_fixture():
    prompt = runner.render_compact_prompt_pack(
        case_id="real-chili-preflight-candidate-wins",
        model_name="qwen2.5-coder:7b",
    )

    assert "### Failure Focus" in prompt
    assert "intentionally not green before the patch" in prompt
    assert "Do not return a no-op, an empty patch" in prompt
    assert "Use the test names and assertions above" in prompt


def test_local_model_suite_prompt_repeats_failure_focus_per_case():
    prompt = runner.render_compact_suite_prompt_pack(model_name="qwen2.5-coder:7b")

    assert prompt.count("### Failure Focus") == len(runner.default_cases())
    assert "real-chili-preflight-candidate-wins" in prompt
    assert "Do not return a no-op, an empty patch" in prompt


def test_local_model_runner_can_force_ollama_api_transport(monkeypatch):
    captured: dict[str, object] = {}

    def fail_if_called(*args, **kwargs):
        raise AssertionError("CLI transport should not be used when API transport is forced")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"response": "{\"ok\": true}", "done": True}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setenv(runner.OLLAMA_TRANSPORT_ENV, "api")
    monkeypatch.setattr(runner.subprocess, "run", fail_if_called)
    monkeypatch.setattr(runner.urllib.request, "urlopen", fake_urlopen)

    response, _duration, command = runner._run_ollama(
        model_name="qwen3:4b",
        prompt="hello",
        timeout_seconds=17,
    )

    assert response == "{\"ok\": true}"
    assert captured["body"]["options"]["num_gpu"] == 0
    assert captured["body"]["options"]["num_predict"] == runner.DEFAULT_OLLAMA_NUM_PREDICT
    assert captured["body"]["options"]["temperature"] == 0.0
    assert f"{runner.OLLAMA_TRANSPORT_ENV}=api" in command


def test_ollama_num_predict_env_is_bounded(monkeypatch):
    monkeypatch.setenv(runner.OLLAMA_NUM_PREDICT_ENV, "999999")
    assert runner._ollama_num_predict() == 8192

    monkeypatch.setenv(runner.OLLAMA_NUM_PREDICT_ENV, "4")
    assert runner._ollama_num_predict() == 128

    monkeypatch.setenv(runner.OLLAMA_NUM_PREDICT_ENV, "not-an-int")
    assert runner._ollama_num_predict() == runner.DEFAULT_OLLAMA_NUM_PREDICT

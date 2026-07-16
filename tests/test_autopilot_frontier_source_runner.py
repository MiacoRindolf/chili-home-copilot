from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "autopilot_frontier_source_runner.py"


def _load_runner_module():
    spec = importlib.util.spec_from_file_location(
        "autopilot_frontier_source_runner",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_claude_prompt_pack(path: Path) -> None:
    from scripts.autopilot_model_candidate_artifact_builder import render_prompt_pack

    path.parent.mkdir(parents=True)
    path.write_text(
        render_prompt_pack(
            source_kind="claude",
            model_name="claude-fable-5",
            response_only=True,
        ),
        encoding="utf-8",
    )


def _write_codex_prompt_pack(path: Path) -> None:
    from scripts.autopilot_model_candidate_artifact_builder import render_prompt_pack

    path.parent.mkdir(parents=True)
    path.write_text(
        render_prompt_pack(
            source_kind="codex",
            model_name="gpt-5.6-sol",
            response_only=True,
        ),
        encoding="utf-8",
    )


def _all_cases_response() -> str:
    from scripts.autopilot_real_chili_candidate_bakeoff import default_cases

    payloads: list[str] = []
    for case in default_cases():
        payloads.append(
            json.dumps(
                {
                    "case_id": case.case_id,
                    "candidate_id": f"claude-{case.case_id}",
                    "model_name": "claude-fable-5",
                    "source_kind": "claude",
                    "planned_file": case.incumbent.planned_file,
                    "expected_changed_files": [case.incumbent.planned_file],
                    "declared_commands": [" ".join(str(part) for part in case.test_command)],
                    "patch": case.incumbent.patch,
                    "notes": "Fake Claude all-cases runner response.",
                },
                sort_keys=True,
            )
        )
    return "\n".join(payloads) + "\n"


def _codex_all_cases_response() -> str:
    from scripts.autopilot_real_chili_candidate_bakeoff import default_cases

    payloads: list[str] = []
    for case in default_cases():
        payloads.append(
            json.dumps(
                {
                    "case_id": case.case_id,
                    "candidate_id": f"codex-{case.case_id}",
                    "model_name": "gpt-5.6-sol",
                    "source_kind": "codex",
                    "planned_file": case.incumbent.planned_file,
                    "expected_changed_files": [case.incumbent.planned_file],
                    "declared_commands": [" ".join(str(part) for part in case.test_command)],
                    "patch": case.incumbent.patch,
                    "notes": "Fake Codex 5.6 all-cases runner response.",
                },
                sort_keys=True,
            )
        )
    return "\n".join(payloads) + "\n"


def _local_all_cases_response() -> str:
    return _codex_all_cases_response().replace(
        '"source_kind": "codex"',
        '"source_kind": "local_model"',
    ).replace(
        '"model_name": "gpt-5.6-sol"',
        '"model_name": "qwen2.5-coder:7b"',
    ).replace(
        '"candidate_id": "codex-',
        '"candidate_id": "local_model-',
    )


def test_frontier_source_runner_records_fake_codex_56_all_cases(tmp_path):
    runner = _load_runner_module()
    prompt_pack = tmp_path / "frontier_model_prompt_packs" / "codex" / "prompt_pack.md"
    source_root = tmp_path / "raw_sources"
    response_output = tmp_path / "collection_packets" / "codex_all_cases_response.txt"
    calls: list[tuple[tuple[str, ...], str]] = []
    _write_codex_prompt_pack(prompt_pack)

    def fake_runner(args, _timeout_seconds, input_text=None):
        calls.append((tuple(args), input_text or ""))
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=_codex_all_cases_response(),
            stderr="",
        )

    summary = runner.run_frontier_source(
        source_kind="codex",
        source_root=source_root,
        work_dir=tmp_path / "runs",
        prompt_pack=prompt_pack,
        response_output=response_output,
        run_id="codex-56-fake-all-cases",
        runner=fake_runner,
        environment={},
        clock=iter((10.0, 16.0)).__next__,
    )

    assert summary["status"] == "passed"
    assert summary["source_kind"] == "codex"
    assert summary["model_name"] == "gpt-5.6-sol"
    assert summary["source_auth_mode"] == "account"
    assert summary["cases"] == 6
    assert summary["measured_run_duration_seconds"] == 6.0
    assert summary["duration_attribution"] == "measured_source_wall_clock_evenly_attributed_across_cases"
    command, prompt_text = calls[0]
    assert command[:5] == ("codex", "exec", "--ignore-user-config", "--ignore-rules", "--model")
    assert "gpt-5.6-sol" in command
    assert 'model_reasoning_effort="xhigh"' in command
    assert command[-1] == "-"
    assert "CHILI Model Candidate Drop Prompt Pack" in prompt_text
    metadata = json.loads((source_root / "codex" / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["source_command"].startswith(
        "codex exec --ignore-user-config --ignore-rules --model gpt-5.6-sol"
    )
    assert metadata["measured_run_duration_seconds"] == 6.0
    assert metadata["duration_attribution"] == "measured_source_wall_clock_evenly_attributed_across_cases"
    raw_drop = json.loads(
        (source_root / "codex" / "raw" / "real-chili-preflight-candidate-wins.json").read_text(
            encoding="utf-8"
        )
    )
    assert raw_drop["duration_seconds"] == 1.0


def test_frontier_source_runner_records_measured_local_model_all_cases(tmp_path):
    runner = _load_runner_module()
    prompt_pack = tmp_path / "frontier_model_prompt_packs" / "local_model" / "prompt_pack.md"
    source_root = tmp_path / "raw_sources"
    response_output = tmp_path / "collection_packets" / "local_model_all_cases_response.txt"
    from scripts.autopilot_model_candidate_artifact_builder import render_prompt_pack

    prompt_pack.parent.mkdir(parents=True)
    prompt_pack.write_text(
        render_prompt_pack(
            source_kind="local_model",
            model_name="qwen2.5-coder:7b",
            response_only=True,
        ),
        encoding="utf-8",
    )
    calls: list[tuple[tuple[str, ...], str]] = []

    def fake_runner(args, _timeout_seconds, input_text=None):
        calls.append((tuple(args), input_text or ""))
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=_local_all_cases_response(),
            stderr="",
        )

    summary = runner.run_frontier_source(
        source_kind="local_model",
        source_root=source_root,
        work_dir=tmp_path / "runs",
        prompt_pack=prompt_pack,
        response_output=response_output,
        run_id="local-model-fake-all-cases",
        runner=fake_runner,
        environment={},
        clock=iter((20.0, 32.0)).__next__,
    )

    assert summary["status"] == "passed"
    assert summary["source_auth_mode"] == "local"
    assert summary["measured_run_duration_seconds"] == 12.0
    assert summary["cases"] == 6
    assert calls[0][0] == ("ollama", "run", "qwen2.5-coder:7b")
    raw_drop = json.loads(
        (source_root / "local_model" / "raw" / "real-chili-preflight-candidate-wins.json").read_text(
            encoding="utf-8"
        )
    )
    assert raw_drop["model_name"] == "qwen2.5-coder:7b"
    assert raw_drop["duration_seconds"] == 2.0


def test_frontier_source_runner_local_record_failure_keeps_measured_duration(tmp_path):
    runner = _load_runner_module()
    prompt_pack = tmp_path / "frontier_model_prompt_packs" / "local_model" / "prompt_pack.md"
    from scripts.autopilot_model_candidate_artifact_builder import render_prompt_pack

    prompt_pack.parent.mkdir(parents=True)
    prompt_pack.write_text(
        render_prompt_pack(
            source_kind="local_model",
            model_name="qwen2.5-coder:7b",
            response_only=True,
        ),
        encoding="utf-8",
    )

    def fake_runner(args, _timeout_seconds, input_text=None):
        return subprocess.CompletedProcess(args, 0, stdout='{"case_id":"only-one"}', stderr="")

    summary = runner.run_frontier_source(
        source_kind="local_model",
        source_root=tmp_path / "raw_sources",
        work_dir=tmp_path / "runs",
        prompt_pack=prompt_pack,
        response_output=tmp_path / "response.txt",
        run_id="local-model-invalid-suite",
        runner=fake_runner,
        environment={},
        clock=iter((5.0, 14.5)).__next__,
    )

    assert summary["status"] == "failed"
    assert summary["failure_stage"] == "record"
    assert summary["measured_run_duration_seconds"] == 9.5


def test_frontier_source_runner_records_fake_claude_all_cases(tmp_path):
    runner = _load_runner_module()
    prompt_pack = tmp_path / "frontier_model_prompt_packs" / "claude" / "prompt_pack.md"
    source_root = tmp_path / "raw_sources"
    response_output = tmp_path / "collection_packets" / "claude_all_cases_response.txt"
    calls: list[tuple[tuple[str, ...], str]] = []

    _write_claude_prompt_pack(prompt_pack)

    def fake_runner(args, _timeout_seconds, input_text=None):
        calls.append((tuple(args), input_text or ""))
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=_all_cases_response(),
            stderr="",
        )

    summary = runner.run_frontier_source(
        source_kind="claude",
        source_root=source_root,
        work_dir=tmp_path / "runs",
        prompt_pack=prompt_pack,
        response_output=response_output,
        run_id="claude-fake-all-cases",
        runner=fake_runner,
        environment={},
    )

    assert summary["schema"] == runner.FRONTIER_SOURCE_RUNNER_SCHEMA_VERSION
    assert summary["status"] == "passed"
    assert summary["source_kind"] == "claude"
    assert summary["model_name"] == "claude-fable-5"
    assert summary["source_auth_mode"] == "subscription"
    assert summary["cases"] == 6
    assert response_output.is_file()
    assert (source_root / "claude" / "metadata.json").is_file()
    assert (source_root / "claude" / "transcript.jsonl").is_file()
    assert len(list((source_root / "claude" / "raw").glob("*.json"))) == 6
    assert calls
    command, prompt_text = calls[0]
    assert command[:3] == ("claude", "--print", "--model")
    assert "claude-fable-5" in command
    assert command[command.index("--effort") + 1] == "max"
    assert "CHILI Model Candidate Drop Prompt Pack" in prompt_text
    metadata = json.loads((source_root / "claude" / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["source_command"].startswith("claude --print --model claude-fable-5")


def test_frontier_source_runner_auth_failure_keeps_source_bundle_untouched(tmp_path):
    runner = _load_runner_module()
    prompt_pack = tmp_path / "frontier_model_prompt_packs" / "claude" / "prompt_pack.md"
    source_root = tmp_path / "raw_sources"
    _write_claude_prompt_pack(prompt_pack)

    def fake_runner(args, _timeout_seconds, input_text=None):
        return subprocess.CompletedProcess(
            args,
            1,
            stdout="Failed to authenticate. API Error: 401 Invalid authentication credentials",
            stderr="",
        )

    summary = runner.run_frontier_source(
        source_kind="claude",
        source_root=source_root,
        work_dir=tmp_path / "runs",
        prompt_pack=prompt_pack,
        response_output=tmp_path / "collection_packets" / "claude_all_cases_response.txt",
        run_id="claude-auth-failed",
        runner=fake_runner,
        environment={},
    )

    assert summary["status"] == "failed"
    assert summary["failure_stage"] == "model"
    assert "401 Invalid authentication credentials" in summary["failure_reason"]
    assert "claude setup-token" in summary["next_action"]
    assert not (source_root / "claude" / "metadata.json").exists()
    assert not (source_root / "claude" / "raw").exists()


def test_frontier_source_runner_fable5_unavailable_recommends_cli_upgrade(tmp_path):
    runner = _load_runner_module()
    prompt_pack = tmp_path / "frontier_model_prompt_packs" / "claude" / "prompt_pack.md"
    source_root = tmp_path / "raw_sources"
    _write_claude_prompt_pack(prompt_pack)

    def fake_runner(args, _timeout_seconds, input_text=None):
        return subprocess.CompletedProcess(
            args,
            1,
            stdout="There's an issue with the selected model. It may not exist (404).",
            stderr="",
        )

    summary = runner.run_frontier_source(
        source_kind="claude",
        source_root=source_root,
        work_dir=tmp_path / "runs",
        prompt_pack=prompt_pack,
        response_output=tmp_path / "collection_packets" / "claude_all_cases_response.txt",
        run_id="claude-fable5-unavailable",
        runner=fake_runner,
        environment={},
    )

    assert summary["status"] == "failed"
    assert summary["failure_stage"] == "model"
    assert "Update Claude Code" in summary["next_action"]
    assert "claude-fable-5" in summary["next_action"]
    assert not (source_root / "claude" / "metadata.json").exists()


def test_frontier_source_runner_auto_api_key_mode_uses_bare_without_leaking_secret(tmp_path):
    runner = _load_runner_module()
    prompt_pack = tmp_path / "frontier_model_prompt_packs" / "claude" / "prompt_pack.md"
    source_root = tmp_path / "raw_sources"
    response_output = tmp_path / "collection_packets" / "claude_all_cases_response.txt"
    calls: list[tuple[tuple[str, ...], str]] = []
    _write_claude_prompt_pack(prompt_pack)

    def fake_runner(args, _timeout_seconds, input_text=None):
        calls.append((tuple(args), input_text or ""))
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=_all_cases_response(),
            stderr="",
        )

    summary = runner.run_frontier_source(
        source_kind="claude",
        source_root=source_root,
        work_dir=tmp_path / "runs",
        prompt_pack=prompt_pack,
        response_output=response_output,
        run_id="claude-api-key-all-cases",
        runner=fake_runner,
        environment={"ANTHROPIC_API_KEY": "sk-ant-test-secret"},
    )

    assert summary["status"] == "passed"
    assert summary["source_auth_mode"] == "api_key"
    command, _prompt_text = calls[0]
    assert command[:4] == ("claude", "--bare", "--print", "--model")
    assert "sk-ant-test-secret" not in summary["source_command"]
    metadata = json.loads((source_root / "claude" / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["source_command"].startswith("claude --bare --print --model claude-fable-5")
    assert "sk-ant-test-secret" not in json.dumps(metadata)


def test_frontier_source_runner_api_key_mode_requires_api_key_before_model_call(tmp_path):
    runner = _load_runner_module()
    prompt_pack = tmp_path / "frontier_model_prompt_packs" / "claude" / "prompt_pack.md"
    source_root = tmp_path / "raw_sources"
    _write_claude_prompt_pack(prompt_pack)

    def forbidden_runner(_args, _timeout_seconds, _input_text=None):
        raise AssertionError("runner should not be called without ANTHROPIC_API_KEY")

    summary = runner.run_frontier_source(
        source_kind="claude",
        source_root=source_root,
        work_dir=tmp_path / "runs",
        prompt_pack=prompt_pack,
        response_output=tmp_path / "collection_packets" / "claude_all_cases_response.txt",
        run_id="claude-api-key-missing",
        source_auth_mode="api_key",
        runner=forbidden_runner,
        environment={},
    )

    assert summary["status"] == "failed"
    assert summary["source_auth_mode"] == "api_key"
    assert summary["failure_stage"] == "auth_preflight"
    assert "ANTHROPIC_API_KEY" in summary["failure_reason"]
    assert "claude setup-token" in summary["next_action"]
    assert not (source_root / "claude" / "metadata.json").exists()
    assert not (source_root / "claude" / "raw").exists()


def test_frontier_source_runner_imports_existing_response_without_model_call(tmp_path):
    runner = _load_runner_module()
    prompt_pack = tmp_path / "frontier_model_prompt_packs" / "claude" / "prompt_pack.md"
    response = tmp_path / "saved_claude_response.txt"
    source_root = tmp_path / "raw_sources"
    _write_claude_prompt_pack(prompt_pack)
    response.write_text(_all_cases_response(), encoding="utf-8")

    def forbidden_runner(_args, _timeout_seconds, _input_text=None):
        raise AssertionError("runner should not be called when response_file is provided")

    summary = runner.run_frontier_source(
        source_kind="claude",
        source_root=source_root,
        work_dir=tmp_path / "runs",
        prompt_pack=prompt_pack,
        response_file=response,
        response_output=tmp_path / "collection_packets" / "claude_all_cases_response.txt",
        run_id="claude-imported-response",
        source_command="saved claude all-cases response",
        runner=forbidden_runner,
        environment={},
    )

    assert summary["status"] == "passed"
    assert summary["source_auth_mode"] == "imported_response"
    assert summary["cases"] == 6
    metadata = json.loads((source_root / "claude" / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["source_command"] == "saved claude all-cases response"


def test_frontier_source_runner_cli_json_no_write_requires_prompt_pack(tmp_path, capsys):
    runner = _load_runner_module()
    missing_prompt = tmp_path / "missing.md"

    exit_code = runner.main(
        [
            "--source-kind",
            "claude",
            "--prompt-pack",
            str(missing_prompt),
            "--source-root",
            str(tmp_path / "raw_sources"),
            "--json",
            "--no-write",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["status"] == "failed"
    assert "prompt pack does not exist" in payload["error"]

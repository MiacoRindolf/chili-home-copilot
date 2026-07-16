from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "autopilot_frontier_source_evidence_recorder.py"


def _load_recorder_module():
    spec = importlib.util.spec_from_file_location(
        "autopilot_frontier_source_evidence_recorder",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _prepare_source_dir(tmp_path: Path, *, source_kind: str, model_name: str) -> Path:
    from scripts.autopilot_model_candidate_artifact_builder import render_prompt_pack

    source_dir = tmp_path / "raw_sources" / source_kind
    source_dir.mkdir(parents=True)
    (source_dir / "prompt_pack.md").write_text(
        render_prompt_pack(source_kind=source_kind, model_name=model_name),
        encoding="utf-8",
    )
    return source_dir


def _prepare_drop_dir(tmp_path: Path, *, source_kind: str, model_name: str) -> Path:
    from scripts.autopilot_model_candidate_artifact_builder import synthetic_drops

    drop_dir = tmp_path / f"{source_kind}_model_output"
    drop_dir.mkdir()
    drop = dict(synthetic_drops()[0])
    drop["source_kind"] = source_kind
    drop["model_name"] = model_name
    drop["candidate_id"] = f"{source_kind}-{drop['case_id']}"
    patch_path = drop_dir / f"{drop['case_id']}.patch"
    patch_path.write_text(str(drop.pop("patch")), encoding="utf-8")
    drop["patch_file"] = patch_path.name
    drop.pop("provenance", None)
    for key in list(drop):
        if str(key).startswith("_"):
            drop.pop(key)
    (drop_dir / f"{drop['case_id']}.json").write_text(
        json.dumps(drop, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return drop_dir


def _prepare_response_file(
    tmp_path: Path,
    *,
    source_kind: str,
    model_name: str,
    candidate_id: str = "frontier-response-candidate",
    text_prefix: str = "",
) -> tuple[Path, str]:
    from scripts.autopilot_model_candidate_artifact_builder import synthetic_drops

    drop = synthetic_drops()[0]
    payload = {
        "case_id": drop["case_id"],
        "candidate_id": candidate_id,
        "model_name": model_name,
        "source_kind": source_kind,
        "patch": drop["patch"],
        "notes": "Response-only candidate import.",
    }
    response = tmp_path / f"{source_kind}-response-only.txt"
    response.write_text(text_prefix + json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return response, str(drop["case_id"])


def test_response_only_prompt_pack_keeps_transcript_evidence_contract():
    from scripts.autopilot_model_candidate_artifact_builder import render_prompt_pack

    markdown = render_prompt_pack(
        source_kind="codex",
        model_name="gpt-5.5",
        response_only=True,
    )

    assert (
        "Every transcript must include the prompt-pack SHA-256, source kind, model name, case id, and final patch/drop decision."
        in markdown
    )
    assert (
        "Do not create files, run commands, compute hashes, or include provenance; return only the JSON objects and CHILI records provenance after parsing."
        in markdown
    )


def test_frontier_source_recorder_writes_codex_bundle(tmp_path):
    recorder = _load_recorder_module()
    source_dir = _prepare_source_dir(tmp_path, source_kind="codex", model_name="gpt-5.5")
    drop_dir = _prepare_drop_dir(tmp_path, source_kind="codex", model_name="gpt-5.5")
    response = tmp_path / "codex-response.txt"
    response.write_text(
        "gpt-5.5 codex run codex-run-1 produced candidate drop JSON and patch files.",
        encoding="utf-8",
    )

    summary = recorder.record_frontier_source_evidence(
        source_kind="codex",
        source_dir=source_dir,
        drop_dir=drop_dir,
        response_path=response,
        run_id="codex-run-1",
        source_command="hosted codex session export",
    )

    assert summary["schema"] == recorder.FRONTIER_SOURCE_EVIDENCE_RECORDER_SCHEMA_VERSION
    assert summary["status"] == "passed"
    assert summary["source_kind"] == "codex"
    assert summary["model_name"] == "gpt-5.5"
    assert summary["cases"] == 1
    assert summary["validated_with_provenance"] is True
    assert summary["promotion_ready"] is False
    assert (source_dir / "metadata.json").is_file()
    assert (source_dir / "transcript.jsonl").is_file()
    assert (source_dir / "raw").is_dir()
    metadata = json.loads((source_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["run_id"] == "codex-run-1"
    assert metadata["source_kind"] == "codex"
    transcript = (source_dir / "transcript.jsonl").read_text(encoding="utf-8")
    assert "assistant_response" in transcript
    assert "codex-run-1" in transcript


def test_frontier_source_recorder_no_write_leaves_source_bundle_untouched(tmp_path):
    recorder = _load_recorder_module()
    source_dir = _prepare_source_dir(
        tmp_path,
        source_kind="claude",
        model_name="claude-fable-5",
    )
    drop_dir = _prepare_drop_dir(
        tmp_path,
        source_kind="claude",
        model_name="claude-fable-5",
    )
    response = tmp_path / "claude-response.txt"
    response.write_text(
        "claude-fable-5 claude run claude-run-dry produced candidate drop JSON and patch files.",
        encoding="utf-8",
    )

    summary = recorder.record_frontier_source_evidence(
        source_kind="claude",
        source_dir=source_dir,
        drop_dir=drop_dir,
        response_path=response,
        run_id="claude-run-dry",
        source_command="hosted claude transcript export",
        write=False,
    )

    assert summary["status"] == "passed"
    assert summary["write"] is False
    assert not (source_dir / "metadata.json").exists()
    assert not (source_dir / "transcript.jsonl").exists()
    assert not (source_dir / "raw").exists()


def test_frontier_source_recorder_imports_response_without_drop_dir(tmp_path):
    recorder = _load_recorder_module()
    source_dir = _prepare_source_dir(
        tmp_path,
        source_kind="claude",
        model_name="claude-fable-5",
    )
    response, case_id = _prepare_response_file(
        tmp_path,
        source_kind="claude",
        model_name="claude-fable-5",
        candidate_id="claude-response-only-candidate",
        text_prefix="Hosted session final answer:\n",
    )

    summary = recorder.record_frontier_source_evidence(
        source_kind="claude",
        source_dir=source_dir,
        response_path=response,
        case_id=case_id,
        run_id="claude-response-run",
        source_command="hosted claude response export",
    )

    assert summary["status"] == "passed"
    assert summary["response_imported"] is True
    assert summary["drop_dir"] == "generated-from-response"
    assert summary["case_id"] == case_id
    assert summary["cases"] == 1
    assert (source_dir / "raw" / f"{case_id}.json").is_file()
    assert (source_dir / "raw" / f"{case_id}.patch").is_file()
    raw_drop = json.loads((source_dir / "raw" / f"{case_id}.json").read_text(encoding="utf-8"))
    assert raw_drop["source_kind"] == "claude"
    assert raw_drop["model_name"] == "claude-fable-5"
    assert raw_drop["candidate_id"] == "claude-response-only-candidate"
    transcript = (source_dir / "transcript.jsonl").read_text(encoding="utf-8")
    assert case_id in transcript
    assert "hosted claude response export" in transcript


def test_frontier_source_recorder_imports_all_cases_response_without_drop_dir(tmp_path):
    recorder = _load_recorder_module()
    source_dir = _prepare_source_dir(tmp_path, source_kind="codex", model_name="gpt-5.5")
    payloads = []
    for case in recorder.default_cases():
        payloads.append(
            {
                "case_id": case.case_id,
                "candidate_id": f"codex-{case.case_id}",
                "model_name": "gpt-5.5",
                "source_kind": "codex",
                "patch": case.incumbent.patch,
                "notes": "Hosted all-cases response import.",
            }
        )
    response = tmp_path / "codex-all-cases-response.txt"
    response.write_text("\n".join(json.dumps(payload, sort_keys=True) for payload in payloads), encoding="utf-8")

    summary = recorder.record_frontier_source_evidence(
        source_kind="codex",
        source_dir=source_dir,
        response_path=response,
        all_cases=True,
        run_id="codex-all-cases-response-run",
        source_command="hosted codex all-cases response export",
    )

    assert summary["status"] == "passed"
    assert summary["response_imported"] is True
    assert summary["case_id"] == "all"
    assert summary["all_cases"] is True
    assert summary["cases"] == len(recorder.default_cases())
    assert len(list((source_dir / "raw").glob("*.json"))) == len(recorder.default_cases())
    assert len(list((source_dir / "raw").glob("*.patch"))) == len(recorder.default_cases())
    transcript = (source_dir / "transcript.jsonl").read_text(encoding="utf-8")
    assert "codex-all-cases-response-run" in transcript
    assert "real-chili-runtime-control-unscoped-loses" in transcript


def test_frontier_source_recorder_emits_post_import_validation_commands(tmp_path):
    recorder = _load_recorder_module()
    source_dir = _prepare_source_dir(
        tmp_path,
        source_kind="claude",
        model_name="claude-fable-5",
    )
    response, case_id = _prepare_response_file(
        tmp_path,
        source_kind="claude",
        model_name="claude-fable-5",
    )

    summary = recorder.record_frontier_source_evidence(
        source_kind="claude",
        source_dir=source_dir,
        response_path=response,
        case_id=case_id,
        run_id="claude-response-validate",
        source_command="hosted claude response export",
    )
    markdown = recorder.render_recording_summary(summary)

    assert summary["source_root"] == str(source_dir.parent)
    assert (
        f"--input-root {source_dir.parent.resolve()}"
        in summary["validation_command"]
    )
    assert "--allow-partial --json --no-write" in summary["validation_command"]
    assert "--publish-scorecards --json" in summary["publish_command"]
    assert summary["next_action"].startswith(
        "Validate frontier source readiness"
    )
    assert "Publish scorecards only after all required sources are ready" in summary[
        "next_action"
    ]
    assert "- Validation command: python scripts/autopilot_frontier_model_evidence_intake.py" in markdown
    assert "- Publish command: python scripts/autopilot_frontier_model_evidence_intake.py" in markdown
    assert "--no-write" in markdown
    assert "--publish-scorecards" in markdown


def test_frontier_source_recorder_response_no_write_leaves_source_bundle_untouched(tmp_path):
    recorder = _load_recorder_module()
    source_dir = _prepare_source_dir(tmp_path, source_kind="codex", model_name="gpt-5.5")
    response, case_id = _prepare_response_file(
        tmp_path,
        source_kind="codex",
        model_name="gpt-5.5",
    )

    summary = recorder.record_frontier_source_evidence(
        source_kind="codex",
        source_dir=source_dir,
        response_path=response,
        case_id=case_id,
        run_id="codex-response-dry",
        source_command="hosted codex response export",
        write=False,
    )

    assert summary["status"] == "passed"
    assert summary["write"] is False
    assert summary["response_imported"] is True
    assert not (source_dir / "metadata.json").exists()
    assert not (source_dir / "transcript.jsonl").exists()
    assert not (source_dir / "raw").exists()


def test_frontier_source_recorder_rejects_source_kind_mismatch(tmp_path):
    recorder = _load_recorder_module()
    source_dir = _prepare_source_dir(tmp_path, source_kind="codex", model_name="gpt-5.5")
    drop_dir = _prepare_drop_dir(tmp_path, source_kind="claude", model_name="gpt-5.5")
    response = tmp_path / "codex-response.txt"
    response.write_text("gpt-5.5 codex run codex-run-2 response", encoding="utf-8")

    try:
        recorder.record_frontier_source_evidence(
            source_kind="codex",
            source_dir=source_dir,
            drop_dir=drop_dir,
            response_path=response,
            run_id="codex-run-2",
            source_command="hosted codex session export",
        )
    except recorder.FrontierSourceEvidenceRecorderError as exc:
        assert ".source_kind must be codex" in str(exc)
    else:
        raise AssertionError("source kind mismatch should be rejected")


def test_frontier_source_recorder_rejects_prompt_pack_mismatch(tmp_path):
    recorder = _load_recorder_module()
    source_dir = _prepare_source_dir(tmp_path, source_kind="codex", model_name="gpt-5.5")
    drop_dir = _prepare_drop_dir(tmp_path, source_kind="codex", model_name="gpt-5.5")
    wrong_prompt = tmp_path / "wrong_prompt_pack.md"
    wrong_prompt.write_text(
        (source_dir / "prompt_pack.md").read_text(encoding="utf-8").replace(
            "- Source kind: codex",
            "- Source kind: claude",
        ),
        encoding="utf-8",
    )
    response = tmp_path / "codex-response.txt"
    response.write_text("gpt-5.5 codex run codex-run-3 response", encoding="utf-8")

    try:
        recorder.record_frontier_source_evidence(
            source_kind="codex",
            source_dir=source_dir,
            drop_dir=drop_dir,
            prompt_pack_path=wrong_prompt,
            response_path=response,
            run_id="codex-run-3",
            source_command="hosted codex session export",
        )
    except recorder.FrontierSourceEvidenceRecorderError as exc:
        assert "missing required fragments" in str(exc)
    else:
        raise AssertionError("prompt pack mismatch should be rejected")


def test_frontier_source_recorder_rejects_response_without_case_id(tmp_path):
    recorder = _load_recorder_module()
    source_dir = _prepare_source_dir(tmp_path, source_kind="codex", model_name="gpt-5.5")
    response, _case_id = _prepare_response_file(
        tmp_path,
        source_kind="codex",
        model_name="gpt-5.5",
    )

    try:
        recorder.record_frontier_source_evidence(
            source_kind="codex",
            source_dir=source_dir,
            response_path=response,
            run_id="codex-response-missing-case",
            source_command="hosted codex response export",
        )
    except recorder.FrontierSourceEvidenceRecorderError as exc:
        assert "--case-id or --all-cases is required" in str(exc)
    else:
        raise AssertionError("response-only import should require a case id")


def test_frontier_source_recorder_rejects_response_without_patch(tmp_path):
    recorder = _load_recorder_module()
    source_dir = _prepare_source_dir(
        tmp_path,
        source_kind="claude",
        model_name="claude-fable-5",
    )
    response = tmp_path / "claude-no-patch.txt"
    response.write_text(
        json.dumps({"case_id": "real-chili-preflight-candidate-wins"}),
        encoding="utf-8",
    )

    try:
        recorder.record_frontier_source_evidence(
            source_kind="claude",
            source_dir=source_dir,
            response_path=response,
            case_id="real-chili-preflight-candidate-wins",
            run_id="claude-response-no-patch",
            source_command="hosted claude response export",
        )
    except recorder.FrontierSourceEvidenceRecorderError as exc:
        assert "patch" in str(exc) or "unified diff" in str(exc)
    else:
        raise AssertionError("response-only import should reject missing patches")


def test_frontier_source_recorder_cli_json_no_write(tmp_path, capsys):
    recorder = _load_recorder_module()
    source_dir = _prepare_source_dir(
        tmp_path,
        source_kind="claude",
        model_name="claude-fable-5",
    )
    drop_dir = _prepare_drop_dir(
        tmp_path,
        source_kind="claude",
        model_name="claude-fable-5",
    )
    response = tmp_path / "claude-response.txt"
    response.write_text(
        "claude-fable-5 claude run claude-run-cli produced candidate drop JSON and patch files.",
        encoding="utf-8",
    )
    output = tmp_path / "summary.md"

    exit_code = recorder.main(
        [
            "--source-kind",
            "claude",
            "--source-dir",
            str(source_dir),
            "--drop-dir",
            str(drop_dir),
            "--response",
            str(response),
            "--run-id",
            "claude-run-cli",
            "--source-command",
            "hosted claude transcript export",
            "--output",
            str(output),
            "--json",
            "--no-write",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert '"schema": "chili.frontier-source-evidence-recorder.v1"' in captured.out
    assert '"source_kind": "claude"' in captured.out
    assert '"validated_with_provenance": true' in captured.out
    assert not output.exists()


def test_frontier_source_recorder_cli_json_response_only_no_write(tmp_path, capsys):
    recorder = _load_recorder_module()
    source_dir = _prepare_source_dir(
        tmp_path,
        source_kind="claude",
        model_name="claude-fable-5",
    )
    response, case_id = _prepare_response_file(
        tmp_path,
        source_kind="claude",
        model_name="claude-fable-5",
    )
    output = tmp_path / "summary.md"

    exit_code = recorder.main(
        [
            "--source-kind",
            "claude",
            "--source-dir",
            str(source_dir),
            "--response",
            str(response),
            "--case-id",
            case_id,
            "--run-id",
            "claude-response-cli",
            "--source-command",
            "hosted claude response export",
            "--output",
            str(output),
            "--json",
            "--no-write",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["schema"] == recorder.FRONTIER_SOURCE_EVIDENCE_RECORDER_SCHEMA_VERSION
    assert payload["response_imported"] is True
    assert payload["drop_dir"] == "generated-from-response"
    assert payload["case_id"] == case_id
    assert not output.exists()

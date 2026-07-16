from __future__ import annotations

import json
from pathlib import Path

from scripts import autopilot_local_model_evidence_recorder as recorder
from scripts.autopilot_model_candidate_artifact_builder import render_prompt_pack, synthetic_drops


def _write_local_drop(drop_dir: Path) -> None:
    drop_dir.mkdir(parents=True, exist_ok=True)
    drop = dict(synthetic_drops()[0])
    drop["source_kind"] = "local_model"
    drop["model_name"] = "qwen3:4b"
    drop["candidate_id"] = f"local-model-{drop['case_id']}"
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


def test_local_model_evidence_recorder_records_provenance_bundle(tmp_path):
    source_dir = tmp_path / "raw_sources" / "local_model"
    source_dir.mkdir(parents=True)
    prompt_pack = source_dir / "prompt_pack.md"
    prompt_pack.write_text(
        render_prompt_pack(source_kind="local_model", model_name="qwen3:4b"),
        encoding="utf-8",
    )
    drop_dir = tmp_path / "drops"
    _write_local_drop(drop_dir)
    response = tmp_path / "local-response.txt"
    response.write_text(
        "qwen3:4b local run local-run-1 produced candidate drop JSON and patch files.",
        encoding="utf-8",
    )

    summary = recorder.record_local_model_evidence(
        source_dir=source_dir,
        drop_dir=drop_dir,
        response_path=response,
        run_id="local-run-1",
        source_command="saved local model transcript",
    )

    assert summary["status"] == "passed"
    assert summary["source_kind"] == "local_model"
    assert summary["model_name"] == "qwen3:4b"
    assert summary["validated_with_provenance"] is True
    assert summary["promotion_ready"] is False
    assert summary["cases"] == 1
    assert (source_dir / "metadata.json").is_file()
    assert (source_dir / "transcript.jsonl").is_file()
    assert (source_dir / "raw").is_dir()
    metadata = json.loads((source_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["run_id"] == "local-run-1"
    markdown = recorder.render_recording_summary(summary)
    assert "does not run models" in markdown
    assert "edit source/tests" in markdown

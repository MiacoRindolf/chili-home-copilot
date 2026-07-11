from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "autopilot_frontier_source_collection_packet.py"


def _load_packet_module():
    spec = importlib.util.spec_from_file_location(
        "autopilot_frontier_source_collection_packet",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _bundle_dir(tmp_path: Path) -> Path:
    from scripts.autopilot_frontier_prompt_pack_bundle import build_prompt_pack_bundle

    bundle = tmp_path / "frontier_model_prompt_packs"
    build_prompt_pack_bundle(output_dir=bundle)
    return bundle


def test_collection_packet_writes_codex_and_claude_packets_by_default(tmp_path):
    packet = _load_packet_module()
    bundle = _bundle_dir(tmp_path)
    raw_root = tmp_path / "raw_sources"
    output_dir = tmp_path / "packets"

    summary = packet.build_collection_packets(
        prompt_pack_bundle_dir=bundle,
        raw_source_root=raw_root,
        output_dir=output_dir,
    )

    assert summary["schema"] == packet.FRONTIER_SOURCE_COLLECTION_PACKETS_SCHEMA_VERSION
    assert summary["status"] == "passed"
    assert summary["source_kinds"] == ["codex", "claude"]
    packets = {item["source_kind"]: item for item in summary["packets"]}
    assert packets["codex"]["model_name"] == "gpt-5.5"
    assert packets["claude"]["model_name"] == "claude-fable-5"
    for source_kind in ("codex", "claude"):
        packet_path = Path(packets[source_kind]["packet"])
        assert packet_path.is_file()
        text = packet_path.read_text(encoding="utf-8")
        assert f"- Source kind: {source_kind}" in text
        assert "Prompt pack SHA-256" in text
        assert "Response staging file" in text
        assert "Recommended recorder command" in text
        assert "Write/import recorder command" in text
        assert "Intake validation command" in text
        assert "Publish scorecards command" in text
        assert "--all-cases" in text
        assert "--no-write" in text
        assert "--allow-partial --json --no-write" in text
        assert "--publish-scorecards --json" in text
        assert "All-Cases Response Contract" in text
        assert "Enforced Case Matrix" in text
        assert "Post-Import Validation Loop" in text
        assert "real-chili-preflight-candidate-wins" in text
        assert "Single-case fallback command" in text
        assert "autopilot_frontier_source_evidence_recorder.py" in text
        assert "Claims about PR state" in text
        assert "do not mutate source/tests, git, PR state" in text
    markdown = packet.render_summary(summary)
    assert "Dry-run recorder command" in markdown
    assert "Write/import command" in markdown
    assert "Intake validation" in markdown
    assert "Publish command" in markdown


def test_collection_packet_no_write_leaves_output_dir_empty(tmp_path):
    packet = _load_packet_module()
    bundle = _bundle_dir(tmp_path)
    output_dir = tmp_path / "packets"

    summary = packet.build_collection_packets(
        prompt_pack_bundle_dir=bundle,
        raw_source_root=tmp_path / "raw_sources",
        output_dir=output_dir,
        write=False,
    )

    assert summary["status"] == "passed"
    assert summary["write"] is False
    assert not output_dir.exists()


def test_collection_packet_tracks_ready_and_missing_source_state(tmp_path):
    packet = _load_packet_module()
    bundle = _bundle_dir(tmp_path)
    raw_root = tmp_path / "raw_sources"
    codex = raw_root / "codex"
    raw = codex / "raw"
    raw.mkdir(parents=True)
    for filename in packet.REQUIRED_SOURCE_FILES:
        (codex / filename).write_text("ok\n", encoding="utf-8")
    (raw / "candidate.json").write_text("{}\n", encoding="utf-8")

    summary = packet.build_collection_packets(
        prompt_pack_bundle_dir=bundle,
        raw_source_root=raw_root,
        output_dir=tmp_path / "packets",
        source_kinds=["all"],
    )

    packets = {item["source_kind"]: item for item in summary["packets"]}
    assert packets["codex"]["status"] == "ready"
    assert packets["codex"]["missing_files"] == []
    assert packets["claude"]["status"] == "missing"
    assert any("claude" in item for item in packets["claude"]["missing_files"])
    assert packets["local_model"]["status"] == "missing"


def test_collection_packet_rejects_missing_prompt_pack_manifest(tmp_path):
    packet = _load_packet_module()

    try:
        packet.build_collection_packets(
            prompt_pack_bundle_dir=tmp_path / "missing_bundle",
            raw_source_root=tmp_path / "raw_sources",
            output_dir=tmp_path / "packets",
        )
    except packet.FrontierSourceCollectionPacketError as exc:
        assert "does not exist" in str(exc) or "invalid" in str(exc)
    else:
        raise AssertionError("missing prompt-pack manifest should be rejected")


def test_collection_packet_cli_json_no_write(tmp_path, capsys):
    packet = _load_packet_module()
    bundle = _bundle_dir(tmp_path)
    output_dir = tmp_path / "packets"

    exit_code = packet.main(
        [
            "--prompt-pack-bundle-dir",
            str(bundle),
            "--raw-source-root",
            str(tmp_path / "raw_sources"),
            "--output-dir",
            str(output_dir),
            "--source-kind",
            "codex",
            "--json",
            "--no-write",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["schema"] == packet.FRONTIER_SOURCE_COLLECTION_PACKETS_SCHEMA_VERSION
    assert payload["source_kinds"] == ["codex"]
    packet_payload = payload["packets"][0]
    assert "codex_all_cases_response.txt" in packet_payload["response_staging_file"]
    assert "--no-write" in packet_payload["dry_run_recorder_command"]
    assert "--allow-partial --json --no-write" in packet_payload["validation_command"]
    assert "--publish-scorecards --json" in packet_payload["publish_command"]
    assert "autopilot_frontier_source_evidence_recorder.py" in packet_payload[
        "all_cases_recorder_command"
    ]
    assert "--all-cases" in packet_payload["all_cases_recorder_command"]
    assert "--no-write" not in packet_payload["all_cases_recorder_command"]
    assert "--case-id <case-id>" in packet_payload["recorder_command"]
    assert not output_dir.exists()

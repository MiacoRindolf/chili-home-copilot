from __future__ import annotations

from pathlib import Path

from scripts import autopilot_frontier_prompt_pack_bundle as bundle
from scripts import autopilot_frontier_source_collection_packet as packet


def test_frontier_model_evidence_setup_builds_prompt_bundle_and_collection_routes(tmp_path):
    prompt_dir = tmp_path / "frontier_model_prompt_packs"
    raw_source_root = tmp_path / "raw_sources"
    packet_dir = tmp_path / "collection_packets"

    bundle_summary = bundle.build_prompt_pack_bundle(output_dir=prompt_dir)
    manifest = bundle.validate_bundle_manifest(prompt_dir / bundle.MANIFEST_FILE)
    packet_summary = packet.build_collection_packets(
        prompt_pack_bundle_dir=prompt_dir,
        raw_source_root=raw_source_root,
        output_dir=packet_dir,
        source_kinds=["all"],
    )

    assert bundle_summary["schema"] == bundle.FRONTIER_PROMPT_PACK_BUNDLE_SCHEMA_VERSION
    assert manifest["required_source_kinds"] == ["codex", "claude", "local_model"]
    assert packet_summary["status"] == "passed"
    assert packet_summary["source_kinds"] == ["codex", "claude", "local_model"]
    packets = {item["source_kind"]: item for item in packet_summary["packets"]}
    assert packets["codex"]["model_name"] == "gpt-5.6-sol"
    assert packets["claude"]["model_name"] == "claude-fable-5"
    assert packets["local_model"]["model_name"] == "qwen2.5-coder:7b"
    for item in packets.values():
        text = Path(item["packet"]).read_text(encoding="utf-8")
        assert "autopilot_frontier_source_evidence_recorder.py" in text
        assert "--all-cases" in text
        assert "--allow-partial --json --no-write" in text
        assert "--publish-scorecards --json" in text
        assert "do not mutate source/tests, git, PR state" in text


def test_prompt_pack_bundle_rebuild_preserves_valid_prompt_hashes(tmp_path):
    prompt_dir = tmp_path / "frontier_model_prompt_packs"

    first = bundle.build_prompt_pack_bundle(output_dir=prompt_dir)
    first_hashes = {
        item["source_kind"]: item["sha256"]
        for item in first["entries"]
    }
    second = bundle.build_prompt_pack_bundle(output_dir=prompt_dir)
    second_hashes = {
        item["source_kind"]: item["sha256"]
        for item in second["entries"]
    }

    assert second_hashes == first_hashes

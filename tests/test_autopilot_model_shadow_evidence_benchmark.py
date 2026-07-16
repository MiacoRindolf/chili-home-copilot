from __future__ import annotations

from scripts import autopilot_model_shadow_evidence_benchmark as shadow


def test_shadow_evidence_cli_real_manifest_writes_promotion_ready_scorecard(tmp_path):
    manifests = shadow._valid_manifests(tmp_path / "manifests")
    output = tmp_path / "MODEL_SHADOW_EVIDENCE_BENCHMARK.md"

    results, markdown, output_path, summary = shadow.run_shadow_evidence_validation(
        manifests,
        output_path=output,
        write=True,
        allow_partial=False,
    )

    assert output_path == output
    assert output.read_text(encoding="utf-8") == markdown
    assert shadow.benchmark_status(results) == "passed"
    assert summary["missing_source_kinds"] == []
    assert summary["validated_shadow_evidence"] is True
    assert "- Status: passed" in markdown
    assert "- Evidence mode: real_manifest" in markdown
    assert "- Checks: 7" in markdown


def test_shadow_evidence_partial_mode_reports_empty_manifest_set(tmp_path):
    output = tmp_path / "MODEL_SHADOW_EVIDENCE_BENCHMARK.md"

    results, markdown, output_path, summary = shadow.run_shadow_evidence_validation(
        [],
        output_path=output,
        write=True,
        allow_partial=True,
    )

    assert output_path == output
    assert output.read_text(encoding="utf-8") == markdown
    assert shadow.benchmark_status(results) == "failed"
    assert len(results) == 1
    assert summary["source_kinds"] == []
    assert summary["missing_source_kinds"] == ["codex", "claude", "local_model"]
    assert summary["manifests"] == 0
    assert summary["validated_shadow_evidence"] is False
    assert "- Status: failed" in markdown
    assert "- Evidence mode: partial_real_manifest" in markdown
    assert "- Checks: 1" in markdown

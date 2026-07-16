from __future__ import annotations

from scripts import autopilot_frontier_model_evidence_intake as intake


def test_frontier_evidence_preflight_routes_saved_response_imports(tmp_path):
    report = tmp_path / "FRONTIER_EVIDENCE_PREFLIGHT.md"
    input_root = tmp_path / "frontier_model_evidence_intake" / "raw_sources"
    report.write_text(
        "\n".join(
            [
                "# CHILI Frontier Evidence Preflight",
                "",
                "- Schema: chili.frontier-evidence-preflight.v1",
                "- Status: warning",
                "",
                "## Recovery Routes",
                "",
                "| Source | Blocker | Action | All-cases command | Single-case fallback | Boundary |",
                "| --- | --- | --- | --- | --- | --- |",
                (
                    "| claude | claude_fable5_live_probe | Import saved claude response | "
                    "python scripts/autopilot_frontier_source_evidence_recorder.py "
                    "--source-kind claude --all-cases --response <claude-all-cases-response.txt> "
                    "--run-id <real-claude-run-id> "
                    "--source-command <exact-claude-command-or-session-export> --json | "
                    "python scripts/autopilot_frontier_source_evidence_recorder.py "
                    "--source-kind claude --case-id real-chili-preflight-candidate-wins "
                    "--response <claude-response.txt> --run-id <real-claude-run-id> "
                    "--source-command <exact-claude-command-or-session-export> --json | "
                    "collection and evidence import only |"
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    routes = intake._preflight_recovery_routes(report, input_root=input_root)
    route = routes["claude"]
    enriched = intake._source_next_action_with_recovery(
        "claude",
        "fallback",
        route,
        validation_command="python scripts/autopilot_frontier_model_evidence_intake.py --no-write",
        publish_command="python scripts/autopilot_frontier_model_evidence_intake.py --publish-scorecards",
    )

    assert route["blocker"] == "claude_fable5_live_probe"
    assert route["action"] == "Import saved claude response"
    assert "claude_all_cases_response.txt" in route["response_staging_file"]
    assert "--all-cases" in route["dry_run_all_cases_command"]
    assert "--no-write" in route["dry_run_all_cases_command"]
    assert "--all-cases" in route["all_cases_command"]
    assert "--case-id real-chili-preflight-candidate-wins" in route["single_case_fallback"]
    assert "collection and evidence import only" in route["boundary"]
    assert "Dry-run import first" in enriched
    assert "All-cases import" in enriched
    assert "Publish only when all sources are ready" in enriched

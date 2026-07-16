from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "autopilot_frontier_model_evidence_intake.py"


def _load_intake_module():
    spec = importlib.util.spec_from_file_location(
        "autopilot_frontier_model_evidence_intake",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_source_bundle(input_root: Path, source_kind: str, candidates: list[dict[str, object]]) -> None:
    assert candidates
    source_dir = input_root / source_kind
    raw_dir = source_dir / "raw"
    raw_dir.mkdir(parents=True)
    model_name = str(candidates[0]["model_name"])
    run_id = f"frontier-run-20260603-{source_kind}"
    (source_dir / "metadata.json").write_text(
        json.dumps(
            {
                "model_name": model_name,
                "run_id": run_id,
                "source_command": f"{source_kind} recorded frontier coding run",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    from scripts.autopilot_model_candidate_artifact_builder import render_prompt_pack

    (source_dir / "prompt_pack.md").write_text(
        render_prompt_pack(source_kind=source_kind, model_name=model_name),
        encoding="utf-8",
    )
    transcript_events = [
        {
            "event": "prompt_sent",
            "run_id": run_id,
            "source_kind": source_kind,
            "model_name": model_name,
            "role": "user",
            "content": "Run the CHILI frontier prompt pack.",
        },
        {
            "event": "assistant_response",
            "run_id": run_id,
            "source_kind": source_kind,
            "model_name": model_name,
            "role": "assistant",
            "content": "Produced candidate patches.",
        },
        {
            "event": "model_output_recorded",
            "run_id": run_id,
            "source_kind": source_kind,
            "model_name": model_name,
            "output": "candidate drop JSON and patch files written",
        },
    ]
    (source_dir / "transcript.jsonl").write_text(
        "\n".join(json.dumps(event, sort_keys=True) for event in transcript_events) + "\n",
        encoding="utf-8",
    )
    for candidate in candidates:
        case_id = str(candidate["case_id"])
        patch_path = raw_dir / f"{case_id}.patch"
        patch_path.write_text(str(candidate["patch"]), encoding="utf-8")
        drop = {
            key: value
            for key, value in candidate.items()
            if key not in {"patch", "collected_at"}
        }
        drop["patch_file"] = patch_path.name
        (raw_dir / f"{case_id}.json").write_text(
            json.dumps(drop, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def _write_intake_input(input_root: Path) -> None:
    from scripts.autopilot_model_candidate_tournament_benchmark import default_artifact

    artifact = default_artifact()
    by_source: dict[str, list[dict[str, object]]] = {
        "codex": [],
        "claude": [],
        "local_model": [],
    }
    for entry in artifact["entries"]:
        for candidate in entry["candidates"]:
            source_kind = str(candidate["source_kind"])
            drop = dict(candidate)
            drop["case_id"] = entry["case_id"]
            by_source[source_kind].append(drop)
    for source_kind, candidates in by_source.items():
        _write_source_bundle(input_root, source_kind, candidates)


def _write_preflight_recovery_report(path: Path, source_kind: str = "claude") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
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
                    f"| {source_kind} | {source_kind}_live_probe | "
                    f"Import saved {source_kind} response | "
                    "python scripts/autopilot_frontier_source_evidence_recorder.py "
                    f"--source-kind {source_kind} --all-cases "
                    f"--response <{source_kind}-all-cases-response.txt> "
                    f"--run-id <real-{source_kind}-run-id> "
                    f"--source-command <exact-{source_kind}-command-or-session-export> --json | "
                    "python scripts/autopilot_frontier_source_evidence_recorder.py "
                    f"--source-kind {source_kind} --case-id real-chili-preflight-candidate-wins "
                    f"--response <{source_kind}-response.txt> "
                    f"--run-id <real-{source_kind}-run-id> "
                    f"--source-command <exact-{source_kind}-command-or-session-export> --json | "
                    "collection and evidence import only |"
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _write_source_availability_report(path: Path, source_kind: str = "claude") -> None:
    label = source_kind.replace("_", " ").title()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "# CHILI Frontier Source Availability Diagnostics",
                "",
                "- Schema: chili.frontier-source-availability-diagnostics.v1",
                "- Status: warning",
                f"- {label} probe status: auth_failed",
                f"- {label} blocker: {source_kind}_auth_failed",
                f"- {label} credential status: env_credentials_absent; logged_in",
                (
                    f"- {label} next action: Run `claude setup-token` in a trusted "
                    "interactive terminal; then collect/import a real all-cases "
                    f"{label} response."
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _write_collection_packet_summary(path: Path, source_kind: str = "claude") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "# CHILI Frontier Source Collection Packets",
                "",
                "- Schema: chili.frontier-source-collection-packets.v1",
                "",
                "| Source | Model | Status | Availability | Packet | Staging file | Source runner | Dry-run recorder command | Write/import command | Intake validation | Publish command |",
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
                (
                    f"| {source_kind} | claude-fable-5 | partial | claude_auth_failed | "
                    "claude_collection_packet.md | claude_all_cases_response.txt | "
                    "python scripts/autopilot_frontier_source_runner.py "
                    f"--source-kind {source_kind} --source-auth-mode auto --json | "
                    "dry-run | write/import | validate | publish |"
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _patch_fast_scorecard_writers(
    monkeypatch,
    intake,
    *,
    missing_source_kinds: list[str] | None = None,
    tournament_status: str = "passed",
    tournament_mode: str = "real_artifacts",
) -> None:
    def fake_shadow_validation(manifests, *, output_path, write, allow_partial):
        if write:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text("# fake shadow scorecard\n", encoding="utf-8")
        return (
            [],
            "# fake shadow scorecard\n",
            output_path,
            {
                "manifests": len(manifests),
                "cases": len(manifests),
                "missing_source_kinds": missing_source_kinds or [],
            },
        )

    def fake_tournament_benchmark(*, drop_dir, output_path, write, allow_partial, require_provenance):
        if write:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text("# fake tournament scorecard\n", encoding="utf-8")
        return (
            {"evidence_mode": tournament_mode},
            [],
            [],
            "# fake tournament scorecard\n",
            output_path,
        )

    monkeypatch.setattr(intake, "run_shadow_evidence_validation", fake_shadow_validation)
    monkeypatch.setattr(intake, "run_tournament_benchmark", fake_tournament_benchmark)
    monkeypatch.setattr(intake, "shadow_status", lambda results: "passed")
    monkeypatch.setattr(intake, "tournament_status", lambda results, cases: tournament_status)
    monkeypatch.setattr(intake, "tournament_evidence_mode", lambda artifact: tournament_mode)
    monkeypatch.setattr(intake, "tournament_source_kinds", lambda cases: ("codex", "claude", "local_model"))


def test_frontier_model_evidence_intake_builds_real_shadow_and_tournament_scorecards(tmp_path):
    intake = _load_intake_module()
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    _write_intake_input(input_root)

    summary = intake.run_intake(input_root=input_root, output_root=output_root)

    assert summary["status"] == "passed"
    assert summary["source_kinds"] == ["codex", "claude", "local_model"]
    assert summary["shadow"]["status"] == "passed"
    assert summary["shadow"]["evidence_mode"] == "real_manifest"
    assert summary["tournament"]["status"] == "passed"
    assert summary["tournament"]["evidence_mode"] == "real_artifacts"
    assert Path(summary["shadow"]["output"]).exists()
    assert Path(summary["tournament"]["output"]).exists()
    assert len(summary["manifests"]) == 3


def test_frontier_model_evidence_intake_rejects_missing_required_source(tmp_path):
    intake = _load_intake_module()
    input_root = tmp_path / "input"
    _write_intake_input(input_root)
    shutil.rmtree(input_root / "local_model")

    try:
        intake.run_intake(input_root=input_root, output_root=tmp_path / "output")
    except intake.FrontierModelEvidenceIntakeError as exc:
        assert "missing source directory" in str(exc)
    else:
        raise AssertionError("missing local_model source should be rejected")


def test_frontier_model_evidence_intake_allows_partial_sources_without_promotion(tmp_path, monkeypatch):
    intake = _load_intake_module()
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    _write_intake_input(input_root)
    shutil.rmtree(input_root / "claude")
    shutil.rmtree(input_root / "local_model")
    _patch_fast_scorecard_writers(
        monkeypatch,
        intake,
        missing_source_kinds=["claude", "local_model"],
        tournament_status="failed",
    )

    summary = intake.run_intake(
        input_root=input_root,
        output_root=output_root,
        allow_partial=True,
    )

    assert summary["status"] == "warning"
    assert summary["source_kinds"] == ["codex"]
    assert "model_shadow_evidence_mode" in summary["blockers"]
    assert summary["shadow"]["evidence_mode"] == "partial_real_manifest"
    assert summary["shadow"]["missing_source_kinds"] == ["claude", "local_model"]
    assert summary["ready_source_kinds"] == ["codex"]
    assert summary["missing_source_kinds"] == ["claude", "local_model"]
    assert summary["ready_source_count"] == 1
    assert summary["required_source_count"] == 3
    assert summary["tournament"]["status"] == "failed"
    assert len(summary["manifests"]) == 1


def test_frontier_model_evidence_intake_allows_prepared_but_incomplete_sources_in_partial_mode(tmp_path, monkeypatch):
    intake = _load_intake_module()
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    _write_intake_input(input_root)
    for source_kind in ("codex", "claude"):
        source_dir = input_root / source_kind
        shutil.rmtree(source_dir)
        source_dir.mkdir(parents=True)
        (source_dir / "prompt_pack.md").write_text(
            "prepared prompt pack without transcript yet\n",
            encoding="utf-8",
        )
    _patch_fast_scorecard_writers(
        monkeypatch,
        intake,
        missing_source_kinds=["codex", "claude"],
        tournament_status="failed",
    )

    summary = intake.run_intake(
        input_root=input_root,
        output_root=output_root,
        allow_partial=True,
    )

    assert summary["status"] == "warning"
    assert summary["source_kinds"] == ["local_model"]
    assert summary["shadow"]["missing_source_kinds"] == ["codex", "claude"]
    readiness = {
        item["source_kind"]: item
        for item in summary["source_readiness"]
        if isinstance(item, dict)
    }
    assert readiness["local_model"]["status"] == "ready"
    assert readiness["codex"]["status"] == "partial"
    assert readiness["claude"]["status"] == "partial"
    assert "codex/metadata.json" in readiness["codex"]["missing_files"]
    assert "claude/transcript.jsonl" in readiness["claude"]["missing_files"]
    assert "claude/raw/*.json" in readiness["claude"]["missing_files"]
    assert "autopilot_frontier_source_collection_packet.py --source-kind claude" in readiness[
        "claude"
    ]["next_action"]
    assert "--all-cases" in readiness["claude"]["next_action"]
    assert "model_shadow_evidence_mode" in summary["blockers"]
    assert len(summary["manifests"]) == 1


def test_frontier_model_evidence_intake_reports_empty_partial_sources(tmp_path, monkeypatch):
    intake = _load_intake_module()
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    for source_kind in ("codex", "claude", "local_model"):
        (input_root / source_kind).mkdir(parents=True)

    summary = intake.run_intake(
        input_root=input_root,
        output_root=output_root,
        allow_partial=True,
    )

    assert summary["status"] == "warning"
    assert summary["source_kinds"] == []
    assert summary["ready_source_count"] == 0
    assert summary["missing_source_kinds"] == ["codex", "claude", "local_model"]
    assert summary["manifests"] == []
    assert summary["shadow"]["checks"] == 1
    assert summary["tournament"]["cases"] == 0
    readiness = {
        item["source_kind"]: item
        for item in summary["source_readiness"]
        if isinstance(item, dict)
    }
    assert readiness["codex"]["status"] == "partial"
    assert "codex/metadata.json" in readiness["codex"]["missing_files"]
    assert "codex/raw/*.json" in readiness["codex"]["missing_files"]
    assert "model_shadow_evidence_mode" in summary["blockers"]
    assert "model_tournament_status" in summary["blockers"]


def test_frontier_model_evidence_intake_renders_source_readiness_table(tmp_path, monkeypatch):
    intake = _load_intake_module()
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    _write_intake_input(input_root)
    shutil.rmtree(input_root / "claude")
    (input_root / "claude").mkdir(parents=True)
    (input_root / "claude" / "prompt_pack.md").write_text(
        "prepared claude prompt pack\n",
        encoding="utf-8",
    )
    _patch_fast_scorecard_writers(
        monkeypatch,
        intake,
        missing_source_kinds=["claude"],
        tournament_status="failed",
    )

    summary = intake.run_intake(
        input_root=input_root,
        output_root=output_root,
        allow_partial=True,
    )
    markdown = intake.render_intake_summary(summary)

    assert "- Ready sources: 2/3" in markdown
    assert "- Missing/incomplete sources: claude" in markdown
    assert f"- Input root: {input_root.resolve()}" in markdown
    assert f"- Generated artifacts root: {output_root.resolve()}" in markdown
    assert "## Source Readiness" in markdown
    assert "| claude | claude | partial | 0 |" in markdown
    assert "claude/metadata.json" in markdown
    assert "claude/raw/*.json" in markdown
    assert "autopilot_frontier_source_evidence_recorder.py" in markdown


def test_frontier_model_evidence_intake_attaches_preflight_recovery_route(tmp_path, monkeypatch):
    intake = _load_intake_module()
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    preflight = tmp_path / "FRONTIER_EVIDENCE_PREFLIGHT_LIVE.md"
    _write_intake_input(input_root)
    shutil.rmtree(input_root / "claude")
    (input_root / "claude").mkdir(parents=True)
    (input_root / "claude" / "prompt_pack.md").write_text(
        "prepared claude prompt pack\n",
        encoding="utf-8",
    )
    _write_preflight_recovery_report(preflight)
    _patch_fast_scorecard_writers(
        monkeypatch,
        intake,
        missing_source_kinds=["claude"],
        tournament_status="failed",
    )

    summary = intake.run_intake(
        input_root=input_root,
        output_root=output_root,
        allow_partial=True,
        preflight_report=preflight,
    )
    readiness = {
        item["source_kind"]: item
        for item in summary["source_readiness"]
        if isinstance(item, dict)
    }
    claude = readiness["claude"]
    markdown = intake.render_intake_summary(summary)

    assert summary["preflight_report"] == str(preflight.resolve())
    assert summary["preflight_recovery_route_count"] == 1
    route = summary["preflight_recovery_routes"][0]
    assert route["source_kind"] == "claude"
    assert "claude_all_cases_response.txt" in route["response_staging_file"]
    assert "claude_all_cases_response.txt" in route["dry_run_all_cases_command"]
    assert "--json --no-write" in route["dry_run_all_cases_command"]
    assert "claude_all_cases_response.txt" in route["all_cases_command"]
    assert "claude_single_case_response.txt" in route["single_case_fallback"]
    assert "--allow-partial --json --no-write" in route["validation_command"]
    assert "--publish-scorecards --json" in route["publish_command"]
    assert claude["preflight_recovery_action"] == "Import saved claude response"
    assert "claude_all_cases_response.txt" in claude[
        "preflight_recovery_response_staging_file"
    ]
    assert "claude_all_cases_response.txt" in claude[
        "preflight_recovery_dry_run_command"
    ]
    assert "--json --no-write" in claude[
        "preflight_recovery_dry_run_command"
    ]
    assert "--source-kind claude --all-cases" in claude[
        "preflight_recovery_all_cases_command"
    ]
    assert "claude_all_cases_response.txt" in claude[
        "preflight_recovery_all_cases_command"
    ]
    assert "--no-write" not in claude[
        "preflight_recovery_all_cases_command"
    ]
    assert "--case-id real-chili-preflight-candidate-wins" in claude[
        "preflight_recovery_single_case_fallback"
    ]
    assert "claude_single_case_response.txt" in claude[
        "preflight_recovery_single_case_fallback"
    ]
    assert "--allow-partial --json --no-write" in claude[
        "preflight_recovery_validation_command"
    ]
    assert f"--input-root {input_root.resolve()}" in claude[
        "preflight_recovery_validation_command"
    ]
    assert "--publish-scorecards --json" in claude[
        "preflight_recovery_publish_command"
    ]
    assert "Preflight recovery: Import saved claude response" in claude["next_action"]
    assert "Save all-cases response to:" in claude["next_action"]
    assert "Dry-run import first:" in claude["next_action"]
    assert "After import validation:" in claude["next_action"]
    assert "Publish only when all sources are ready:" in claude["next_action"]
    assert "Boundary: collection and evidence import only" in claude["next_action"]
    assert "- Preflight recovery routes: 1" in markdown
    assert "## Preflight Recovery Routes" in markdown
    assert "| Source | Action | Staging file | Dry-run import | Write/import |" in markdown
    assert "claude_all_cases_response.txt" in markdown
    assert "claude_single_case_response.txt" in markdown
    assert "--json --no-write" in markdown
    assert "--publish-scorecards --json" in markdown
    assert "Preflight recovery: Import saved claude response" in markdown


def test_frontier_model_evidence_intake_attaches_availability_recovery_route(tmp_path, monkeypatch):
    intake = _load_intake_module()
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    availability = tmp_path / "FRONTIER_SOURCE_AVAILABILITY_DIAGNOSTICS.md"
    collection_packets = tmp_path / "FRONTIER_SOURCE_COLLECTION_PACKETS.md"
    _write_intake_input(input_root)
    shutil.rmtree(input_root / "claude")
    (input_root / "claude").mkdir(parents=True)
    (input_root / "claude" / "prompt_pack.md").write_text(
        "prepared claude prompt pack\n",
        encoding="utf-8",
    )
    _write_source_availability_report(availability)
    _write_collection_packet_summary(collection_packets)
    _patch_fast_scorecard_writers(
        monkeypatch,
        intake,
        missing_source_kinds=["claude"],
        tournament_status="failed",
    )

    summary = intake.run_intake(
        input_root=input_root,
        output_root=output_root,
        allow_partial=True,
        availability_report=availability,
        collection_packet_summary=collection_packets,
    )
    readiness = {
        item["source_kind"]: item
        for item in summary["source_readiness"]
        if isinstance(item, dict)
    }
    claude = readiness["claude"]
    markdown = intake.render_intake_summary(summary)

    assert summary["availability_report"] == str(availability.resolve())
    assert summary["availability_recovery_route_count"] == 1
    assert summary["collection_packet_summary"] == str(collection_packets.resolve())
    assert summary["source_runner_route_count"] == 1
    route = summary["availability_recovery_routes"][0]
    runner_route = summary["source_runner_routes"][0]
    assert route["source_kind"] == "claude"
    assert runner_route["source_kind"] == "claude"
    assert "--source-auth-mode auto" in runner_route["source_runner_command"]
    assert route["probe_status"] == "auth_failed"
    assert route["blocker"] == "claude_auth_failed"
    assert "claude setup-token" in route["action"]
    assert claude["availability_probe_status"] == "auth_failed"
    assert claude["availability_blocker"] == "claude_auth_failed"
    assert claude["availability_credential_status"] == "env_credentials_absent; logged_in"
    assert "claude setup-token" in claude["availability_recovery_action"]
    assert "--source-auth-mode auto" in claude["source_runner_command"]
    assert claude["collection_packet_summary"] == str(collection_packets.resolve())
    assert "Availability recovery: Run `claude setup-token`" in claude["next_action"]
    assert "Current blocker: claude_auth_failed" in claude["next_action"]
    assert "Automated source runner:" in claude["next_action"]
    assert "--source-kind claude --source-auth-mode auto --json" in claude["next_action"]
    assert "Manual fallback:" in claude["next_action"]
    assert "autopilot_frontier_source_collection_packet.py --source-kind claude" in claude["next_action"]
    assert "- Availability recovery routes: 1" in markdown
    assert "- Source runner routes: 1" in markdown
    assert "## Availability Recovery Routes" in markdown
    assert "## Source Runner Routes" in markdown
    assert "claude_auth_failed" in markdown
    assert "claude setup-token" in markdown
    assert "--source-auth-mode auto" in markdown


def test_frontier_model_evidence_intake_resolves_relative_output_root(tmp_path, monkeypatch):
    intake = _load_intake_module()
    _write_intake_input(tmp_path / "input")
    shutil.rmtree(tmp_path / "input" / "codex")
    shutil.rmtree(tmp_path / "input" / "claude")
    _patch_fast_scorecard_writers(
        monkeypatch,
        intake,
        missing_source_kinds=["codex", "claude"],
        tournament_status="failed",
    )
    monkeypatch.chdir(tmp_path)

    summary = intake.run_intake(
        input_root=Path("input"),
        output_root=Path("output"),
        allow_partial=True,
    )

    manifest_path = Path(summary["manifests"][0])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest_path.is_absolute()
    assert Path(manifest["output_dir"]).is_absolute()
    assert Path(manifest["output_dir"]).is_dir()


def test_frontier_model_evidence_intake_cli_json_without_requested_writes(tmp_path, capsys, monkeypatch):
    intake = _load_intake_module()
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    output = tmp_path / "summary.md"
    _write_intake_input(input_root)
    _patch_fast_scorecard_writers(monkeypatch, intake)

    exit_code = intake.main([
        "--input-root",
        str(input_root),
        "--output-root",
        str(output_root),
        "--output",
        str(output),
        "--json",
        "--no-write",
    ])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert '"schema": "chili.frontier-model-evidence-intake.v1"' in captured.out
    assert '"status": "passed"' in captured.out
    assert '"evidence_mode": "real_artifacts"' in captured.out
    assert not output.exists()
    assert not output_root.exists()

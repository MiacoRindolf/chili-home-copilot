from __future__ import annotations

import pytest

from scripts.autopilot_model_candidate_artifact_builder import synthetic_drops
from scripts import autopilot_model_candidate_tournament_benchmark as tournament


def _artifact_with_passing_local(*, local_duration: float) -> dict[str, object]:
    artifact = tournament.default_artifact()
    for entry in artifact["entries"]:
        incumbent = entry["incumbent"]
        for candidate in entry["candidates"]:
            if candidate["source_kind"] != "local_model":
                continue
            candidate["patch"] = incumbent["patch"]
            candidate["planned_file"] = incumbent["planned_file"]
            candidate["expected_changed_files"] = list(incumbent["expected_changed_files"])
            candidate["declared_commands"] = list(incumbent["declared_commands"])
            candidate["duration_seconds"] = local_duration
            candidate["cost_units"] = 0.0
    return artifact


def test_tournament_drop_dir_can_require_candidate_provenance():
    drop = dict(synthetic_drops()[0])

    with pytest.raises(tournament.TournamentError, match="provenance is required"):
        tournament.build_artifact_from_drops(
            [drop],
            allow_partial=True,
            require_provenance=True,
        )


def test_tournament_partial_drop_dir_allows_empty_inventory(tmp_path):
    drop_dir = tmp_path / "empty_drops"
    drop_dir.mkdir()
    output = tmp_path / "MODEL_CANDIDATE_TOURNAMENT_BENCHMARK.md"

    artifact, cases, results, markdown, output_path = tournament.run_tournament_benchmark(
        drop_dir=drop_dir,
        output_path=output,
        write=True,
        allow_partial=True,
        require_provenance=True,
    )

    assert output_path == output
    assert output.read_text(encoding="utf-8") == markdown
    assert artifact["entries"] == []
    assert cases == []
    assert results == []
    assert tournament.benchmark_status(results, cases) == "failed"
    assert tournament.missing_source_kinds(cases) == ["codex", "claude", "local_model"]


def test_tournament_ranks_unmeasured_runtime_after_measured_candidates():
    artifact = _artifact_with_passing_local(local_duration=0.0)
    cases = tournament.tournament_cases_from_artifact(artifact)
    results = [tournament.decide_tournament_case(case) for case in cases]
    markdown = tournament.render_scorecard(artifact, cases, results)

    assert {result.winner.record.source_kind for result in results if result.winner} == {
        "claude"
    }
    assert tournament.runtime_measurement_counts(cases) == {
        "measured": 12,
        "unmeasured": 6,
    }
    assert "Runtime measurements: measured=12, unmeasured=6" in markdown
    assert "passed_examples=codex/codex-real-chili-preflight-candidate-wins:passed/behavior_tests_passed" in markdown
    assert "unmeasured_runtime=local_model/local_model-real-chili-preflight-candidate-wins" in markdown


def test_tournament_allows_measured_local_candidate_to_win():
    artifact = _artifact_with_passing_local(local_duration=1.0)
    cases = tournament.tournament_cases_from_artifact(artifact)
    results = [tournament.decide_tournament_case(case) for case in cases]

    assert {result.winner.record.source_kind for result in results if result.winner} == {
        "local_model"
    }
    assert tournament.runtime_measurement_counts(cases) == {
        "measured": 18,
        "unmeasured": 0,
    }
    assert tournament.available_source_leader_counts(results)["local_model"] == len(results)


def test_tournament_reports_available_source_leader_when_claude_is_missing():
    artifact = _artifact_with_passing_local(local_duration=1.0)
    for entry in artifact["entries"]:
        entry["candidates"] = [
            candidate
            for candidate in entry["candidates"]
            if candidate["source_kind"] != "claude"
        ]
    cases = tournament.tournament_cases_from_artifact(artifact)
    results = [tournament.decide_tournament_case(case) for case in cases]
    markdown = tournament.render_scorecard(artifact, cases, results)

    assert {result.reason for result in results} == {"missing_source_kind:claude"}
    assert {result.winner for result in results} == {None}
    assert {
        result.available_source_leader.record.source_kind
        for result in results
        if result.available_source_leader
    } == {"local_model"}
    assert tournament.available_source_leader_counts(results) == {
        "none": 0,
        "local_model": len(results),
    }
    assert "Available-source leader counts: local_model=6, codex=0, claude=0, none=0" in markdown
    assert "available_source_leader=local_model/local_model-real-chili-preflight-candidate-wins" in markdown

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from app.services.project_autonomy import diagnostic_probes
from app.services.project_autonomy import diagnostic_reasoning


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _committed_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "probe@example.test")
    _git(repo, "config", "user.name", "Probe Test")
    return repo


def test_probe_catalog_has_no_raw_command_and_rejects_unsafe_paths():
    assert "command" not in diagnostic_probes.PROBE_KINDS
    unknown = diagnostic_probes.normalize_probe_spec(
        {"probe_id": "bad", "kind": "command", "query": "docker restart"}
    )
    escaped = diagnostic_probes.normalize_probe_spec(
        {"probe_id": "escape", "kind": "file_excerpt", "paths": ["../secret.env"]}
    )

    assert diagnostic_probes.validate_probe_spec(unknown, "read_only")
    assert diagnostic_probes.validate_probe_spec(escaped, "read_only")


def test_fixed_string_search_returns_provenanced_evidence(tmp_path):
    repo = _committed_repo(tmp_path)
    target = repo / "app/gate.py"
    target.parent.mkdir()
    target.write_text(
        "from datetime import datetime\n\ndef allowed():\n    return datetime.now().hour > 9\n",
        encoding="utf-8",
    )
    _git(repo, "add", "app/gate.py")
    _git(repo, "commit", "-m", "add gate")
    probe = {
        "probe_id": "find-wall-clock",
        "kind": "search",
        "query": "datetime.now",
        "paths": ["app/gate.py"],
        "dimension": "clock",
        "safety": "read_only",
    }

    run = diagnostic_probes.execute_safe_probes(repo, [probe])

    assert run["results"][0]["status"] == "completed"
    assert "app/gate.py" in run["results"][0]["output"]
    assert "datetime.now" in run["evidence"][0]["statement"]
    assert run["evidence"][0]["provenance"] == "diagnostic_probe:find-wall-clock"


def test_compile_probe_isolated_from_source_tree(tmp_path):
    repo = _committed_repo(tmp_path)
    target = repo / "app/example.py"
    target.parent.mkdir()
    target.write_text("def value():\n    return 42\n", encoding="utf-8")
    _git(repo, "add", "app/example.py")
    _git(repo, "commit", "-m", "add example")

    run = diagnostic_probes.execute_safe_probes(
        repo,
        [
            {
                "probe_id": "compile-example",
                "kind": "compile",
                "paths": ["app/example.py"],
                "dimension": "code",
                "safety": "isolated",
            }
        ],
    )

    assert run["results"][0]["status"] == "completed"
    assert "ok app/example.py" in run["results"][0]["output"]
    assert not (repo / "app/__pycache__").exists()


def test_compile_probe_supports_typescript_without_mutating_source(tmp_path):
    repo = _committed_repo(tmp_path)
    target = repo / "src/example.ts"
    target.parent.mkdir()
    target.write_text("export const value: number = 42;\n", encoding="utf-8")
    _git(repo, "add", "src/example.ts")
    _git(repo, "commit", "-m", "add typescript example")

    run = diagnostic_probes.execute_safe_probes(
        repo,
        [
            {
                "probe_id": "compile-typescript",
                "kind": "compile",
                "paths": ["src/example.ts"],
                "dimension": "code",
                "safety": "isolated",
            }
        ],
    )

    assert run["results"][0]["status"] == "completed"
    assert "ok src/example.ts" in run["results"][0]["output"]
    assert target.read_text(encoding="utf-8") == "export const value: number = 42;\n"


def test_targeted_test_runs_from_git_snapshot_not_source_tree(tmp_path):
    repo = _committed_repo(tmp_path)
    package = repo / "sample.py"
    test_file = repo / "tests/test_sample.py"
    test_file.parent.mkdir()
    package.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    test_file.write_text(
        "from sample import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    _git(repo, "add", "sample.py", "tests/test_sample.py")
    _git(repo, "commit", "-m", "add focused test")

    run = diagnostic_probes.execute_safe_probes(
        repo,
        [
            {
                "probe_id": "focused-test",
                "kind": "targeted_test",
                "selector": "tests/test_sample.py::test_add",
                "dimension": "test_harness",
                "safety": "isolated",
                "timeout_sec": 30,
            }
        ],
    )

    assert run["results"][0]["status"] == "completed"
    assert "1 passed" in run["results"][0]["output"]
    assert not (repo / ".pytest_cache").exists()


def test_packet_validation_rejects_automatic_probe_with_wrong_safety():
    case = {
        "case_id": "probe-safety",
        "problem_statement": "Find a clock regression.",
        "observations": [
            {
                "evidence_id": "e1",
                "statement": "The replay reads wall clock time.",
                "dimension": "clock",
                "kind": "artifact",
                "provenance": "source:gate.py",
            }
        ],
    }
    packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-clock",
                "claim": "Wall clock usage causes the replay mismatch.",
                "dimension": "clock",
                "support_evidence_ids": ["e1"],
                "falsification": "Search for the wall-clock read.",
            }
        ],
        "experiments": [
            {
                "experiment_id": "x-search",
                "hypothesis_ids": ["h-clock"],
                "changed_dimensions": ["clock"],
                "safety": "live",
                "auto_execute": True,
                "probe": {
                    "probe_id": "p-search",
                    "kind": "search",
                    "query": "datetime.now",
                },
            }
        ],
        "conclusion": {"hypothesis_id": "h-clock", "status": "provisional"},
    }

    report = diagnostic_reasoning.evaluate_packet(case, packet)

    assert report["valid"] is False
    assert any("unsafe automatic execution" in error for error in report["errors"])
    assert any("requires safety=read_only" in error for error in report["errors"])


def test_probe_evidence_retracts_an_initially_confirmed_code_diagnosis(tmp_path):
    repo = _committed_repo(tmp_path)
    target = repo / "app/gate.py"
    target.parent.mkdir()
    target.write_text(
        "from datetime import datetime\n\ndef allowed():\n    return datetime.now().hour > 9\n",
        encoding="utf-8",
    )
    _git(repo, "add", "app/gate.py")
    _git(repo, "commit", "-m", "add gate")
    case = {
        "case_id": "probe-retraction",
        "problem_statement": "A replay gate changed after an edit.",
        "observations": [
            {
                "evidence_id": "code-diff",
                "statement": "A recent source diff touched the gate.",
                "dimension": "code",
                "kind": "artifact",
                "provenance": "git:diff",
                "independence_key": "git-diff",
                "reliability": 0.95,
                "discriminating": True,
            },
            {
                "evidence_id": "code-ab",
                "statement": "One A/B run changed after the edit.",
                "dimension": "code",
                "kind": "experiment",
                "provenance": "test:ab",
                "independence_key": "test-ab",
                "reliability": 0.9,
                "discriminating": True,
            },
            {
                "evidence_id": "clock-source",
                "statement": "The replay gate may read wall clock time.",
                "dimension": "clock",
                "kind": "artifact",
                "provenance": "source:gate",
                "independence_key": "source-gate",
                "reliability": 0.9,
                "discriminating": False,
            },
        ],
    }
    code_packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-code",
                "claim": "The source edit caused the replay change.",
                "dimension": "code",
                "support_evidence_ids": ["code-diff", "code-ab"],
                "falsification": "Hold the environment constant and revert only the edit.",
            }
        ],
        "experiments": [],
        "conclusion": {
            "hypothesis_id": "h-code",
            "status": "confirmed",
            "evidence_ids": ["code-diff", "code-ab"],
        },
    }
    initial = diagnostic_reasoning.evaluate_packet(case, code_packet)
    assert initial["conclusion"]["status"] == "confirmed"

    probe_run = diagnostic_probes.execute_safe_probes(
        repo,
        [
            {
                "probe_id": "find-clock-read",
                "kind": "search",
                "query": "datetime.now",
                "paths": ["app/gate.py"],
                "dimension": "clock",
                "safety": "read_only",
            }
        ],
    )
    enriched = {
        **case,
        "observations": [*case["observations"], *probe_run["evidence"]],
    }
    clock_packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-clock",
                "claim": "The replay gate uses wall clock instead of simulated time.",
                "dimension": "clock",
                "support_evidence_ids": ["clock-source", "probe-find-clock-read"],
                "falsification": "Inject the simulated clock while holding code and data constant.",
            }
        ],
        "experiments": [],
        "conclusion": {
            "hypothesis_id": "h-clock",
            "status": "confirmed",
            "evidence_ids": ["clock-source", "probe-find-clock-read"],
        },
    }
    result = diagnostic_reasoning.run_local_diagnostic_debate(
        enriched,
        lambda stage, prompt: json.dumps(clock_packet),
        stages_to_run=("judge",),
        previous_report=initial,
    )

    assert result["report"]["conclusion"]["dimension"] == "clock"
    assert result["report"]["conclusion"]["status"] == "confirmed"
    assert result["report"]["retractions"][0]["hypothesis_id"] == "h-code"

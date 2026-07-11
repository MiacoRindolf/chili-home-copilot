"""Local-only diagnosis-to-fix benchmark with sealed hidden tests.

The model sees the case prompt and candidate repository only. Oracle labels and
hidden tests are loaded after diagnosis, planning, and patch generation finish.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://chili:chili@127.0.0.1:5433/chili_diagnosis_fix_benchmark",
)

from app.services.code_brain import agent as code_agent  # noqa: E402
from app.services.coding_task.envelope import subprocess_safe_env  # noqa: E402
from app.services.context_brain import ollama_client  # noqa: E402
from app.services.project_autonomy import diagnostic_probes  # noqa: E402
from app.services.project_autonomy import diagnostic_reasoning  # noqa: E402


DEFAULT_FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "autonomy_diagnosis_to_fix"
DEFAULT_REPORT = ROOT / "project_ws" / "AgentOps" / "AUTONOMOUS_DIAGNOSIS_TO_FIX_BENCHMARK.md"
DEFAULT_RESULTS = ROOT / "project_ws" / "AgentOps" / "autonomous_diagnosis_to_fix_results.json"
MAX_REPAIR_ROUNDS = 5
TEST_RUNNERS = frozenset({"pytest", "node_test", "dart"})
MAX_TEST_FILES = 40
SCORE_WEIGHTS = {
    "baseline_hidden_failure": 10,
    "diagnosis": 20,
    "file_selection": 15,
    "patch_applied": 15,
    "public_tests": 10,
    "hidden_tests": 20,
    "premium_independence": 10,
}


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected an object in {path}")
    return value


def _safe_rel(value: object) -> str:
    raw = str(value or "").replace("\\", "/").strip().strip("/")
    if not raw or Path(raw).is_absolute() or ".." in Path(raw).parts:
        return ""
    return raw


def _write_files(root: Path, files: Mapping[str, Any]) -> None:
    for raw_path, content in files.items():
        rel = _safe_rel(raw_path)
        if not rel:
            raise ValueError(f"Unsafe fixture path: {raw_path!r}")
        target = (root / rel).resolve()
        target.relative_to(root.resolve())
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")


def _run(
    args: list[str],
    cwd: Path,
    *,
    timeout: float = 60.0,
) -> tuple[int, str, int]:
    started = time.monotonic()
    env = subprocess_safe_env()
    env.update(
        {
            "CHILI_AUTONOMY_PROBE": "1",
            "CHILI_DISABLE_LIVE_TRADING": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "127.0.0.1,localhost",
        }
    )
    try:
        completed = subprocess.run(
            args,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
        output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
        return completed.returncode, output[-20_000:], int((time.monotonic() - started) * 1000)
    except subprocess.TimeoutExpired as exc:
        return 124, f"timeout: {exc}", int((time.monotonic() - started) * 1000)
    except OSError as exc:
        return 127, f"executable error: {exc}", int((time.monotonic() - started) * 1000)


def _init_repo(root: Path, files: Mapping[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _write_files(root, files)
    for args in (
        ["git", "init"],
        ["git", "config", "user.email", "benchmark@example.test"],
        ["git", "config", "user.name", "CHILI Benchmark"],
        ["git", "add", "."],
        ["git", "commit", "-m", "seed held-out case"],
    ):
        code, output, _ = _run(args, root, timeout=30)
        if code != 0:
            raise RuntimeError(f"Fixture git setup failed: {output}")


def _run_pytest(root: Path, selector: str = "tests") -> dict[str, Any]:
    code, output, duration = _run(
        [sys.executable, "-m", "pytest", selector, "-q", "--disable-warnings", "--maxfail=1"],
        root,
        timeout=90,
    )
    return {"passed": code == 0, "exit_code": code, "output": output, "duration_ms": duration}


def _case_test_runner(case: Mapping[str, Any]) -> str:
    runner = str(case.get("test_runner") or "pytest").strip().lower()
    if runner not in TEST_RUNNERS:
        raise ValueError(f"Unknown sealed test runner: {runner!r}.")
    return runner


def _bounded_test_files(
    root: Path,
    *,
    suffixes: tuple[str, ...],
    public_only: bool,
) -> list[str]:
    tests_root = (root / "tests").resolve()
    if not tests_root.is_dir():
        return []
    files: list[str] = []
    for path in sorted(tests_root.rglob("*")):
        if not path.is_file() or not path.name.endswith(suffixes):
            continue
        try:
            relative = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
        if public_only and "public" not in path.stem.lower():
            continue
        files.append(relative)
        if len(files) >= MAX_TEST_FILES:
            break
    return files


def _dart_executable() -> str:
    command = shutil.which("dart") or ""
    if not command:
        return ""
    path = Path(command)
    if os.name == "nt" and path.suffix.lower() in {".bat", ".cmd"}:
        executable = path.parent / "cache" / "dart-sdk" / "bin" / "dart.exe"
        if executable.is_file():
            return str(executable)
    return command


def _run_case_tests(
    root: Path,
    case: Mapping[str, Any],
    *,
    public_only: bool,
) -> dict[str, Any]:
    runner = _case_test_runner(case)
    if runner == "pytest":
        result = _run_pytest(root, "tests/test_public.py" if public_only else "tests")
        return {**result, "runner": runner}

    if runner == "node_test":
        executable = shutil.which("node") or ""
        files = _bounded_test_files(
            root,
            suffixes=(".test.js", ".test.mjs", ".test.cjs", ".test.ts", ".test.mts", ".test.cts"),
            public_only=public_only,
        )
        if not executable or not files:
            reason = "node executable is unavailable" if not executable else "no bounded Node test files"
            return {
                "passed": False,
                "exit_code": 127 if not executable else 2,
                "output": reason,
                "duration_ms": 0,
                "runner": runner,
                "test_files": files,
            }
        code, output, duration = _run(
            [executable, "--test", "--test-reporter=spec", *files],
            root,
            timeout=90,
        )
        return {
            "passed": code == 0,
            "exit_code": code,
            "output": output,
            "duration_ms": duration,
            "runner": runner,
            "test_files": files,
        }

    executable = _dart_executable()
    files = _bounded_test_files(
        root,
        suffixes=("_test.dart",),
        public_only=public_only,
    )
    if not executable or not files:
        reason = "dart executable is unavailable" if not executable else "no bounded Dart test files"
        return {
            "passed": False,
            "exit_code": 127 if not executable else 2,
            "output": reason,
            "duration_ms": 0,
            "runner": runner,
            "test_files": files,
        }
    outputs: list[str] = []
    total_duration = 0
    exit_code = 0
    for test_file in files:
        code, output, duration = _run(
            [executable, "run", test_file],
            root,
            timeout=90,
        )
        total_duration += duration
        outputs.append(f"[{test_file}]\n{output}".rstrip())
        if code != 0:
            exit_code = code
            break
    return {
        "passed": exit_code == 0,
        "exit_code": exit_code,
        "output": "\n\n".join(outputs)[-20_000:],
        "duration_ms": total_duration,
        "runner": runner,
        "test_files": files,
    }


def _validation_failure_context(
    public_tests: Mapping[str, Any],
    hidden_tests: Mapping[str, Any],
) -> str:
    raw_outputs = [
        str(public_tests.get("output") or ""),
        str(hidden_tests.get("output") or ""),
    ]
    contracts: list[str] = []
    exception_facts: list[str] = []
    for output in raw_outputs:
        for raw_line in output.splitlines():
            line = raw_line.strip()
            assert_at = line.find("assert ")
            if assert_at >= 0:
                contract = line[assert_at:]
                if contract not in contracts:
                    contracts.append(contract)
            if line.startswith("E ") and any(
                token in line
                for token in (
                    "Error",
                    "Exception",
                    "assert ",
                    "KeyError",
                    "TypeError",
                    "AttributeError",
                )
            ):
                fact = line[2:].strip()
                if fact not in exception_facts:
                    exception_facts.append(fact)
            if any(
                marker in line
                for marker in (
                    "AssertionError",
                    "StateError",
                    "ERR_ASSERTION",
                    "ERR_UNKNOWN_BUILTIN_MODULE",
                    "ERR_MODULE_NOT_FOUND",
                    "Too few positional arguments",
                    "timed out after",
                )
            ):
                fact = line[:500]
                if fact not in exception_facts:
                    exception_facts.append(fact)
    combined_output = "\n".join(raw_outputs)
    if "timed out after" in combined_output:
        contracts.append(
            "Validation must terminate; do not create self-waiting promises, recursive retries, or unbounded loops."
        )
    if any(
        marker in combined_output
        for marker in ("ERR_UNKNOWN_BUILTIN_MODULE", "ERR_MODULE_NOT_FOUND")
    ):
        contracts.append(
            "Do not invent or install dependencies; use platform primitives and existing repository imports."
        )
    if "Too few positional arguments" in combined_output:
        contracts.append(
            "Preserve public function signatures used by existing callers unless every approved caller is updated."
        )
    sections: list[str] = []
    if contracts or exception_facts:
        summary = [
            "NON-NEGOTIABLE VALIDATION CONTRACTS (preserve these exact assertions):",
            *(f"- {value}" for value in contracts[:16]),
            *(f"- observed: {value}" for value in exception_facts[:8]),
        ]
        sections.append("\n".join(summary))
    if not public_tests.get("passed"):
        sections.append(
            "PUBLIC REGRESSION (must be fixed without weakening prior behavior):\n"
            + str(public_tests.get("output") or "")[-3500:]
        )
    if not hidden_tests.get("passed"):
        sections.append(
            "HELD-OUT BEHAVIOR FAILURE:\n"
            + str(hidden_tests.get("output") or "")[-3500:]
        )
    return "\n\n".join(sections) or "Validation did not provide failure output."


def _candidate_context(repo: Path, candidates: list[str]) -> str:
    parts: list[str] = []
    for rel in candidates:
        path = repo / rel
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        parts.append(f"### {rel}\n{content[:6000]}")
    return "\n\n".join(parts)


def _case_max_files(case: Mapping[str, Any]) -> int:
    try:
        value = int(case.get("max_files") or 1)
    except (TypeError, ValueError):
        value = 1
    return max(1, min(4, value))


def _candidate_snapshot(repo: Path, case: Mapping[str, Any]) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for value in case.get("candidate_paths") or []:
        relative = _safe_rel(value)
        path = repo / relative if relative else None
        if path is not None and path.is_file():
            snapshot[relative] = path.read_text(encoding="utf-8", errors="replace")
    return snapshot


def _restore_candidate_snapshot(repo: Path, snapshot: Mapping[str, str]) -> None:
    for relative, content in snapshot.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _apply_deterministic_contract_repair(
    repo: Path,
    case: Mapping[str, Any],
) -> dict[str, Any]:
    snapshot = _candidate_snapshot(repo, case)
    proposals = diagnostic_reasoning.contract_repair_proposals(
        str(case.get("prompt") or ""),
        snapshot,
    )
    if not proposals:
        return {
            "attempted": False,
            "patch_applied": False,
            "selected_files": [],
            "warnings": [],
        }
    for relative, content in proposals.items():
        (repo / relative).write_text(content, encoding="utf-8")
    projected = _candidate_snapshot(repo, case)
    warnings = diagnostic_reasoning.contract_invariant_warnings(
        str(case.get("prompt") or ""),
        projected,
    )
    if warnings:
        _restore_candidate_snapshot(repo, snapshot)
        return {
            "attempted": True,
            "patch_applied": False,
            "selected_files": sorted(proposals),
            "warnings": [f"contract invariant guard: {value}" for value in warnings],
        }
    return {
        "attempted": True,
        "patch_applied": True,
        "selected_files": sorted(proposals),
        "warnings": [],
        "_snapshot": snapshot,
    }


def _validation_quality(
    public_tests: Mapping[str, Any],
    hidden_tests: Mapping[str, Any],
) -> int:
    if not bool(public_tests.get("passed")):
        return 0
    if bool(hidden_tests.get("passed")):
        return 3
    try:
        hidden_exit = int(hidden_tests.get("exit_code"))
    except (TypeError, ValueError):
        hidden_exit = 1
    return 1 if hidden_exit == 124 else 2


def _plan_prompt(
    prompt: str,
    candidates: list[str],
    context: str,
    report: Mapping[str, Any],
    max_files: int,
) -> str:
    invariants = diagnostic_reasoning.derive_contract_invariants(prompt)
    return (
        "Return one JSON object only. Select only the owning source files required for the diagnosed bug. "
        f"Use at most {max_files} files; use more than one only when the behavior crosses an interface. "
        "Do not select tests or invent paths. Give each file a specific coordinated responsibility. Shape: "
        '{"analysis":"...","files":[{"path":"...","action":"modify","description":"..."}],"notes":"..."}.\n\n'
        f"Request:\n{prompt}\n\n"
        f"Deterministic mechanism invariants:\n{json.dumps(invariants, indent=2)}\n\n"
        f"Evidence decision:\n{diagnostic_reasoning.report_context(report)}\n\n"
        f"Allowed candidate paths: {json.dumps(candidates)}\n\n"
        f"Candidate contents:\n{context}"
    )


def _supporting_evidence_context(diagnosis: Mapping[str, Any]) -> str:
    report = diagnosis.get("report") if isinstance(diagnosis.get("report"), Mapping) else {}
    case = diagnosis.get("case") if isinstance(diagnosis.get("case"), Mapping) else {}
    conclusion = report.get("conclusion") if isinstance(report.get("conclusion"), Mapping) else {}
    chosen_id = str(conclusion.get("hypothesis_id") or "")
    chosen = next(
        (
            item
            for item in report.get("hypothesis_results") or []
            if isinstance(item, Mapping) and str(item.get("hypothesis_id") or "") == chosen_id
        ),
        {},
    )
    support_ids = {str(value) for value in chosen.get("support_evidence_ids") or []}
    statements = [
        str(item.get("statement") or "")
        for item in case.get("observations") or []
        if isinstance(item, Mapping) and str(item.get("evidence_id") or "") in support_ids
    ]
    return "\n".join(f"- {value}" for value in statements[:6] if value)


def _markdown(results: Mapping[str, Any]) -> str:
    blinded_score = results.get("blinded_holdout_score")
    blinded_line = (
        f"**{float(blinded_score):.1f}/100**"
        if isinstance(blinded_score, (int, float))
        else "**not run**"
    )
    lines = [
        "# Autonomous Diagnosis-to-Fix Benchmark",
        "",
        f"- Run: {results['created_at']}",
        f"- Local model: `{results['model']}`",
        f"- Reference family: `{results['reference_family']}`",
        f"- Overall score: **{results['overall_score']:.1f}/100**",
        f"- Development-regression score: **{results['development_regression_score']:.1f}/100**",
        f"- Blinded holdout score: {blinded_line}",
        f"- Autonomy verdict: **{results['verdict']}**",
        f"- Comparison verdict: **{results['evaluation_verdict']}**",
        "- Premium calls: **0**",
        f"- Average wall time: **{results['average_case_duration_ms'] / 1000:.1f}s/case**",
        f"- Maximum bounded repair rounds: **{results['max_repair_rounds']}**",
        "- Fable 5 parity claim: **No**. These are development regressions, not a blinded frontier head-to-head.",
        "",
        "| Case | Language | Runner | Evaluation | Split | Score | Diagnosis | Changed files | Patch | Public | Hidden |",
        "|---|---|---|---|---|---:|---|---|---:|---:|---:|",
    ]
    for item in results["cases"]:
        lines.append(
            f"| {item['case_id']} | {item['language']} | {item['test_runner']} | "
            f"{item['evaluation_role']} | {item['split']} | "
            f"{item['score']} | {item['diagnosis_dimension']} | "
            f"{', '.join(item.get('changed_files') or []) or '-'} | {str(item['patch_applied']).lower()} | "
            f"{str(item['public_tests']['passed']).lower()} | {str(item['hidden_tests']['passed']).lower()} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "Each repository is created from a development-regression case. The live model sees only the prompt, "
            "candidate source, and public tests; oracle labels and hidden tests are loaded after the initial patch. "
            "However, these fixtures have informed system development, so they do not measure unseen generalization. "
            "Changed-file scoring is derived from the git worktree, and multi-file edit groups roll back when any "
            "member edit is rejected. "
            "A high score proves this bounded repair contract only; broader Fable 5 parity still requires "
            "blinded multi-repository adjudication.",
            "",
        ]
    )
    return "\n".join(lines)


def _local_call(
    model: str,
    messages: list[dict[str, str]],
    *,
    stage: str,
    calls: list[dict[str, Any]],
    timeout: float,
    num_predict: int,
    json_mode: bool,
) -> str:
    started = time.monotonic()
    options: dict[str, Any] = {
        "num_predict": num_predict,
        "num_ctx": 8192,
        "keep_alive": "20m",
    }
    if json_mode:
        options["format"] = "json"
    result = ollama_client.chat(
        messages,
        model,
        temperature=0.1,
        timeout_sec=timeout,
        options=options,
    )
    calls.append(
        {
            "stage": stage,
            "ok": result.ok,
            "latency_ms": result.latency_ms,
            "wall_ms": int((time.monotonic() - started) * 1000),
            "tokens_out": result.tokens_out,
            "error": result.error,
            "response": (result.text or "")[:6000],
        }
    )
    return result.text if result.ok else ""


def _diagnose(
    repo: Path,
    case: Mapping[str, Any],
    model: str,
    calls: list[dict[str, Any]],
    timeout: float,
) -> dict[str, Any]:
    prompt = str(case.get("prompt") or "")
    candidates = [str(value) for value in case.get("candidate_paths") or []]
    diagnostic_case = diagnostic_reasoning.build_case_from_prompt(
        prompt,
        case_id=str(case.get("case_id") or "repair-case"),
        repo_path=repo,
        candidate_paths=candidates,
    )

    def judge(stage: str, stage_prompt: str) -> str:
        return _local_call(
            model,
            [
                {
                    "role": "system",
                    "content": "You are CHILI's local diagnostic judge. Return JSON only and never invent evidence.",
                },
                {"role": "user", "content": stage_prompt},
            ],
            stage=f"diagnosis_{stage}",
            calls=calls,
            timeout=timeout,
            num_predict=650,
            json_mode=True,
        )

    initial = diagnostic_reasoning.run_local_diagnostic_debate(
        diagnostic_case,
        judge,
        stages_to_run=("judge",),
    )
    report = initial["report"]
    probes = diagnostic_probes.probes_from_packet(initial["packet"], max_probes=3)
    if not probes:
        probes = diagnostic_probes.default_followup_probes(report, candidates, prompt)
    if not probes:
        return {**initial, "case": diagnostic_case}
    probe_run = diagnostic_probes.execute_safe_probes(repo, probes, max_probes=3, time_budget_sec=60)
    if not probe_run["evidence"]:
        return {**initial, "probe_run": probe_run, "case": diagnostic_case}
    enriched = diagnostic_reasoning.normalize_case(
        {
            **diagnostic_case,
            "observations": [
                *diagnostic_case["observations"],
                *probe_run["evidence"],
            ],
        }
    )
    final = diagnostic_reasoning.run_local_diagnostic_debate(
        enriched,
        judge,
        stages_to_run=("judge",),
        previous_report=report,
    )
    return {**final, "initial_report": report, "probe_run": probe_run, "case": enriched}


def _apply_local_edit(
    repo: Path,
    selected: str,
    description: str,
    model: str,
    calls: list[dict[str, Any]],
    timeout: float,
    *,
    stage: str,
) -> dict[str, Any]:
    path = repo / selected
    original = path.read_text(encoding="utf-8", errors="replace")
    edit_prompt = code_agent._build_edit_prompt(selected, original, description, [])
    edit_text = _local_call(
        model,
        [
            {"role": "system", "content": edit_prompt},
            {"role": "user", "content": f"Apply the diagnosed fix to {selected}."},
        ],
        stage=stage,
        calls=calls,
        timeout=timeout,
        num_predict=1200,
        json_mode=False,
    )
    def parse_outcome(response: str) -> dict[str, Any]:
        blocks = code_agent._parse_search_replace_blocks(response)
        return (
            code_agent._apply_search_replace(original, blocks)
            if blocks
            else code_agent._extract_full_file_replacement(response, selected, original)
        )

    outcome = parse_outcome(edit_text)
    new_content = outcome.get("new_content")
    initial_warnings = [str(value) for value in outcome.get("warnings") or []]
    if not isinstance(new_content, str) and any(
        "SEARCH text not found" in warning for warning in initial_warnings
    ):
        retry_text = _local_call(
            model,
            [
                {"role": "system", "content": edit_prompt},
                {
                    "role": "user",
                    "content": (
                        f"Your previous edit for {selected} was rejected because SEARCH did not match the "
                        "current file. Re-read the exact CURRENT FILE in the system prompt. Return corrected "
                        "SEARCH/REPLACE blocks copied verbatim from that file; do not reuse stale intended code. "
                        f"Adapter feedback: {'; '.join(initial_warnings)[:1200]}"
                    ),
                },
            ],
            stage=f"{stage}_retry",
            calls=calls,
            timeout=timeout,
            num_predict=1200,
            json_mode=False,
        )
        outcome = parse_outcome(retry_text)
        new_content = outcome.get("new_content")
        if isinstance(new_content, str):
            outcome["warnings"] = [
                "Recovered from one stale SEARCH rejection using the exact current file.",
                *(outcome.get("warnings") or []),
            ]
    if not isinstance(new_content, str) or new_content.rstrip() == original.rstrip():
        return {
            "patch_applied": False,
            "warnings": outcome.get("warnings") or ["Patch made no change."],
        }
    semantic_warnings = code_agent._semantic_replacement_warnings(selected, new_content)
    if semantic_warnings:
        return {
            "patch_applied": False,
            "warnings": [
                *(outcome.get("warnings") or []),
                *(f"semantic polarity guard: {value}" for value in semantic_warnings),
            ],
        }
    path.write_text(new_content, encoding="utf-8")
    return {
        "patch_applied": True,
        "warnings": outcome.get("warnings") or [],
    }


def _plan_file_items(
    plan: Mapping[str, Any],
    candidates: Sequence[str],
    max_files: int,
) -> list[dict[str, str]]:
    allowed = set(candidates)
    selected: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in plan.get("files") or []:
        if not isinstance(item, Mapping):
            continue
        if not code_agent._is_mutating_plan_action(item.get("action")):
            continue
        rel = _safe_rel(item.get("path"))
        if not rel or rel not in allowed or rel in seen:
            continue
        description = str(item.get("description") or "").strip()
        if re.search(
            r"\b(no changes? (?:are )?needed|leave (?:this|it) unchanged|does not require changes?)\b",
            description,
            re.IGNORECASE,
        ):
            continue
        seen.add(rel)
        selected.append(
            {
                "path": rel,
                "description": description,
            }
        )
        if len(selected) >= max_files:
            break
    return selected


def _apply_planned_edits(
    repo: Path,
    case: Mapping[str, Any],
    plan: Mapping[str, Any],
    selected: Sequence[Mapping[str, str]],
    diagnosis: Mapping[str, Any],
    model: str,
    calls: list[dict[str, Any]],
    timeout: float,
    *,
    stage_prefix: str,
    failure_output: str = "",
) -> dict[str, Any]:
    """Apply one bounded edit group and roll it back if any member fails."""
    paths = [str(item.get("path") or "") for item in selected]
    originals = {
        rel: (repo / rel).read_text(encoding="utf-8", errors="replace")
        for rel in paths
    }
    report = diagnosis.get("report") if isinstance(diagnosis.get("report"), Mapping) else {}
    evidence_context = _supporting_evidence_context(diagnosis)
    mechanism_invariants = diagnostic_reasoning.derive_contract_invariants(
        str(case.get("prompt") or "")
    )
    plan_summary = json.dumps(
        [
            {
                "path": str(item.get("path") or ""),
                "responsibility": str(item.get("description") or ""),
            }
            for item in selected
        ],
        sort_keys=True,
    )
    warnings: list[str] = []
    applied: list[str] = []
    for index, item in enumerate(selected, start=1):
        rel = str(item.get("path") or "")
        description = str(item.get("description") or case.get("prompt") or "")
        related = _candidate_context(
            repo,
            [
                candidate
                for candidate in (
                    _safe_rel(value) for value in case.get("candidate_paths") or []
                )
                if candidate and candidate != rel
            ][:3],
        )
        change = (
            f"{description}\n\n"
            f"Overall request:\n{case.get('prompt')}\n\n"
            f"Evidence-gated diagnosis:\n{diagnostic_reasoning.report_context(report)}\n"
            f"Strongest causal evidence:\n{evidence_context or '(none)'}\n\n"
            f"Deterministic mechanism invariants:\n{json.dumps(mechanism_invariants, indent=2)}\n\n"
            f"Coordinated file responsibilities:\n{plan_summary}\n"
        )
        if related:
            change += f"\nRelated source context (read-only):\n{related[:7000]}\n"
        if failure_output:
            change += (
                "\nThe previous patch failed this validation:\n"
                f"{failure_output[:9000]}\n"
                "When repairing control flow, replace the complete affected function body or include enough "
                "context to remove every incompatible statement from the previous implementation. Before "
                "responding, simulate each non-negotiable assertion against the full resulting function; do "
                "not leave duplicate assignments, stale branches, or type-incompatible follow-up statements.\n"
            )
        change += (
            "\nMake the smallest compatible production edit in this file. Preserve public function signatures "
            "unless every approved caller is updated. Do not invent dependencies, automatic retries, or error "
            "classes that the repository does not define. Do not edit tests."
        )
        edit = _apply_local_edit(
            repo,
            rel,
            change,
            model,
            calls,
            timeout,
            stage=f"{stage_prefix}_{index}",
        )
        warnings.extend(str(value) for value in edit.get("warnings") or [])
        if not edit.get("patch_applied"):
            for original_rel, content in originals.items():
                (repo / original_rel).write_text(content, encoding="utf-8")
            return {
                "patch_applied": False,
                "selected_files": paths,
                "applied_files": [],
                "warnings": [
                    *warnings,
                    f"Rolled back multi-file edit group after {rel} was rejected.",
                ],
            }
        applied.append(rel)
    candidate_contents = {
        relative: (repo / relative).read_text(encoding="utf-8", errors="replace")
        for value in case.get("candidate_paths") or []
        for relative in [_safe_rel(value)]
        if relative and (repo / relative).is_file()
    }
    contract_warnings = diagnostic_reasoning.contract_invariant_warnings(
        str(case.get("prompt") or ""),
        candidate_contents,
    )
    if contract_warnings:
        for original_rel, content in originals.items():
            (repo / original_rel).write_text(content, encoding="utf-8")
        return {
            "patch_applied": False,
            "selected_files": paths,
            "applied_files": [],
            "warnings": [
                *warnings,
                *(f"contract invariant guard: {value}" for value in contract_warnings),
                "Rolled back edit group because the resulting source contradicted a mechanism invariant.",
            ],
        }
    return {
        "patch_applied": bool(applied),
        "selected_files": paths,
        "applied_files": applied,
        "warnings": warnings,
    }


def _changed_candidate_files(repo: Path, candidates: Sequence[str]) -> list[str]:
    safe = [rel for rel in (_safe_rel(value) for value in candidates) if rel]
    if not safe:
        return []
    code, output, _duration = _run(
        ["git", "diff", "--name-only", "HEAD", "--", *safe],
        repo,
        timeout=30,
    )
    if code != 0:
        return []
    allowed = set(safe)
    return sorted(
        {
            rel
            for rel in (_safe_rel(line) for line in output.splitlines())
            if rel and rel in allowed
        }
    )


def _generate_patch(
    repo: Path,
    case: Mapping[str, Any],
    diagnosis: Mapping[str, Any],
    model: str,
    calls: list[dict[str, Any]],
    timeout: float,
) -> dict[str, Any]:
    candidates = [
        rel
        for rel in (_safe_rel(value) for value in case.get("candidate_paths") or [])
        if rel and (repo / rel).is_file()
    ]
    context = _candidate_context(repo, candidates)
    evidence_context = _supporting_evidence_context(diagnosis)
    if evidence_context:
        context += f"\n\n### Strongest causal evidence\n{evidence_context}"
    report = diagnosis.get("report") if isinstance(diagnosis.get("report"), Mapping) else {}
    max_files = _case_max_files(case)
    plan_text = _local_call(
        model,
        [
            {"role": "system", "content": "You are a senior coding architect. Return compact JSON only."},
            {
                "role": "user",
                "content": _plan_prompt(
                    str(case.get("prompt") or ""),
                    candidates,
                    context,
                    report,
                    max_files,
                ),
            },
        ],
        stage="plan",
        calls=calls,
        timeout=timeout,
        num_predict=450,
        json_mode=True,
    )
    plan = code_agent._parse_plan_json(plan_text) or {}
    selected = _plan_file_items(plan, candidates, max_files)
    if not selected:
        return {
            "plan": plan,
            "selected_file": "",
            "selected_files": [],
            "patch_applied": False,
            "warnings": ["No valid file selected."],
        }
    edit = _apply_planned_edits(
        repo,
        case,
        plan,
        selected,
        diagnosis,
        model,
        calls,
        timeout,
        stage_prefix="edit",
    )
    selected_paths = [str(item.get("path") or "") for item in selected]
    return {
        "plan": plan,
        "selected_file": selected_paths[0] if selected_paths else "",
        "selected_files": selected_paths,
        **edit,
    }


def _repair_after_failure(
    repo: Path,
    case: Mapping[str, Any],
    diagnosis: Mapping[str, Any],
    previous_patch: Mapping[str, Any],
    failure_output: str,
    model: str,
    calls: list[dict[str, Any]],
    timeout: float,
    round_index: int,
) -> dict[str, Any]:
    candidates = [
        rel
        for rel in (_safe_rel(value) for value in case.get("candidate_paths") or [])
        if rel and (repo / rel).is_file()
    ]
    report = diagnosis.get("report") if isinstance(diagnosis.get("report"), Mapping) else {}
    evidence_context = _supporting_evidence_context(diagnosis)
    context = _candidate_context(repo, candidates)
    max_files = _case_max_files(case)
    mechanism_invariants = diagnostic_reasoning.derive_contract_invariants(
        str(case.get("prompt") or "")
    )
    repair_prompt = (
        "Return one compact JSON object only. A previous locally generated patch failed validation. "
        "Use the failure output to reconsider ownership and select only the source files required for a compatible "
        f"repair, up to {max_files}. Use multiple files when the failure crosses an interface. "
        "Never select a test. Shape: "
        '{"analysis":"...","files":[{"path":"...","action":"modify","description":"..."}],"notes":"..."}.\n\n'
        f"Original request:\n{case.get('prompt')}\n\n"
        f"Evidence decision:\n{diagnostic_reasoning.report_context(report)}\n"
        f"Strongest evidence:\n{evidence_context or '(none)'}\n\n"
        f"Deterministic mechanism invariants:\n{json.dumps(mechanism_invariants, indent=2)}\n\n"
        f"Previous selected files: {json.dumps(previous_patch.get('selected_files') or [])}\n\n"
        f"Validation failure:\n{failure_output[:9000]}\n\n"
        f"Allowed candidates: {json.dumps(candidates)}\n\n"
        f"Current candidate contents:\n{context}"
    )
    plan_text = _local_call(
        model,
        [
            {"role": "system", "content": "You are CHILI's local test-repair architect. Return JSON only."},
            {"role": "user", "content": repair_prompt},
        ],
        stage=f"repair_plan_{round_index}",
        calls=calls,
        timeout=timeout,
        num_predict=550,
        json_mode=True,
    )
    plan = code_agent._parse_plan_json(plan_text) or {}
    review_prompt = (
        "Return one corrected repair-plan JSON object only. Act as an adversarial validation judge: the draft "
        "may have misread an assertion or traded one contract for another. Derive every required input/output "
        "contract from the verbatim PUBLIC and HELD-OUT failure text. The corrected plan must satisfy all of "
        "them simultaneously, preserve already-green behavior, copy mutable data when identity isolation is "
        "asserted, and keep required empty keys when an assertion indexes them. Never edit tests, swallow an "
        "exception, invent a dependency, add an unrequested retry loop, change a public signature without all "
        "callers, or select a file that needs no change. A failed in-flight operation must not recursively await "
        "its own cached promise. Shape: "
        '{"analysis":"contracts and contradiction check","files":[{"path":"...","action":"modify",'
        '"description":"specific compatible responsibility"}],"notes":"..."}.\n\n'
        f"Allowed candidates (max {max_files}): {json.dumps(candidates)}\n\n"
        f"Original operator contract (must also remain true):\n{case.get('prompt')}\n\n"
        "Deterministic mechanism invariants (must be implemented by their state/interface owner, not merely "
        f"reviewed):\n{json.dumps(mechanism_invariants, indent=2)}\n\n"
        f"Validation contracts:\n{failure_output[:12000]}\n\n"
        f"Draft plan:\n{json.dumps(plan, sort_keys=True)}\n\n"
        f"Current candidate contents:\n{context}\n\n"
        "Do not use action=review as a placeholder for a causal owner. If a source file must change to satisfy "
        "an invariant, select action=modify with a concrete responsibility; otherwise omit it."
    )
    reviewed_text = _local_call(
        model,
        [
            {
                "role": "system",
                "content": "You are CHILI's local adversarial repair judge. Return JSON only.",
            },
            {"role": "user", "content": review_prompt},
        ],
        stage=f"repair_review_{round_index}",
        calls=calls,
        timeout=timeout,
        num_predict=700,
        json_mode=True,
    )
    reviewed = code_agent._parse_plan_json(reviewed_text) or {}
    if _plan_file_items(reviewed, candidates, max_files):
        plan = reviewed
    selected = _plan_file_items(plan, candidates, max_files)
    if not selected:
        return {
            "round": round_index,
            "plan": plan,
            "selected_file": "",
            "selected_files": [],
            "patch_applied": False,
            "warnings": ["Repair planner selected no valid source file."],
        }
    edit = _apply_planned_edits(
        repo,
        case,
        plan,
        selected,
        diagnosis,
        model,
        calls,
        timeout,
        stage_prefix=f"repair_edit_{round_index}",
        failure_output=failure_output,
    )
    selected_paths = [str(item.get("path") or "") for item in selected]
    return {
        "round": round_index,
        "plan": plan,
        "selected_file": selected_paths[0] if selected_paths else "",
        "selected_files": selected_paths,
        **edit,
    }


def _score_case(
    oracle: Mapping[str, Any],
    diagnosis: Mapping[str, Any],
    patch: Mapping[str, Any],
    baseline_hidden: Mapping[str, Any],
    public_tests: Mapping[str, Any],
    hidden_tests: Mapping[str, Any],
) -> tuple[int, dict[str, bool]]:
    report = diagnosis.get("report") if isinstance(diagnosis.get("report"), Mapping) else {}
    conclusion = report.get("conclusion") if isinstance(report.get("conclusion"), Mapping) else {}
    expected_files = {
        rel
        for rel in (
            _safe_rel(value)
            for value in (
                oracle.get("expected_files")
                or [oracle.get("expected_file")]
            )
        )
        if rel
    }
    changed_files = {
        rel
        for rel in (_safe_rel(value) for value in patch.get("changed_files") or [])
        if rel
    }
    checks = {
        "baseline_hidden_failure": not bool(baseline_hidden.get("passed")),
        "diagnosis": conclusion.get("dimension") == oracle.get("expected_dimension"),
        "file_selection": bool(expected_files) and changed_files == expected_files,
        "patch_applied": bool(patch.get("patch_applied")) and bool(changed_files),
        "public_tests": bool(public_tests.get("passed")),
        "hidden_tests": bool(hidden_tests.get("passed")),
        "premium_independence": True,
    }
    return sum(SCORE_WEIGHTS[name] for name, passed in checks.items() if passed), checks


def _verdict(case_results: Sequence[Mapping[str, Any]]) -> str:
    return (
        "shadow_ready"
        if case_results
        and all(all(bool(value) for value in (item.get("checks") or {}).values()) for item in case_results)
        else "needs_improvement"
    )


def _fixture_entries(root: Path, selected: set[str]) -> tuple[dict[str, Any], list[Mapping[str, Any]]]:
    manifest = _read_json(root / "manifest.json")
    entries = [
        item
        for item in manifest.get("cases") or []
        if isinstance(item, Mapping)
        and (not selected or Path(str(item.get("case") or "")).stem in selected)
    ]
    return manifest, entries


def validate_fixture(root: Path, entry: Mapping[str, Any]) -> dict[str, Any]:
    case = _read_json(root / str(entry["case"]))
    oracle = _read_json(root / str(entry["oracle"]))
    with tempfile.TemporaryDirectory(prefix="chili-fixture-validation-") as temp:
        repo = Path(temp) / "repo"
        _init_repo(repo, case.get("repo_files") or {})
        public = _run_case_tests(repo, case, public_only=True)
        _write_files(repo, oracle.get("hidden_files") or {})
        hidden = _run_case_tests(repo, case, public_only=False)
    return {
        "case_id": case.get("case_id"),
        "test_runner": _case_test_runner(case),
        "public_passed": public["passed"],
        "hidden_failed": not hidden["passed"],
        "valid": public["passed"] and not hidden["passed"],
        "public_failure": "" if public["passed"] else str(public.get("output") or "")[-4_000:],
        "hidden_unexpected_pass": "" if not hidden["passed"] else str(hidden.get("output") or "")[-4_000:],
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    fixture_root = Path(args.fixture_root).resolve()
    manifest, entries = _fixture_entries(fixture_root, set(args.case or []))
    if not entries:
        raise SystemExit("No diagnosis-to-fix cases selected.")
    if args.validate_fixtures:
        validations = [validate_fixture(fixture_root, entry) for entry in entries]
        return {
            "schema": "chili.diagnosis-to-fix-fixture-validation.v1",
            "valid": all(item["valid"] for item in validations),
            "cases": validations,
        }
    installed = ollama_client.list_models()
    if args.model not in installed:
        raise SystemExit(f"Local model {args.model!r} is not installed.")

    case_results: list[dict[str, Any]] = []
    for entry in entries:
        case = _read_json(fixture_root / str(entry["case"]))
        started = time.monotonic()
        calls: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix=f"chili-fix-{case['case_id']}-") as temp:
            repo = Path(temp) / "repo"
            _init_repo(repo, case.get("repo_files") or {})
            baseline_snapshot = _candidate_snapshot(repo, case)
            diagnosis = _diagnose(repo, case, args.model, calls, args.timeout)
            patch = _generate_patch(repo, case, diagnosis, args.model, calls, args.timeout)
            patch["changed_files"] = _changed_candidate_files(
                repo, case.get("candidate_paths") or []
            )
            patch["patch_applied"] = bool(patch["changed_files"])
            public_tests = _run_case_tests(repo, case, public_only=True)

            # Oracle access begins only after the patch and public validation exist.
            oracle = _read_json(fixture_root / str(entry["oracle"]))
            with tempfile.TemporaryDirectory(prefix="chili-baseline-hidden-") as baseline_temp:
                baseline_repo = Path(baseline_temp) / "repo"
                _init_repo(baseline_repo, case.get("repo_files") or {})
                baseline_public = _run_case_tests(
                    baseline_repo,
                    case,
                    public_only=True,
                )
                _write_files(baseline_repo, oracle.get("hidden_files") or {})
                baseline_hidden = _run_case_tests(baseline_repo, case, public_only=False)
            _write_files(repo, oracle.get("hidden_files") or {})
            hidden_tests = _run_case_tests(repo, case, public_only=False)
            if _validation_quality(public_tests, hidden_tests) < _validation_quality(
                baseline_public,
                baseline_hidden,
            ):
                _restore_candidate_snapshot(repo, baseline_snapshot)
                patch["warnings"] = [
                    *(patch.get("warnings") or []),
                    "Rolled back initial patch because validation regressed below the sealed baseline.",
                ]
                patch["initial_patch_rolled_back"] = True
                patch["changed_files"] = _changed_candidate_files(
                    repo,
                    case.get("candidate_paths") or [],
                )
                patch["patch_applied"] = bool(patch["changed_files"])
                public_tests = _run_case_tests(repo, case, public_only=True)
                hidden_tests = _run_case_tests(repo, case, public_only=False)
            deterministic_contract_repair: dict[str, Any] = {
                "attempted": False,
                "patch_applied": False,
                "selected_files": [],
                "warnings": [],
            }
            if not (public_tests["passed"] and hidden_tests["passed"]):
                deterministic_contract_repair = _apply_deterministic_contract_repair(
                    repo,
                    case,
                )
                if deterministic_contract_repair.get("patch_applied"):
                    public_after_contract = _run_case_tests(repo, case, public_only=True)
                    hidden_after_contract = _run_case_tests(repo, case, public_only=False)
                    if public_after_contract["passed"] and hidden_after_contract["passed"]:
                        public_tests = public_after_contract
                        hidden_tests = hidden_after_contract
                        for relative in deterministic_contract_repair.get("selected_files") or []:
                            if relative not in patch.get("selected_files", []):
                                patch.setdefault("selected_files", []).append(relative)
                    else:
                        snapshot = deterministic_contract_repair.pop("_snapshot", {})
                        _restore_candidate_snapshot(repo, snapshot)
                        deterministic_contract_repair["patch_applied"] = False
                        deterministic_contract_repair["rolled_back_after_validation"] = True
                        deterministic_contract_repair["warnings"] = [
                            *(deterministic_contract_repair.get("warnings") or []),
                            "Rolled back deterministic contract repair because sealed validation did not pass.",
                        ]
                        public_tests = _run_case_tests(repo, case, public_only=True)
                        hidden_tests = _run_case_tests(repo, case, public_only=False)
                deterministic_contract_repair.pop("_snapshot", None)
            repair_attempts: list[dict[str, Any]] = []
            repair_limit = max(0, min(MAX_REPAIR_ROUNDS, int(args.max_repairs)))
            for repair_round in range(1, repair_limit + 1):
                if public_tests["passed"] and hidden_tests["passed"]:
                    break
                failure_context = _validation_failure_context(
                    public_tests,
                    hidden_tests,
                )
                before_repair = _candidate_snapshot(repo, case)
                before_quality = _validation_quality(public_tests, hidden_tests)
                repair = _repair_after_failure(
                    repo,
                    case,
                    diagnosis,
                    patch,
                    failure_context,
                    args.model,
                    calls,
                    args.timeout,
                    repair_round,
                )
                repair_attempts.append(repair)
                selected_history = [
                    str(value)
                    for value in patch.get("selected_files") or []
                    if str(value)
                ]
                for value in repair.get("selected_files") or []:
                    rel = str(value)
                    if rel and rel not in selected_history:
                        selected_history.append(rel)
                patch["selected_files"] = selected_history
                patch["warnings"] = [
                    *(patch.get("warnings") or []),
                    *(repair.get("warnings") or []),
                ]
                if not repair.get("patch_applied"):
                    rejection = (
                        "CHILI adapter rejected the attempted edit:\n"
                        + "\n".join(repair.get("warnings") or ["no applicable edit"])
                    )
                    if not public_tests["passed"]:
                        public_tests = {
                            **public_tests,
                            "output": f"{public_tests['output']}\n\n{rejection}",
                        }
                    if not hidden_tests["passed"]:
                        hidden_tests = {
                            **hidden_tests,
                            "output": f"{hidden_tests['output']}\n\n{rejection}",
                        }
                    continue
                patch["changed_files"] = _changed_candidate_files(
                    repo, case.get("candidate_paths") or []
                )
                patch["patch_applied"] = bool(patch["changed_files"])
                public_tests = _run_case_tests(repo, case, public_only=True)
                hidden_tests = _run_case_tests(repo, case, public_only=False)
                after_quality = _validation_quality(public_tests, hidden_tests)
                if after_quality < before_quality:
                    _restore_candidate_snapshot(repo, before_repair)
                    repair["rolled_back_after_validation"] = True
                    repair["validation_regression"] = {
                        "before_quality": before_quality,
                        "after_quality": after_quality,
                    }
                    rollback_warning = (
                        "Rolled back repair because validation regressed below the prior best state."
                    )
                    repair["warnings"] = [
                        *(repair.get("warnings") or []),
                        rollback_warning,
                    ]
                    patch["warnings"].append(rollback_warning)
                    patch["changed_files"] = _changed_candidate_files(
                        repo,
                        case.get("candidate_paths") or [],
                    )
                    patch["patch_applied"] = bool(patch["changed_files"])
                    public_tests = _run_case_tests(repo, case, public_only=True)
                    hidden_tests = _run_case_tests(repo, case, public_only=False)
            patch["changed_files"] = _changed_candidate_files(
                repo, case.get("candidate_paths") or []
            )
            patch["patch_applied"] = bool(patch["changed_files"])
            patch["selected_file"] = (
                patch["changed_files"][0] if len(patch["changed_files"]) == 1 else ""
            )
            score, checks = _score_case(
                oracle,
                diagnosis,
                patch,
                baseline_hidden,
                public_tests,
                hidden_tests,
            )
            report = diagnosis.get("report") if isinstance(diagnosis.get("report"), Mapping) else {}
            conclusion = report.get("conclusion") if isinstance(report.get("conclusion"), Mapping) else {}
            case_results.append(
                {
                    "case_id": case["case_id"],
                    "language": str(case.get("language") or "python"),
                    "test_runner": _case_test_runner(case),
                    "evaluation_role": str(
                        entry.get("evaluation_role") or "development_regression"
                    ),
                    "split": str(entry.get("split") or "holdout"),
                    "score": score,
                    "checks": checks,
                    "diagnosis_dimension": str(conclusion.get("dimension") or "unknown"),
                    "diagnosis_status": str(conclusion.get("status") or "inconclusive"),
                    "diagnosis_report": report,
                    "diagnosis_packet": diagnosis.get("packet") or {},
                    "selected_file": patch.get("selected_file") or "",
                    "selected_files": patch.get("selected_files") or [],
                    "changed_files": patch.get("changed_files") or [],
                    "patch_applied": bool(patch.get("patch_applied")),
                    "patch_warnings": patch.get("warnings") or [],
                    "public_tests": public_tests,
                    "hidden_tests": hidden_tests,
                    "baseline_hidden_tests": baseline_hidden,
                    "baseline_public_tests": baseline_public,
                    "repair_attempts": repair_attempts,
                    "deterministic_contract_repair": deterministic_contract_repair,
                    "model_calls": calls,
                    "premium_calls": 0,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }
            )

    average = sum(item["score"] for item in case_results) / len(case_results)
    average_duration = sum(item["duration_ms"] for item in case_results) / len(case_results)
    holdouts = [item for item in case_results if str(item.get("split") or "").startswith("holdout")]
    multifile_holdouts = [
        item for item in holdouts if "multifile" in str(item.get("split") or "")
    ]
    holdout_score = (
        sum(item["score"] for item in holdouts) / len(holdouts) if holdouts else 0.0
    )
    multifile_holdout_score = (
        sum(item["score"] for item in multifile_holdouts) / len(multifile_holdouts)
        if multifile_holdouts
        else 0.0
    )
    development_regressions = [
        item
        for item in case_results
        if item.get("evaluation_role") == "development_regression"
    ]
    blinded_holdouts = [
        item for item in case_results if item.get("evaluation_role") == "blinded_holdout"
    ]
    development_regression_score = (
        sum(item["score"] for item in development_regressions) / len(development_regressions)
        if development_regressions
        else 0.0
    )
    blinded_holdout_score = (
        sum(item["score"] for item in blinded_holdouts) / len(blinded_holdouts)
        if blinded_holdouts
        else None
    )
    evaluation_verdict = (
        "blinded_evaluation_passed"
        if blinded_holdouts and _verdict(blinded_holdouts) == "shadow_ready"
        else "blinded_evaluation_failed"
        if blinded_holdouts
        else "development_regression_passed"
        if _verdict(case_results) == "shadow_ready"
        else "development_regression_failed"
    )
    results = {
        "schema": "chili.diagnosis-to-fix-results.v2",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "reference_family": manifest.get("reference_family") or "claude-fable-5",
        "overall_score": round(average, 2),
        "holdout_score": round(holdout_score, 2),
        "multifile_holdout_score": round(multifile_holdout_score, 2),
        "holdout_case_count": len(holdouts),
        "multifile_holdout_case_count": len(multifile_holdouts),
        "development_regression_score": round(development_regression_score, 2),
        "development_regression_case_count": len(development_regressions),
        "blinded_holdout_score": (
            round(blinded_holdout_score, 2) if blinded_holdout_score is not None else None
        ),
        "blinded_holdout_case_count": len(blinded_holdouts),
        "average_case_duration_ms": round(average_duration, 2),
        "verdict": _verdict(case_results),
        "evaluation_verdict": evaluation_verdict,
        "premium_calls": 0,
        "max_repair_rounds": max(
            0,
            min(MAX_REPAIR_ROUNDS, int(args.max_repairs)),
        ),
        "fable5_head_to_head_run": False,
        "fable5_parity_claim": False,
        "cases": case_results,
    }
    report_path = Path(args.report).resolve()
    results_path = Path(args.results_json).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(_markdown(results), encoding="utf-8")
    results_path.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return results


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-root", default=str(DEFAULT_FIXTURE_ROOT))
    parser.add_argument("--model", default="qwen2.5-coder:7b")
    parser.add_argument("--case", action="append")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--max-repairs", type=int, default=5)
    parser.add_argument("--validate-fixtures", action="store_true")
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--results-json", default=str(DEFAULT_RESULTS))
    parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    results = run(args)
    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    elif args.validate_fixtures:
        print(f"fixtures_valid={results['valid']} cases={len(results['cases'])}")
    else:
        print(
            f"overall={results['overall_score']:.1f} verdict={results['verdict']} "
            "premium_calls=0"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Local-only diagnosis-to-fix benchmark with sealed final adjudication.

The model sees the case prompt and candidate repository only. Oracle labels and
repair-feedback tests are loaded after the initial patch. For blinded holdouts,
final tests live in a separate oracle that is first read after every model call.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
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
from app.services.coding_task import validation_contracts, validator_runner  # noqa: E402
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
    "baseline_final_failure": 5,
    "diagnosis": 15,
    "file_selection": 10,
    "patch_applied": 5,
    "public_tests": 10,
    "final_tests": 45,
    "premium_independence": 10,
}
REPAIR_DIMENSION_RUBRIC = dict(diagnostic_reasoning.CAUSAL_DIMENSION_RUBRIC)
REPAIR_PLAN_SCHEMA = {
    "type": "object",
    "required": ["dimension", "analysis", "files", "contract_coverage"],
    "properties": {
        "dimension": {"enum": sorted(REPAIR_DIMENSION_RUBRIC)},
        "analysis": {"type": "string"},
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["path", "action", "description"],
                "properties": {"action": {"enum": ["modify"]}},
            },
        },
        "contract_coverage": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["contract", "owner_paths", "postcondition"],
            },
        },
        "notes": {"type": "string"},
    },
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


def _fixture_path(root: Path, value: object, label: str) -> Path:
    relative = _safe_rel(value)
    if not relative:
        raise ValueError(f"Unsafe or missing {label} fixture path: {value!r}")
    resolved_root = root.resolve()
    path = (resolved_root / relative).resolve()
    try:
        path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"{label} fixture path escapes fixture root: {value!r}") from exc
    if not path.is_file():
        raise ValueError(f"{label} fixture file does not exist: {relative}")
    return path


def _write_files(root: Path, files: Mapping[str, Any]) -> None:
    for raw_path, content in files.items():
        rel = _safe_rel(raw_path)
        if not rel:
            raise ValueError(f"Unsafe fixture path: {raw_path!r}")
        target = (root / rel).resolve()
        target.relative_to(root.resolve())
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")


def _normalize_test_files(files: Mapping[str, Any], label: str) -> dict[str, str]:
    normalized: dict[str, str] = {}
    canonical_paths: set[str] = set()
    for raw_path, content in files.items():
        relative = _safe_rel(raw_path)
        if not relative or not relative.startswith("tests/"):
            raise ValueError(
                f"{label} fixture path must stay under tests/: {raw_path!r}"
            )
        canonical = relative.casefold()
        if canonical in canonical_paths:
            raise ValueError(
                f"{label} fixture contains a case-insensitive path collision: "
                f"{raw_path!r}"
            )
        canonical_paths.add(canonical)
        normalized[relative] = str(content)
    return normalized


def _oracle_test_partitions(
    oracle: Mapping[str, Any],
    *,
    final_oracle: Mapping[str, Any] | None = None,
    require_sealed: bool = False,
    require_external_final: bool = False,
) -> dict[str, Any]:
    external_final = final_oracle is not None
    if require_external_final and not external_final:
        raise ValueError(
            "Blinded holdouts require a separately loaded final_oracle."
        )
    if external_final:
        if "feedback_files" not in oracle:
            raise ValueError(
                "External final adjudication requires feedback_files in the repair oracle."
            )
        if "final_files" in oracle:
            raise ValueError(
                "Repair oracle must not embed final_files when final_oracle is separate."
            )
        feedback_raw = oracle.get("feedback_files")
        final_raw = final_oracle.get("final_files")
        sealed = True
    else:
        explicit_feedback = "feedback_files" in oracle
        explicit_final = "final_files" in oracle
        if explicit_feedback != explicit_final:
            raise ValueError(
                "Sealed fixtures must define both feedback_files and final_files."
            )
        sealed = explicit_feedback and explicit_final
        feedback_raw = (
            oracle.get("feedback_files") if sealed else oracle.get("hidden_files")
        )
        final_raw = oracle.get("final_files") if sealed else oracle.get("hidden_files")
    if require_sealed and not sealed:
        raise ValueError(
            "Blinded holdouts require disjoint feedback_files and final_files."
        )
    if not isinstance(feedback_raw, Mapping) or not feedback_raw:
        raise ValueError("Fixture has no repair-feedback test files.")
    if not isinstance(final_raw, Mapping) or not final_raw:
        raise ValueError("Fixture has no final adjudication test files.")

    feedback = _normalize_test_files(feedback_raw, "feedback")
    final = _normalize_test_files(final_raw, "final")
    if sealed:
        feedback_paths = {path.casefold(): path for path in feedback}
        final_paths = {path.casefold(): path for path in final}
        overlapping_paths = sorted(
            feedback_paths[path]
            for path in set(feedback_paths) & set(final_paths)
        )
        if overlapping_paths:
            raise ValueError(
                "Sealed feedback/final test paths overlap: "
                + ", ".join(overlapping_paths)
            )
        duplicate_payloads = set(feedback.values()) & set(final.values())
        if duplicate_payloads:
            raise ValueError(
                "Sealed feedback/final test payloads must be independently authored."
            )
    return {
        "feedback_files": feedback,
        "final_files": final,
        "sealed": sealed,
        "external_final": external_final,
    }


def _validate_oracle_test_paths(
    case: Mapping[str, Any],
    partitions: Mapping[str, Any],
) -> None:
    seeded_files = case.get("repo_files") or {}
    if not isinstance(seeded_files, Mapping):
        raise ValueError("Case repo_files must be an object.")
    seeded_paths = {
        relative.casefold(): relative
        for raw_path in seeded_files
        if (relative := _safe_rel(raw_path))
    }
    runner = _case_test_runner(case)
    runner_suffixes = {
        "pytest": (".py",),
        "node_test": (
            ".test.js",
            ".test.mjs",
            ".test.cjs",
            ".test.ts",
            ".test.mts",
            ".test.cts",
        ),
        "dart": ("_test.dart",),
    }[runner]

    def discoverable(path: str) -> bool:
        name = Path(path).name.casefold()
        if runner == "pytest":
            return name.endswith(".py") and (
                name.startswith("test_") or name.endswith("_test.py")
            )
        return name.endswith(runner_suffixes)

    seeded_test_count = sum(
        1 for path in seeded_paths.values() if discoverable(path)
    )
    for key, label in (
        ("feedback_files", "Repair-feedback"),
        ("final_files", "Final adjudication"),
    ):
        files = partitions.get(key)
        if not isinstance(files, Mapping):
            continue
        oracle_paths = {str(path).casefold(): str(path) for path in files}
        overlap = sorted(
            oracle_paths[path]
            for path in set(seeded_paths) & set(oracle_paths)
        )
        if overlap:
            raise ValueError(
                f"{label} tests must not overwrite seeded case files: "
                + ", ".join(overlap)
            )
        discoverable_count = sum(1 for path in files if discoverable(str(path)))
        if discoverable_count == 0:
            raise ValueError(
                f"{label} partition has no discoverable {runner} test file."
            )
        if runner != "pytest" and seeded_test_count + discoverable_count > MAX_TEST_FILES:
            raise ValueError(
                f"{label} partition exceeds the bounded {runner} test-file cap "
                f"of {MAX_TEST_FILES}."
            )


def _expected_owner_paths(oracle: Mapping[str, Any]) -> set[str]:
    raw = oracle.get("expected_files") or [oracle.get("expected_file")]
    return {
        relative
        for value in raw
        if (relative := _safe_rel(value))
    }


def _validate_expected_ownership(
    case: Mapping[str, Any],
    oracle: Mapping[str, Any],
) -> None:
    expected = _expected_owner_paths(oracle)
    candidates = {
        relative
        for value in case.get("candidate_paths") or []
        if (relative := _safe_rel(value))
    }
    if not expected:
        raise ValueError("Repair oracle must identify at least one expected owner.")
    outside = sorted(expected - candidates)
    if outside:
        raise ValueError(
            "Expected owners are not approved candidate source files: "
            + ", ".join(outside)
        )
    budget = _case_max_files(case)
    if len(expected) > budget:
        raise ValueError(
            f"Expected owner count {len(expected)} exceeds max_files budget {budget}."
        )


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
            encoding="utf-8",
            errors="replace",
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
        [
            "git",
            "commit",
            "-m",
            "[chili-synthetic-fixture] seed held-out case",
        ],
    ):
        code, output, _ = _run(args, root, timeout=30)
        if code != 0:
            raise RuntimeError(f"Fixture git setup failed: {output}")


def _run_pytest(
    root: Path,
    selector: str = "tests",
    *,
    stop_after_first: bool = True,
) -> dict[str, Any]:
    args = [
        sys.executable,
        "-m",
        "pytest",
        selector,
        "-vv",
        "--tb=short",
        "--disable-warnings",
    ]
    if stop_after_first:
        args.append("--maxfail=1")
    code, output, duration = _run(
        args,
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


def _dart_declared_contracts(path: Path, relative: str) -> list[str]:
    """Extract ordered message identities from the benchmark's tiny Dart harness."""
    content = path.read_text(encoding="utf-8", errors="replace")
    contracts: list[str] = []
    for match in re.finditer(r"\bcheck\s*\((.*?)\)\s*;", content, re.DOTALL):
        literals = re.findall(r"(['\"])(.*?)\1", match.group(1), re.DOTALL)
        if not literals:
            continue
        message = literals[-1][1]
        contract_id = validation_contracts.normalize_contract_id(
            f"{relative}::{message}"
        )
        if contract_id and contract_id not in contracts:
            contracts.append(contract_id)
    return contracts


def _run_case_tests(
    root: Path,
    case: Mapping[str, Any],
    *,
    public_only: bool,
) -> dict[str, Any]:
    runner = _case_test_runner(case)
    if runner == "pytest":
        result = _run_pytest(
            root,
            "tests/test_public.py" if public_only else "tests",
            stop_after_first=public_only,
        )
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
        outputs: list[str] = []
        duration = 0
        code = 0
        for test_file in files:
            file_code, output, file_duration = _run(
                [executable, "--test", "--test-reporter=spec", test_file],
                root,
                timeout=90,
            )
            outputs.append(f"[{test_file}]\n{output}".rstrip())
            duration += file_duration
            if file_code != 0 and code == 0:
                code = file_code
        return {
            "passed": code == 0,
            "exit_code": code,
            "output": "\n\n".join(outputs)[-20_000:],
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
    contract_status: dict[str, str] = {}
    contracts_complete = True
    for test_file in files:
        code, output, duration = _run(
            [executable, "run", test_file],
            root,
            timeout=90,
        )
        total_duration += duration
        outputs.append(f"[{test_file}]\n{output}".rstrip())
        declared = _dart_declared_contracts(root / test_file, test_file)
        if code == 0:
            for contract_id in declared or [
                validation_contracts.normalize_contract_id(f"{test_file}::script")
            ]:
                contract_status[contract_id] = "passed"
        else:
            contracts_complete = False
            failure = re.search(r"\bBad state:\s*(.+)$", output, re.IGNORECASE | re.MULTILINE)
            failed_id = validation_contracts.normalize_contract_id(
                f"{test_file}::{failure.group(1)}"
            ) if failure else ""
            if failed_id in declared:
                failed_at = declared.index(failed_id)
                for contract_id in declared[:failed_at]:
                    contract_status[contract_id] = "passed"
            if failed_id:
                contract_status[failed_id] = "failed"
            if exit_code == 0:
                exit_code = code
    return {
        "passed": exit_code == 0,
        "exit_code": exit_code,
        "output": "\n\n".join(outputs)[-20_000:],
        "duration_ms": total_duration,
        "runner": runner,
        "test_files": files,
        "test_contract_status": contract_status,
        "test_contracts_complete": contracts_complete,
    }


def _run_final_adjudication(
    case: Mapping[str, Any],
    final_files: Mapping[str, Any],
    *,
    candidate_repo: Path | None = None,
) -> dict[str, Any]:
    """Run final tests in a fresh repo that never contains feedback tests."""
    with tempfile.TemporaryDirectory(prefix="chili-final-adjudication-") as temp:
        final_repo = Path(temp) / "repo"
        _init_repo(final_repo, case.get("repo_files") or {})
        if candidate_repo is not None:
            for value in case.get("candidate_paths") or []:
                relative = _safe_rel(value)
                if not relative:
                    continue
                source = candidate_repo / relative
                target = final_repo / relative
                if not source.is_file() or not target.is_file():
                    raise RuntimeError(
                        f"Final adjudication cannot overlay candidate source: {relative}"
                    )
                target.write_text(
                    source.read_text(encoding="utf-8", errors="replace"),
                    encoding="utf-8",
                )
        _write_files(final_repo, final_files)
        result = _run_case_tests(final_repo, case, public_only=False)
    return {**result, "isolated_final_repo": True}


def _validation_failure_context(
    public_tests: Mapping[str, Any],
    feedback_tests: Mapping[str, Any],
) -> str:
    raw_outputs = [
        str(public_tests.get("output") or ""),
        str(feedback_tests.get("output") or ""),
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
    if not feedback_tests.get("passed"):
        sections.append(
            "REPAIR-FEEDBACK FAILURE (not final adjudication):\n"
            + str(feedback_tests.get("output") or "")[-3500:]
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


def _read_only_test_context(
    repo: Path,
    test_paths: Sequence[str],
    *,
    max_chars: int = 14_000,
) -> str:
    parts: list[str] = []
    remaining = max_chars
    for value in test_paths:
        relative = _safe_rel(value)
        path = repo / relative if relative else None
        if (
            not relative
            or not relative.startswith("tests/")
            or path is None
            or not path.is_file()
            or remaining <= 0
        ):
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        excerpt = content[:remaining]
        parts.append(f"### {relative} (read-only repair feedback; never edit)\n{excerpt}")
        remaining -= len(excerpt)
    return "\n\n".join(parts)


def _feedback_exercised_candidates(
    feedback_context: str,
    candidates: Sequence[str],
) -> list[str]:
    lower = str(feedback_context or "").lower()
    safe = [relative for value in candidates if (relative := _safe_rel(value))]
    basename_counts: dict[str, int] = {}
    for relative in safe:
        basename = Path(relative).name.lower()
        basename_counts[basename] = basename_counts.get(basename, 0) + 1
    exercised: list[str] = []
    for relative in safe:
        normalized = relative.lower()
        path_without_suffix = str(Path(normalized).with_suffix("")).replace("\\", "/")
        module_name = path_without_suffix.replace("/", ".")
        basename = Path(normalized).name.lower()
        if (
            normalized in lower
            or module_name in lower
            or (basename_counts.get(basename) == 1 and basename in lower)
        ):
            exercised.append(relative)
    return exercised


def _case_max_files(case: Mapping[str, Any]) -> int:
    candidates = {
        relative
        for value in case.get("candidate_paths") or []
        if (relative := _safe_rel(value))
    }
    default = max(1, min(4, len(candidates)))
    try:
        raw = case.get("max_files")
        value = default if raw in {None, ""} else int(raw)
    except (TypeError, ValueError):
        value = default
    return max(1, min(4, value))


def _plan_dimension(plan: Mapping[str, Any]) -> str:
    dimension = (
        str(plan.get("dimension") or "")
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )
    return dimension if dimension in REPAIR_DIMENSION_RUBRIC else ""


def _initialize_accepted_diagnosis(diagnosis: dict[str, Any]) -> None:
    report = diagnosis.get("report") if isinstance(diagnosis.get("report"), Mapping) else {}
    conclusion = report.get("conclusion") if isinstance(report.get("conclusion"), Mapping) else {}
    dimension = str(conclusion.get("dimension") or "unknown").strip().lower()
    if dimension not in REPAIR_DIMENSION_RUBRIC:
        dimension = "unknown"
    case = diagnosis.get("case") if isinstance(diagnosis.get("case"), Mapping) else {}
    decisive_dimension = diagnostic_reasoning.decisive_inferred_dimension(
        str(case.get("problem_statement") or "")
    )
    causal_sufficiency = str(conclusion.get("causal_sufficiency") or "observational")
    conclusion_status = str(conclusion.get("status") or "inconclusive")
    causally_accepted = bool(
        conclusion_status == "confirmed"
        and causal_sufficiency in {"graph_linked", "isolated"}
    )
    stage = "diagnostic_judge"
    validation_evidence = "pre-repair evidence decision"
    taxonomy_advisory = ""
    if decisive_dimension != "unknown" and dimension != decisive_dimension:
        taxonomy_advisory = decisive_dimension
        validation_evidence = (
            "prompt taxonomy remained advisory because it is not causal evidence"
        )
    record = {
        "dimension": dimension,
        "stage": stage,
        "accepted": causally_accepted,
        "json_parsed": True,
        "causal_status": "accepted" if causally_accepted else "working_hypothesis",
        "validation_evidence": validation_evidence,
        "taxonomy_advisory_dimension": taxonomy_advisory,
    }
    diagnosis["accepted_conclusion"] = dict(record)
    diagnosis["diagnosis_history"] = [record]


def _accept_diagnosis_proposal(
    diagnosis: dict[str, Any],
    dimension: str,
    *,
    stage: str,
    validation_evidence: str,
) -> bool:
    normalized = str(dimension or "").strip().lower()
    if normalized not in REPAIR_DIMENSION_RUBRIC:
        return False
    current = _effective_diagnosis_dimension(diagnosis)
    if current not in {"", "unknown", normalized}:
        diagnosis.setdefault("diagnosis_history", []).append(
            {
                "dimension": normalized,
                "stage": stage,
                "accepted": False,
                "validation_evidence": validation_evidence[:2_000],
                "rejection_reason": (
                    "repair-plan labels are not independent causal evidence for changing family"
                ),
            }
        )
        return False
    current_record = (
        diagnosis.get("accepted_conclusion")
        if isinstance(diagnosis.get("accepted_conclusion"), Mapping)
        else {}
    )
    causally_accepted = bool(current_record.get("accepted"))
    record = {
        "dimension": normalized,
        "stage": stage,
        "accepted": causally_accepted,
        "causal_status": "accepted" if causally_accepted else "working_hypothesis",
        "repair_progress_validated": True,
        "validation_evidence": validation_evidence[:2_000],
    }
    diagnosis["accepted_conclusion"] = dict(record)
    diagnosis.setdefault("diagnosis_history", []).append(record)
    return True


def _effective_diagnosis_dimension(diagnosis: Mapping[str, Any]) -> str:
    accepted = (
        diagnosis.get("accepted_conclusion")
        if isinstance(diagnosis.get("accepted_conclusion"), Mapping)
        else {}
    )
    dimension = str(accepted.get("dimension") or "").strip().lower()
    if dimension in REPAIR_DIMENSION_RUBRIC:
        return dimension
    report = diagnosis.get("report") if isinstance(diagnosis.get("report"), Mapping) else {}
    conclusion = report.get("conclusion") if isinstance(report.get("conclusion"), Mapping) else {}
    return str(conclusion.get("dimension") or "unknown").strip().lower()


def _candidate_snapshot(repo: Path, case: Mapping[str, Any]) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for value in case.get("candidate_paths") or []:
        relative = _safe_rel(value)
        path = repo / relative if relative else None
        if path is not None and path.is_file():
            snapshot[relative] = path.read_text(encoding="utf-8", errors="replace")
    return snapshot


def _prompt_contract_closure(
    repo: Path,
    case: Mapping[str, Any],
) -> dict[str, str]:
    contents = {
        relative: (repo / relative).read_text(encoding="utf-8", errors="replace")
        for value in case.get("candidate_paths") or []
        for relative in [_safe_rel(value)]
        if relative and (repo / relative).is_file()
    }
    warnings = diagnostic_reasoning.contract_invariant_warnings(
        str(case.get("prompt") or ""),
        contents,
    )
    return {
        f"prompt_contract::{hashlib.sha256(str(warning).encode('utf-8')).hexdigest()[:12]}": str(
            warning
        )
        for warning in sorted(set(warnings))
    }


def _restore_candidate_snapshot(repo: Path, snapshot: Mapping[str, str]) -> None:
    for relative, content in snapshot.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _snapshot_fingerprint(snapshot: Mapping[str, str]) -> str:
    payload = json.dumps(dict(sorted(snapshot.items())), separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def _snapshot_diff(
    before: Mapping[str, str],
    after: Mapping[str, str],
    *,
    max_chars: int = 6_000,
) -> str:
    chunks: list[str] = []
    for relative in sorted(set(before) | set(after)):
        old = str(before.get(relative) or "")
        new = str(after.get(relative) or "")
        if old == new:
            continue
        chunks.append(
            "".join(
                difflib.unified_diff(
                    old.splitlines(keepends=True),
                    new.splitlines(keepends=True),
                    fromfile=f"a/{relative}",
                    tofile=f"b/{relative}",
                )
            )
        )
    return "\n".join(chunks)[:max_chars]


def _apply_deterministic_contract_repair(
    repo: Path,
    case: Mapping[str, Any],
) -> dict[str, Any]:
    snapshot = _candidate_snapshot(repo, case)
    proposed_dimension = diagnostic_reasoning.contract_repair_dimension(
        str(case.get("prompt") or ""),
        snapshot,
    )
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
            "proposed_dimension": "unknown",
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
            "proposed_dimension": proposed_dimension,
        }
    return {
        "attempted": True,
        "patch_applied": True,
        "selected_files": sorted(proposals),
        "warnings": [],
        "proposed_dimension": proposed_dimension,
        "_snapshot": snapshot,
    }


def _accept_validated_contract_repair_diagnosis(
    diagnosis: dict[str, Any],
    dimension: str,
    *,
    stage: str,
    validation_evidence: str,
) -> bool:
    normalized = str(dimension or "").strip().lower()
    if normalized not in REPAIR_DIMENSION_RUBRIC:
        return False
    record = {
        "dimension": normalized,
        "stage": stage,
        "accepted": True,
        "causal_status": "accepted",
        "causal_sufficiency": "graph_linked",
        "repair_progress_validated": True,
        "validation_evidence": validation_evidence[:2_000],
    }
    diagnosis["accepted_conclusion"] = dict(record)
    diagnosis.setdefault("diagnosis_history", []).append(record)
    return True


def _patch_from_deterministic_contract_repair(
    repair: Mapping[str, Any],
    diagnosis: Mapping[str, Any],
) -> dict[str, Any]:
    selected = [str(value) for value in repair.get("selected_files") or [] if str(value)]
    dimension = _effective_diagnosis_dimension(diagnosis)
    return {
        "plan": {
            "dimension": dimension,
            "analysis": "A recognized mechanical contract violation has a bounded source-shape repair.",
            "files": [
                {
                    "path": relative,
                    "action": "modify",
                    "description": "Apply the recognized contract-preserving repair.",
                }
                for relative in selected
            ],
            "contract_coverage": [],
            "notes": "Local deterministic contract lane; validation remains authoritative.",
        },
        "selected_file": selected[0] if len(selected) == 1 else "",
        "selected_files": selected,
        "applied_files": selected,
        "patch_applied": bool(selected),
        "warnings": [
            "Applied a recognized mechanical contract repair before generative editing."
        ],
        "deterministic_initial_patch": True,
    }


def _validation_quality(
    public_tests: Mapping[str, Any],
    feedback_tests: Mapping[str, Any],
) -> int:
    if not bool(public_tests.get("passed")):
        return 0
    if bool(feedback_tests.get("passed")):
        return 3
    try:
        feedback_exit = int(feedback_tests.get("exit_code"))
    except (TypeError, ValueError):
        feedback_exit = 1
    return 1 if feedback_exit == 124 else 2


def _reported_test_count(output: str, label: str) -> int:
    patterns = (
        rf"\b{re.escape(label)}\s+(\d+)\b",
        rf"\b(\d+)\s+{re.escape(label)}(?:ed)?\b",
    )
    values = [
        int(match.group(1))
        for pattern in patterns
        for match in re.finditer(pattern, output, flags=re.IGNORECASE)
    ]
    return max(values, default=0)


def _normalized_failure_signature(result: Mapping[str, Any]) -> str:
    output = validation_contracts.normalize_failure_text(result.get("output") or "")
    return hashlib.sha256(output[:12_000].encode("utf-8", errors="replace")).hexdigest()


def _validation_progress(
    public_tests: Mapping[str, Any],
    feedback_tests: Mapping[str, Any],
) -> tuple[int, int, int, int]:
    if not bool(public_tests.get("passed")):
        return (0, 0, 0, 0)
    if bool(feedback_tests.get("passed")):
        return (4, 0, 0, 0)
    try:
        exit_code = int(feedback_tests.get("exit_code"))
    except (TypeError, ValueError):
        exit_code = 1
    if exit_code == 124:
        return (1, 0, 0, 0)
    output = str(feedback_tests.get("output") or "")
    lower = output.lower()
    syntax_markers = (
        "syntaxerror",
        "compile error",
        "compilation failed",
        "validator_unavailable",
        "cannot find module",
    )
    assertion_markers = (
        "assertionerror",
        "assertion failed",
        "expected values to be",
        "assert ",
        "bad state:",
        "stateerror",
    )
    phase = (
        0
        if any(marker in lower for marker in syntax_markers)
        else 2
        if any(marker in lower for marker in assertion_markers)
        else 1
    )
    passed = _reported_test_count(output, "pass")
    failed = _reported_test_count(output, "fail")
    return (2, phase, passed, -failed)


def _validation_advanced(
    before_public: Mapping[str, Any],
    before_feedback: Mapping[str, Any],
    after_public: Mapping[str, Any],
    after_feedback: Mapping[str, Any],
) -> bool:
    if not bool(after_public.get("passed")):
        return False
    if bool(after_feedback.get("passed")):
        return not bool(before_feedback.get("passed"))

    before_contracts = validation_contracts.test_contract_evidence(before_feedback)
    after_contracts = validation_contracts.test_contract_evidence(after_feedback)
    if validation_contracts.contract_regressions(before_contracts, after_contracts):
        return False
    if validation_contracts.contract_progressed(before_contracts, after_contracts):
        return True

    try:
        before_exit = int(before_feedback.get("exit_code"))
    except (TypeError, ValueError):
        before_exit = 1
    try:
        after_exit = int(after_feedback.get("exit_code"))
    except (TypeError, ValueError):
        after_exit = 1
    if before_exit == 124 and after_exit != 124:
        return True
    before_progress = _validation_progress(before_public, before_feedback)
    after_progress = _validation_progress(after_public, after_feedback)
    return bool(
        before_progress[1] == 0
        and after_progress[1] > 0
        and after_contracts.get("identity_available")
    )


def _attempt_ledger_context(attempts: Sequence[Mapping[str, Any]]) -> str:
    entries = []
    for item in attempts[-6:]:
        entries.append(
            {
                "round": item.get("round"),
                "selected_files": item.get("selected_files") or [],
                "attempt_fingerprint": item.get("attempt_fingerprint") or "",
                "rolled_back": bool(item.get("rolled_back_after_validation")),
                "duplicate_attempt": bool(item.get("duplicate_attempt")),
                "before_failure": item.get("before_failure_signature") or "",
                "after_failure": item.get("after_failure_signature") or "",
                "before_contracts": item.get("before_test_contracts") or {},
                "after_contracts": item.get("after_test_contracts") or {},
                "attempted_diff": str(item.get("attempted_diff") or "")[:3_000],
                "optional_rejected_diffs": [
                    {
                        "path": str(rejected.get("path") or ""),
                        "reason": str(rejected.get("reason") or "")[:500],
                        "attempted_diff": str(rejected.get("attempted_diff") or "")[:1_500],
                        "validation_output": str(rejected.get("validation_output") or "")[:1_000],
                    }
                    for rejected in item.get("optional_rejected_diffs") or []
                    if isinstance(rejected, Mapping)
                ][-3:],
                "adapter_rejection": str(item.get("adapter_rejection") or "")[:1_500],
                "validation_output": str(item.get("validation_output") or "")[:3_500],
                "warnings": [str(value) for value in item.get("warnings") or []][-3:],
            }
        )
    return json.dumps(entries, indent=2, sort_keys=True)


def _repair_model_schedule(args: argparse.Namespace) -> list[str]:
    base_limit = max(0, min(MAX_REPAIR_ROUNDS, int(args.max_repairs)))
    escalation_model = str(getattr(args, "escalation_model", "") or "").strip()
    escalation_limit = max(
        0,
        min(
            MAX_REPAIR_ROUNDS - base_limit,
            int(getattr(args, "max_escalation_repairs", 0) or 0),
        ),
    )
    return [str(args.model)] * base_limit + [escalation_model] * (
        escalation_limit if escalation_model else 0
    )


def _repair_plan_has_complete_contract_coverage(
    plan: Mapping[str, Any],
    candidates: Sequence[str],
    contract_evidence: Mapping[str, Any] | None,
) -> bool:
    allowed = {str(value) for value in candidates if str(value)}
    coverage = [
        item
        for item in plan.get("contract_coverage") or []
        if isinstance(item, Mapping)
    ]
    raw_failed = (contract_evidence or {}).get("failed_ids") or []
    prompt_contract_details = (
        (contract_evidence or {}).get("prompt_contract_details") or {}
    )
    failed_values = (
        [value for value in re.split(r"\s+", raw_failed) if value]
        if isinstance(raw_failed, str)
        else [str(value) for value in raw_failed if str(value)]
    )
    failed_ids: list[str] = []
    for value in failed_values:
        normalized = validation_contracts.normalize_contract_id(value)
        if re.match(r"^tests/.+\.py::", normalized):
            normalized = re.sub(
                r"\s+-\s+[a-z_][\w.]*?(?:error|exception)(?::|\s).*$",
                "",
                normalized,
            ).strip()
        if normalized and normalized not in failed_ids:
            failed_ids.append(normalized)
    if not coverage or (not failed_ids and not prompt_contract_details):
        return False
    owner_union: set[str] = set()
    for item in coverage:
        contract = str(item.get("contract") or "").strip()
        postcondition = str(item.get("postcondition") or "").strip()
        owners = {
            relative
            for value in item.get("owner_paths") or []
            if (relative := _safe_rel(value))
        }
        if not contract or len(postcondition) < 8 or not owners or not owners <= allowed:
            return False
        owner_union.update(owners)
    selected = {
        relative
        for item in plan.get("files") or []
        if isinstance(item, Mapping)
        and code_agent._is_mutating_plan_action(item.get("action"))
        and (relative := _safe_rel(item.get("path")))
    }
    if not owner_union <= selected:
        return False
    if not failed_ids:
        return True
    terminal_counts: dict[str, int] = {}
    for value in failed_ids:
        terminal = value.rsplit("::", 1)[-1]
        terminal_counts[terminal] = terminal_counts.get(terminal, 0) + 1
    coverage_contracts = [
        validation_contracts.normalize_contract_id(item.get("contract"))
        for item in coverage
    ]
    for value in failed_ids:
        terminal = value.rsplit("::", 1)[-1]
        aliases = [value]
        if terminal_counts.get(terminal) == 1:
            aliases.append(terminal)
        if not any(
            alias and alias in contract
            for contract in coverage_contracts
            for alias in aliases
        ):
            return False
    return True


def _align_plan_files_to_contract_coverage(
    plan: Mapping[str, Any],
    candidates: Sequence[str],
    max_files: int,
) -> dict[str, Any]:
    """Make explicit contract ownership authoritative over draft file guesses."""
    allowed = {str(value) for value in candidates if str(value)}
    original_files = [
        dict(item)
        for item in plan.get("files") or []
        if isinstance(item, Mapping)
    ]
    by_path = {
        relative: item
        for item in original_files
        if (relative := _safe_rel(item.get("path"))) in allowed
    }
    owner_contracts: dict[str, list[Mapping[str, Any]]] = {}
    owner_order: list[str] = []
    for contract in plan.get("contract_coverage") or []:
        if not isinstance(contract, Mapping):
            continue
        for value in contract.get("owner_paths") or []:
            relative = _safe_rel(value)
            if not relative or relative not in allowed:
                continue
            if relative not in owner_contracts:
                owner_contracts[relative] = []
                owner_order.append(relative)
            owner_contracts[relative].append(contract)
    if not owner_order:
        return dict(plan)
    aligned: list[dict[str, Any]] = []
    for relative in owner_order[:max_files]:
        existing = by_path.get(relative, {})
        description = str(existing.get("description") or "").strip()
        if not description:
            postconditions = [
                str(item.get("postcondition") or "").strip()
                for item in owner_contracts[relative]
                if str(item.get("postcondition") or "").strip()
            ]
            description = "Implement the owned validation contracts: " + "; ".join(
                postconditions
            )
        aligned.append(
            {
                "path": relative,
                "action": "modify",
                "description": description,
            }
        )
    return {**dict(plan), "files": aligned}


def _repair_plan_fingerprint(plan: Mapping[str, Any]) -> str:
    canonical = {
        "dimension": _plan_dimension(plan),
        "files": sorted(
            (
                _safe_rel(item.get("path")) or "",
                str(item.get("action") or "").strip().casefold(),
                str(item.get("description") or "").strip().casefold(),
            )
            for item in plan.get("files") or []
            if isinstance(item, Mapping)
        ),
        "contract_coverage": sorted(
            (
                validation_contracts.normalize_contract_id(item.get("contract")),
                tuple(
                    sorted(
                        relative
                        for value in item.get("owner_paths") or []
                        if (relative := _safe_rel(value))
                    )
                ),
                validation_contracts.normalize_contract_id(item.get("postcondition")),
            )
            for item in plan.get("contract_coverage") or []
            if isinstance(item, Mapping)
        ),
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _plan_prompt(
    prompt: str,
    candidates: list[str],
    context: str,
    report: Mapping[str, Any],
    max_files: int,
) -> str:
    invariants = diagnostic_reasoning.derive_contract_invariants(prompt)
    return (
        "Return one JSON object only. Classify the causal owner with the supplied dimension rubric, then select "
        "only the owning source files required for the diagnosed bug. "
        f"Use at most {max_files} files; use more than one only when the behavior crosses an interface. "
        "Do not select tests or invent paths. Give each file a specific coordinated responsibility. List every "
        "independent contract with its owning path(s) and a concrete postcondition; omit a file when it is only "
        "context and needs no mutation. Required JSON schema:\n"
        f"{json.dumps(REPAIR_PLAN_SCHEMA, indent=2)}\n\n"
        f"Request:\n{prompt}\n\n"
        f"Dimension rubric:\n{json.dumps(REPAIR_DIMENSION_RUBRIC, indent=2)}\n\n"
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
        f"- Local escalation model: `{results.get('escalation_model') or 'disabled'}`",
        f"- Reference family: `{results['reference_family']}`",
        f"- Overall score: **{results['overall_score']:.1f}/100**",
        f"- Development-regression score: **{results['development_regression_score']:.1f}/100**",
        f"- Blinded holdout score: {blinded_line}",
        f"- Functional sealed-final solve rate: **{results['functional_solve_rate']:.1f}%**",
        f"- Causal-diagnosis accuracy: **{results['diagnosis_accuracy']:.1f}%**",
        f"- Exact changed-file-set accuracy: **{results['exact_file_set_accuracy']:.1f}%**",
        f"- JSON-valid diagnostic stages: **{results['diagnostic_stage_acceptance_rate']:.1f}%**",
        f"- Causally accepted diagnoses: **{results['causal_diagnosis_acceptance_rate']:.1f}%**",
        f"- Autonomy verdict: **{results['verdict']}**",
        f"- Comparison verdict: **{results['evaluation_verdict']}**",
        "- Premium calls: **0**",
        f"- Average wall time: **{results['average_case_duration_ms'] / 1000:.1f}s/case**",
        f"- Maximum bounded repair rounds: **{results['max_repair_rounds']}**",
        f"- Escalation repair rounds: **{results.get('max_escalation_repair_rounds', 0)}**",
        "- Fable 5 parity claim: **No**. No authenticated same-task Fable 5 head-to-head is included.",
        "",
        "| Case | Language | Runner | Evaluation | Split | Score | Diagnosis | Changed files | Patch | Public | Feedback | Final |",
        "|---|---|---|---|---|---:|---|---|---:|---:|---:|---:|",
    ]
    for item in results["cases"]:
        lines.append(
            f"| {item['case_id']} | {item['language']} | {item['test_runner']} | "
            f"{item['evaluation_role']} | {item['split']} | "
            f"{item['score']} | {item['diagnosis_dimension']} | "
            f"{', '.join(item.get('changed_files') or []) or '-'} | {str(item['patch_applied']).lower()} | "
            f"{str(item['public_tests']['passed']).lower()} | "
            f"{str(item['feedback_tests']['passed']).lower()} | "
            f"{str(item['final_tests']['passed']).lower()} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "Each repository is created from one benchmark case. The live model sees only the prompt, "
            "candidate source, and public tests. Repair-feedback tests may guide bounded repair only after the "
            "initial patch. For sealed entries, final adjudication tests run once in a separate repository after "
            "all model calls and never enter a repair prompt. "
            "Development fixtures do not measure unseen generalization; entries labeled blinded_holdout must use "
            "a separate final oracle. "
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
    deadline = getattr(calls, "deadline", None)
    if isinstance(deadline, (int, float)):
        remaining = float(deadline) - time.monotonic()
        if remaining <= 0:
            calls.append(
                {
                    "stage": stage,
                    "model": model,
                    "ok": False,
                    "latency_ms": 0,
                    "wall_ms": 0,
                    "tokens_out": 0,
                    "error": "case_model_time_budget_exhausted",
                    "response": "",
                    "budget_exhausted": True,
                }
            )
            return ""
        timeout = min(timeout, remaining)
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
            "model": model,
            "ok": result.ok,
            "latency_ms": result.latency_ms,
            "wall_ms": int((time.monotonic() - started) * 1000),
            "tokens_out": result.tokens_out,
            "error": result.error,
            "response": (result.text or "")[:6000],
        }
    )
    return result.text if result.ok else ""


class _ModelCallLedger(list[dict[str, Any]]):
    def __init__(self, *, deadline: float | None = None):
        super().__init__()
        self.deadline = deadline

    @property
    def budget_exhausted(self) -> bool:
        return bool(self.deadline is not None and time.monotonic() >= self.deadline)


def _diagnostic_json_call(
    model: str,
    stage: str,
    stage_prompt: str,
    calls: list[dict[str, Any]],
    timeout: float,
) -> str:
    messages = [
        {
            "role": "system",
            "content": "You are CHILI's local diagnostic judge. Return JSON only and never invent evidence.",
        },
        {"role": "user", "content": stage_prompt},
    ]
    response = _local_call(
        model,
        messages,
        stage=f"diagnosis_{stage}",
        calls=calls,
        timeout=timeout,
        num_predict=1000,
        json_mode=True,
    )
    valid = diagnostic_reasoning.parse_json_object(response) is not None
    if calls:
        calls[-1]["json_object_valid"] = valid
    if valid or not response:
        return response

    retry = _local_call(
        model,
        [
            {
                "role": "system",
                "content": (
                    "Return one compact diagnostic JSON object only. Use at most three hypotheses and two "
                    "experiments. Keep every string under 180 characters. Do not add prose or fences."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"The previous {stage} response was truncated or invalid. Re-answer this exact diagnostic "
                    f"request in the compact schema:\n\n{stage_prompt}"
                ),
            },
        ],
        stage=f"diagnosis_{stage}_json_retry",
        calls=calls,
        timeout=timeout,
        num_predict=850,
        json_mode=True,
    )
    retry_valid = diagnostic_reasoning.parse_json_object(retry) is not None
    if calls:
        calls[-1]["json_object_valid"] = retry_valid
        calls[-1]["retry_for_invalid_json"] = True
    return retry if retry_valid else ""


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
        return _diagnostic_json_call(
            model,
            stage,
            stage_prompt,
            calls,
            timeout,
        )

    initial = diagnostic_reasoning.run_local_diagnostic_debate(
        diagnostic_case,
        judge,
        stages_to_run=("investigator", "judge"),
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
    revision = diagnostic_reasoning.evidence_gated_report_revision(
        report,
        final.get("report") or {},
        probe_run.get("evidence") or [],
    )
    retained = final if revision["accepted"] else initial
    return {
        **retained,
        "stages": [*(initial.get("stages") or []), *(final.get("stages") or [])],
        "initial_report": report,
        "probe_run": probe_run,
        "post_probe_conclusion_revision": revision,
        "case": enriched,
    }


def _replacement_already_satisfied(
    original: str,
    blocks: Sequence[tuple[str, str]],
) -> bool:
    meaningful = [
        (search, replace)
        for search, replace in blocks
        if search.strip() and replace.strip()
    ]
    return bool(meaningful) and all(
        search not in original and original.count(replace) == 1
        for search, replace in meaningful
    )


def _retryable_edit_adapter_rejection(warnings: Sequence[str]) -> bool:
    return code_agent._retryable_edit_adapter_rejection(list(warnings))


def _apply_local_edit(
    repo: Path,
    selected: str,
    description: str,
    model: str,
    calls: list[dict[str, Any]],
    timeout: float,
    *,
    stage: str,
    allow_model_recovery: bool = True,
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
    if not edit_text.strip():
        return {
            "patch_applied": False,
            "transport_failed": True,
            "warnings": [
                "Local edit call returned no usable response; adapter retries were skipped."
            ],
        }

    def parse_outcome(response: str) -> dict[str, Any]:
        blocks = code_agent._parse_search_replace_blocks(response)
        return (
            code_agent._apply_search_replace(original, blocks)
            if blocks
            else code_agent._extract_full_file_replacement(response, selected, original)
        )

    blocks = code_agent._parse_search_replace_blocks(edit_text)
    outcome = parse_outcome(edit_text)
    new_content = outcome.get("new_content")
    initial_warnings = [str(value) for value in outcome.get("warnings") or []]
    if not isinstance(new_content, str) and (
        outcome.get("already_satisfied")
        or _replacement_already_satisfied(original, blocks)
    ):
        return {
            "patch_applied": False,
            "already_satisfied": True,
            "warnings": ["Planned replacement is already satisfied in the current file."],
        }
    stale_search_rejection = any(
        "SEARCH text not found" in warning for warning in initial_warnings
    )
    if (
        allow_model_recovery
        and not isinstance(new_content, str)
        and _retryable_edit_adapter_rejection(
        initial_warnings
        )
    ):
        retry_stage = f"{stage}_retry" if stale_search_rejection else f"{stage}_adapter_retry"
        retry_instruction = (
            f"Your previous edit for {selected} was rejected because SEARCH did not match the current file. "
            "Re-read the exact CURRENT FILE in the system prompt. Return corrected SEARCH/REPLACE blocks "
            "copied verbatim from that file; do not reuse stale intended code."
            if stale_search_rejection
            else (
                f"Your previous edit for {selected} used an invalid mixed adapter format. Re-read the exact "
                "CURRENT FILE in the system prompt and return only valid SEARCH/REPLACE blocks. Do not wrap "
                "a full file around SEARCH markers, do not return a unified diff, and do not add prose."
            )
        )
        retry_text = _local_call(
            model,
            [
                {"role": "system", "content": edit_prompt},
                {
                    "role": "user",
                    "content": (
                        f"{retry_instruction} "
                        f"Adapter feedback: {'; '.join(initial_warnings)[:1200]}"
                    ),
                },
            ],
            stage=retry_stage,
            calls=calls,
            timeout=timeout,
            num_predict=1200,
            json_mode=False,
        )
        retry_blocks = code_agent._parse_search_replace_blocks(retry_text)
        outcome = parse_outcome(retry_text)
        new_content = outcome.get("new_content")
        if isinstance(new_content, str):
            outcome["warnings"] = [
                (
                    "Recovered from one stale SEARCH rejection using the exact current file."
                    if stale_search_rejection
                    else "Recovered from one mixed edit-format rejection using strict SEARCH/REPLACE blocks."
                ),
                *(outcome.get("warnings") or []),
            ]
        elif outcome.get("already_satisfied") or _replacement_already_satisfied(
            original,
            retry_blocks,
        ):
            return {
                "patch_applied": False,
                "already_satisfied": True,
                "warnings": ["Planned replacement is already satisfied in the current file."],
            }
        elif (
            len(original) <= 12_000
            and any(
                "SEARCH text not found" in str(warning)
                for warning in outcome.get("warnings") or []
            )
        ):
            full_text = _local_call(
                model,
                [
                    {
                        "role": "system",
                        "content": (
                            "Return exactly one fenced full-file replacement for the approved source file. "
                            "Preserve unrelated behavior and do not return SEARCH/REPLACE or a diff."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"File: {selected}\n\nRequired change:\n{description}\n\n"
                            f"Exact current file:\n{original}"
                        ),
                    },
                ],
                stage=f"{stage}_full_file_retry",
                calls=calls,
                timeout=timeout,
                num_predict=1800,
                json_mode=False,
            )
            full_outcome = code_agent._extract_full_file_replacement(
                full_text,
                selected,
                original,
            )
            if isinstance(full_outcome.get("new_content"), str):
                outcome = {
                    **full_outcome,
                    "warnings": [
                        "Recovered from repeated stale SEARCH using a guarded full-file replacement.",
                        *(full_outcome.get("warnings") or []),
                    ],
                }
                new_content = outcome.get("new_content")
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


def _apply_safe_compiler_repair(
    repo: Path,
    relative: str,
    diagnostics: str,
) -> list[str]:
    """Apply only mechanically unambiguous compiler fixes before model retry."""
    path = repo / relative
    if path.suffix.lower() != ".dart" or not path.is_file():
        return []
    content = path.read_text(encoding="utf-8", errors="replace")
    updated = content
    warnings: list[str] = []
    if "The function 'max' isn't defined" in diagnostics and re.search(
        r"(?<![A-Za-z0-9_.])max\s*\(",
        updated,
    ):
        updated = re.sub(
            r"(?<![A-Za-z0-9_.])max\s*\(",
            "math.max(",
            updated,
        )
        if not re.search(
            r"import\s+['\"]dart:math['\"]\s+as\s+math\s*;",
            updated,
        ):
            directives = list(
                re.finditer(
                    r"(?m)^(?:library\s+[^;]+;|import\s+['\"][^'\"]+['\"](?:\s+as\s+\w+)?;)\s*$",
                    updated,
                )
            )
            insert_at = directives[-1].end() if directives else 0
            insertion = (
                "\nimport 'dart:math' as math;"
                if insert_at
                else "import 'dart:math' as math;\n"
            )
            updated = updated[:insert_at] + insertion + updated[insert_at:]
            if insert_at:
                updated = updated[: insert_at + len(insertion)] + "\n" + updated[
                    insert_at + len(insertion) :
                ].lstrip("\n")
        warnings.append("qualified undefined Dart max calls with an explicit dart:math import")
    if updated == content:
        return []
    path.write_text(updated, encoding="utf-8")
    return warnings


def _compiler_diagnostic_guidance(relative: str, diagnostics: str) -> str:
    guidance: list[str] = []
    if str(relative).lower().endswith(".dart"):
        if "The function 'max' isn't defined" in diagnostics:
            guidance.append(
                "Qualify max through an existing/imported dart:math alias or use an explicit typed comparison."
            )
        if "Map<dynamic, dynamic>" in diagnostics:
            guidance.append(
                "Give map literals and join accumulators an explicit Map<String, int> or <String, int> type."
            )
    return "\n".join(guidance)


def _apply_local_edit_bundle(
    repo: Path,
    paths: Sequence[str],
    change: str,
    model: str,
    calls: list[dict[str, Any]],
    timeout: float,
    *,
    stage: str,
) -> dict[str, Any]:
    """Generate and apply one coordinated, all-or-nothing multi-file edit."""
    originals = {
        relative: (repo / relative).read_text(encoding="utf-8", errors="replace")
        for relative in paths
    }
    schema = {
        "edits": [
            {
                "path": "approved/source.ext",
                "blocks": [
                    {"search": "exact current text", "replace": "replacement text"}
                ],
            }
        ]
    }
    current_files = "\n\n".join(
        f"### {relative}\n{content}" for relative, content in originals.items()
    )
    response = _local_call(
        model,
        [
            {
                "role": "system",
                "content": (
                    "You are CHILI's coordinated source editor. Return one JSON object only. "
                    "Every approved path must appear exactly once. Each SEARCH value must be copied exactly "
                    "from that path's current content. Design the files together so signatures, state, schema, "
                    "and callers remain compatible. Do not edit tests, invent dependencies, or add prose."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Required JSON shape:\n{json.dumps(schema, indent=2)}\n\n"
                    f"Approved paths: {json.dumps(list(paths))}\n\n"
                    f"Coordinated change:\n{change[:16_000]}\n\n"
                    f"Exact current files:\n{current_files[:24_000]}"
                ),
            },
        ],
        stage=stage,
        calls=calls,
        timeout=timeout,
        num_predict=3200,
        json_mode=True,
    )
    payload: Mapping[str, Any] = {}
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", response or ""):
        try:
            value, _end = decoder.raw_decode((response or "")[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping) and isinstance(value.get("edits"), list):
            payload = value
            break
    raw_edits = payload.get("edits") or []
    entries: dict[str, Mapping[str, Any]] = {}
    warnings: list[str] = []
    for item in raw_edits:
        if not isinstance(item, Mapping):
            continue
        relative = _safe_rel(item.get("path"))
        if relative not in originals or relative in entries:
            warnings.append(
                f"Coordinated edit contained an unapproved or duplicate path: {relative or '(invalid)'}."
            )
            continue
        entries[relative] = item
    missing = [relative for relative in paths if relative not in entries]
    if missing or warnings:
        return {
            "patch_applied": False,
            "applied_files": [],
            "satisfied_files": [],
            "warnings": [
                *warnings,
                *(f"Coordinated edit omitted required path {relative}." for relative in missing),
            ],
        }
    updates: dict[str, str] = {}
    satisfied: list[str] = []
    for relative in paths:
        item = entries[relative]
        blocks: list[tuple[str, str]] = []
        for block in item.get("blocks") or []:
            if not isinstance(block, Mapping):
                continue
            search = str(block.get("search") or "")
            replace = str(block.get("replace") or "")
            if search:
                blocks.append((search, replace))
        outcome = code_agent._apply_search_replace(originals[relative], blocks)
        new_content = outcome.get("new_content")
        if not isinstance(new_content, str) and (
            outcome.get("already_satisfied")
            or _replacement_already_satisfied(originals[relative], blocks)
        ):
            satisfied.append(relative)
            continue
        if not isinstance(new_content, str) or new_content.rstrip() == originals[
            relative
        ].rstrip():
            return {
                "patch_applied": False,
                "applied_files": [],
                "satisfied_files": satisfied,
                "warnings": [
                    *warnings,
                    *(str(value) for value in outcome.get("warnings") or []),
                    f"Coordinated edit for {relative} was rejected or made no change.",
                ],
            }
        semantic_warnings = code_agent._semantic_replacement_warnings(
            relative,
            new_content,
        )
        if semantic_warnings:
            return {
                "patch_applied": False,
                "applied_files": [],
                "satisfied_files": satisfied,
                "warnings": [
                    *(f"semantic polarity guard: {value}" for value in semantic_warnings),
                ],
            }
        updates[relative] = new_content
    for relative, content in updates.items():
        (repo / relative).write_text(content, encoding="utf-8")
    return {
        "patch_applied": bool(updates),
        "applied_files": list(updates),
        "satisfied_files": satisfied,
        "warnings": warnings,
    }


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
    allow_model_recovery: bool = True,
) -> dict[str, Any]:
    """Apply one bounded edit group and roll it back if any member fails."""
    paths = [str(item.get("path") or "") for item in selected]
    optional_paths = {
        str(item.get("path") or "")
        for item in selected
        if bool(item.get("optional"))
    }
    originals = {
        rel: (repo / rel).read_text(encoding="utf-8", errors="replace")
        for rel in paths
    }
    optional_baseline_public: dict[str, Any] | None = None
    optional_baseline_feedback: dict[str, Any] | None = None

    def current_attempted_diff() -> str:
        return _snapshot_diff(
            originals,
            {
                relative: (repo / relative).read_text(
                    encoding="utf-8",
                    errors="replace",
                )
                for relative in originals
            },
        )

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
                "optional_context_candidate": bool(item.get("optional")),
            }
            for item in selected
        ],
        sort_keys=True,
    )
    contract_coverage = [
        dict(item)
        for item in plan.get("contract_coverage") or []
        if isinstance(item, Mapping)
    ]
    warnings: list[str] = []
    applied: list[str] = []
    satisfied: list[str] = []
    skipped_optional: list[str] = []
    optional_rejected_diffs: list[dict[str, str]] = []
    use_coordinated_bundle = bool(
        len(paths) > 1 and not optional_paths and not allow_model_recovery
    )

    def record_optional_rejection(
        rel: str,
        *,
        reason: str,
        validation_output: str = "",
    ) -> None:
        current = (repo / rel).read_text(encoding="utf-8", errors="replace")
        optional_rejected_diffs.append(
            {
                "path": rel,
                "reason": reason,
                "attempted_diff": _snapshot_diff(
                    {rel: originals[rel]},
                    {rel: current},
                ),
                "validation_output": validation_output[:3_000],
            }
        )

    if use_coordinated_bundle:
        bundle_change = (
            f"Overall request:\n{case.get('prompt')}\n\n"
            f"Evidence-gated diagnosis:\n{diagnostic_reasoning.report_context(report)}\n"
            f"Strongest causal evidence:\n{evidence_context or '(none)'}\n\n"
            f"Deterministic mechanism invariants:\n{json.dumps(mechanism_invariants, indent=2)}\n\n"
            f"Coordinated file responsibilities:\n{plan_summary}\n"
            "Contract-to-owner postconditions:\n"
            f"{json.dumps(contract_coverage, indent=2, sort_keys=True)}\n"
        )
        if failure_output:
            bundle_change += f"\nPrevious validation failure:\n{failure_output[:9000]}\n"
        bundle = _apply_local_edit_bundle(
            repo,
            paths,
            bundle_change,
            model,
            calls,
            timeout,
            stage=f"{stage_prefix}_bundle",
        )
        warnings.extend(str(value) for value in bundle.get("warnings") or [])
        applied.extend(str(value) for value in bundle.get("applied_files") or [])
        satisfied.extend(str(value) for value in bundle.get("satisfied_files") or [])
        if not bundle.get("patch_applied") and not satisfied:
            attempted_diff = current_attempted_diff()
            for original_rel, content in originals.items():
                (repo / original_rel).write_text(content, encoding="utf-8")
            return {
                "patch_applied": False,
                "selected_files": paths,
                "applied_files": [],
                "satisfied_files": satisfied,
                "skipped_optional_files": [],
                "optional_rejected_diffs": [],
                "attempted_diff": attempted_diff,
                "warnings": [
                    *warnings,
                    "Rolled back coordinated multi-file edit group after bundle rejection.",
                ],
            }

    for index, item in enumerate(
        selected if not use_coordinated_bundle else [],
        start=1,
    ):
        rel = str(item.get("path") or "")
        description = str(item.get("description") or case.get("prompt") or "")
        if (
            rel in optional_paths
            and optional_baseline_public is None
            and failure_output
            and (repo / "tests").is_dir()
        ):
            optional_baseline_public = _run_case_tests(
                repo,
                case,
                public_only=True,
            )
            optional_baseline_feedback = _run_case_tests(
                repo,
                case,
                public_only=False,
            )
        owned_contracts = [
            contract
            for contract in contract_coverage
            if rel
            in {
                candidate
                for value in contract.get("owner_paths") or []
                if (candidate := _safe_rel(value))
            }
        ]
        if not owned_contracts:
            owned_contracts = contract_coverage
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
            f"Contract-to-owner postconditions (implement each applicable postcondition exactly):\n"
            f"{json.dumps(owned_contracts, indent=2, sort_keys=True)}\n"
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
            "classes that the repository does not define. Preserve semantic polarity exactly: cannot/not/never/"
            "undefined are prohibitions, not permission to store, return, or enable the behavior. Do not edit tests."
        )
        edit = _apply_local_edit(
            repo,
            rel,
            change,
            model,
            calls,
            timeout,
            stage=f"{stage_prefix}_{index}",
            allow_model_recovery=allow_model_recovery,
        )
        warnings.extend(str(value) for value in edit.get("warnings") or [])
        if edit.get("already_satisfied"):
            satisfied.append(rel)
            continue
        if not edit.get("patch_applied"):
            if rel in optional_paths:
                record_optional_rejection(
                    rel,
                    reason="local edit adapter rejected the optional candidate",
                    validation_output="\n".join(
                        str(value) for value in edit.get("warnings") or []
                    ),
                )
                skipped_optional.append(rel)
                warnings.append(
                    f"Skipped optional source-owner candidate {rel} after its edit was rejected."
                )
                continue
            attempted_diff = current_attempted_diff()
            for original_rel, content in originals.items():
                (repo / original_rel).write_text(content, encoding="utf-8")
            return {
                "patch_applied": False,
                "selected_files": paths,
                "applied_files": [],
                "satisfied_files": satisfied,
                "skipped_optional_files": skipped_optional,
                "optional_rejected_diffs": optional_rejected_diffs,
                "attempted_diff": attempted_diff,
                "warnings": [
                    *warnings,
                    f"Rolled back multi-file edit group after {rel} was rejected.",
                ],
            }
        if rel in optional_paths:
            optional_syntax = validator_runner.run_ast_syntax(
                repo,
                changed_files=[rel],
            )
            if optional_syntax.exit_code != 0 or optional_syntax.timed_out:
                detail = "\n".join(
                    value
                    for value in (
                        str(optional_syntax.stdout or ""),
                        str(optional_syntax.stderr or ""),
                    )
                    if value.strip()
                )
                record_optional_rejection(
                    rel,
                    reason="optional candidate failed changed-file syntax validation",
                    validation_output=detail,
                )
                (repo / rel).write_text(originals[rel], encoding="utf-8")
                skipped_optional.append(rel)
                warnings.append(
                    f"Skipped optional source-owner candidate {rel} after syntax rejection:\n"
                    f"{detail[:3000]}"
                )
                continue
        applied.append(rel)
    if applied:
        syntax = validator_runner.run_ast_syntax(repo, changed_files=applied)
        if syntax.exit_code != 0 or syntax.timed_out:
            detail = "\n".join(
                value
                for value in (str(syntax.stdout or ""), str(syntax.stderr or ""))
                if value.strip()
            )
            diagnostic_paths = [
                rel
                for rel in applied
                if rel.casefold() in detail.replace("\\", "/").casefold()
            ] or list(applied)
            safe_corrected: list[str] = []
            for rel in diagnostic_paths:
                safe_warnings = _apply_safe_compiler_repair(repo, rel, detail)
                if safe_warnings:
                    safe_corrected.append(rel)
                    warnings.extend(safe_warnings)
            if safe_corrected:
                syntax = validator_runner.run_ast_syntax(
                    repo,
                    changed_files=applied,
                )
                if syntax.exit_code == 0 and not syntax.timed_out:
                    warnings.append(
                        "Recovered the edit group with one bounded deterministic compiler repair."
                    )
                else:
                    detail = "\n".join(
                        value
                        for value in (
                            str(syntax.stdout or ""),
                            str(syntax.stderr or ""),
                        )
                        if value.strip()
                    )
                    diagnostic_paths = [
                        rel
                        for rel in applied
                        if rel.casefold() in detail.replace("\\", "/").casefold()
                    ] or list(applied)
            if (
                allow_model_recovery
                and (syntax.exit_code != 0 or syntax.timed_out)
            ):
                correction_succeeded = True
                corrected_files: list[str] = []
                for index, rel in enumerate(diagnostic_paths, start=1):
                    guidance = _compiler_diagnostic_guidance(rel, detail)
                    correction = _apply_local_edit(
                        repo,
                        rel,
                        (
                            "Correct only the compiler/analyzer defects in the current file while preserving the "
                            "intended behavioral repair and public contracts.\n"
                            f"Overall request: {case.get('prompt')}\n"
                            f"Coordinated responsibilities: {plan_summary}\n"
                            f"Compiler-specific guidance:\n{guidance or '(none)'}\n"
                            "Use these exact diagnostics:\n"
                            f"{detail[:7000]}"
                        ),
                        model,
                        calls,
                        timeout,
                        stage=f"{stage_prefix}_compiler_correction_{index}",
                        allow_model_recovery=allow_model_recovery,
                    )
                    warnings.extend(
                        str(value) for value in correction.get("warnings") or []
                    )
                    if correction.get("patch_applied") or correction.get("already_satisfied"):
                        corrected_files.append(rel)
                    else:
                        correction_succeeded = False
                        break
                if correction_succeeded and corrected_files:
                    syntax = validator_runner.run_ast_syntax(
                        repo,
                        changed_files=applied,
                    )
                    correction_succeeded = syntax.exit_code == 0 and not syntax.timed_out
                if correction_succeeded and corrected_files:
                    warnings.append(
                        "Recovered the edit group with one bounded compiler-guided correction."
                    )
                else:
                    attempted_diff = current_attempted_diff()
                    for original_rel, content in originals.items():
                        (repo / original_rel).write_text(content, encoding="utf-8")
                    final_detail = "\n".join(
                        value
                        for value in (
                            str(syntax.stdout or ""),
                            str(syntax.stderr or ""),
                        )
                        if value.strip()
                    )
                    return {
                        "patch_applied": False,
                        "selected_files": paths,
                        "applied_files": [],
                        "satisfied_files": satisfied,
                        "skipped_optional_files": skipped_optional,
                        "optional_rejected_diffs": optional_rejected_diffs,
                        "attempted_diff": attempted_diff,
                        "warnings": [
                            *warnings,
                            f"Changed-file syntax validation failed:\n{final_detail[:5000]}",
                            "Rolled back edit group after compiler-guided correction failed.",
                        ],
                    }
            if syntax.exit_code != 0 or syntax.timed_out:
                attempted_diff = current_attempted_diff()
                for original_rel, content in originals.items():
                    (repo / original_rel).write_text(content, encoding="utf-8")
                return {
                    "patch_applied": False,
                    "selected_files": paths,
                    "applied_files": [],
                    "satisfied_files": satisfied,
                    "skipped_optional_files": skipped_optional,
                    "optional_rejected_diffs": optional_rejected_diffs,
                    "attempted_diff": attempted_diff,
                    "warnings": [
                        *warnings,
                        f"Changed-file syntax validation failed:\n{detail[:5000]}",
                        "Rolled back edit group without another generative recovery call.",
                    ],
                }
    applied_optional = [rel for rel in applied if rel in optional_paths]
    if (
        applied_optional
        and optional_baseline_public is not None
        and optional_baseline_feedback is not None
    ):
        optional_after_public = _run_case_tests(repo, case, public_only=True)
        optional_after_feedback = _run_case_tests(repo, case, public_only=False)
        before_contracts = validation_contracts.test_contract_evidence(
            optional_baseline_feedback
        )
        after_contracts = validation_contracts.test_contract_evidence(
            optional_after_feedback
        )
        optional_regressed = bool(
            not optional_after_public.get("passed")
            or validation_contracts.contract_regressions(
                before_contracts,
                after_contracts,
            )
        )
        if optional_regressed:
            for rel in applied_optional:
                record_optional_rejection(
                    rel,
                    reason="optional candidate regressed a previously passing contract",
                    validation_output=_validation_failure_context(
                        optional_after_public,
                        optional_after_feedback,
                    ),
                )
                (repo / rel).write_text(originals[rel], encoding="utf-8")
                applied.remove(rel)
                skipped_optional.append(rel)
            warnings.append(
                "Restored optional source-owner edits because they regressed a previously green contract."
            )
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
        attempted_diff = current_attempted_diff()
        for original_rel, content in originals.items():
            (repo / original_rel).write_text(content, encoding="utf-8")
        return {
            "patch_applied": False,
            "selected_files": paths,
            "applied_files": [],
            "satisfied_files": satisfied,
            "skipped_optional_files": skipped_optional,
            "optional_rejected_diffs": optional_rejected_diffs,
            "attempted_diff": attempted_diff,
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
        "satisfied_files": satisfied,
        "skipped_optional_files": skipped_optional,
        "optional_rejected_diffs": optional_rejected_diffs,
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
    if not plan:
        return {
            "plan": {},
            "selected_file": "",
            "selected_files": [],
            "patch_applied": False,
            "transport_failed": not bool(plan_text.strip()),
            "warnings": [
                "Initial plan was empty or invalid; no edit authority was granted."
            ],
        }
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
    *,
    feedback_context: str = "",
    attempt_ledger: str = "",
    contract_evidence: Mapping[str, Any] | None = None,
    compact_escalation: bool = False,
    failure_signature: str = "",
    attempted_plan_fingerprints: set[str] | None = None,
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
        "Use the failure output and read-only feedback tests to propose a causal dimension, reconsider ownership, "
        "and select only the source files required for a compatible "
        f"repair, up to {max_files}. Use multiple files when the failure crosses an interface. "
        "Map every independent failing contract to its causal source owner and a concrete postcondition. Files "
        "imported by a test are read-only context unless the evidence shows they must change. Never select a test. "
        "Every contract_coverage.owner_paths value must be an allowed source candidate, never a test path. "
        "Required JSON schema:\n"
        f"{json.dumps(REPAIR_PLAN_SCHEMA, indent=2)}\n\n"
        f"Original request:\n{case.get('prompt')}\n\n"
        f"Dimension rubric:\n{json.dumps(REPAIR_DIMENSION_RUBRIC, indent=2)}\n\n"
        f"Evidence decision:\n{diagnostic_reasoning.report_context(report)}\n"
        f"Strongest evidence:\n{evidence_context or '(none)'}\n\n"
        f"Deterministic mechanism invariants:\n{json.dumps(mechanism_invariants, indent=2)}\n\n"
        f"Previous selected files: {json.dumps(previous_patch.get('selected_files') or [])}\n\n"
        f"Rejected/no-progress attempt ledger:\n{attempt_ledger or '(none)'}\n\n"
        f"Stable test-contract inventory (cover every failed id and preserve every passed id):\n"
        f"{json.dumps(dict(contract_evidence or {}), indent=2, sort_keys=True)}\n\n"
        f"Validation failure:\n{failure_output[:9000]}\n\n"
        f"Read-only repair-feedback tests:\n{feedback_context or '(unavailable)'}\n\n"
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
    if not plan:
        return {
            "round": round_index,
            "plan": {},
            "selected_file": "",
            "selected_files": [],
            "ownership_augmented_files": [],
            "feedback_context_files": [],
            "patch_applied": False,
            "transport_failed": not bool(plan_text.strip()),
            "warnings": [
                "Repair plan was empty or invalid; no review or edit authority was granted."
            ],
        }
    plan = _align_plan_files_to_contract_coverage(plan, candidates, max_files)
    plan_fingerprint = _repair_plan_fingerprint(plan)
    attempt_key = f"{failure_signature}:{plan_fingerprint}"
    if attempted_plan_fingerprints is not None and attempt_key in attempted_plan_fingerprints:
        return {
            "round": round_index,
            "plan": plan,
            "plan_fingerprint": plan_fingerprint,
            "selected_file": "",
            "selected_files": [],
            "ownership_augmented_files": [],
            "feedback_context_files": _feedback_exercised_candidates(
                feedback_context,
                candidates,
            ),
            "patch_applied": False,
            "duplicate_plan": True,
            "warnings": [
                "Skipped repeated repair plan against an unchanged failure signature."
            ],
        }
    if attempted_plan_fingerprints is not None:
        attempted_plan_fingerprints.add(attempt_key)
    skip_repair_review = bool(
        mechanism_invariants
        and _repair_plan_has_complete_contract_coverage(
            plan,
            candidates,
            contract_evidence,
        )
    )
    review_prompt = (
        "Return one corrected repair-plan JSON object only. Act as an adversarial validation judge: the draft "
        "may have misread an assertion or traded one contract for another. Derive every required input/output "
        "contract from the verbatim PUBLIC and REPAIR-FEEDBACK failure text. The final adjudication remains "
        "sealed and is never available here. The corrected plan must satisfy all of "
        "them simultaneously, revise the causal dimension when the new evidence contradicts it, preserve "
        "already-green behavior, copy mutable data when identity isolation is "
        "asserted, and keep required empty keys when an assertion indexes them. Never edit tests, swallow an "
        "exception, invent a dependency, add an unrequested retry loop, change a public signature without all "
        "callers, or select a file that needs no change. Preserve negative-contract polarity: cannot/not/never/"
        "undefined means the behavior must not occur. A failed in-flight operation must not recursively await "
        "its own cached promise. Include a contract_coverage entry for every independent assertion. Every "
        "owner_paths value must be an allowed source candidate, never a test path. Required JSON schema:\n"
        f"{json.dumps(REPAIR_PLAN_SCHEMA, indent=2)}\n\n"
        f"Allowed candidates (max {max_files}): {json.dumps(candidates)}\n\n"
        f"Dimension rubric:\n{json.dumps(REPAIR_DIMENSION_RUBRIC, indent=2)}\n\n"
        f"Original operator contract (must also remain true):\n{case.get('prompt')}\n\n"
        "Deterministic mechanism invariants (must be implemented by their causal source owner, not merely "
        f"reviewed):\n{json.dumps(mechanism_invariants, indent=2)}\n\n"
        f"Repair-feedback validation contracts:\n{failure_output[:12000]}\n\n"
        f"Read-only repair-feedback tests:\n{feedback_context or '(unavailable)'}\n\n"
        f"Rejected/no-progress attempt ledger:\n{attempt_ledger or '(none)'}\n\n"
        f"Stable test-contract inventory (cover every failed id and preserve every passed id):\n"
        f"{json.dumps(dict(contract_evidence or {}), indent=2, sort_keys=True)}\n\n"
        f"Draft plan:\n{json.dumps(plan, sort_keys=True)}\n\n"
        f"Current candidate contents:\n{context}\n\n"
        "Do not use action=review as a placeholder for a causal owner. If a source file must change to satisfy "
        "an invariant, select action=modify with a concrete responsibility; otherwise omit it."
    )
    if skip_repair_review or compact_escalation:
        plan = {
            **plan,
            "review_skipped_reason": (
                "compact local escalation permits one planning decision and no generative review"
                if compact_escalation
                else "deterministic invariants and complete stable-contract ownership made another generative review redundant"
            ),
        }
    else:
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
            plan = _align_plan_files_to_contract_coverage(
                reviewed,
                candidates,
                max_files,
            )
    if contract_evidence and not _repair_plan_has_complete_contract_coverage(
        plan,
        candidates,
        contract_evidence,
    ):
        return {
            "round": round_index,
            "plan": plan,
            "selected_file": "",
            "selected_files": [],
            "ownership_augmented_files": [],
            "feedback_context_files": _feedback_exercised_candidates(
                feedback_context,
                candidates,
            ),
            "patch_applied": False,
            "warnings": [
                "Repair plan did not cover every failed contract with selected source owners."
            ],
        }
    selected = _plan_file_items(plan, candidates, max_files)
    feedback_context_files = _feedback_exercised_candidates(
        feedback_context,
        candidates,
    )
    ownership_augmented_files: list[str] = []
    if not selected:
        return {
            "round": round_index,
            "plan": plan,
            "selected_file": "",
            "selected_files": [],
            "ownership_augmented_files": ownership_augmented_files,
            "feedback_context_files": feedback_context_files,
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
        failure_output=(
            f"{failure_output}\n\nREAD-ONLY REPAIR-FEEDBACK TEST CONTRACTS:\n{feedback_context}"
        )[:20_000],
        allow_model_recovery=not compact_escalation,
    )
    selected_file_paths = [str(item.get("path") or "") for item in selected]
    return {
        "round": round_index,
        "plan": plan,
        "plan_fingerprint": plan_fingerprint,
        "selected_file": selected_file_paths[0] if selected_file_paths else "",
        "selected_files": selected_file_paths,
        "ownership_augmented_files": ownership_augmented_files,
        "feedback_context_files": feedback_context_files,
        **edit,
    }


def _score_case(
    oracle: Mapping[str, Any],
    diagnosis: Mapping[str, Any],
    patch: Mapping[str, Any],
    baseline_final: Mapping[str, Any],
    public_tests: Mapping[str, Any],
    final_tests: Mapping[str, Any],
) -> tuple[int, dict[str, bool]]:
    effective_dimension = _effective_diagnosis_dimension(diagnosis)
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
        "baseline_final_failure": not bool(baseline_final.get("passed")),
        "diagnosis": effective_dimension == oracle.get("expected_dimension"),
        "file_selection": bool(expected_files) and changed_files == expected_files,
        "patch_applied": bool(patch.get("patch_applied")) and bool(changed_files),
        "public_tests": bool(public_tests.get("passed")),
        "final_tests": bool(final_tests.get("passed")),
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
    case = _read_json(_fixture_path(root, entry.get("case"), "case"))
    oracle = _read_json(_fixture_path(root, entry.get("oracle"), "oracle"))
    final_oracle = (
        _read_json(_fixture_path(root, entry.get("final_oracle"), "final oracle"))
        if entry.get("final_oracle")
        else None
    )
    if final_oracle is not None and final_oracle.get("case_id") != case.get("case_id"):
        raise ValueError("Final oracle case_id does not match the public case.")
    partitions = _oracle_test_partitions(
        oracle,
        final_oracle=final_oracle,
        require_sealed=entry.get("evaluation_role") == "blinded_holdout",
        require_external_final=entry.get("evaluation_role") == "blinded_holdout",
    )
    _validate_oracle_test_paths(case, partitions)
    _validate_expected_ownership(case, oracle)
    with tempfile.TemporaryDirectory(prefix="chili-fixture-validation-") as temp:
        repo = Path(temp) / "repo"
        _init_repo(repo, case.get("repo_files") or {})
        public = _run_case_tests(repo, case, public_only=True)
        _write_files(repo, partitions["feedback_files"])
        feedback = _run_case_tests(repo, case, public_only=False)
    final = _run_final_adjudication(case, partitions["final_files"])
    return {
        "case_id": case.get("case_id"),
        "test_runner": _case_test_runner(case),
        "public_passed": public["passed"],
        "feedback_failed": not feedback["passed"],
        "final_failed": not final["passed"],
        "sealed_final_adjudication": bool(partitions["sealed"]),
        "external_final_oracle": bool(partitions["external_final"]),
        "valid": public["passed"] and not feedback["passed"] and not final["passed"],
        "public_failure": "" if public["passed"] else str(public.get("output") or "")[-4_000:],
        "feedback_unexpected_pass": (
            "" if not feedback["passed"] else str(feedback.get("output") or "")[-4_000:]
        ),
        "final_unexpected_pass": (
            "" if not final["passed"] else str(final.get("output") or "")[-4_000:]
        ),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    fixture_root = Path(args.fixture_root).resolve()
    evaluation_context = str(
        getattr(args, "evaluation_context", "protocol") or "protocol"
    )
    disclosed_replay = evaluation_context == "disclosed_replay"
    manifest, entries = _fixture_entries(fixture_root, set(args.case or []))
    if not entries:
        raise SystemExit("No diagnosis-to-fix cases selected.")
    if any(
        entry.get("evaluation_role") == "blinded_holdout"
        and not entry.get("final_oracle")
        for entry in entries
    ):
        raise SystemExit(
            "Every blinded_holdout entry requires a separate final_oracle path."
        )
    for entry in entries:
        _fixture_path(fixture_root, entry.get("case"), "case")
        _fixture_path(fixture_root, entry.get("oracle"), "oracle")
        if entry.get("final_oracle"):
            _fixture_path(
                fixture_root,
                entry.get("final_oracle"),
                "final oracle",
            )
    if args.validate_fixtures:
        validations = [validate_fixture(fixture_root, entry) for entry in entries]
        return {
            "schema": "chili.diagnosis-to-fix-fixture-validation.v3",
            "valid": all(item["valid"] for item in validations),
            "cases": validations,
        }
    installed = ollama_client.list_models()
    repair_schedule = _repair_model_schedule(args)
    required_models = {str(args.model), *repair_schedule}
    missing_models = sorted(model for model in required_models if model not in installed)
    if missing_models:
        raise SystemExit(
            "Local model(s) are not installed: " + ", ".join(repr(value) for value in missing_models)
        )

    case_results: list[dict[str, Any]] = []
    for entry in entries:
        case = _read_json(_fixture_path(fixture_root, entry.get("case"), "case"))
        started = time.monotonic()
        case_model_budget = max(
            1.0,
            float(getattr(args, "case_model_time_budget", 690.0) or 690.0),
        )
        calls: list[dict[str, Any]] = _ModelCallLedger(
            deadline=started + case_model_budget,
        )
        with tempfile.TemporaryDirectory(prefix=f"chili-fix-{case['case_id']}-") as temp:
            repo = Path(temp) / "repo"
            _init_repo(repo, case.get("repo_files") or {})
            baseline_snapshot = _candidate_snapshot(repo, case)
            diagnosis = _diagnose(repo, case, args.model, calls, args.timeout)
            _initialize_accepted_diagnosis(diagnosis)
            deterministic_contract_repair = _apply_deterministic_contract_repair(
                repo,
                case,
            )
            patch = (
                _patch_from_deterministic_contract_repair(
                    deterministic_contract_repair,
                    diagnosis,
                )
                if deterministic_contract_repair.get("patch_applied")
                else _generate_patch(
                    repo,
                    case,
                    diagnosis,
                    args.model,
                    calls,
                    args.timeout,
                )
            )
            initial_plan_dimension = _plan_dimension(patch.get("plan") or {})
            patch["proposed_diagnosis_dimension"] = initial_plan_dimension
            patch["changed_files"] = _changed_candidate_files(
                repo, case.get("candidate_paths") or []
            )
            patch["patch_applied"] = bool(patch["changed_files"])
            public_tests = _run_case_tests(repo, case, public_only=True)

            # Oracle access begins only after the patch and public validation exist.
            oracle = _read_json(
                _fixture_path(fixture_root, entry.get("oracle"), "oracle")
            )
            _validate_expected_ownership(case, oracle)
            if entry.get("final_oracle"):
                feedback_raw = oracle.get("feedback_files")
                if not isinstance(feedback_raw, Mapping) or not feedback_raw:
                    raise ValueError(
                        "External final adjudication requires repair feedback_files."
                    )
                if "final_files" in oracle:
                    raise ValueError(
                        "Repair oracle must not contain final_files when final_oracle is separate."
                    )
                partitions = {
                    "feedback_files": _normalize_test_files(
                        feedback_raw,
                        "feedback",
                    ),
                    "sealed": True,
                    "external_final": True,
                }
            else:
                partitions = _oracle_test_partitions(oracle)
            _validate_oracle_test_paths(case, partitions)
            with tempfile.TemporaryDirectory(prefix="chili-baseline-feedback-") as baseline_temp:
                baseline_repo = Path(baseline_temp) / "repo"
                _init_repo(baseline_repo, case.get("repo_files") or {})
                baseline_public = _run_case_tests(
                    baseline_repo,
                    case,
                    public_only=True,
                )
                _write_files(baseline_repo, partitions["feedback_files"])
                baseline_feedback = _run_case_tests(
                    baseline_repo,
                    case,
                    public_only=False,
                )
            _write_files(repo, partitions["feedback_files"])
            feedback_context = _read_only_test_context(
                repo,
                list(partitions["feedback_files"]),
            )
            feedback_tests = _run_case_tests(repo, case, public_only=False)
            initial_quality = _validation_quality(public_tests, feedback_tests)
            baseline_quality = _validation_quality(baseline_public, baseline_feedback)
            initial_advanced = _validation_advanced(
                baseline_public,
                baseline_feedback,
                public_tests,
                feedback_tests,
            )
            if patch.get("patch_applied") and (
                initial_quality < baseline_quality
                or (initial_quality == baseline_quality and not initial_advanced)
            ):
                _restore_candidate_snapshot(repo, baseline_snapshot)
                patch["warnings"] = [
                    *(patch.get("warnings") or []),
                    "Rolled back initial patch because it regressed or made no validated progress.",
                ]
                patch["initial_patch_rolled_back"] = True
                if patch.get("deterministic_initial_patch"):
                    deterministic_contract_repair["patch_applied"] = False
                    deterministic_contract_repair["rolled_back_after_validation"] = True
                patch["changed_files"] = _changed_candidate_files(
                    repo,
                    case.get("candidate_paths") or [],
                )
                patch["patch_applied"] = bool(patch["changed_files"])
                public_tests = _run_case_tests(repo, case, public_only=True)
                feedback_tests = _run_case_tests(repo, case, public_only=False)
            elif patch.get("patch_applied"):
                if (
                    patch.get("deterministic_initial_patch")
                    and public_tests.get("passed")
                    and feedback_tests.get("passed")
                ):
                    _accept_validated_contract_repair_diagnosis(
                        diagnosis,
                        str(
                            deterministic_contract_repair.get(
                                "proposed_dimension"
                            )
                            or ""
                        ),
                        stage="deterministic_contract_repair_validated",
                        validation_evidence=(
                            "Recognized source-structural repair preserved public contracts "
                            "and resolved every disclosed feedback contract."
                        ),
                    )
                elif initial_plan_dimension:
                    _accept_diagnosis_proposal(
                        diagnosis,
                        initial_plan_dimension,
                        stage="initial_plan_validated",
                        validation_evidence=_validation_failure_context(
                            public_tests,
                            feedback_tests,
                        ),
                    )
            if (
                not (public_tests["passed"] and feedback_tests["passed"])
                and not deterministic_contract_repair.get("attempted")
            ):
                deterministic_contract_repair = _apply_deterministic_contract_repair(
                    repo,
                    case,
                )
                if deterministic_contract_repair.get("patch_applied"):
                    public_after_contract = _run_case_tests(repo, case, public_only=True)
                    feedback_after_contract = _run_case_tests(
                        repo,
                        case,
                        public_only=False,
                    )
                    if public_after_contract["passed"] and feedback_after_contract["passed"]:
                        public_tests = public_after_contract
                        feedback_tests = feedback_after_contract
                        _accept_validated_contract_repair_diagnosis(
                            diagnosis,
                            str(
                                deterministic_contract_repair.get(
                                    "proposed_dimension"
                                )
                                or ""
                            ),
                            stage="deterministic_contract_repair_validated",
                            validation_evidence=(
                                "Recognized source-structural repair preserved public contracts "
                                "and resolved every disclosed feedback contract."
                            ),
                        )
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
                            "Rolled back deterministic contract repair because repair-feedback validation did not pass.",
                        ]
                        public_tests = _run_case_tests(repo, case, public_only=True)
                        feedback_tests = _run_case_tests(repo, case, public_only=False)
            deterministic_contract_repair.pop("_snapshot", None)
            repair_attempts: list[dict[str, Any]] = []
            rejected_attempt_fingerprints: set[str] = set()
            attempted_plan_fingerprints: set[str] = set()
            prompt_contract_closure = _prompt_contract_closure(repo, case)
            for repair_round, repair_model in enumerate(repair_schedule, start=1):
                if (
                    public_tests["passed"]
                    and feedback_tests["passed"]
                    and not prompt_contract_closure
                ):
                    break
                if isinstance(calls, _ModelCallLedger) and calls.budget_exhausted:
                    patch["warnings"] = [
                        *(patch.get("warnings") or []),
                        "Stopped local repair because the per-case model wall budget was exhausted.",
                    ]
                    break
                failure_context = _validation_failure_context(
                    public_tests,
                    feedback_tests,
                )
                if prompt_contract_closure:
                    failure_context += (
                        "\n\nUNRESOLVED PROMPT-DERIVED CONTRACT CLOSURE:\n"
                        + "\n".join(
                            f"- {contract_id}: {detail}"
                            for contract_id, detail in prompt_contract_closure.items()
                        )
                    )
                before_public_tests = dict(public_tests)
                before_feedback_tests = dict(feedback_tests)
                before_feedback_contracts = validation_contracts.test_contract_evidence(
                    feedback_tests
                )
                before_prompt_contract_closure = dict(prompt_contract_closure)
                before_repair = _candidate_snapshot(repo, case)
                before_quality = _validation_quality(public_tests, feedback_tests)
                before_failure_signature = _normalized_failure_signature(feedback_tests)
                before_test_contracts = dict(before_feedback_contracts)
                if prompt_contract_closure:
                    before_test_contracts["prompt_contract_details"] = dict(
                        prompt_contract_closure
                    )
                before_failure_signature = hashlib.sha256(
                    (
                        before_failure_signature
                        + json.dumps(prompt_contract_closure, sort_keys=True)
                    ).encode("utf-8")
                ).hexdigest()
                repair = _repair_after_failure(
                    repo,
                    case,
                    diagnosis,
                    patch,
                    failure_context,
                    repair_model,
                    calls,
                    args.timeout,
                    repair_round,
                    feedback_context=feedback_context,
                    attempt_ledger=_attempt_ledger_context(repair_attempts),
                    contract_evidence=before_test_contracts,
                    compact_escalation=repair_model != str(args.model),
                    failure_signature=before_failure_signature,
                    attempted_plan_fingerprints=attempted_plan_fingerprints,
                )
                repair_attempts.append(repair)
                repair["model"] = repair_model
                repair["escalated"] = repair_model != str(args.model)
                repair["before_failure_signature"] = before_failure_signature
                repair["before_test_contracts"] = before_test_contracts
                repair["before_validation_output"] = failure_context[:6_000]
                revised_dimension = _plan_dimension(repair.get("plan") or {})
                repair["proposed_diagnosis_dimension"] = revised_dimension
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
                    repair["after_failure_signature"] = before_failure_signature
                    repair["after_test_contracts"] = before_test_contracts
                    repair["after_validation_output"] = failure_context[:6_000]
                    repair["adapter_rejection"] = (
                        "CHILI adapter rejected the attempted edit:\n"
                        + "\n".join(repair.get("warnings") or ["no applicable edit"])
                    )
                    continue
                attempted_snapshot = _candidate_snapshot(repo, case)
                attempt_fingerprint = _snapshot_fingerprint(attempted_snapshot)
                repair["attempt_fingerprint"] = attempt_fingerprint
                repair.setdefault(
                    "attempted_diff",
                    _snapshot_diff(before_repair, attempted_snapshot),
                )
                if attempt_fingerprint in rejected_attempt_fingerprints:
                    _restore_candidate_snapshot(repo, before_repair)
                    repair["duplicate_attempt"] = True
                    repair["rolled_back_after_validation"] = True
                    duplicate_warning = (
                        "Rejected duplicate repair state already proven regressive or no-progress."
                    )
                    repair["warnings"] = [
                        *(repair.get("warnings") or []),
                        duplicate_warning,
                    ]
                    patch["warnings"].append(duplicate_warning)
                    patch["changed_files"] = _changed_candidate_files(
                        repo,
                        case.get("candidate_paths") or [],
                    )
                    patch["patch_applied"] = bool(patch["changed_files"])
                    public_tests = _run_case_tests(repo, case, public_only=True)
                    feedback_tests = _run_case_tests(repo, case, public_only=False)
                    prompt_contract_closure = _prompt_contract_closure(repo, case)
                    repair["after_failure_signature"] = _normalized_failure_signature(
                        feedback_tests
                    )
                    repair["after_test_contracts"] = validation_contracts.test_contract_evidence(
                        feedback_tests
                    )
                    repair["after_validation_output"] = _validation_failure_context(
                        public_tests,
                        feedback_tests,
                    )[:6_000]
                    continue
                patch["changed_files"] = _changed_candidate_files(
                    repo, case.get("candidate_paths") or []
                )
                patch["patch_applied"] = bool(patch["changed_files"])
                public_tests = _run_case_tests(repo, case, public_only=True)
                feedback_tests = _run_case_tests(repo, case, public_only=False)
                prompt_contract_closure = _prompt_contract_closure(repo, case)
                after_quality = _validation_quality(public_tests, feedback_tests)
                after_failure_signature = _normalized_failure_signature(feedback_tests)
                repair["after_failure_signature"] = after_failure_signature
                after_feedback_contracts = validation_contracts.test_contract_evidence(
                    feedback_tests
                )
                repair["after_test_contracts"] = dict(after_feedback_contracts)
                if prompt_contract_closure:
                    repair["after_test_contracts"]["prompt_contract_details"] = dict(
                        prompt_contract_closure
                    )
                repair["after_validation_output"] = _validation_failure_context(
                    public_tests,
                    feedback_tests,
                )[:6_000]
                advanced = _validation_advanced(
                    before_public_tests,
                    before_feedback_tests,
                    public_tests,
                    feedback_tests,
                )
                closure_advanced = bool(
                    before_prompt_contract_closure
                    and set(prompt_contract_closure)
                    < set(before_prompt_contract_closure)
                    and public_tests.get("passed")
                    and not validation_contracts.contract_regressions(
                        before_feedback_contracts,
                        after_feedback_contracts,
                    )
                )
                advanced = bool(advanced or closure_advanced)
                if not advanced:
                    repair["validation_output"] = _validation_failure_context(
                        public_tests,
                        feedback_tests,
                    )[:6_000]
                    _restore_candidate_snapshot(repo, before_repair)
                    repair["rolled_back_after_validation"] = True
                    repair["validation_regression"] = {
                        "before_quality": before_quality,
                        "after_quality": after_quality,
                        "before_failure_signature": before_failure_signature,
                        "after_failure_signature": after_failure_signature,
                    }
                    rejected_attempt_fingerprints.add(attempt_fingerprint)
                    rollback_warning = (
                        "Rolled back repair because validation regressed or made no measurable progress."
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
                    feedback_tests = _run_case_tests(repo, case, public_only=False)
                    prompt_contract_closure = _prompt_contract_closure(repo, case)
                    continue
                if revised_dimension:
                    _accept_diagnosis_proposal(
                        diagnosis,
                        revised_dimension,
                        stage=f"repair_{repair_round}_validated",
                        validation_evidence=repair["after_validation_output"],
                    )
            patch["changed_files"] = _changed_candidate_files(
                repo, case.get("candidate_paths") or []
            )
            patch["patch_applied"] = bool(patch["changed_files"])
            patch["selected_file"] = (
                patch["changed_files"][0] if len(patch["changed_files"]) == 1 else ""
            )
            model_calls_before_final = len(calls)
            if entry.get("final_oracle"):
                final_oracle = _read_json(
                    _fixture_path(
                        fixture_root,
                        entry.get("final_oracle"),
                        "final oracle",
                    )
                )
                if final_oracle.get("case_id") != case.get("case_id"):
                    raise ValueError(
                        "Final oracle case_id does not match the public case."
                    )
                complete_partitions = _oracle_test_partitions(
                    oracle,
                    final_oracle=final_oracle,
                    require_sealed=True,
                    require_external_final=True,
                )
                if complete_partitions["feedback_files"] != partitions["feedback_files"]:
                    raise RuntimeError(
                        "Repair feedback changed before final adjudication."
                    )
                _validate_oracle_test_paths(case, complete_partitions)
                partitions = complete_partitions
            baseline_final = _run_final_adjudication(
                case,
                partitions["final_files"],
            )
            final_tests = _run_final_adjudication(
                case,
                partitions["final_files"],
                candidate_repo=repo,
            )
            accepted_conclusion = (
                diagnosis.get("accepted_conclusion")
                if isinstance(diagnosis.get("accepted_conclusion"), Mapping)
                else {}
            )
            if (
                str(accepted_conclusion.get("stage") or "")
                == "deterministic_contract_repair_validated"
                and not final_tests.get("passed")
            ):
                retracted = {
                    **dict(accepted_conclusion),
                    "accepted": False,
                    "causal_status": "retracted",
                    "retraction_reason": (
                        "sealed final adjudication exposed an unresolved boundary"
                    ),
                }
                diagnosis["accepted_conclusion"] = retracted
                diagnosis.setdefault("diagnosis_history", []).append(retracted)
            if len(calls) != model_calls_before_final:
                raise RuntimeError(
                    "A model call occurred after final adjudication began."
                )
            score, checks = _score_case(
                oracle,
                diagnosis,
                patch,
                baseline_final,
                public_tests,
                final_tests,
            )
            prompt_contract_closure = _prompt_contract_closure(repo, case)
            checks["prompt_contract_closure"] = not bool(prompt_contract_closure)
            report = diagnosis.get("report") if isinstance(diagnosis.get("report"), Mapping) else {}
            conclusion = report.get("conclusion") if isinstance(report.get("conclusion"), Mapping) else {}
            initial_report = (
                diagnosis.get("initial_report")
                if isinstance(diagnosis.get("initial_report"), Mapping)
                else {}
            )
            initial_conclusion = (
                initial_report.get("conclusion")
                if isinstance(initial_report.get("conclusion"), Mapping)
                else {}
            )
            effective_dimension = _effective_diagnosis_dimension(diagnosis)
            case_results.append(
                {
                    "case_id": case["case_id"],
                    "language": str(case.get("language") or "python"),
                    "test_runner": _case_test_runner(case),
                    "evaluation_role": (
                        "development_regression"
                        if disclosed_replay
                        else str(
                            entry.get("evaluation_role")
                            or "development_regression"
                        )
                    ),
                    "original_evaluation_role": str(
                        entry.get("evaluation_role") or "development_regression"
                    ),
                    "split": (
                        "disclosed_replay"
                        if disclosed_replay
                        else str(entry.get("split") or "holdout")
                    ),
                    "score": score,
                    "checks": checks,
                    "diagnosis_dimension": effective_dimension,
                    "initial_diagnosis_dimension": str(
                        initial_conclusion.get("dimension")
                        or conclusion.get("dimension")
                        or "unknown"
                    ),
                    "retained_diagnosis_dimension": str(
                        conclusion.get("dimension") or "unknown"
                    ),
                    "diagnosis_history": diagnosis.get("diagnosis_history") or [],
                    "accepted_diagnosis_conclusion": diagnosis.get("accepted_conclusion") or {},
                    "diagnosis_status": str(conclusion.get("status") or "inconclusive"),
                    "diagnosis_report": report,
                    "diagnosis_packet": diagnosis.get("packet") or {},
                    "diagnosis_stages": diagnosis.get("stages") or [],
                    "diagnosis_probe_run": diagnosis.get("probe_run") or {},
                    "post_probe_conclusion_revision": diagnosis.get(
                        "post_probe_conclusion_revision"
                    )
                    or {},
                    "selected_file": patch.get("selected_file") or "",
                    "selected_files": patch.get("selected_files") or [],
                    "changed_files": patch.get("changed_files") or [],
                    "patch_applied": bool(patch.get("patch_applied")),
                    "functional_repair_passed": bool(final_tests.get("passed")),
                    "prompt_contract_closure_passed": not bool(
                        prompt_contract_closure
                    ),
                    "prompt_contract_closure_warnings": prompt_contract_closure,
                    "patch_warnings": patch.get("warnings") or [],
                    "public_tests": public_tests,
                    "feedback_tests": feedback_tests,
                    "final_tests": final_tests,
                    "baseline_feedback_tests": baseline_feedback,
                    "baseline_final_tests": baseline_final,
                    "baseline_public_tests": baseline_public,
                    "sealed_final_adjudication": bool(partitions["sealed"]),
                    "external_final_oracle": bool(partitions["external_final"]),
                    "model_calls_before_final": model_calls_before_final,
                    "model_calls_after_final": 0,
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
        "disclosed_replay_passed"
        if disclosed_replay and _verdict(case_results) == "shadow_ready"
        else "disclosed_replay_failed"
        if disclosed_replay
        else
        "blinded_evaluation_passed"
        if blinded_holdouts and _verdict(blinded_holdouts) == "shadow_ready"
        else "blinded_evaluation_failed"
        if blinded_holdouts
        else "development_regression_passed"
        if _verdict(case_results) == "shadow_ready"
        else "development_regression_failed"
    )
    total_cases = len(case_results)
    functional_solve_count = sum(
        1 for item in case_results if item.get("functional_repair_passed")
    )
    diagnosis_correct_count = sum(
        1 for item in case_results if (item.get("checks") or {}).get("diagnosis")
    )
    exact_file_set_count = sum(
        1 for item in case_results if (item.get("checks") or {}).get("file_selection")
    )
    diagnostic_stages = [
        stage
        for item in case_results
        for stage in item.get("diagnosis_stages") or []
        if isinstance(stage, Mapping)
    ]
    accepted_diagnostic_stages = sum(
        1 for stage in diagnostic_stages if stage.get("accepted")
    )
    causally_confirmed_diagnostic_stages = sum(
        1
        for stage in diagnostic_stages
        if str(((stage.get("report") or {}).get("conclusion") or {}).get("status"))
        == "confirmed"
        and str(
            ((stage.get("report") or {}).get("conclusion") or {}).get(
                "causal_sufficiency"
            )
        )
        in {"graph_linked", "isolated"}
    )
    causally_accepted_diagnoses = sum(
        1
        for item in case_results
        if bool((item.get("accepted_diagnosis_conclusion") or {}).get("accepted"))
    )
    results = {
        "schema": "chili.diagnosis-to-fix-results.v5",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "evaluation_context": evaluation_context,
        "model": args.model,
        "escalation_model": str(getattr(args, "escalation_model", "") or ""),
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
        "sealed_final_case_count": sum(
            1 for item in case_results if item.get("sealed_final_adjudication")
        ),
        "functional_solve_count": functional_solve_count,
        "functional_solve_rate": round(100 * functional_solve_count / total_cases, 2),
        "diagnosis_correct_count": diagnosis_correct_count,
        "diagnosis_accuracy": round(100 * diagnosis_correct_count / total_cases, 2),
        "exact_file_set_count": exact_file_set_count,
        "exact_file_set_accuracy": round(100 * exact_file_set_count / total_cases, 2),
        "diagnostic_stage_count": len(diagnostic_stages),
        "diagnostic_stage_acceptance_rate": round(
            100 * accepted_diagnostic_stages / len(diagnostic_stages),
            2,
        )
        if diagnostic_stages
        else 0.0,
        "diagnostic_stage_schema_acceptance_rate": round(
            100 * accepted_diagnostic_stages / len(diagnostic_stages),
            2,
        )
        if diagnostic_stages
        else 0.0,
        "diagnostic_stage_causal_confirmation_rate": round(
            100 * causally_confirmed_diagnostic_stages / len(diagnostic_stages),
            2,
        )
        if diagnostic_stages
        else 0.0,
        "causally_accepted_diagnosis_count": causally_accepted_diagnoses,
        "causal_diagnosis_acceptance_rate": round(
            100 * causally_accepted_diagnoses / total_cases,
            2,
        ),
        "average_case_duration_ms": round(average_duration, 2),
        "verdict": _verdict(case_results),
        "evaluation_verdict": evaluation_verdict,
        "premium_calls": 0,
        "max_repair_rounds": len(repair_schedule),
        "max_base_repair_rounds": max(
            0, min(MAX_REPAIR_ROUNDS, int(args.max_repairs))
        ),
        "max_escalation_repair_rounds": max(
            0,
            len(repair_schedule)
            - max(0, min(MAX_REPAIR_ROUNDS, int(args.max_repairs))),
        ),
        "fable5_head_to_head_run": False,
        "fable5_parity_claim": False,
        "score_weights": SCORE_WEIGHTS,
        "maximum_score_without_final_pass": sum(
            weight for key, weight in SCORE_WEIGHTS.items() if key != "final_tests"
        ),
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
    parser.add_argument("--case-model-time-budget", type=float, default=690.0)
    parser.add_argument("--max-repairs", type=int, default=5)
    parser.add_argument("--escalation-model", default="")
    parser.add_argument("--max-escalation-repairs", type=int, default=0)
    parser.add_argument("--validate-fixtures", action="store_true")
    parser.add_argument(
        "--evaluation-context",
        choices=("protocol", "disclosed_replay"),
        default="protocol",
    )
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

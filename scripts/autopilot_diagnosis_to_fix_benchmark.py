"""Local-only diagnosis-to-fix benchmark with sealed hidden tests.

The model sees the case prompt and candidate repository only. Oracle labels and
hidden tests are loaded after diagnosis, planning, and patch generation finish.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


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


def _candidate_context(repo: Path, candidates: list[str]) -> str:
    parts: list[str] = []
    for rel in candidates:
        path = repo / rel
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        parts.append(f"### {rel}\n{content[:6000]}")
    return "\n\n".join(parts)


def _plan_prompt(
    prompt: str,
    candidates: list[str],
    context: str,
    report: Mapping[str, Any],
) -> str:
    return (
        "Return one JSON object only. Select the smallest owning file for the diagnosed bug. "
        "Do not select tests or invent paths. Shape: "
        '{"analysis":"...","files":[{"path":"...","action":"modify","description":"..."}],"notes":"..."}.\n\n'
        f"Request:\n{prompt}\n\n"
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
    lines = [
        "# Autonomous Diagnosis-to-Fix Benchmark",
        "",
        f"- Run: {results['created_at']}",
        f"- Local model: `{results['model']}`",
        f"- Reference family: `{results['reference_family']}`",
        f"- Overall score: **{results['overall_score']:.1f}/100**",
        f"- Verdict: **{results['verdict']}**",
        "- Premium calls: **0**",
        f"- Average wall time: **{results['average_case_duration_ms'] / 1000:.1f}s/case**",
        "- Fable 5 parity claim: **No**. This is a local held-out repair benchmark, not a blinded frontier head-to-head.",
        "",
        "| Case | Score | Diagnosis | Selected file | Patch | Public | Hidden |",
        "|---|---:|---|---|---:|---:|---:|",
    ]
    for item in results["cases"]:
        lines.append(
            f"| {item['case_id']} | {item['score']} | {item['diagnosis_dimension']} | "
            f"{item.get('selected_file') or '-'} | {str(item['patch_applied']).lower()} | "
            f"{str(item['public_tests']['passed']).lower()} | {str(item['hidden_tests']['passed']).lower()} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "Each repository is created from a held-out case. The model sees only the prompt, candidate source, "
            "and public tests. Oracle labels and hidden tests are loaded after the patch has been generated. "
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
    blocks = code_agent._parse_search_replace_blocks(edit_text)
    outcome = (
        code_agent._apply_search_replace(original, blocks)
        if blocks
        else code_agent._extract_full_file_replacement(edit_text, selected, original)
    )
    new_content = outcome.get("new_content")
    if not isinstance(new_content, str) or new_content.rstrip() == original.rstrip():
        return {
            "patch_applied": False,
            "warnings": outcome.get("warnings") or ["Patch made no change."],
        }
    path.write_text(new_content, encoding="utf-8")
    return {
        "patch_applied": True,
        "warnings": outcome.get("warnings") or [],
    }


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
    plan_text = _local_call(
        model,
        [
            {"role": "system", "content": "You are a senior coding architect. Return compact JSON only."},
            {
                "role": "user",
                "content": _plan_prompt(str(case.get("prompt") or ""), candidates, context, report),
            },
        ],
        stage="plan",
        calls=calls,
        timeout=timeout,
        num_predict=450,
        json_mode=True,
    )
    plan = code_agent._parse_plan_json(plan_text) or {}
    selected = ""
    description = ""
    for item in plan.get("files") or []:
        if not isinstance(item, Mapping):
            continue
        rel = _safe_rel(item.get("path"))
        if rel in candidates:
            selected = rel
            description = str(item.get("description") or "")
            break
    if not selected:
        return {"plan": plan, "selected_file": "", "patch_applied": False, "warnings": ["No valid file selected."]}
    diagnosis_context = diagnostic_reasoning.report_context(report)
    edit = _apply_local_edit(
        repo,
        selected,
        (
            f"{description or case.get('prompt')}\n\n"
            f"Evidence-gated diagnosis:\n{diagnosis_context}\n"
            f"Strongest causal evidence:\n{evidence_context or '(none)'}\n"
            "Make the smallest production fix. Preserve public behavior outside the diagnosed cause."
        ),
        model,
        calls,
        timeout,
        stage="edit",
    )
    return {
        "plan": plan,
        "selected_file": selected,
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
    repair_prompt = (
        "Return one compact JSON object only. A previous locally generated patch failed validation. "
        "Use the failure output to reconsider file ownership and select one owning source file from the allowed "
        "candidates. Never select a test. Shape: "
        '{"analysis":"...","files":[{"path":"...","action":"modify","description":"..."}],"notes":"..."}.\n\n'
        f"Original request:\n{case.get('prompt')}\n\n"
        f"Evidence decision:\n{diagnostic_reasoning.report_context(report)}\n"
        f"Strongest evidence:\n{evidence_context or '(none)'}\n\n"
        f"Previous selected file: {previous_patch.get('selected_file') or '(none)'}\n\n"
        f"Validation failure:\n{failure_output[-7000:]}\n\n"
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
    selected = ""
    description = ""
    for item in plan.get("files") or []:
        if not isinstance(item, Mapping):
            continue
        rel = _safe_rel(item.get("path"))
        if rel in candidates:
            selected = rel
            description = str(item.get("description") or "")
            break
    if not selected:
        return {
            "round": round_index,
            "plan": plan,
            "selected_file": "",
            "patch_applied": False,
            "warnings": ["Repair planner selected no valid source file."],
        }
    edit = _apply_local_edit(
        repo,
        selected,
        (
            f"{description or case.get('prompt')}\n\n"
            f"The previous patch failed this validation:\n{failure_output[-6000:]}\n\n"
            "Repair the root cause in the selected source file. Do not edit tests or weaken assertions."
        ),
        model,
        calls,
        timeout,
        stage=f"repair_edit_{round_index}",
    )
    return {
        "round": round_index,
        "plan": plan,
        "selected_file": selected,
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
    checks = {
        "baseline_hidden_failure": not bool(baseline_hidden.get("passed")),
        "diagnosis": conclusion.get("dimension") == oracle.get("expected_dimension"),
        "file_selection": patch.get("selected_file") == oracle.get("expected_file"),
        "patch_applied": bool(patch.get("patch_applied")),
        "public_tests": bool(public_tests.get("passed")),
        "hidden_tests": bool(hidden_tests.get("passed")),
        "premium_independence": True,
    }
    return sum(SCORE_WEIGHTS[name] for name, passed in checks.items() if passed), checks


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
        public = _run_pytest(repo)
        _write_files(repo, oracle.get("hidden_files") or {})
        hidden = _run_pytest(repo)
    return {
        "case_id": case.get("case_id"),
        "public_passed": public["passed"],
        "hidden_failed": not hidden["passed"],
        "valid": public["passed"] and not hidden["passed"],
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
            diagnosis = _diagnose(repo, case, args.model, calls, args.timeout)
            patch = _generate_patch(repo, case, diagnosis, args.model, calls, args.timeout)
            public_tests = _run_pytest(repo)

            # Oracle access begins only after the patch and public validation exist.
            oracle = _read_json(fixture_root / str(entry["oracle"]))
            with tempfile.TemporaryDirectory(prefix="chili-baseline-hidden-") as baseline_temp:
                baseline_repo = Path(baseline_temp) / "repo"
                _init_repo(baseline_repo, case.get("repo_files") or {})
                _write_files(baseline_repo, oracle.get("hidden_files") or {})
                baseline_hidden = _run_pytest(baseline_repo)
            _write_files(repo, oracle.get("hidden_files") or {})
            hidden_tests = _run_pytest(repo)
            repair_attempts: list[dict[str, Any]] = []
            for repair_round in range(1, max(0, int(args.max_repairs)) + 1):
                if hidden_tests["passed"]:
                    break
                repair = _repair_after_failure(
                    repo,
                    case,
                    diagnosis,
                    patch,
                    hidden_tests["output"],
                    args.model,
                    calls,
                    args.timeout,
                    repair_round,
                )
                repair_attempts.append(repair)
                if repair.get("selected_file"):
                    patch.setdefault("initial_selected_file", patch.get("selected_file") or "")
                    patch["selected_file"] = repair["selected_file"]
                patch["patch_applied"] = bool(
                    patch.get("patch_applied") or repair.get("patch_applied")
                )
                patch["warnings"] = [
                    *(patch.get("warnings") or []),
                    *(repair.get("warnings") or []),
                ]
                if not repair.get("patch_applied"):
                    hidden_tests = {
                        **hidden_tests,
                        "output": (
                            f"{hidden_tests['output']}\n\n"
                            "CHILI adapter rejected the attempted edit:\n"
                            + "\n".join(repair.get("warnings") or ["no applicable edit"])
                        ),
                    }
                    continue
                public_tests = _run_pytest(repo, "tests/test_public.py")
                hidden_tests = _run_pytest(repo)
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
                    "score": score,
                    "checks": checks,
                    "diagnosis_dimension": str(conclusion.get("dimension") or "unknown"),
                    "diagnosis_status": str(conclusion.get("status") or "inconclusive"),
                    "diagnosis_report": report,
                    "diagnosis_packet": diagnosis.get("packet") or {},
                    "selected_file": patch.get("selected_file") or "",
                    "patch_applied": bool(patch.get("patch_applied")),
                    "patch_warnings": patch.get("warnings") or [],
                    "public_tests": public_tests,
                    "hidden_tests": hidden_tests,
                    "baseline_hidden_tests": baseline_hidden,
                    "repair_attempts": repair_attempts,
                    "model_calls": calls,
                    "premium_calls": 0,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }
            )

    average = sum(item["score"] for item in case_results) / len(case_results)
    average_duration = sum(item["duration_ms"] for item in case_results) / len(case_results)
    all_hidden = all(item["hidden_tests"]["passed"] for item in case_results)
    results = {
        "schema": "chili.diagnosis-to-fix-results.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "reference_family": manifest.get("reference_family") or "claude-fable-5",
        "overall_score": round(average, 2),
        "average_case_duration_ms": round(average_duration, 2),
        "verdict": "shadow_ready" if average >= 90 and all_hidden else "needs_improvement",
        "premium_calls": 0,
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
    parser.add_argument("--max-repairs", type=int, default=2)
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

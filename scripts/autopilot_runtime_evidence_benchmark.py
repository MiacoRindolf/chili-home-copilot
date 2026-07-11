"""Local-only dynamic diagnosis benchmark for typed log/database evidence."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get("TEST_DATABASE_URL")
    or "postgresql://chili:chili@127.0.0.1:5433/chili_runtime_evidence_benchmark",
)

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, create_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.services.coding_task.envelope import subprocess_safe_env  # noqa: E402
from app.services.context_brain import ollama_client  # noqa: E402
from app.services.project_autonomy import diagnostic_probes  # noqa: E402
from app.services.project_autonomy import diagnostic_reasoning  # noqa: E402
from app.services.project_autonomy import diagnostic_runtime_evidence  # noqa: E402


DEFAULT_FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "autonomy_runtime_diagnostics"
DEFAULT_REPORT = ROOT / "project_ws" / "AgentOps" / "RUNTIME_EVIDENCE_DIAGNOSTIC_BENCHMARK.md"
DEFAULT_RESULTS = ROOT / "project_ws" / "AgentOps" / "runtime_evidence_diagnostic_results.json"
SCORE_WEIGHTS = {
    "dimension": 35,
    "confirmed": 15,
    "probe_coverage": 20,
    "retraction": 10,
    "valid": 10,
    "premium_independence": 10,
}


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected an object in {path}")
    return value


def _safe_rel(value: object) -> str:
    rendered = str(value or "").replace("\\", "/").strip().strip("/")
    if not rendered or Path(rendered).is_absolute() or ".." in Path(rendered).parts:
        return ""
    return rendered


def _write_files(root: Path, files: Mapping[str, Any]) -> None:
    for raw_path, content in files.items():
        rel = _safe_rel(raw_path)
        if not rel:
            raise ValueError(f"Unsafe fixture path: {raw_path!r}")
        target = (root / rel).resolve()
        target.relative_to(root.resolve())
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")


def _run(args: list[str], cwd: Path, timeout: float = 30.0) -> None:
    completed = subprocess.run(
        args,
        cwd=str(cwd),
        env=subprocess_safe_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "command failed")[-2_000:])


def _init_repo(root: Path, files: Mapping[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _write_files(root, files)
    for args in (
        ["git", "init"],
        ["git", "config", "user.email", "runtime-benchmark@example.test"],
        ["git", "config", "user.name", "CHILI Runtime Benchmark"],
        ["git", "add", "."],
        ["git", "commit", "-m", "seed runtime evidence case"],
    ):
        _run(args, root)


@contextmanager
def _database_fixture(test_url: str, specs: list[Mapping[str, Any]]):
    if not diagnostic_runtime_evidence._is_test_database_url(test_url):
        raise RuntimeError("Runtime evidence benchmark requires TEST_DATABASE_URL ending in _test.")
    engine = create_engine(test_url, poolclass=NullPool)
    metadata = MetaData(schema="public")
    tables: list[Table] = []
    for spec in specs:
        name = diagnostic_runtime_evidence._safe_identifier(spec.get("name"))
        if not name:
            raise ValueError("Unsafe runtime benchmark table name.")
        tables.append(
            Table(
                name,
                metadata,
                Column("id", Integer, primary_key=True, autoincrement=True),
                Column("cause", String(80), nullable=False),
                Column("status", String(24), nullable=False, default="pending"),
                Column("created_at", DateTime(timezone=True), nullable=False),
            )
        )
    metadata.drop_all(engine, tables=tables, checkfirst=True)
    metadata.create_all(engine, tables=tables, checkfirst=True)
    try:
        now = datetime.now(timezone.utc)
        with engine.begin() as connection:
            for table, spec in zip(tables, specs):
                rows = [
                    {"cause": cause, "status": "pending", "created_at": now}
                    for cause, raw_count in (spec.get("groups") or {}).items()
                    for _ in range(max(0, int(raw_count)))
                ]
                if rows:
                    connection.execute(table.insert(), rows)
        yield
    finally:
        metadata.drop_all(engine, tables=tables, checkfirst=True)
        engine.dispose()


def _local_call(
    model: str,
    prompt: str,
    stage: str,
    calls: list[dict[str, Any]],
    timeout: float,
) -> str:
    result = ollama_client.chat(
        [
            {
                "role": "system",
                "content": (
                    "You are CHILI's premium-independent runtime diagnostic judge. "
                    "Use only supplied evidence and return JSON only."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        model,
        temperature=0.1,
        timeout_sec=timeout,
        options={"num_predict": 900, "num_ctx": 8192, "keep_alive": "20m", "format": "json"},
    )
    calls.append(
        {
            "stage": stage,
            "ok": result.ok,
            "latency_ms": result.latency_ms,
            "tokens_out": result.tokens_out,
            "error": result.error,
        }
    )
    return result.text if result.ok else ""


def _score(
    oracle: Mapping[str, Any],
    final: Mapping[str, Any],
    probes: list[Mapping[str, Any]],
) -> tuple[int, dict[str, bool]]:
    report = final.get("report") if isinstance(final.get("report"), Mapping) else {}
    conclusion = report.get("conclusion") if isinstance(report.get("conclusion"), Mapping) else {}
    expected_retraction = str(oracle.get("expected_retraction_hypothesis") or "")
    retractions = {
        str(item.get("hypothesis_id") or "")
        for item in report.get("retractions") or []
        if isinstance(item, Mapping)
    }
    kinds = {str(item.get("kind") or "") for item in probes}
    checks = {
        "dimension": conclusion.get("dimension") == oracle.get("expected_dimension"),
        "confirmed": conclusion.get("status") == "confirmed",
        "probe_coverage": set(oracle.get("expected_probe_kinds") or []) <= kinds,
        "retraction": (expected_retraction in retractions) if expected_retraction else True,
        "valid": bool(report.get("valid")),
        "premium_independence": True,
    }
    return sum(SCORE_WEIGHTS[key] for key, passed in checks.items() if passed), checks


def _markdown(results: Mapping[str, Any]) -> str:
    lines = [
        "# Runtime Evidence Diagnostic Benchmark",
        "",
        f"- Run: {results['created_at']}",
        f"- Local model: `{results['model']}`",
        f"- Reference family: `{results['reference_family']}`",
        f"- Overall score: **{results['overall_score']:.1f}/100**",
        f"- Verdict: **{results['verdict']}**",
        "- Premium calls: **0**",
        f"- Average wall time: **{results['average_case_duration_ms'] / 1000:.1f}s/case**",
        "- Fable 5 parity claim: **No**. This is a typed runtime-evidence holdout, not a frontier head-to-head.",
        "",
        "| Case | Score | Final dimension | Status | Probes | Retractions |",
        "|---|---:|---|---|---|---:|",
    ]
    for item in results["cases"]:
        lines.append(
            f"| {item['case_id']} | {item['score']} | {item['final_dimension']} | "
            f"{item['final_status']} | {', '.join(item['probe_kinds'])} | "
            f"{len(item['retractions'])} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "Cases begin without access to log or database contents. CHILI must request typed probes, execute "
            "bounded log-tail or aggregate-only PostgreSQL reads, ingest their provenance as evidence, and "
            "re-evaluate the conclusion. Database fixtures are created only in a `_test` database; probe "
            "transactions independently enforce PostgreSQL read-only mode.",
            "",
        ]
    )
    return "\n".join(lines)


def _verdict(case_results: list[Mapping[str, Any]]) -> str:
    return (
        "shadow_ready"
        if case_results and all(all((item.get("checks") or {}).values()) for item in case_results)
        else "needs_improvement"
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    fixture_root = Path(args.fixture_root).resolve()
    manifest = _read_json(fixture_root / "manifest.json")
    entries = [
        item
        for item in manifest.get("cases") or []
        if not args.case or Path(str(item.get("case") or "")).stem in set(args.case)
    ]
    if not entries:
        raise SystemExit("No runtime diagnostic cases selected.")
    test_url = (os.environ.get("TEST_DATABASE_URL") or "").strip()
    if not diagnostic_runtime_evidence._is_test_database_url(test_url):
        raise SystemExit("TEST_DATABASE_URL ending in _test is required.")
    if args.model not in ollama_client.list_models():
        raise SystemExit(f"Local model {args.model!r} is not installed.")

    case_results: list[dict[str, Any]] = []
    with _database_fixture(test_url, list(manifest.get("database_tables") or [])):
        for entry in entries:
            case = _read_json(fixture_root / str(entry["case"]))
            started = time.monotonic()
            calls: list[dict[str, Any]] = []
            with tempfile.TemporaryDirectory(prefix=f"chili-runtime-{case['case_id']}-") as temp:
                repo = Path(temp) / "repo"
                _init_repo(repo, case.get("repo_files") or {})
                built = diagnostic_reasoning.build_case_from_prompt(
                    str(case.get("prompt") or ""),
                    case_id=str(case["case_id"]),
                    repo_path=repo,
                    candidate_paths=[str(value) for value in case.get("candidate_paths") or []],
                )
                diagnostic_case = diagnostic_reasoning.normalize_case(
                    {
                        **built,
                        "observations": [
                            *(built.get("observations") or []),
                            *(case.get("observations") or []),
                        ],
                        "prior_conclusion": case.get("prior_conclusion"),
                    }
                )

                def judge(stage: str, prompt: str) -> str:
                    return _local_call(args.model, prompt, stage, calls, args.timeout)

                initial = diagnostic_reasoning.run_local_diagnostic_debate(
                    diagnostic_case,
                    judge,
                    stages_to_run=("judge",),
                )
                initial_report = initial.get("report") or {}
                defaults = diagnostic_probes.default_followup_probes(
                    initial_report,
                    case.get("candidate_paths") or [],
                    str(case.get("prompt") or ""),
                )
                model_probes = diagnostic_probes.probes_from_packet(
                    initial.get("packet") or {},
                    max_probes=4,
                )
                probes = diagnostic_probes.merge_probe_sets(defaults, model_probes, max_probes=4)
                probe_run = diagnostic_probes.execute_safe_probes(
                    repo,
                    probes,
                    max_probes=4,
                    time_budget_sec=120,
                    explicit_test_database_url=test_url,
                )
                enriched = diagnostic_reasoning.normalize_case(
                    {
                        **diagnostic_case,
                        "observations": [
                            *(diagnostic_case.get("observations") or []),
                            *(probe_run.get("evidence") or []),
                        ],
                    }
                )
                final = diagnostic_reasoning.run_local_diagnostic_debate(
                    enriched,
                    judge,
                    stages_to_run=("judge",),
                    previous_report=initial_report,
                )

                # Oracle access begins only after reasoning and probe execution finish.
                oracle = _read_json(fixture_root / str(entry["oracle"]))
                score, checks = _score(oracle, final, probes)
                report = final.get("report") or {}
                conclusion = report.get("conclusion") or {}
                case_results.append(
                    {
                        "case_id": case["case_id"],
                        "split": str(entry.get("split") or "holdout"),
                        "score": score,
                        "checks": checks,
                        "initial_report": initial_report,
                        "final_report": report,
                        "final_dimension": str(conclusion.get("dimension") or "unknown"),
                        "final_status": str(conclusion.get("status") or "inconclusive"),
                        "retractions": report.get("retractions") or [],
                        "probes": probes,
                        "probe_kinds": [str(item.get("kind") or "") for item in probes],
                        "probe_run": probe_run,
                        "model_calls": calls,
                        "premium_calls": 0,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                    }
                )

    average = sum(item["score"] for item in case_results) / len(case_results)
    results = {
        "schema": "chili.runtime-evidence-diagnostic-results.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "reference_family": manifest.get("reference_family") or "claude-fable-5",
        "overall_score": round(average, 2),
        "average_case_duration_ms": round(
            sum(item["duration_ms"] for item in case_results) / len(case_results),
            2,
        ),
        "verdict": _verdict(case_results),
        "premium_calls": 0,
        "fable5_head_to_head_run": False,
        "fable5_parity_claim": False,
        "cases": case_results,
    }
    report_path = Path(args.report).resolve()
    result_path = Path(args.results_json).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(_markdown(results), encoding="utf-8")
    result_path.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return results


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-root", default=str(DEFAULT_FIXTURE_ROOT))
    parser.add_argument("--model", default="qwen2.5-coder:7b")
    parser.add_argument("--case", action="append")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--results-json", default=str(DEFAULT_RESULTS))
    parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    results = run(_parser().parse_args())
    print(
        json.dumps(results, indent=2, sort_keys=True)
        if "--json" in sys.argv
        else (
            f"overall={results['overall_score']:.1f} verdict={results['verdict']} "
            "premium_calls=0"
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

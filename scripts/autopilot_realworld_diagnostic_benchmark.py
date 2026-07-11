"""Run CHILI's local-only real-world diagnostic benchmark.

Case inputs are loaded before model calls. Sealed oracle files are opened only
after each debate completes, preventing expected labels from entering prompts.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://chili:chili@127.0.0.1:5433/chili_diagnostic_benchmark",
)

from app.services.context_brain import ollama_client  # noqa: E402
from app.services.project_autonomy import diagnostic_reasoning  # noqa: E402


DEFAULT_FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "project_autonomy_diagnostics"
DEFAULT_REPORT = ROOT / "project_ws" / "AgentOps" / "REALWORLD_DIAGNOSTIC_REASONING_BENCHMARK.md"
DEFAULT_RESULTS = ROOT / "project_ws" / "AgentOps" / "realworld_diagnostic_reasoning_results.json"
WEIGHTS = {
    "dimension": 25,
    "decision": 20,
    "status": 10,
    "baseline_drift": 10,
    "grounded": 10,
    "safety": 10,
    "premium_independence": 10,
    "hypothesis_breadth": 5,
}


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected an object in {path}")
    return value


def _ids(values: object) -> set[str]:
    return {str(value) for value in values or [] if str(value)}


def score_debate(
    case: Mapping[str, Any],
    oracle: Mapping[str, Any],
    debate: Mapping[str, Any],
) -> dict[str, Any]:
    report = debate.get("report") if isinstance(debate.get("report"), Mapping) else {}
    packet = debate.get("packet") if isinstance(debate.get("packet"), Mapping) else {}
    conclusion = report.get("conclusion") if isinstance(report.get("conclusion"), Mapping) else {}
    packet_conclusion = (
        packet.get("conclusion") if isinstance(packet.get("conclusion"), Mapping) else {}
    )
    dimension = str(conclusion.get("dimension") or "unknown")
    decision = str(report.get("decision") or "investigate")
    status = str(conclusion.get("status") or "inconclusive")
    evidence_ids = _ids(
        item.get("evidence_id")
        for item in case.get("observations") or []
        if isinstance(item, Mapping)
    )
    conclusion_ids = _ids(packet_conclusion.get("evidence_ids"))
    hypotheses = [
        item for item in packet.get("hypotheses") or [] if isinstance(item, Mapping)
    ]
    hypothesis_dimensions = {
        str(item.get("dimension") or "unknown") for item in hypotheses
    }
    hypothesis_dimensions.update(
        str(item.get("dimension") or "unknown")
        for item in report.get("next_experiments") or []
        if isinstance(item, Mapping)
    )
    unsafe_auto = [
        str(item.get("experiment_id") or "experiment")
        for item in packet.get("experiments") or []
        if isinstance(item, Mapping)
        and bool(item.get("auto_execute"))
        and str(item.get("safety") or "") not in diagnostic_reasoning.AUTO_SAFE_LEVELS
    ]
    forbid_confirmed_code = bool(oracle.get("forbid_confirmed_code"))
    confirmed_code = status == "confirmed" and dimension == "code"
    expected_drift = bool(oracle.get("expected_baseline_drift"))
    actual_drift = bool(report.get("baseline_drift"))
    minimum_dimensions = int(oracle.get("minimum_hypothesis_dimensions") or 1)
    minimum_retractions = int(oracle.get("minimum_retractions") or 0)
    premium_calls = int(debate.get("premium_calls") or 0) + int(
        report.get("premium_calls") or 0
    )

    checks = {
        "dimension": dimension in _ids(oracle.get("expected_dimensions")),
        "decision": decision in _ids(oracle.get("expected_decisions")),
        "status": status in _ids(oracle.get("expected_statuses")),
        "baseline_drift": actual_drift == expected_drift,
        "grounded": bool(report.get("valid"))
        and not (conclusion_ids - evidence_ids)
        and (status != "confirmed" or bool(conclusion_ids))
        and len(report.get("retractions") or []) >= minimum_retractions,
        "safety": not unsafe_auto and not (forbid_confirmed_code and confirmed_code),
        "premium_independence": premium_calls == 0,
        "hypothesis_breadth": len(hypothesis_dimensions) >= minimum_dimensions,
    }
    score = sum(WEIGHTS[name] for name, passed in checks.items() if passed)
    return {
        "score": score,
        "checks": checks,
        "actual": {
            "dimension": dimension,
            "decision": decision,
            "status": status,
            "baseline_drift": actual_drift,
            "hypothesis_dimensions": sorted(hypothesis_dimensions),
            "unsafe_auto_experiments": unsafe_auto,
            "premium_calls": premium_calls,
        },
    }


def model_output_quality(
    case_results: list[dict[str, Any]],
    requested_stages: tuple[str, ...],
    *,
    heuristic_only: bool,
) -> dict[str, Any]:
    calls = [
        call
        for item in case_results
        for call in item.get("model_calls") or []
        if isinstance(call, Mapping)
    ]
    stages = [
        stage
        for item in case_results
        for stage in item.get("stages") or []
        if isinstance(stage, Mapping)
    ]
    expected_calls = 0 if heuristic_only else len(case_results) * len(requested_stages)
    accepted = sum(1 for stage in stages if bool(stage.get("accepted")))
    cases_with_accepted_stage = sum(
        1
        for item in case_results
        if any(
            bool(stage.get("accepted"))
            for stage in item.get("stages") or []
            if isinstance(stage, Mapping)
        )
    )
    transport_ok = sum(1 for call in calls if bool(call.get("ok")))
    gate_passed = heuristic_only or (
        len(calls) == expected_calls
        and transport_ok == expected_calls
        and cases_with_accepted_stage == len(case_results)
    )
    return {
        "expected_model_calls": expected_calls,
        "recorded_model_calls": len(calls),
        "successful_model_calls": transport_ok,
        "accepted_model_stages": accepted,
        "cases_with_accepted_model_stage": cases_with_accepted_stage,
        "model_output_usable_rate": (
            round(accepted / len(stages), 4) if stages else 1.0 if heuristic_only else 0.0
        ),
        "model_output_gate_passed": gate_passed,
    }


def _markdown(results: Mapping[str, Any]) -> str:
    rows = [
        "# Real-World Diagnostic Reasoning Benchmark",
        "",
        f"- Run: {results['created_at']}",
        f"- Local model: `{results['model']}`",
        f"- Reference family: `{results['reference_model']}`",
        f"- Overall score: **{results['overall_score']:.1f}/100**",
        f"- Holdout score: **{results['holdout_score']:.1f}/100**",
        f"- Verdict: **{results['verdict']}**",
        "- Premium calls: **0**",
        f"- Usable local model stages: **{results['accepted_model_stages']}/{results['recorded_model_calls']}**",
        f"- Model-output promotion gate: **{'pass' if results['model_output_gate_passed'] else 'fail'}**",
        f"- Average local model latency: **{results['average_local_latency_ms'] / 1000:.1f}s/case**",
        f"- Maximum local model latency: **{results['max_local_latency_ms'] / 1000:.1f}s**",
        "- Fable 5 parity claim: **No**. This run does not include a blinded Fable 5 head-to-head.",
        "",
        "| Case | Split | Score | Dimension | Decision | Status | Valid stages |",
        "|---|---:|---:|---|---|---|---:|",
    ]
    for item in results["cases"]:
        actual = item["score_detail"]["actual"]
        accepted = sum(1 for stage in item["stages"] if stage.get("accepted"))
        stage_total = len(item["stages"])
        rows.append(
            f"| {item['case_id']} | {item['split']} | {item['score_detail']['score']} | "
            f"{actual['dimension']} | {actual['decision']} | {actual['status']} | {accepted}/{stage_total} |"
        )
    rows.extend(
        [
            "",
            "## Interpretation",
            "",
            "Calibration cases reproduce three exact Fable 5 incident contracts, including a conclusion retraction. "
            "Holdout cases are sealed variants "
            "whose oracle labels are loaded only after local reasoning finishes. A high score validates this "
            "diagnostic contract, not universal superiority over a frontier model.",
            "",
            "The system is eligible for shadow use only when holdout score is at least 90, every case preserves "
            "the safety gate, premium calls remain zero, and every model-backed case has at least one accepted "
            "local packet. Transport success without usable JSON is not model reasoning. Frontier parity requires a separate blinded, "
            "multi-repository head-to-head with human adjudication. A rejected local stage is allowed only when "
            "the deterministic evidence fallback remains valid, safe, and fully scored.",
            "",
        ]
    )
    return "\n".join(rows)


def run(args: argparse.Namespace) -> dict[str, Any]:
    fixture_root = Path(args.fixture_root).resolve()
    manifest = _read_json(fixture_root / "manifest.json")
    selected_ids = set(args.case or [])
    entries = [
        item
        for item in manifest.get("cases") or []
        if isinstance(item, Mapping)
        and (not selected_ids or Path(str(item.get("case") or "")).stem in selected_ids)
    ]
    installed = ollama_client.list_models()
    if not args.heuristic_only and args.model not in installed:
        raise SystemExit(
            f"Local model {args.model!r} is not installed. Installed: {', '.join(installed) or 'none'}"
        )

    requested_stages = tuple(
        stage.strip()
        for stage in str(args.stages or "judge").split(",")
        if stage.strip()
    )
    case_results: list[dict[str, Any]] = []
    for entry in entries:
        case = _read_json(fixture_root / str(entry["case"]))
        model_calls: list[dict[str, Any]] = []

        def model_call(stage: str, prompt: str) -> str:
            print(f"[{case['case_id']}] {stage}: local model start", file=sys.stderr, flush=True)
            result = ollama_client.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are one role in CHILI's local-only diagnostic council. "
                            "Return JSON only and do not invent evidence."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                args.model,
                temperature=0.1,
                timeout_sec=args.timeout,
                options={
                    "num_predict": args.num_predict,
                    "num_ctx": args.num_ctx,
                    "keep_alive": args.keep_alive,
                    "format": "json",
                },
            )
            model_calls.append(
                {
                    "stage": stage,
                    "ok": result.ok,
                    "latency_ms": result.latency_ms,
                    "tokens_out": result.tokens_out,
                    "error": result.error,
                }
            )
            print(
                f"[{case['case_id']}] {stage}: ok={result.ok} "
                f"latency_ms={result.latency_ms} tokens_out={result.tokens_out}",
                file=sys.stderr,
                flush=True,
            )
            return result.text if result.ok else ""

        debate = diagnostic_reasoning.run_local_diagnostic_debate(
            case,
            None if args.heuristic_only else model_call,
            stages_to_run=requested_stages,
        )
        oracle = _read_json(fixture_root / str(entry["oracle"]))
        detail = score_debate(case, oracle, debate)
        case_results.append(
            {
                "case_id": case["case_id"],
                "split": str(entry.get("split") or "unknown"),
                "source": str(entry.get("source") or "unknown"),
                "score_detail": detail,
                "stages": debate.get("stages") or [],
                "packet": debate.get("packet") or {},
                "report": debate.get("report") or {},
                "model_calls": model_calls,
                "premium_calls": 0,
            }
        )

    if not case_results:
        raise SystemExit("No benchmark cases selected.")
    overall = sum(item["score_detail"]["score"] for item in case_results) / len(case_results)
    holdouts = [item for item in case_results if item["split"] == "holdout"]
    holdout_score = (
        sum(item["score_detail"]["score"] for item in holdouts) / len(holdouts)
        if holdouts
        else 0.0
    )
    all_safe = all(item["score_detail"]["checks"]["safety"] for item in case_results)
    all_local = all(
        item["score_detail"]["checks"]["premium_independence"] for item in case_results
    )
    output_quality = model_output_quality(
        case_results,
        requested_stages,
        heuristic_only=bool(args.heuristic_only),
    )
    shadow_ready = bool(holdouts) and holdout_score >= 90 and all_safe and all_local and bool(
        output_quality["model_output_gate_passed"]
    ) and all(
        item["score_detail"]["score"] >= 80 for item in holdouts
    )
    local_latencies = [
        int(call.get("latency_ms") or 0)
        for item in case_results
        for call in item.get("model_calls") or []
        if isinstance(call, Mapping)
    ]
    results = {
        "schema": "chili.realworld-diagnostic-results.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": "heuristic-only" if args.heuristic_only else args.model,
        "reference_model": manifest.get("reference_model") or "claude-fable-5",
        "overall_score": round(overall, 2),
        "holdout_score": round(holdout_score, 2),
        "verdict": "shadow_ready" if shadow_ready else "needs_improvement",
        "premium_calls": 0,
        "average_local_latency_ms": round(
            sum(local_latencies) / len(local_latencies),
            2,
        ) if local_latencies else 0.0,
        "max_local_latency_ms": max(local_latencies, default=0),
        "fable5_head_to_head_run": False,
        "fable5_parity_claim": False,
        **output_quality,
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
    parser.add_argument("--timeout", type=float, default=150.0)
    parser.add_argument("--num-predict", type=int, default=900)
    parser.add_argument("--num-ctx", type=int, default=8192)
    parser.add_argument("--keep-alive", default="20m")
    parser.add_argument(
        "--stages",
        default="judge",
        help="Comma-separated local roles. Use investigator,skeptic,judge for a deep audit.",
    )
    parser.add_argument("--heuristic-only", action="store_true")
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--results-json", default=str(DEFAULT_RESULTS))
    parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    results = run(args)
    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        print(
            f"overall={results['overall_score']:.1f} holdout={results['holdout_score']:.1f} "
            f"verdict={results['verdict']} premium_calls=0"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Prepare and score an authenticated Fable 5 diagnostic head-to-head.

Prompt-pack generation reads manifest-declared public case files only. Evaluation
requires the exact saved prompt pack, the complete provider response, and a
provider-native transcript that binds both prompt and response to
``claude-fable-5`` before any sealed oracle is opened.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.project_autonomy import diagnostic_reasoning  # noqa: E402
from scripts import autopilot_model_candidate_artifact_builder as identity_tools  # noqa: E402
from scripts.autopilot_realworld_diagnostic_benchmark import WEIGHTS, score_debate  # noqa: E402


TARGET_MODEL = "claude-fable-5"
PACK_SCHEMA = "chili.fable5-diagnostic-prompt-pack.v1"
RESPONSE_SCHEMA = "chili.fable5-diagnostic-response.v1"
RESULT_SCHEMA = "chili.fable5-diagnostic-headtohead.v1"
ALLOWED_DECISIONS = frozenset({"patch_root_cause", "instrument_first", "investigate"})
ALLOWED_STATUSES = frozenset({"confirmed", "provisional", "inconclusive", "rejected"})
PUBLIC_CASE_FIELDS = (
    "schema",
    "case_id",
    "problem_statement",
    "observations",
    "constraints",
)
DEFAULT_FIXTURE_ROOT = (
    ROOT / "tests" / "fixtures" / "project_autonomy_diagnostics_blinded8_20260712"
)
DEFAULT_OUTPUT_ROOT = ROOT / "project_ws" / "AgentOps" / "fable5_diagnostic_headtohead"


class HeadToHeadError(ValueError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise HeadToHeadError(f"Expected a JSON object in {path}")
    return value


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _safe_fixture_file(fixture_root: Path, raw_path: object, *, label: str) -> Path:
    relative = Path(str(raw_path or ""))
    if not str(relative) or relative.is_absolute() or ".." in relative.parts:
        raise HeadToHeadError(f"{label} must be a contained relative path")
    root = fixture_root.resolve()
    resolved = (root / relative).resolve()
    if root not in resolved.parents or not resolved.is_file():
        raise HeadToHeadError(f"{label} is missing or escapes the fixture root: {relative}")
    return resolved


def load_public_cases(
    fixture_root: Path,
    selected_case_ids: Sequence[str] = (),
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load only manifest and public case inputs; never open oracle paths."""
    fixture_root = fixture_root.resolve()
    manifest_path = fixture_root / "manifest.json"
    manifest = _read_json(manifest_path)
    entries = manifest.get("cases")
    if not isinstance(entries, list) or not entries:
        raise HeadToHeadError("Fixture manifest must contain a non-empty cases list")
    selected = {str(value) for value in selected_case_ids if str(value)}
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise HeadToHeadError(f"Manifest case {index} must be an object")
        case_path = _safe_fixture_file(
            fixture_root,
            entry.get("case"),
            label=f"manifest.cases[{index}].case",
        )
        case = _read_json(case_path)
        case_id = str(case.get("case_id") or "")
        if not case_id or case_id in seen:
            raise HeadToHeadError(f"Missing or duplicate case id: {case_id!r}")
        seen.add(case_id)
        if selected and case_id not in selected:
            continue
        public_case = {key: case[key] for key in PUBLIC_CASE_FIELDS if key in case}
        public_case["case_sha256"] = _sha256_bytes(case_path.read_bytes())
        public_case["manifest_case_path"] = case_path.relative_to(fixture_root).as_posix()
        cases.append(public_case)
    missing = selected - {str(case.get("case_id") or "") for case in cases}
    if missing:
        raise HeadToHeadError(f"Requested case ids are absent: {sorted(missing)}")
    if not cases:
        raise HeadToHeadError("No public cases selected")
    return manifest, cases


def render_prompt_pack(
    fixture_root: Path,
    selected_case_ids: Sequence[str] = (),
) -> str:
    manifest, cases = load_public_cases(fixture_root, selected_case_ids)
    manifest_sha256 = _sha256_bytes((fixture_root / "manifest.json").read_bytes())
    response_shape = {
        "schema": RESPONSE_SCHEMA,
        "cases": [
            {
                "case_id": "copy the case id",
                "dimension": "one allowed causal dimension",
                "decision": "patch_root_cause|instrument_first|investigate",
                "status": "confirmed|provisional|inconclusive|rejected",
                "baseline_drift": False,
                "evidence_ids": ["public evidence ids supporting the conclusion"],
                "reason": "concise causal explanation",
                "causal_chain": ["earliest break", "mechanism", "observed effect"],
                "hypotheses": [
                    {
                        "dimension": "one allowed causal dimension",
                        "claim": "competing causal claim",
                        "evidence_ids": ["public evidence id"],
                    }
                ],
                "retractions": ["specific rejected or revised claim"],
                "experiments": [
                    {
                        "experiment_id": "x1",
                        "dimension": "one allowed causal dimension",
                        "auto_execute": False,
                        "safety": "read_only|isolated|runtime|live",
                    }
                ],
            }
        ],
    }
    lines = [
        "# Authenticated Fable 5 Diagnostic Head-to-Head Pack",
        "",
        f"- Schema: `{PACK_SCHEMA}`",
        f"- Target model: `{TARGET_MODEL}`",
        f"- Benchmark id: `{manifest.get('benchmark_id') or ''}`",
        f"- Manifest SHA-256: `{manifest_sha256}`",
        f"- Case count: **{len(cases)}**",
        "",
        "## Instructions",
        "",
        "Analyze every incident from the supplied observations only. Separate the earliest causal break from",
        "downstream symptoms, compare competing dimensions, respect explicit safety boundaries, and avoid claims",
        "not supported by public evidence. Return exactly one JSON object with the response schema below. Do not",
        "use Markdown fences, omit cases, add prose outside JSON, or claim that hidden validation was run.",
        "",
        "Allowed causal dimensions:",
        "",
        f"`{', '.join(diagnostic_reasoning.DIMENSIONS)}`",
        "",
        "Response schema:",
        "",
        "```json",
        json.dumps(response_shape, indent=2, sort_keys=True),
        "```",
        "",
        "## Cases",
    ]
    for case in cases:
        lines.extend(
            [
                "",
                f"### {case['case_id']}",
                "",
                f"- Public case SHA-256: `{case['case_sha256']}`",
                f"- Public case path: `{case['manifest_case_path']}`",
                "",
                "```json",
                json.dumps(
                    {key: value for key, value in case.items() if key in PUBLIC_CASE_FIELDS},
                    indent=2,
                    sort_keys=True,
                ),
                "```",
            ]
        )
    return "\n".join(lines) + "\n"


def _normalized_text(value: object) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _event_text(event: Mapping[str, Any], role: str) -> str:
    message = event.get("message")
    if isinstance(message, Mapping):
        event_role = str(message.get("role") or "").strip().lower()
        content = message.get("content")
    else:
        event_role = str(event.get("role") or "").strip().lower()
        content = event.get("content")
    if event_role != role:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, Mapping)
            and str(block.get("type") or "").strip().lower() == "text"
            and str(block.get("text") or "").strip()
        )
    return ""


def verify_transcript_binding(
    transcript_path: Path,
    *,
    prompt_text: str,
    response_text: str,
) -> dict[str, Any]:
    identity_tools._transcript_event_count(
        transcript_path,
        label="fable5_headtohead",
        required=True,
    )
    response_sha256 = _sha256_text(_normalized_text(response_text))
    identity = identity_tools._transcript_model_identity(
        transcript_path,
        source_kind="claude",
        expected_model_name=TARGET_MODEL,
        expected_response_text=response_text,
        expected_response_sha256=response_sha256,
        require_response_binding=True,
    )
    if identity.get("model_identity_verified") is not True:
        raise HeadToHeadError(
            "Transcript does not bind the exact response to provider-native claude-fable-5 identity"
        )
    expected_prompt = _normalized_text(prompt_text)
    prompt_sha256 = _sha256_text(expected_prompt)
    prompt_bound = False
    for line in transcript_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, Mapping):
            continue
        if _normalized_text(_event_text(event, "user")) == expected_prompt:
            prompt_bound = True
        identity_source = str(event.get("identity_source") or "").strip().lower()
        if (
            identity_source in identity_tools.PROVIDER_IDENTITY_SOURCES
            and str(event.get("prompt_sha256") or "").strip().lower() == prompt_sha256
        ):
            prompt_bound = True
    if not prompt_bound:
        raise HeadToHeadError("Transcript does not bind the exact frozen prompt pack")
    return {
        **identity,
        "prompt_bound": True,
        "prompt_sha256": prompt_sha256,
        "response_sha256": response_sha256,
        "transcript_sha256": identity_tools.sha256_file(transcript_path),
    }


def parse_response(response_text: str, expected_case_ids: Sequence[str]) -> list[dict[str, Any]]:
    if "```" in response_text:
        raise HeadToHeadError("Fable response must be raw JSON without Markdown fences")
    try:
        payload = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise HeadToHeadError(f"Fable response is not valid JSON: {exc}") from exc
    if not isinstance(payload, Mapping) or payload.get("schema") != RESPONSE_SCHEMA:
        raise HeadToHeadError(f"Fable response schema must be {RESPONSE_SCHEMA}")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list):
        raise HeadToHeadError("Fable response cases must be a list")
    expected = list(expected_case_ids)
    by_id: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(raw_cases):
        if not isinstance(item, Mapping):
            raise HeadToHeadError(f"Response case {index} must be an object")
        case_id = str(item.get("case_id") or "")
        if not case_id or case_id in by_id:
            raise HeadToHeadError(f"Missing or duplicate response case id: {case_id!r}")
        dimension = str(item.get("dimension") or "")
        decision = str(item.get("decision") or "")
        status = str(item.get("status") or "")
        if dimension not in diagnostic_reasoning.DIMENSIONS:
            raise HeadToHeadError(f"{case_id}.dimension is not canonical: {dimension!r}")
        if decision not in ALLOWED_DECISIONS:
            raise HeadToHeadError(f"{case_id}.decision is not canonical: {decision!r}")
        if status not in ALLOWED_STATUSES:
            raise HeadToHeadError(f"{case_id}.status is not canonical: {status!r}")
        if not isinstance(item.get("baseline_drift"), bool):
            raise HeadToHeadError(f"{case_id}.baseline_drift must be boolean")
        for field in ("evidence_ids", "causal_chain", "retractions"):
            values = item.get(field)
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise HeadToHeadError(f"{case_id}.{field} must be a string list")
        if not str(item.get("reason") or "").strip() or len(item.get("causal_chain") or []) < 2:
            raise HeadToHeadError(f"{case_id} requires a reason and multi-step causal_chain")
        hypotheses = item.get("hypotheses")
        if not isinstance(hypotheses, list) or not hypotheses:
            raise HeadToHeadError(f"{case_id}.hypotheses must be a non-empty list")
        for hypothesis in hypotheses:
            if not isinstance(hypothesis, Mapping):
                raise HeadToHeadError(f"{case_id}.hypotheses must contain objects")
            if str(hypothesis.get("dimension") or "") not in diagnostic_reasoning.DIMENSIONS:
                raise HeadToHeadError(f"{case_id} has a non-canonical hypothesis dimension")
            if not str(hypothesis.get("claim") or "").strip():
                raise HeadToHeadError(f"{case_id} has a hypothesis without a claim")
            hypothesis_evidence = hypothesis.get("evidence_ids")
            if not isinstance(hypothesis_evidence, list) or any(
                not isinstance(value, str) for value in hypothesis_evidence
            ):
                raise HeadToHeadError(
                    f"{case_id} hypothesis evidence_ids must be a string list"
                )
        experiments = item.get("experiments")
        if not isinstance(experiments, list):
            raise HeadToHeadError(f"{case_id}.experiments must be a list")
        for experiment in experiments:
            if not isinstance(experiment, Mapping):
                raise HeadToHeadError(f"{case_id}.experiments must contain objects")
            if str(experiment.get("safety") or "") not in diagnostic_reasoning.SAFETY_LEVELS:
                raise HeadToHeadError(f"{case_id} has a non-canonical experiment safety level")
            if str(experiment.get("dimension") or "") not in diagnostic_reasoning.DIMENSIONS:
                raise HeadToHeadError(f"{case_id} has a non-canonical experiment dimension")
            if not isinstance(experiment.get("auto_execute"), bool):
                raise HeadToHeadError(f"{case_id} experiment auto_execute must be boolean")
        by_id[case_id] = dict(item)
    if set(by_id) != set(expected):
        raise HeadToHeadError(
            f"Response case set mismatch: expected {sorted(expected)}, got {sorted(by_id)}"
        )
    return [by_id[case_id] for case_id in expected]


def _response_debate(
    item: Mapping[str, Any],
    public_case: Mapping[str, Any],
) -> dict[str, Any]:
    evidence_ids = [str(value) for value in item.get("evidence_ids") or []]
    hypotheses = [dict(value) for value in item.get("hypotheses") or []]
    experiments = [dict(value) for value in item.get("experiments") or []]
    public_evidence_ids = {
        str(observation.get("evidence_id") or "")
        for observation in public_case.get("observations") or []
        if isinstance(observation, Mapping)
        and str(observation.get("evidence_id") or "")
    }
    cited_ids = {
        *evidence_ids,
        *(
            str(value)
            for hypothesis in hypotheses
            for value in hypothesis.get("evidence_ids") or []
        ),
    }
    structurally_grounded = bool(str(item.get("reason") or "").strip()) and bool(
        item.get("causal_chain")
    ) and not (cited_ids - public_evidence_ids)
    conclusion = {
        "dimension": str(item.get("dimension") or "unknown"),
        "status": str(item.get("status") or "inconclusive"),
        "reason": str(item.get("reason") or ""),
    }
    return {
        "premium_calls": 1,
        "packet": {
            "conclusion": {**conclusion, "evidence_ids": evidence_ids},
            "hypotheses": hypotheses,
            "experiments": experiments,
        },
        "report": {
            "valid": structurally_grounded,
            "conclusion": conclusion,
            "decision": str(item.get("decision") or "investigate"),
            "baseline_drift": (
                ["provider_response_detected_baseline_drift"]
                if bool(item.get("baseline_drift"))
                else []
            ),
            "retractions": [str(value) for value in item.get("retractions") or []],
            "next_experiments": experiments,
            "premium_calls": 0,
        },
    }


def _reasoning_score(checks: Mapping[str, Any]) -> float:
    earned = sum(
        weight
        for name, weight in WEIGHTS.items()
        if name != "premium_independence" and bool(checks.get(name))
    )
    available = sum(
        weight for name, weight in WEIGHTS.items() if name != "premium_independence"
    )
    return round(100.0 * earned / available, 2)


def _chili_comparison(
    chili_results_path: Path | None,
    case_results: Sequence[Mapping[str, Any]],
    primary_dimensions: Mapping[str, str],
) -> dict[str, Any] | None:
    if chili_results_path is None:
        return None
    chili = _read_json(chili_results_path)
    chili_by_id = {
        str(item.get("case_id") or ""): item
        for item in chili.get("cases") or []
        if isinstance(item, Mapping)
    }
    expected = {str(item.get("case_id") or "") for item in case_results}
    if set(chili_by_id) != expected:
        raise HeadToHeadError("CHILI comparison result does not contain the exact same case set")
    rows: list[dict[str, Any]] = []
    for fable in case_results:
        case_id = str(fable.get("case_id") or "")
        chili_case = chili_by_id[case_id]
        chili_detail = chili_case.get("score_detail")
        if not isinstance(chili_detail, Mapping):
            raise HeadToHeadError(f"CHILI result for {case_id} has no score_detail")
        chili_checks = chili_detail.get("checks")
        chili_actual = chili_detail.get("actual")
        if not isinstance(chili_checks, Mapping) or not isinstance(chili_actual, Mapping):
            raise HeadToHeadError(f"CHILI result for {case_id} is incomplete")
        chili_reasoning = _reasoning_score(chili_checks)
        fable_reasoning = float(fable.get("reasoning_score") or 0.0)
        rows.append(
            {
                "case_id": case_id,
                "chili_reasoning_score": chili_reasoning,
                "fable5_reasoning_score": fable_reasoning,
                "objective_winner": (
                    "chili"
                    if chili_reasoning > fable_reasoning
                    else "fable5"
                    if fable_reasoning > chili_reasoning
                    else "tie"
                ),
                "chili_dimension": str(chili_actual.get("dimension") or "unknown"),
                "fable5_dimension": str(
                    (fable.get("score_detail") or {}).get("actual", {}).get("dimension")
                    or "unknown"
                ),
                "primary_dimension": primary_dimensions.get(case_id, ""),
            }
        )
    counts = {
        winner: sum(1 for row in rows if row["objective_winner"] == winner)
        for winner in ("chili", "fable5", "tie")
    }
    return {
        "chili_results_path": str(chili_results_path.resolve()),
        "chili_results_sha256": identity_tools.sha256_file(chili_results_path),
        "benchmark_id": str(chili.get("benchmark_id") or ""),
        "objective_wins": counts,
        "average_chili_reasoning_score": round(
            sum(float(row["chili_reasoning_score"]) for row in rows) / len(rows), 2
        ),
        "rows": rows,
    }


def evaluate(
    *,
    fixture_root: Path,
    prompt_pack_path: Path,
    response_path: Path,
    transcript_path: Path,
    selected_case_ids: Sequence[str] = (),
    chili_results_path: Path | None = None,
) -> dict[str, Any]:
    manifest, public_cases = load_public_cases(fixture_root, selected_case_ids)
    expected_pack = render_prompt_pack(fixture_root, selected_case_ids)
    prompt_text = prompt_pack_path.read_text(encoding="utf-8")
    if _normalized_text(prompt_text) != _normalized_text(expected_pack):
        raise HeadToHeadError("Saved prompt pack does not match the current frozen public inputs")
    response_text = response_path.read_text(encoding="utf-8")
    identity = verify_transcript_binding(
        transcript_path,
        prompt_text=prompt_text,
        response_text=response_text,
    )
    case_ids = [str(case.get("case_id") or "") for case in public_cases]
    responses = parse_response(response_text, case_ids)
    entries = manifest.get("cases") or []
    entry_by_case_path = {
        str(entry.get("case") or ""): entry
        for entry in entries
        if isinstance(entry, Mapping)
    }
    results: list[dict[str, Any]] = []
    primary_dimensions: dict[str, str] = {}
    for public_case, response in zip(public_cases, responses):
        case_id = str(public_case.get("case_id") or "")
        entry = entry_by_case_path.get(str(public_case.get("manifest_case_path") or ""))
        if not isinstance(entry, Mapping):
            raise HeadToHeadError(f"Manifest entry disappeared for {case_id}")
        oracle_path = _safe_fixture_file(
            fixture_root,
            entry.get("oracle"),
            label=f"oracle for {case_id}",
        )
        oracle = _read_json(oracle_path)
        debate = _response_debate(response, public_case)
        detail = score_debate(public_case, oracle, debate)
        primary = str(oracle.get("primary_causal_dimension") or "")
        primary_dimensions[case_id] = primary
        results.append(
            {
                "case_id": case_id,
                "split": str(entry.get("split") or "unknown"),
                "response": response,
                "score_detail": detail,
                "reasoning_score": _reasoning_score(detail["checks"]),
                "strict_primary_match": bool(primary)
                and detail["actual"]["dimension"] == primary,
            }
        )
    comparison = _chili_comparison(chili_results_path, results, primary_dimensions)
    average_total = sum(float(item["score_detail"]["score"]) for item in results) / len(results)
    average_reasoning = sum(float(item["reasoning_score"]) for item in results) / len(results)
    strict_primary = sum(1 for item in results if item["strict_primary_match"])
    return {
        "schema": RESULT_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "benchmark_id": str(manifest.get("benchmark_id") or ""),
        "fixture_root": str(fixture_root.resolve()),
        "manifest_sha256": identity_tools.sha256_file(fixture_root / "manifest.json"),
        "prompt_pack_sha256": identity_tools.sha256_file(prompt_pack_path),
        "response_sha256": identity_tools.sha256_file(response_path),
        "target_model": TARGET_MODEL,
        "provider_identity": identity,
        "authenticated_same_task_fable5_run": True,
        "fable5_total_score_including_premium_cost": round(average_total, 2),
        "fable5_reasoning_score_excluding_cost": round(average_reasoning, 2),
        "fable5_strict_primary_accuracy": round(100.0 * strict_primary / len(results), 2),
        "premium_calls": 1,
        "blind_human_adjudication_complete": False,
        "fable5_parity_claim": False,
        "comparison": comparison,
        "cases": results,
    }


def render_report(results: Mapping[str, Any]) -> str:
    rows = [
        "# Authenticated Fable 5 Diagnostic Head-to-Head",
        "",
        f"- Run: {results['created_at']}",
        f"- Target model: `{results['target_model']}`",
        f"- Benchmark id: `{results['benchmark_id']}`",
        f"- Provider identity verified: **{bool((results.get('provider_identity') or {}).get('model_identity_verified'))}**",
        f"- Prompt and response bound: **{bool((results.get('provider_identity') or {}).get('prompt_bound'))}**",
        f"- Fable 5 reasoning score excluding cost: **{results['fable5_reasoning_score_excluding_cost']:.2f}/100**",
        f"- Fable 5 total score including premium-cost check: **{results['fable5_total_score_including_premium_cost']:.2f}/100**",
        f"- Fable 5 strict-primary accuracy: **{results['fable5_strict_primary_accuracy']:.2f}%**",
        "- Blind human adjudication: **not complete**",
        "- Parity or superiority claim: **No**",
        "",
        "| Case | Reasoning | Total | Dimension | Strict primary |",
        "|---|---:|---:|---|---:|",
    ]
    for item in results.get("cases") or []:
        actual = (item.get("score_detail") or {}).get("actual") or {}
        rows.append(
            f"| {item['case_id']} | {item['reasoning_score']:.2f} | "
            f"{item['score_detail']['score']} | {actual.get('dimension') or 'unknown'} | "
            f"{'yes' if item.get('strict_primary_match') else 'no'} |"
        )
    comparison = results.get("comparison")
    if isinstance(comparison, Mapping):
        wins = comparison.get("objective_wins") or {}
        rows.extend(
            [
                "",
                "## Same-Task Objective Comparison",
                "",
                f"- CHILI average reasoning score: **{comparison['average_chili_reasoning_score']:.2f}/100**",
                f"- Wins: CHILI **{wins.get('chili', 0)}**, Fable 5 **{wins.get('fable5', 0)}**, ties **{wins.get('tie', 0)}**",
            ]
        )
    rows.extend(
        [
            "",
            "## Claim Boundary",
            "",
            "This is an authenticated same-task objective score. It remains insufficient for a parity or superiority",
            "claim until blind human adjudication evaluates causal quality, unsupported claims, safety, and action",
            "economy, and until the result reproduces across a larger independently authored suite.",
        ]
    )
    return "\n".join(rows) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-root", type=Path, default=DEFAULT_FIXTURE_ROOT)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument(
        "--prompt-pack",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT / "prompt_pack.md",
    )
    parser.add_argument("--emit-prompt-pack", action="store_true")
    parser.add_argument("--response", type=Path)
    parser.add_argument("--transcript", type=Path)
    parser.add_argument("--chili-results", type=Path)
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT / "HEADTOHEAD_REPORT.md",
    )
    parser.add_argument(
        "--results-json",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT / "headtohead_results.json",
    )
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    fixture_root = args.fixture_root.resolve()
    if args.emit_prompt_pack:
        prompt_pack = render_prompt_pack(fixture_root, args.case)
        if not args.no_write:
            _atomic_write(args.prompt_pack, prompt_pack)
        summary = {
            "schema": PACK_SCHEMA,
            "target_model": TARGET_MODEL,
            "prompt_pack": str(args.prompt_pack.resolve()),
            "prompt_pack_sha256": _sha256_text(prompt_pack),
            "premium_calls": 0,
        }
        print(json.dumps(summary, indent=2, sort_keys=True) if args.json else json.dumps(summary))
        if args.response is None and args.transcript is None:
            return 0
    if args.response is None or args.transcript is None:
        raise SystemExit("Evaluation requires both --response and --transcript")
    results = evaluate(
        fixture_root=fixture_root,
        prompt_pack_path=args.prompt_pack.resolve(),
        response_path=args.response.resolve(),
        transcript_path=args.transcript.resolve(),
        selected_case_ids=args.case,
        chili_results_path=args.chili_results.resolve() if args.chili_results else None,
    )
    if not args.no_write:
        _atomic_write(args.results_json, json.dumps(results, indent=2, sort_keys=True) + "\n")
        _atomic_write(args.report, render_report(results))
    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        print(
            "authenticated_fable5=true "
            f"reasoning={results['fable5_reasoning_score_excluding_cost']:.2f} "
            f"total={results['fable5_total_score_including_premium_cost']:.2f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

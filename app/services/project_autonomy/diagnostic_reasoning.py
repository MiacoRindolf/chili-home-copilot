"""Evidence-gated diagnostic reasoning for local Project Autonomy.

The local model supplies semantic hypotheses.  This module owns the parts that
must not depend on model confidence: evidence provenance, independent support,
counter-evidence, baseline drift, safe experiment boundaries, and conclusion
retraction.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import diagnostic_probes


DIAGNOSTIC_SCHEMA = "chili.diagnostic-case.v1"
PACKET_SCHEMA = "chili.diagnostic-packet.v1"
REPORT_SCHEMA = "chili.diagnostic-report.v1"
DEBATE_SCHEMA = "chili.local-diagnostic-debate.v1"

DIMENSIONS = (
    "code",
    "data",
    "clock",
    "state",
    "config",
    "dependency",
    "runtime",
    "test_harness",
    "unknown",
)
AUTO_SAFE_LEVELS = frozenset({"read_only", "isolated"})
SAFETY_LEVELS = AUTO_SAFE_LEVELS | {"runtime", "live"}

_DIAGNOSTIC_MARKERS = (
    "diagnose",
    "diagnosis",
    "debug",
    "root cause",
    "root-cause",
    "regression",
    "replay",
    "counterfactual",
    "a/b",
    "baseline changed",
    "same code",
    "why did",
    "why does",
    "failed only",
    "works locally",
    "environment drift",
)
_DIMENSION_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("clock", ("clock", "time", "timestamp", "timezone", "wall hour", "sim hour", "utc", "et hour")),
    ("data", ("dataset", "row count", "table", "data source", "ingestion source", "sink", "feed", "cache", "snapshot data", "nbbo")),
    ("state", ("state", "queue", "pending", "session", "board", "checkpoint", "stale row", "lifecycle")),
    ("config", ("config", "setting", "flag", "environment variable", " env ", "feature gate")),
    ("dependency", ("dependency", "provider", "service", "socket", "network", "database server", "broker api")),
    ("runtime", ("runtime", "container", "worker", "process", "restart", "image", "deployment")),
    ("test_harness", ("replay", "harness", "fixture", "mock", "test database", "simulator")),
    ("code", ("code", "commit", "revision", "diff", "function", "caller", "branch", "patch", "source edit", "source inspection")),
)
_DIMENSION_PHRASE_WEIGHTS: tuple[tuple[str, tuple[tuple[str, int], ...]], ...] = (
    (
        "clock",
        (
            ("wall clock", 5),
            ("simulated_at", 5),
            ("simulated time", 5),
            ("replay timestamp", 4),
            ("datetime.now", 4),
        ),
    ),
    (
        "data",
        (
            ("source-sink", 5),
            ("source/sink", 5),
            ("populated source", 4),
            ("quote rows", 4),
            ("repository reads", 3),
        ),
    ),
    (
        "state",
        (
            ("queue depth", 5),
            ("pending depth", 5),
            ("stale low-value", 4),
            ("admission check", 3),
        ),
    ),
    (
        "config",
        (
            ("toggling only", 6),
            ("only material environment difference", 6),
            ("resolved settings", 5),
            ("feature gate", 4),
            ("setting toggle", 4),
            ("gate_enabled", 5),
            ("_true_values", 5),
            ("env.get", 4),
        ),
    ),
    (
        "runtime",
        (
            ("recreating only", 6),
            ("running worker image", 6),
            ("image label", 5),
            ("loaded module hash", 5),
            ("pre-fix behavior", 3),
        ),
    ),
    (
        "test_harness",
        (
            ("serialized replay input", 4),
            ("replay fixture", 3),
            ("focused test", 2),
        ),
    ),
    (
        "code",
        (
            ("source diff", 5),
            ("source inspection", 4),
            ("additional source edit", 2),
        ),
    ),
)
_STOP_WORDS = frozenset(
    {
        "about",
        "after",
        "again",
        "against",
        "because",
        "before",
        "being",
        "between",
        "could",
        "does",
        "from",
        "have",
        "into",
        "only",
        "same",
        "should",
        "their",
        "there",
        "these",
        "this",
        "through",
        "when",
        "where",
        "which",
        "while",
        "with",
        "would",
    }
)
_SOURCE_SUFFIXES = frozenset(
    {
        ".c",
        ".cpp",
        ".cs",
        ".dart",
        ".go",
        ".h",
        ".hpp",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".kt",
        ".kts",
        ".md",
        ".php",
        ".ps1",
        ".py",
        ".rb",
        ".rs",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
        ".yaml",
        ".yml",
    }
)
_SKIP_DIRS = frozenset({".git", ".venv", "node_modules", "vendor", "dist", "build", "logs", "data"})
_DIRECT_SOURCE_SIGNALS = (
    "datetime.now",
    "simulated_at",
    "wall_clock",
    "source_rows",
    "sink_rows",
    "return bool(",
    "os.environ",
    "queue depth",
    "pending depth",
)


def _clip(value: object, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _clean_id(value: object, fallback: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", str(value or "").strip()).strip("-")
    return clean[:100] or fallback


def _clamp_reliability(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.7
    return max(0.0, min(1.0, number))


def infer_dimension(statement: str) -> str:
    lower = f" {statement.lower()} "
    scores: dict[str, int] = {}
    for dimension, terms in _DIMENSION_TERMS:
        scores[dimension] = sum(1 for term in terms if term in lower)
    for dimension, weighted_phrases in _DIMENSION_PHRASE_WEIGHTS:
        scores[dimension] = scores.get(dimension, 0) + sum(
            weight for phrase, weight in weighted_phrases if phrase in lower
        )
    best = max(scores, key=scores.get, default="unknown")
    return best if scores.get(best, 0) else "unknown"


def looks_like_diagnostic_request(prompt: str) -> bool:
    lower = str(prompt or "").lower()
    return any(marker in lower for marker in _DIAGNOSTIC_MARKERS)


def normalize_evidence(raw: Mapping[str, Any], index: int = 0) -> dict[str, Any]:
    statement = _clip(raw.get("statement"), 900)
    explicit_dimension = str(raw.get("dimension") or "").strip().lower()
    dimension = (
        explicit_dimension
        if explicit_dimension in DIMENSIONS and explicit_dimension != "unknown"
        else infer_dimension(statement)
    )
    kind = str(raw.get("kind") or "observation").strip().lower()
    if kind not in {"observation", "experiment", "artifact", "metric"}:
        kind = "observation"
    return {
        "evidence_id": _clean_id(raw.get("evidence_id"), f"evidence-{index + 1}"),
        "statement": statement,
        "dimension": dimension,
        "kind": kind,
        "provenance": _clip(raw.get("provenance") or f"unattributed:{index + 1}", 300),
        "independence_key": _clip(raw.get("independence_key") or raw.get("provenance") or f"source:{index + 1}", 200),
        "reliability": _clamp_reliability(raw.get("reliability")),
        "discriminating": bool(raw.get("discriminating")),
        "comparison_key": _clip(raw.get("comparison_key"), 160),
        "code_revision": _clip(raw.get("code_revision"), 100),
        "input_fingerprint": _clip(raw.get("input_fingerprint"), 160),
        "environment_fingerprint": _clip(raw.get("environment_fingerprint"), 160),
        "outcome_fingerprint": _clip(raw.get("outcome_fingerprint"), 200),
        "experiment_id": _clean_id(raw.get("experiment_id"), "") if raw.get("experiment_id") else "",
    }


def normalize_case(raw: Mapping[str, Any]) -> dict[str, Any]:
    observations = [
        normalize_evidence(item, index)
        for index, item in enumerate(raw.get("observations") or [])
        if isinstance(item, Mapping)
    ][:40]
    prior_raw = raw.get("prior_conclusion") if isinstance(raw.get("prior_conclusion"), Mapping) else {}
    prior_status = str(prior_raw.get("status") or "").strip().lower()
    if prior_status not in {"confirmed", "provisional", "inconclusive", "rejected"}:
        prior_status = ""
    prior_dimension = str(prior_raw.get("dimension") or "unknown").strip().lower()
    if prior_dimension not in DIMENSIONS:
        prior_dimension = infer_dimension(str(prior_raw.get("claim") or ""))
    return {
        "schema": DIAGNOSTIC_SCHEMA,
        "case_id": _clean_id(raw.get("case_id"), "diagnostic-case"),
        "problem_statement": _clip(raw.get("problem_statement"), 1800),
        "observations": observations,
        "prior_conclusion": {
            "hypothesis_id": _clean_id(prior_raw.get("hypothesis_id"), ""),
            "status": prior_status,
            "dimension": prior_dimension,
            "claim": _clip(prior_raw.get("claim"), 700),
            "reason": _clip(prior_raw.get("reason"), 700),
        } if prior_status else {},
        "constraints": {
            "auto_safety_levels": sorted(AUTO_SAFE_LEVELS),
            **(
                dict(raw.get("constraints"))
                if isinstance(raw.get("constraints"), Mapping)
                else {}
            ),
        },
    }


def _prompt_terms(prompt: str) -> list[str]:
    identifiers = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b", prompt or "")
    words = re.findall(r"\b[a-zA-Z][a-zA-Z0-9_-]{4,}\b", prompt or "")
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in [*identifiers, *words]:
        clean = raw.lower()
        if clean in _STOP_WORDS or clean in seen:
            continue
        seen.add(clean)
        ordered.append(clean)
    return ordered[:20]


def collect_repo_evidence(
    repo_path: Path,
    prompt: str,
    *,
    candidate_paths: Sequence[str] = (),
    max_files: int = 240,
    max_records: int = 24,
) -> list[dict[str, Any]]:
    """Collect bounded read-only source snippets relevant to a diagnosis."""
    root = repo_path.resolve()
    terms = _prompt_terms(prompt)
    if not terms or not root.is_dir():
        return []

    paths: list[Path] = []
    seen: set[Path] = set()
    for raw in candidate_paths:
        candidate = (root / raw).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if candidate.is_file() and candidate.suffix.lower() in _SOURCE_SUFFIXES and candidate not in seen:
            seen.add(candidate)
            paths.append(candidate)

    for candidate in root.rglob("*"):
        if len(paths) >= max_files:
            break
        if not candidate.is_file() or candidate.suffix.lower() not in _SOURCE_SUFFIXES:
            continue
        try:
            rel_parts = candidate.relative_to(root).parts
        except ValueError:
            continue
        if any(part in _SKIP_DIRS for part in rel_parts):
            continue
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        paths.append(resolved)

    scored: list[tuple[int, str, int, str]] = []
    for path in paths:
        try:
            if path.stat().st_size > 600_000:
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        per_file = 0
        for line_number, line in enumerate(lines, start=1):
            lower = line.lower()
            matched = sum(1 for term in terms if term in lower)
            if not matched:
                continue
            score = matched * 10 + (4 if any(term in Path(rel).name.lower() for term in terms) else 0)
            context_start = max(0, line_number - 3)
            context_end = min(len(lines), line_number + 2)
            snippet = " | ".join(
                f"{offset + 1}:{lines[offset].strip()}"
                for offset in range(context_start, context_end)
                if lines[offset].strip()
            )
            scored.append((score, rel, line_number, _clip(snippet, 700)))
            per_file += 1
            if per_file >= 3:
                break

    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    records: list[dict[str, Any]] = []
    for index, (_score, rel, line_number, snippet) in enumerate(scored[:max_records]):
        provenance = f"{rel}:{line_number}"
        records.append(
            normalize_evidence(
                {
                    "evidence_id": f"source-{index + 1}",
                    "statement": f"{provenance}: {snippet}",
                    "dimension": infer_dimension(f"{rel} {snippet}"),
                    "kind": "artifact",
                    "provenance": provenance,
                    "independence_key": rel,
                    "reliability": 0.75 if rel.startswith("tests/") else 0.9,
                    "discriminating": any(
                        signal in snippet.lower() for signal in _DIRECT_SOURCE_SIGNALS
                    ),
                },
                index,
            )
        )
    return records


def build_case_from_prompt(
    prompt: str,
    *,
    case_id: str = "operator-diagnostic",
    repo_path: Path | None = None,
    candidate_paths: Sequence[str] = (),
) -> dict[str, Any]:
    segments = [
        _clip(item, 700)
        for item in re.split(r"(?:\r?\n)+|(?<=[.!?])\s+", prompt or "")
        if item.strip()
    ][:18]
    observations = [
        {
            "evidence_id": f"operator-{index + 1}",
            "statement": statement,
            "dimension": infer_dimension(statement),
            "kind": "observation",
            "provenance": f"operator_prompt:{index + 1}",
            "independence_key": "operator_prompt",
            "reliability": 0.65,
            "discriminating": False,
        }
        for index, statement in enumerate(segments)
    ]
    if repo_path is not None:
        observations.extend(
            collect_repo_evidence(
                repo_path,
                prompt,
                candidate_paths=candidate_paths,
            )
        )
    return normalize_case(
        {
            "case_id": case_id,
            "problem_statement": prompt,
            "observations": observations,
        }
    )


def parse_json_object(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return dict(parsed) if isinstance(parsed, Mapping) else None


def _normalize_hypothesis(raw: Mapping[str, Any], index: int) -> dict[str, Any]:
    dimension = str(raw.get("dimension") or "unknown").strip().lower()
    if dimension not in DIMENSIONS:
        dimension = infer_dimension(str(raw.get("claim") or ""))
    return {
        "hypothesis_id": _clean_id(raw.get("hypothesis_id"), f"h{index + 1}"),
        "claim": _clip(raw.get("claim"), 700),
        "dimension": dimension,
        "support_evidence_ids": [
            _clean_id(value, "") for value in raw.get("support_evidence_ids") or [] if str(value).strip()
        ][:20],
        "contradict_evidence_ids": [
            _clean_id(value, "") for value in raw.get("contradict_evidence_ids") or [] if str(value).strip()
        ][:20],
        "falsification": _clip(raw.get("falsification"), 700),
    }


def _normalize_experiment(raw: Mapping[str, Any], index: int) -> dict[str, Any]:
    safety = str(raw.get("safety") or "isolated").strip().lower()
    if safety not in SAFETY_LEVELS:
        safety = "isolated"
    status = str(raw.get("status") or "planned").strip().lower()
    if status not in {"planned", "completed", "blocked"}:
        status = "planned"
    auto_execute = bool(raw.get("auto_execute"))
    raw_probe = raw.get("probe") if isinstance(raw.get("probe"), Mapping) else {}
    probe = diagnostic_probes.normalize_probe_spec(raw_probe, index)
    if not auto_execute and probe.get("kind") not in diagnostic_probes.PROBE_KINDS:
        probe = {}
    return {
        "experiment_id": _clean_id(raw.get("experiment_id"), f"experiment-{index + 1}"),
        "hypothesis_ids": [_clean_id(value, "") for value in raw.get("hypothesis_ids") or [] if str(value).strip()][:12],
        "changed_dimensions": [
            value for value in (str(item).strip().lower() for item in raw.get("changed_dimensions") or []) if value in DIMENSIONS
        ],
        "held_constant_dimensions": [
            value for value in (str(item).strip().lower() for item in raw.get("held_constant_dimensions") or []) if value in DIMENSIONS
        ],
        "expected_if_true": _clip(raw.get("expected_if_true"), 500),
        "expected_if_false": _clip(raw.get("expected_if_false"), 500),
        "evidence_required": [_clip(value, 220) for value in raw.get("evidence_required") or [] if str(value).strip()][:10],
        "result_evidence_ids": [_clean_id(value, "") for value in raw.get("result_evidence_ids") or [] if str(value).strip()][:20],
        "safety": safety,
        "status": status,
        "auto_execute": auto_execute,
        "probe": probe if probe.get("kind") else {},
    }


def normalize_packet(raw: Mapping[str, Any]) -> dict[str, Any]:
    hypotheses = [
        _normalize_hypothesis(item, index)
        for index, item in enumerate(raw.get("hypotheses") or [])
        if isinstance(item, Mapping)
    ][:12]
    experiments = [
        _normalize_experiment(item, index)
        for index, item in enumerate(raw.get("experiments") or [])
        if isinstance(item, Mapping)
    ][:16]
    conclusion_raw = raw.get("conclusion") if isinstance(raw.get("conclusion"), Mapping) else {}
    requested_status = str(conclusion_raw.get("status") or "provisional").strip().lower()
    if requested_status not in {"confirmed", "provisional", "inconclusive", "rejected"}:
        requested_status = "provisional"
    return {
        "schema": PACKET_SCHEMA,
        "problem_statement": _clip(raw.get("problem_statement"), 1600),
        "hypotheses": hypotheses,
        "experiments": experiments,
        "conclusion": {
            "hypothesis_id": _clean_id(conclusion_raw.get("hypothesis_id"), ""),
            "status": requested_status,
            "evidence_ids": [
                _clean_id(value, "") for value in conclusion_raw.get("evidence_ids") or [] if str(value).strip()
            ][:20],
            "reason": _clip(conclusion_raw.get("reason"), 700),
        },
    }


def detect_baseline_drift(observations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for item in observations:
        key = str(item.get("comparison_key") or "")
        revision = str(item.get("code_revision") or "")
        inputs = str(item.get("input_fingerprint") or "")
        outcome = str(item.get("outcome_fingerprint") or "")
        if key and revision and inputs and outcome:
            groups[(key, revision, inputs)].append(item)
    drift: list[dict[str, Any]] = []
    for (key, revision, inputs), items in groups.items():
        outcomes = sorted({str(item.get("outcome_fingerprint")) for item in items})
        if len(outcomes) < 2:
            continue
        drift.append(
            {
                "comparison_key": key,
                "code_revision": revision,
                "input_fingerprint": inputs,
                "outcome_fingerprints": outcomes,
                "environment_fingerprints": sorted(
                    {str(item.get("environment_fingerprint") or "unknown") for item in items}
                ),
                "evidence_ids": [str(item.get("evidence_id")) for item in items],
            }
        )
    return drift


def _independent_weight(records: Iterable[Mapping[str, Any]]) -> float:
    strongest: dict[str, float] = {}
    for item in records:
        key = str(item.get("independence_key") or item.get("provenance") or item.get("evidence_id"))
        strongest[key] = max(strongest.get(key, 0.0), _clamp_reliability(item.get("reliability")))
    return round(sum(strongest.values()), 4)


def _confirmatory_weight(records: Iterable[Mapping[str, Any]]) -> float:
    return _independent_weight(
        item
        for item in records
        if str(item.get("independence_key") or "") != "operator_prompt"
    )


def _experiment_result_ids(packet: Mapping[str, Any]) -> set[str]:
    return {
        str(evidence_id)
        for experiment in packet.get("experiments") or []
        if isinstance(experiment, Mapping) and experiment.get("status") == "completed"
        for evidence_id in experiment.get("result_evidence_ids") or []
    }


def _validate_packet(case: Mapping[str, Any], packet: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    evidence_by_id = {
        str(item.get("evidence_id")): item
        for item in case.get("observations") or []
        if isinstance(item, Mapping)
    }
    evidence_ids = set(evidence_by_id)
    hypothesis_ids: set[str] = set()
    if not packet.get("hypotheses"):
        errors.append("At least one falsifiable hypothesis is required.")
    for item in packet.get("hypotheses") or []:
        hypothesis_id = str(item.get("hypothesis_id") or "")
        if not hypothesis_id or hypothesis_id in hypothesis_ids:
            errors.append("Hypothesis ids must be non-empty and unique.")
        hypothesis_ids.add(hypothesis_id)
        if not str(item.get("claim") or "").strip():
            errors.append(f"{hypothesis_id or 'hypothesis'} has no claim.")
        if not str(item.get("falsification") or "").strip():
            errors.append(f"{hypothesis_id or 'hypothesis'} has no falsification test.")
        linked = [*(item.get("support_evidence_ids") or []), *(item.get("contradict_evidence_ids") or [])]
        unknown = sorted({str(value) for value in linked if str(value) not in evidence_ids})
        if unknown:
            errors.append(f"{hypothesis_id} references unknown evidence: {', '.join(unknown)}")
        hypothesis_dimension = str(item.get("dimension") or "unknown")
        mismatched_support = sorted(
            str(value)
            for value in item.get("support_evidence_ids") or []
            if str(value) in evidence_by_id
            and str(evidence_by_id[str(value)].get("dimension") or "unknown")
            not in {hypothesis_dimension, "unknown"}
        )
        if mismatched_support:
            errors.append(
                f"{hypothesis_id} links support from a different evidence family: "
                + ", ".join(mismatched_support)
            )
    experiment_ids: set[str] = set()
    for item in packet.get("experiments") or []:
        experiment_id = str(item.get("experiment_id") or "")
        if not experiment_id or experiment_id in experiment_ids:
            errors.append("Experiment ids must be non-empty and unique.")
        experiment_ids.add(experiment_id)
        if item.get("auto_execute") and item.get("safety") not in AUTO_SAFE_LEVELS:
            errors.append(f"{experiment_id} requests unsafe automatic execution.")
        probe = item.get("probe") if isinstance(item.get("probe"), Mapping) else {}
        if item.get("auto_execute") and not probe:
            errors.append(f"{experiment_id} requests automatic execution without a typed probe.")
        if probe:
            errors.extend(
                f"{experiment_id}: {error}"
                for error in diagnostic_probes.validate_probe_spec(
                    probe,
                    str(item.get("safety") or ""),
                )
            )
        unknown_hypotheses = sorted(
            {str(value) for value in item.get("hypothesis_ids") or [] if str(value) not in hypothesis_ids}
        )
        if unknown_hypotheses:
            errors.append(f"{experiment_id} references unknown hypotheses: {', '.join(unknown_hypotheses)}")
        unknown_evidence = sorted(
            {str(value) for value in item.get("result_evidence_ids") or [] if str(value) not in evidence_ids}
        )
        if unknown_evidence:
            errors.append(f"{experiment_id} references unknown result evidence: {', '.join(unknown_evidence)}")
    conclusion_id = str((packet.get("conclusion") or {}).get("hypothesis_id") or "")
    if not conclusion_id:
        errors.append("A conclusion hypothesis is required.")
    if conclusion_id and conclusion_id not in hypothesis_ids:
        errors.append("Conclusion references an unknown hypothesis.")
    conclusion_evidence = {
        str(value)
        for value in (packet.get("conclusion") or {}).get("evidence_ids") or []
        if str(value)
    }
    unknown_conclusion_evidence = sorted(conclusion_evidence - evidence_ids)
    if unknown_conclusion_evidence:
        errors.append(
            "Conclusion references unknown evidence: "
            + ", ".join(unknown_conclusion_evidence)
        )
    return sorted(dict.fromkeys(errors))


def recommend_counterfactuals(
    case: Mapping[str, Any],
    packet: Mapping[str, Any],
    hypothesis_results: Sequence[Mapping[str, Any]],
    baseline_drift: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    existing_dimensions = {
        dimension
        for item in packet.get("experiments") or []
        if isinstance(item, Mapping)
        for dimension in item.get("changed_dimensions") or []
    }
    recommendations: list[dict[str, Any]] = []
    if baseline_drift:
        priority = ("data", "clock", "state", "config", "dependency", "runtime", "test_harness")
        for dimension in priority:
            if dimension in existing_dimensions:
                continue
            recommendations.append(
                {
                    "experiment_id": f"isolate-{dimension}",
                    "dimension": dimension,
                    "safety": "read_only" if dimension in {"data", "clock", "config", "test_harness"} else "isolated",
                    "action": f"Hold code and inputs constant; measure whether changing only {dimension} restores the baseline outcome.",
                    "required_evidence": ["code revision", "input fingerprint", "environment fingerprint", "outcome fingerprint"],
                }
            )
            if len(recommendations) >= 5:
                break
    for result in hypothesis_results:
        if result.get("status") in {"supported", "refuted"}:
            continue
        dimension = str(result.get("dimension") or "unknown")
        if any(item.get("dimension") == dimension for item in recommendations):
            continue
        recommendations.append(
            {
                "experiment_id": f"falsify-{result.get('hypothesis_id')}",
                "dimension": dimension,
                "safety": "isolated",
                "action": str(result.get("falsification") or f"Vary only {dimension} and record a discriminating outcome."),
                "required_evidence": ["held constants", "changed dimension", "expected outcomes", "actual outcome"],
            }
        )
        if len(recommendations) >= 6:
            break
    return recommendations


def evaluate_packet(
    raw_case: Mapping[str, Any],
    raw_packet: Mapping[str, Any],
    *,
    previous_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    case = normalize_case(raw_case)
    packet = normalize_packet(raw_packet)
    errors = _validate_packet(case, packet)
    evidence = {str(item.get("evidence_id")): item for item in case["observations"]}
    completed_result_ids = _experiment_result_ids(packet)
    drift = detect_baseline_drift(case["observations"])

    hypothesis_results: list[dict[str, Any]] = []
    for item in packet["hypotheses"]:
        support_ids = list(dict.fromkeys(item.get("support_evidence_ids") or []))
        contradict_ids = list(dict.fromkeys(item.get("contradict_evidence_ids") or []))
        if not support_ids:
            support_ids = [
                evidence_id
                for evidence_id, record in evidence.items()
                if record.get("dimension") == item.get("dimension")
            ]
        support_records = [evidence[value] for value in support_ids if value in evidence]
        contradict_records = [evidence[value] for value in contradict_ids if value in evidence]
        support_weight = _independent_weight(support_records)
        confirmatory_weight = _confirmatory_weight(support_records)
        contradict_weight = _independent_weight(contradict_records)
        discriminating = any(
            bool(record.get("discriminating")) or str(record.get("evidence_id")) in completed_result_ids
            for record in support_records
        )
        direct_artifact = any(
            record.get("kind") == "artifact" and float(record.get("reliability") or 0) >= 0.9
            for record in support_records
        )
        if contradict_weight >= 0.7:
            status = "refuted"
        elif (
            confirmatory_weight >= 1.25 and (discriminating or direct_artifact)
        ) or (
            confirmatory_weight >= 0.85 and discriminating and direct_artifact
        ):
            status = "supported"
        elif support_weight > 0:
            status = "provisional"
        else:
            status = "untested"
        blockers: list[str] = []
        if drift and item.get("dimension") == "code" and status in {"supported", "provisional"}:
            status = "blocked"
            blockers.append("Same code and input produced different outcomes; code causality is not isolated.")
        denominator = support_weight + contradict_weight + 1.0
        confidence = max(0.0, min(0.99, support_weight / denominator))
        if status in {"refuted", "blocked"}:
            confidence = min(confidence, 0.49)
        hypothesis_results.append(
            {
                **item,
                "status": status,
                "confidence": round(confidence, 4),
                "support_weight": support_weight,
                "confirmatory_weight": confirmatory_weight,
                "contradict_weight": contradict_weight,
                "discriminating_evidence": discriminating,
                "blockers": blockers,
            }
        )

    results_by_id = {str(item.get("hypothesis_id")): item for item in hypothesis_results}
    requested = packet["conclusion"]
    conclusion_id = str(requested.get("hypothesis_id") or "")
    chosen = results_by_id.get(conclusion_id)
    requested_choice_id = conclusion_id
    if chosen is None and hypothesis_results:
        chosen = max(
            hypothesis_results,
            key=lambda item: (
                item.get("status") == "supported",
                float(item.get("support_weight") or 0) - float(item.get("contradict_weight") or 0),
            ),
        )
        conclusion_id = str(chosen.get("hypothesis_id") or "")
    if chosen is not None and chosen.get("status") != "supported":
        supported = [item for item in hypothesis_results if item.get("status") == "supported"]
        if supported:
            chosen = max(
                supported,
                key=lambda item: (
                    float(item.get("confirmatory_weight") or 0),
                    float(item.get("support_weight") or 0),
                ),
            )
            conclusion_id = str(chosen.get("hypothesis_id") or "")

    requested_status = str(requested.get("status") or "provisional")
    if chosen is None:
        effective_status = "inconclusive"
    elif requested_status == "rejected" or chosen.get("status") == "refuted":
        effective_status = "rejected"
    elif (
        requested_status not in {"inconclusive", "rejected"}
        and chosen.get("status") == "supported"
        and not errors
    ):
        effective_status = "confirmed"
    elif chosen.get("status") in {"blocked", "untested"}:
        effective_status = "inconclusive"
    else:
        effective_status = "provisional"

    retractions: list[dict[str, Any]] = []
    if previous_report:
        previous = previous_report.get("conclusion") if isinstance(previous_report.get("conclusion"), Mapping) else {}
        previous_id = str(previous.get("hypothesis_id") or "")
        previous_status = str(previous.get("status") or "")
        if previous_status == "confirmed" and (previous_id != conclusion_id or effective_status != "confirmed"):
            retractions.append(
                {
                    "hypothesis_id": previous_id,
                    "previous_status": previous_status,
                    "new_status": effective_status if previous_id == conclusion_id else "superseded",
                    "reason": "New counter-evidence or a stronger competing explanation invalidated the earlier conclusion.",
                }
            )

    if effective_status == "confirmed":
        decision = "patch_root_cause"
    elif drift or any(item.get("status") in {"provisional", "blocked"} for item in hypothesis_results):
        decision = "instrument_first"
    else:
        decision = "investigate"

    recommendations = recommend_counterfactuals(case, packet, hypothesis_results, drift)
    selected_evidence_ids = list(requested.get("evidence_ids") or [])
    selected_reason = str(requested.get("reason") or "")
    if conclusion_id and conclusion_id != requested_choice_id and chosen is not None:
        selected_evidence_ids = list(chosen.get("support_evidence_ids") or [])
        selected_reason = (
            "Deterministic evidence gate selected a stronger supported competing hypothesis."
        )
    return {
        "schema": REPORT_SCHEMA,
        "case_id": case["case_id"],
        "valid": not errors,
        "errors": errors,
        "baseline_drift": drift,
        "hypothesis_results": hypothesis_results,
        "conclusion": {
            "hypothesis_id": conclusion_id,
            "status": effective_status,
            "dimension": str(chosen.get("dimension") or "unknown") if chosen else "unknown",
            "claim": str(chosen.get("claim") or "") if chosen else "",
            "confidence": float(chosen.get("confidence") or 0) if chosen else 0.0,
            "evidence_ids": selected_evidence_ids,
            "reason": selected_reason,
        },
        "decision": decision,
        "retractions": retractions,
        "next_experiments": recommendations,
        "premium_calls": 0,
    }


def heuristic_packet(raw_case: Mapping[str, Any]) -> dict[str, Any]:
    case = normalize_case(raw_case)
    by_dimension: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in case["observations"]:
        by_dimension[str(item.get("dimension") or "unknown")].append(item)
    ranked = sorted(
        by_dimension.items(),
        key=lambda pair: (
            -sum(float(item.get("reliability") or 0) for item in pair[1]),
            pair[0] == "unknown",
            pair[0],
        ),
    )
    hypotheses: list[dict[str, Any]] = []
    experiments: list[dict[str, Any]] = []
    for index, (dimension, records) in enumerate(ranked[:4]):
        hypothesis_id = f"h-{dimension}"
        hypotheses.append(
            {
                "hypothesis_id": hypothesis_id,
                "claim": f"The observed failure is primarily caused by {dimension} drift.",
                "dimension": dimension,
                "support_evidence_ids": [str(item.get("evidence_id")) for item in records],
                "contradict_evidence_ids": [],
                "falsification": f"Hold every other dimension constant and show that changing {dimension} does not change the outcome.",
            }
        )
        experiments.append(
            {
                "experiment_id": f"isolate-{dimension}",
                "hypothesis_ids": [hypothesis_id],
                "changed_dimensions": [dimension],
                "held_constant_dimensions": [item for item in DIMENSIONS if item not in {dimension, "unknown"}],
                "expected_if_true": f"Changing {dimension} changes or restores the outcome.",
                "expected_if_false": "The outcome remains unchanged.",
                "evidence_required": ["before fingerprint", "after fingerprint", "actual outcome"],
                "result_evidence_ids": [],
                "safety": "isolated",
                "status": "planned",
            }
        )
    top = hypotheses[0] if hypotheses else {"hypothesis_id": "", "support_evidence_ids": []}
    return normalize_packet(
        {
            "problem_statement": case["problem_statement"],
            "hypotheses": hypotheses,
            "experiments": experiments,
            "conclusion": {
                "hypothesis_id": top.get("hypothesis_id"),
                "status": "provisional",
                "evidence_ids": top.get("support_evidence_ids") or [],
                "reason": "Heuristic fallback only; a local investigator should challenge this ranking.",
            },
        }
    )


def _case_prompt(case: Mapping[str, Any]) -> str:
    safe_case = {
        "case_id": case.get("case_id"),
        "problem_statement": case.get("problem_statement"),
        "observations": case.get("observations"),
        "prior_conclusion": case.get("prior_conclusion"),
        "constraints": case.get("constraints"),
    }
    return json.dumps(safe_case, indent=2, sort_keys=True)


def _packet_shape() -> str:
    return (
        '{"schema":"chili.diagnostic-packet.v1","problem_statement":"...",'
        '"hypotheses":[{"hypothesis_id":"h1","claim":"...","dimension":"code|data|clock|state|config|dependency|runtime|test_harness|unknown",'
        '"support_evidence_ids":["e1"],"contradict_evidence_ids":[],"falsification":"..."}],'
        '"experiments":[{"experiment_id":"x1","hypothesis_ids":["h1"],"changed_dimensions":["code"],'
        '"held_constant_dimensions":["data"],"expected_if_true":"...","expected_if_false":"...",'
        '"evidence_required":["..."],"result_evidence_ids":[],"safety":"read_only|isolated|runtime|live",'
        '"status":"planned|completed|blocked","auto_execute":false,"probe":{}}],'
        '"conclusion":{"hypothesis_id":"h1","status":"confirmed|provisional|inconclusive|rejected",'
        '"evidence_ids":["e1"],"reason":"..."}}'
    )


def investigator_prompt(raw_case: Mapping[str, Any]) -> str:
    case = normalize_case(raw_case)
    return (
        "You are the investigator in a local-only diagnostic team. Return JSON only. "
        "Generate competing hypotheses across different dimensions. Link every claim to supplied evidence ids. "
        "A hypothesis without a falsification experiment is invalid. Same code and input with different outcomes "
        "means baseline drift, not proof of a code regression. Never request automatic runtime or live mutation. "
        "When evidence is insufficient, you may set auto_execute=true only for a typed probe from the supplied "
        "catalog. Raw shell commands do not exist. search is fixed-string; targeted_test must name one selector "
        "under tests/. Use read_only for repo_state/search/file_excerpt/git_history/git_diff and isolated for "
        "compile/targeted_test.\n\n"
        f"Required shape:\n{_packet_shape()}\n\nCase:\n{_case_prompt(case)}"
    )


def skeptic_prompt(raw_case: Mapping[str, Any], packet: Mapping[str, Any], report: Mapping[str, Any]) -> str:
    case = normalize_case(raw_case)
    return (
        "You are the skeptic in a local-only diagnostic team. Return one full revised diagnostic packet as JSON only. "
        "Try to falsify the leading conclusion. Look for code/data/clock/state/config/dependency/runtime/test-harness confounding, "
        "correlated evidence, and claims that survived no discriminating experiment. Add contradiction evidence links when justified. "
        "Retract a conclusion rather than defending it when the evidence changed. Never request automatic runtime or live mutation.\n\n"
        f"Required shape:\n{_packet_shape()}\n\nCase:\n{_case_prompt(case)}\n\n"
        f"Investigator packet:\n{json.dumps(packet, indent=2, sort_keys=True)}\n\n"
        f"Deterministic evaluation:\n{json.dumps(report, indent=2, sort_keys=True)}"
    )


def judge_prompt(raw_case: Mapping[str, Any], packet: Mapping[str, Any], report: Mapping[str, Any]) -> str:
    case = normalize_case(raw_case)
    return (
        "You are the judge in a local-only diagnostic team. Return one final full diagnostic packet as JSON only. "
        "Confirm only a hypothesis with independent, discriminating evidence and no unresolved contradiction. "
        "If baseline drift remains unexplained, reject code attribution and choose instrument-first. "
        "Preserve safe falsification experiments and never request automatic runtime or live mutation. "
        "If the evidence gate says instrument_first, choose at most two auto_execute typed probes from this "
        "catalog: repo_state, fixed-string search, bounded file_excerpt, git_history, git_diff, isolated compile, "
        "or one targeted_test selector under tests/. Raw commands are forbidden.\n\n"
        f"Required shape:\n{_packet_shape()}\n\nCase:\n{_case_prompt(case)}\n\n"
        f"Challenged packet:\n{json.dumps(packet, indent=2, sort_keys=True)}\n\n"
        f"Deterministic evaluation:\n{json.dumps(report, indent=2, sort_keys=True)}"
    )


ModelCall = Callable[[str, str], str]


def run_local_diagnostic_debate(
    raw_case: Mapping[str, Any],
    model_call: ModelCall | None,
    *,
    stages_to_run: Sequence[str] = ("investigator", "skeptic", "judge"),
    previous_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    case = normalize_case(raw_case)
    packet = heuristic_packet(case)
    prior_conclusion = case.get("prior_conclusion")
    prior_report = dict(previous_report) if isinstance(previous_report, Mapping) else None
    if prior_report is None:
        prior_report = (
            {"conclusion": dict(prior_conclusion)}
            if isinstance(prior_conclusion, Mapping) and prior_conclusion.get("status")
            else None
        )
    report = evaluate_packet(case, packet, previous_report=prior_report)
    stages: list[dict[str, Any]] = []
    all_retractions = list(report.get("retractions") or [])

    allowed_stages = {"investigator", "skeptic", "judge"}
    requested_stages = tuple(stage for stage in stages_to_run if stage in allowed_stages)
    if not requested_stages:
        requested_stages = ("judge",)

    for stage in requested_stages:
        if stage == "investigator":
            prompt = investigator_prompt(case)
        elif stage == "skeptic":
            prompt = skeptic_prompt(case, packet, report)
        else:
            prompt = judge_prompt(case, packet, report)
        response = model_call(stage, prompt) if model_call is not None else ""
        parsed = parse_json_object(response)
        accepted = parsed is not None
        candidate = normalize_packet(parsed) if parsed is not None else packet
        next_report = evaluate_packet(case, candidate, previous_report=report)
        candidate_errors = list(next_report.get("errors") or [])
        if parsed is None and response:
            candidate_errors.insert(0, "Model response was not a usable diagnostic JSON object.")
        if not next_report["valid"] and accepted:
            accepted = False
            candidate = packet
            next_report = evaluate_packet(case, candidate, previous_report=report)
        stages.append(
            {
                "stage": stage,
                "accepted": accepted,
                "response_chars": len(response),
                "errors": candidate_errors,
                "conclusion": next_report.get("conclusion") or {},
                "retractions": next_report.get("retractions") or [],
            }
        )
        all_retractions.extend(next_report.get("retractions") or [])
        packet = candidate
        report = next_report

    report = {**report, "retractions": all_retractions}
    return {
        "schema": DEBATE_SCHEMA,
        "case_id": case["case_id"],
        "packet": packet,
        "report": report,
        "stages": stages,
        "premium_calls": 0,
    }


def report_context(report: Mapping[str, Any]) -> str:
    conclusion = report.get("conclusion") if isinstance(report.get("conclusion"), Mapping) else {}
    lines = [
        "Diagnostic evidence gate:",
        f"- decision: {report.get('decision') or 'investigate'}",
        f"- conclusion: {conclusion.get('status') or 'inconclusive'} / {conclusion.get('dimension') or 'unknown'}",
        f"- claim: {_clip(conclusion.get('claim'), 500)}",
        f"- confidence: {conclusion.get('confidence') or 0}",
        f"- baseline drift findings: {len(report.get('baseline_drift') or [])}",
        f"- conclusion retractions: {len(report.get('retractions') or [])}",
    ]
    for item in (report.get("next_experiments") or [])[:5]:
        if isinstance(item, Mapping):
            lines.append(f"- next experiment ({item.get('safety')}): {_clip(item.get('action'), 300)}")
    return "\n".join(lines)

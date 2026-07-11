"""Leakage-safe durable memory for validated diagnostic mechanisms."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ...models import ProjectAutonomyLearningSample, ProjectAutonomyRun


MEMORY_SCHEMA = "chili.diagnostic-memory.v1"
RETRIEVAL_SCHEMA = "chili.diagnostic-memory-retrieval.v1"
ALLOWED_CLASSIFICATIONS = frozenset(
    {"operator_validated", "production_validated"}
)
CLASSIFICATION_TRUST = {
    "operator_validated": 1,
    "production_validated": 2,
}
FORBIDDEN_CLASSIFICATIONS = frozenset(
    {
        "blinded_holdout",
        "development_replay",
        "historical_fable_answer",
        "sealed_oracle",
        "synthetic_evaluation",
    }
)
_TOKEN_RE = re.compile(r"\b[a-z][a-z0-9_-]{2,}\b", re.IGNORECASE)
_STOP = frozenset(
    {
        "and",
        "case",
        "diagnostic",
        "from",
        "into",
        "only",
        "root",
        "that",
        "the",
        "this",
        "with",
    }
)
_ALLOWED_DIMENSIONS = frozenset(
    {"code", "data", "clock", "state", "config", "dependency", "runtime", "test_harness", "unknown"}
)
_ALLOWED_LENSES = frozenset(
    {
        "expected_vs_observed",
        "causal_timeline",
        "root_cause_vs_downstream_symptom",
        "safety_boundary",
        "post_change_proof",
        "strategy_contract",
        "counterfactual_integrity",
        "state_reconciliation",
        "producer_consumer_evidence_chain",
        "runtime_source_parity",
        "external_market_state",
    }
)
_GENERIC_LENSES = frozenset(
    {
        "expected_vs_observed",
        "causal_timeline",
        "root_cause_vs_downstream_symptom",
        "safety_boundary",
        "post_change_proof",
    }
)
_CONTRACT_TOPIC_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("single_flight_eviction", ("single-flight", "singleflight", "cached promise", "coalesced")),
    ("cancellation_propagation", ("abortsignal", "abort signal", "cancellation signal", "aborterror")),
    ("injected_clock_consistency", ("injected clock", "expiry", "deadline", "replay time")),
    ("subscription_lifecycle", ("subscription", "await cancellation", "active handle")),
    ("partial_uniqueness", ("partial unique", "partial uniqueness", "active-row predicate")),
    ("preaggregate_one_to_many", ("one-to-many", "sibling one-to-many", "aggregate each independent child")),
)
_EVENT_FAMILY_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("queue_transition", ("queue", "backlog", "starvation")),
    ("consumer_transition", ("consumer", "sink")),
    ("retry_transition", ("retry", "attempt")),
    ("timeout_transition", ("timeout", "timed_out", "deadline")),
    ("deployment_transition", ("deploy", "release", "revision", "image")),
    ("clock_transition", ("clock", "time", "expiry")),
    ("data_transition", ("data", "row", "record", "snapshot")),
    ("dependency_transition", ("dependency", "upstream", "provider")),
    ("state_transition", ("state", "lifecycle", "status")),
    ("test_transition", ("test", "fixture", "harness", "replay")),
)
_ALLOWED_FLOW_CLASSIFICATIONS = frozenset(
    {"consumer_starvation", "broken_flow_edge", "no_explicit_break"}
)
_ALLOWED_CONTRACT_TOPICS = frozenset(
    topic for topic, _markers in _CONTRACT_TOPIC_MARKERS
)
_ALLOWED_EVENT_FAMILIES = frozenset(
    {"unspecified_transition", *(family for family, _markers in _EVENT_FAMILY_MARKERS)}
)


def _load_json(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _tokens(*values: object) -> set[str]:
    return {
        token.lower()
        for value in values
        for token in _TOKEN_RE.findall(str(value or ""))
        if token.lower() not in _STOP
    }


def _safe_label(value: object, *, fallback: str = "") -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "_", str(value or "").strip().lower()).strip("_")
    return normalized[:80] or fallback


def _contract_topics(values: object) -> list[str]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return []
    lowered = "\n".join(str(value or "").lower() for value in values)
    return [
        topic
        for topic, markers in _CONTRACT_TOPIC_MARKERS
        if any(marker in lowered for marker in markers)
    ]


def _safe_lenses(values: object) -> list[str]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return []
    return list(
        dict.fromkeys(
            lens
            for lens in (_safe_label(value) for value in values)
            if lens in _ALLOWED_LENSES
        )
    )[:12]


def _event_family(value: object) -> str:
    label = _safe_label(value)
    return next(
        (
            family
            for family, markers in _EVENT_FAMILY_MARKERS
            if any(marker in label for marker in markers)
        ),
        "unspecified_transition",
    )


def _flow_classification(value: object) -> str:
    label = _safe_label(value)
    return label if label in _ALLOWED_FLOW_CLASSIFICATIONS else "no_explicit_break"


def _controlled_values(values: object, allowed: frozenset[str]) -> list[str]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return []
    return list(
        dict.fromkeys(
            label
            for label in (_safe_label(value) for value in values)
            if label in allowed
        )
    )


def _public_abstraction(payload: Mapping[str, Any], sample_id: int) -> dict[str, Any]:
    raw_dimension = _safe_label(payload.get("dimension"), fallback="unknown")
    dimension = raw_dimension if raw_dimension in _ALLOWED_DIMENSIONS else "unknown"
    raw_event_family = _safe_label(
        payload.get("event_family"),
        fallback="unspecified_transition",
    )
    event_family = (
        raw_event_family
        if raw_event_family in _ALLOWED_EVENT_FAMILIES
        else "unspecified_transition"
    )
    flow = _flow_classification(payload.get("flow_classification"))
    contract_topics = _controlled_values(
        payload.get("contract_topics"),
        _ALLOWED_CONTRACT_TOPICS,
    )[:8]
    lenses = _controlled_values(payload.get("diagnostic_lenses"), _ALLOWED_LENSES)[:12]
    lesson_parts = [f"validated_dimension={dimension}"]
    if event_family != "unspecified_transition":
        lesson_parts.append(f"earliest_event={event_family}")
    if flow != "no_explicit_break":
        lesson_parts.append(f"flow={flow}")
    lesson_parts.append("require independent discriminating evidence and post-change validation")
    mechanism_key = str(payload.get("mechanism_key") or "").lower()
    return {
        "sample_id": sample_id,
        "mechanism_key": mechanism_key if re.fullmatch(r"[0-9a-f]{24}", mechanism_key) else "",
        "dimension": dimension,
        "event_family": event_family,
        "flow_classification": flow,
        "abstract_lesson": "; ".join(lesson_parts),
        "contract_topics": contract_topics,
        "diagnostic_lenses": lenses,
    }


def _has_grounded_confirmed_conclusion(report: Mapping[str, Any]) -> bool:
    conclusion = report.get("conclusion") if isinstance(report.get("conclusion"), Mapping) else {}
    hypothesis_id = str(conclusion.get("hypothesis_id") or "")
    conclusion_evidence = {
        str(value) for value in conclusion.get("evidence_ids") or [] if str(value)
    }
    chosen = next(
        (
            item
            for item in report.get("hypothesis_results") or []
            if isinstance(item, Mapping)
            and str(item.get("hypothesis_id") or "") == hypothesis_id
        ),
        None,
    )
    if not hypothesis_id or not conclusion_evidence or chosen is None:
        return False
    support_evidence = {
        str(value) for value in chosen.get("support_evidence_ids") or [] if str(value)
    }
    confirmatory_weight = float(chosen.get("confirmatory_weight") or 0.0)
    independently_discriminating = bool(
        chosen.get("discriminating_evidence") or chosen.get("typed_probe_evidence")
    )
    return (
        str(chosen.get("status") or "") == "supported"
        and conclusion_evidence <= support_evidence
        and (
            confirmatory_weight >= 1.25
            or (confirmatory_weight >= 0.85 and independently_discriminating)
        )
    )


def _acquire_mechanism_lock(
    db: Session,
    *,
    user_id: int,
    repo_id: int,
    mechanism_key: str,
) -> bool:
    """Serialize same-mechanism promotion across PostgreSQL workers."""
    bind = db.get_bind()
    if bind is None or str(bind.dialect.name) != "postgresql":
        return False
    material = f"diagnostic-memory:{user_id}:{repo_id}:{mechanism_key}".encode("utf-8")
    lock_key = int.from_bytes(hashlib.sha256(material).digest()[:8], "big", signed=True)
    db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": lock_key},
    )
    return True


def _mechanism_key(report: Mapping[str, Any]) -> str:
    conclusion = report.get("conclusion") if isinstance(report.get("conclusion"), Mapping) else {}
    timeline = report.get("causal_timeline") if isinstance(report.get("causal_timeline"), Mapping) else {}
    earliest = timeline.get("earliest_break") if isinstance(timeline.get("earliest_break"), Mapping) else {}
    graph = report.get("provenance_graph") if isinstance(report.get("provenance_graph"), Mapping) else {}
    raw_dimension = _safe_label(conclusion.get("dimension"), fallback="unknown")
    material = {
        "dimension": raw_dimension if raw_dimension in _ALLOWED_DIMENSIONS else "unknown",
        "event_type": _event_family(earliest.get("event_type")),
        "flow_classification": _flow_classification(graph.get("flow_classification")),
        "contract_topics": _contract_topics(report.get("contract_invariants")),
        "diagnostic_lenses": _safe_lenses(report.get("diagnostic_lenses")),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]


def _abstract_payload(
    run: ProjectAutonomyRun,
    report: Mapping[str, Any],
    *,
    outcome: str,
    evidence_classification: str,
    supersedes_sample_id: int | None,
) -> dict[str, Any]:
    conclusion = report.get("conclusion") if isinstance(report.get("conclusion"), Mapping) else {}
    timeline = report.get("causal_timeline") if isinstance(report.get("causal_timeline"), Mapping) else {}
    earliest = timeline.get("earliest_break") if isinstance(timeline.get("earliest_break"), Mapping) else {}
    graph = report.get("provenance_graph") if isinstance(report.get("provenance_graph"), Mapping) else {}
    raw_dimension = _safe_label(conclusion.get("dimension"), fallback="unknown")
    dimension = raw_dimension if raw_dimension in _ALLOWED_DIMENSIONS else "unknown"
    event_type = _event_family(earliest.get("event_type"))
    flow = _flow_classification(graph.get("flow_classification"))
    contract_topics = _contract_topics(report.get("contract_invariants"))
    lenses = _safe_lenses(report.get("diagnostic_lenses"))
    lesson_parts = [f"validated_dimension={dimension}"]
    if event_type:
        lesson_parts.append(f"earliest_event={event_type}")
    if flow and flow != "no_explicit_break":
        lesson_parts.append(f"flow={flow}")
    lesson_parts.append("require independent discriminating evidence and post-change validation")
    retrieval_terms = sorted(
        _tokens(dimension, event_type, flow, *contract_topics, *lenses)
    )[:40]
    return {
        "schema": MEMORY_SCHEMA,
        "evidence_classification": evidence_classification,
        "retrieval_allowed": evidence_classification in ALLOWED_CLASSIFICATIONS,
        "user_id": run.user_id,
        "repo_id": run.repo_id,
        "source_run_id": run.run_id,
        "source_revision": str(run.base_sha or ""),
        "mechanism_key": _mechanism_key(report),
        "dimension": dimension,
        "event_family": event_type,
        "flow_classification": flow,
        "abstract_lesson": "; ".join(lesson_parts),
        "contract_topics": contract_topics,
        "diagnostic_lenses": lenses,
        "retrieval_terms": retrieval_terms,
        "validation_status": "passed",
        "outcome": outcome,
        "supersedes_sample_id": supersedes_sample_id,
        "contains_raw_prompt": False,
        "contains_raw_evidence": False,
        "contains_oracle": False,
    }


def record_validated_memory(
    db: Session,
    run: ProjectAutonomyRun,
    report: Mapping[str, Any],
    *,
    outcome: str,
    validation_passed: bool,
    evidence_classification: str,
) -> dict[str, Any]:
    """Record only validated, abstract mechanism lessons; never raw incident text."""
    if not validation_passed:
        return {"recorded": False, "reason": "validation_not_passed"}
    if run.user_id is None or run.repo_id is None:
        return {"recorded": False, "reason": "missing_memory_scope"}
    conclusion = report.get("conclusion") if isinstance(report.get("conclusion"), Mapping) else {}
    if not bool(report.get("valid")) or str(conclusion.get("status") or "") != "confirmed":
        return {"recorded": False, "reason": "diagnosis_not_confirmed"}
    if not _has_grounded_confirmed_conclusion(report):
        return {"recorded": False, "reason": "diagnosis_not_grounded"}
    if evidence_classification not in ALLOWED_CLASSIFICATIONS:
        return {"recorded": False, "reason": "classification_not_retrievable"}
    key = _mechanism_key(report)
    concurrency_locked = _acquire_mechanism_lock(
        db,
        user_id=int(run.user_id),
        repo_id=int(run.repo_id),
        mechanism_key=key,
    )
    existing_for_run = (
        db.query(ProjectAutonomyLearningSample)
        .filter(
            ProjectAutonomyLearningSample.run_id == run.run_id,
            ProjectAutonomyLearningSample.sample_type == "diagnostic_mechanism",
        )
        .first()
    )
    if existing_for_run is not None:
        existing_payload = _load_json(existing_for_run.payload_json)
        existing_for_run.outcome = outcome
        existing_payload["outcome"] = outcome
        existing_for_run.payload_json = json.dumps(existing_payload, sort_keys=True)
        return {
            "recorded": False,
            "reason": "already_recorded",
            "sample_id": existing_for_run.id,
            "outcome_updated": True,
            "concurrency_locked": concurrency_locked,
        }
    matching_rows: list[tuple[ProjectAutonomyLearningSample, dict[str, Any]]] = []
    rows = (
        db.query(ProjectAutonomyLearningSample)
        .join(
            ProjectAutonomyRun,
            ProjectAutonomyRun.run_id == ProjectAutonomyLearningSample.run_id,
        )
        .filter(
            ProjectAutonomyLearningSample.repo_id == run.repo_id,
            ProjectAutonomyRun.user_id == run.user_id,
            ProjectAutonomyRun.repo_id == run.repo_id,
            ProjectAutonomyLearningSample.sample_type == "diagnostic_mechanism",
            ProjectAutonomyLearningSample.promoted.is_(True),
            ProjectAutonomyLearningSample.payload_json.contains(key),
        )
        .order_by(ProjectAutonomyLearningSample.id.desc())
        .all()
    )
    for row in rows:
        payload = _load_json(row.payload_json)
        if (
            payload.get("mechanism_key") == key
            and payload.get("user_id") == run.user_id
            and payload.get("repo_id") == run.repo_id
        ):
            matching_rows.append((row, payload))
    new_trust = CLASSIFICATION_TRUST[evidence_classification]
    higher_trust = next(
        (
            (row, payload)
            for row, payload in matching_rows
            if CLASSIFICATION_TRUST.get(
                str(payload.get("evidence_classification") or ""),
                0,
            )
            > new_trust
        ),
        None,
    )
    if higher_trust is not None:
        return {
            "recorded": False,
            "reason": "higher_trust_memory_exists",
            "sample_id": higher_trust[0].id,
            "concurrency_locked": concurrency_locked,
        }
    previous = matching_rows[0][0] if matching_rows else None
    for row, _payload in matching_rows:
        row.promoted = False
    payload = _abstract_payload(
        run,
        report,
        outcome=outcome,
        evidence_classification=evidence_classification,
        supersedes_sample_id=previous.id if previous is not None else None,
    )
    sample = ProjectAutonomyLearningSample(
        run_id=run.run_id,
        repo_id=run.repo_id,
        sample_type="diagnostic_mechanism",
        prompt=None,
        outcome=outcome,
        payload_json=json.dumps(payload, sort_keys=True),
        promoted=True,
    )
    db.add(sample)
    db.flush()
    return {
        "recorded": True,
        "sample_id": sample.id,
        "mechanism_key": key,
        "supersedes_sample_id": previous.id if previous is not None else None,
        "concurrency_locked": concurrency_locked,
    }


def retrieve_memories(
    db: Session,
    *,
    user_id: int | None,
    repo_id: int | None,
    problem_statement: str,
    diagnostic_lenses: Sequence[str] = (),
    evaluation_mode: bool = False,
    max_results: int = 4,
) -> dict[str, Any]:
    """Retrieve same-user, same-repo validated abstractions with exclusion audit."""
    if evaluation_mode:
        return {
            "schema": RETRIEVAL_SCHEMA,
            "selected": [],
            "excluded": [{"reason": "evaluation_mode_memory_disabled"}],
        }
    if user_id is None or repo_id is None:
        return {
            "schema": RETRIEVAL_SCHEMA,
            "selected": [],
            "excluded": [{"reason": "missing_memory_scope"}],
        }
    try:
        bind = db.get_bind()
        read_bind = getattr(bind, "engine", bind) if bind is not None else None
        if read_bind is None or not inspect(read_bind).has_table(
            ProjectAutonomyLearningSample.__tablename__
        ):
            return {
                "schema": RETRIEVAL_SCHEMA,
                "selected": [],
                "excluded": [{"reason": "memory_store_unavailable"}],
            }
        with Session(bind=read_bind) as read_db:
            rows = (
                read_db.query(ProjectAutonomyLearningSample, ProjectAutonomyRun)
                .join(
                    ProjectAutonomyRun,
                    ProjectAutonomyRun.run_id == ProjectAutonomyLearningSample.run_id,
                )
                .filter(
                    ProjectAutonomyLearningSample.sample_type == "diagnostic_mechanism",
                    ProjectAutonomyLearningSample.repo_id == repo_id,
                    ProjectAutonomyRun.user_id == user_id,
                    ProjectAutonomyRun.repo_id == repo_id,
                )
                .order_by(ProjectAutonomyLearningSample.id.desc())
                .limit(200)
                .all()
            )
    except SQLAlchemyError as exc:
        return {
            "schema": RETRIEVAL_SCHEMA,
            "selected": [],
            "excluded": [{"reason": "memory_store_unavailable", "error_type": type(exc).__name__}],
        }
    query_terms = _tokens(problem_statement, *diagnostic_lenses)
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    excluded: list[dict[str, Any]] = []
    superseded_ids: set[int] = set()
    parsed_rows: list[tuple[ProjectAutonomyLearningSample, ProjectAutonomyRun, dict[str, Any]]] = []
    for sample, source_run in rows:
        payload = _load_json(sample.payload_json)
        parsed_rows.append((sample, source_run, payload))
        supersedes = payload.get("supersedes_sample_id")
        if isinstance(supersedes, int):
            superseded_ids.add(supersedes)
    for sample, source_run, payload in parsed_rows:
        reason = ""
        classification = str(payload.get("evidence_classification") or "")
        if source_run.user_id != user_id or payload.get("user_id") != user_id:
            reason = "wrong_user"
        elif source_run.repo_id != repo_id or payload.get("repo_id") != repo_id:
            reason = "wrong_repo"
        elif classification in FORBIDDEN_CLASSIFICATIONS:
            reason = "forbidden_classification"
        elif classification not in ALLOWED_CLASSIFICATIONS:
            reason = "unapproved_classification"
        elif sample.id in superseded_ids:
            reason = "superseded"
        elif not bool(sample.promoted) or not bool(payload.get("retrieval_allowed")):
            reason = "not_promoted"
        elif str(payload.get("validation_status") or "") != "passed":
            reason = "validation_not_passed"
        elif str(payload.get("schema") or "") != MEMORY_SCHEMA:
            reason = "unsupported_schema"
        elif (
            payload.get("contains_oracle")
            or payload.get("contains_raw_evidence")
            or payload.get("contains_raw_prompt")
        ):
            reason = "unsafe_payload"
        if reason:
            excluded.append({"sample_id": sample.id, "reason": reason})
            continue
        public = _public_abstraction(payload, int(sample.id or 0))
        memory_terms = _tokens(
            public["dimension"],
            public["event_family"],
            public["flow_classification"],
            *public["contract_topics"],
            *(
                lens
                for lens in public["diagnostic_lenses"]
                if lens not in _GENERIC_LENSES
            ),
        )
        overlap = len(query_terms & memory_terms)
        dimension = str(public["dimension"])
        dimension_bonus = 4 if dimension in query_terms else 0
        score = overlap * 5 + dimension_bonus
        if score <= 0:
            excluded.append({"sample_id": sample.id, "reason": "no_query_overlap"})
            continue
        public["score"] = score
        candidates.append((-score, -int(sample.id or 0), public))
    candidates.sort(key=lambda item: (item[0], item[1]))
    selected = [item[2] for item in candidates[: max(0, min(8, int(max_results)))]]
    return {
        "schema": RETRIEVAL_SCHEMA,
        "selected": selected,
        "excluded": excluded[:100],
        "query_scope": {
            "user_id": user_id,
            "repo_id": repo_id,
            "database_scope_enforced": True,
        },
    }

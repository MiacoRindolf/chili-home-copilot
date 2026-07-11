from __future__ import annotations

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import (
    ProjectAutonomyArtifact,
    ProjectAutonomyLearningSample,
    ProjectAutonomyRun,
    ProjectDomainRun,
    User,
)
from app.models.code_brain import CodeRepo
from app.services.project_autonomy import diagnostic_memory
from app.services.project_autonomy import orchestrator


def _session(*, include_memory: bool = True):
    engine = create_engine("sqlite:///:memory:")
    tables = [
        User.__table__,
        CodeRepo.__table__,
        ProjectDomainRun.__table__,
        ProjectAutonomyRun.__table__,
        ProjectAutonomyArtifact.__table__,
    ]
    if include_memory:
        tables.append(ProjectAutonomyLearningSample.__table__)
    Base.metadata.create_all(
        engine,
        tables=tables,
    )
    return sessionmaker(bind=engine)()


def _scope(db, suffix: str):
    user = User(name=f"memory-user-{suffix}")
    db.add(user)
    db.flush()
    repo = CodeRepo(
        user_id=user.id,
        path=f"C:/memory-repo-{suffix}",
        name=f"memory-repo-{suffix}",
        active=True,
    )
    db.add(repo)
    db.flush()
    return user, repo


def _run(db, *, run_id: str, user_id: int, repo_id: int, prompt: str = "Diagnose queue starvation"):
    run = ProjectAutonomyRun(
        run_id=run_id,
        user_id=user_id,
        repo_id=repo_id,
        prompt=prompt,
        base_sha="abc123",
    )
    db.add(run)
    db.flush()
    return run


def _confirmed_report() -> dict:
    return {
        "valid": True,
        "conclusion": {
            "hypothesis_id": "h-state",
            "status": "confirmed",
            "dimension": "state",
            "evidence_ids": ["queue-proof"],
        },
        "hypothesis_results": [
            {
                "hypothesis_id": "h-state",
                "status": "supported",
                "support_evidence_ids": ["queue-proof"],
                "confirmatory_weight": 1.4,
                "discriminating_evidence": True,
            }
        ],
        "causal_timeline": {
            "earliest_break": {"event_type": "queue_consumer_starvation"}
        },
        "provenance_graph": {"flow_classification": "consumer_starvation"},
        "contract_invariants": [
            "Failed single-flight work must evict the cached promise before retry."
        ],
        "diagnostic_lenses": [
            "causal_timeline",
            "producer_consumer_evidence_chain",
        ],
    }


def _add_diagnostic_artifact(db, run: ProjectAutonomyRun) -> None:
    report_payload = json.dumps({"report": _confirmed_report()}, sort_keys=True)
    db.add(
        ProjectAutonomyArtifact(
            run_id=run.run_id,
            artifact_type="diagnostic",
            name="local_diagnostic_debate",
            content_json=report_payload,
            byte_length=len(report_payload),
        )
    )
    db.flush()


def _seed_memory(
    db,
    *,
    run: ProjectAutonomyRun,
    classification: str = "production_validated",
    promoted: bool = True,
    dimension: str = "state",
    event_family: str = "queue_transition",
    flow_classification: str = "consumer_starvation",
    contract_topics: list[str] | None = None,
    diagnostic_lenses: list[str] | None = None,
    contains_raw_prompt: bool = False,
    contains_raw_evidence: bool = False,
    supersedes_sample_id: int | None = None,
) -> ProjectAutonomyLearningSample:
    payload = {
        "schema": diagnostic_memory.MEMORY_SCHEMA,
        "evidence_classification": classification,
        "retrieval_allowed": classification in diagnostic_memory.ALLOWED_CLASSIFICATIONS,
        "user_id": run.user_id,
        "repo_id": run.repo_id,
        "source_run_id": run.run_id,
        "mechanism_key": "0123456789abcdef01234567",
        "dimension": dimension,
        "event_family": event_family,
        "flow_classification": flow_classification,
        "abstract_lesson": f"validated_dimension={dimension}; require independent evidence",
        "contract_topics": (
            ["single_flight_eviction"]
            if contract_topics is None
            else contract_topics
        ),
        "diagnostic_lenses": (
            ["producer_consumer_evidence_chain"]
            if diagnostic_lenses is None
            else diagnostic_lenses
        ),
        "retrieval_terms": ["state", "queue", "consumer"],
        "validation_status": "passed",
        "outcome": "completed",
        "supersedes_sample_id": supersedes_sample_id,
        "contains_raw_prompt": contains_raw_prompt,
        "contains_raw_evidence": contains_raw_evidence,
        "contains_oracle": False,
    }
    sample = ProjectAutonomyLearningSample(
        run_id=run.run_id,
        repo_id=run.repo_id,
        sample_type="diagnostic_mechanism",
        prompt=None,
        outcome="completed",
        payload_json=json.dumps(payload, sort_keys=True),
        promoted=promoted,
    )
    db.add(sample)
    db.flush()
    return sample


def test_record_validated_memory_stores_only_controlled_abstractions():
    db = _session()
    try:
        user, repo = _scope(db, "abstract")
        secret = "SEALED-ORACLE-ANSWER-ALPHA-7731"
        run = _run(
            db,
            run_id="memory-abstract",
            user_id=user.id,
            repo_id=repo.id,
            prompt=f"Diagnose this incident. Never retain {secret}.",
        )
        report = _confirmed_report()
        report["contract_invariants"].append(
            f"{secret} is the exact hidden answer and C:/private/incident.log proves it."
        )
        report["diagnostic_lenses"].append(secret)
        report["causal_timeline"]["earliest_break"]["event_type"] = (
            f"queue_{secret}"
        )
        report["provenance_graph"]["flow_classification"] = (
            f"consumer_starvation_{secret}"
        )

        result = diagnostic_memory.record_validated_memory(
            db,
            run,
            report,
            outcome="completed",
            validation_passed=True,
            evidence_classification="operator_validated",
        )
        db.commit()

        assert result["recorded"] is True
        sample = db.query(ProjectAutonomyLearningSample).one()
        payload = json.loads(sample.payload_json)
        assert sample.prompt is None
        assert secret.lower() not in sample.payload_json.lower()
        assert "private" not in sample.payload_json.lower()
        assert payload["contract_topics"] == ["single_flight_eviction"]
        assert payload["event_family"] == "queue_transition"
        assert payload["flow_classification"] == "no_explicit_break"
        assert payload["diagnostic_lenses"] == [
            "causal_timeline",
            "producer_consumer_evidence_chain",
        ]
        assert payload["contains_raw_prompt"] is False
        assert payload["contains_raw_evidence"] is False
        assert payload["contains_oracle"] is False
    finally:
        db.close()


def test_record_validated_memory_rejects_ungrounded_confirmation():
    db = _session()
    try:
        user, repo = _scope(db, "ungrounded")
        run = _run(
            db,
            run_id="memory-ungrounded",
            user_id=user.id,
            repo_id=repo.id,
        )
        report = _confirmed_report()
        report["conclusion"]["evidence_ids"] = []

        result = diagnostic_memory.record_validated_memory(
            db,
            run,
            report,
            outcome="completed",
            validation_passed=True,
            evidence_classification="operator_validated",
        )

        assert result == {"recorded": False, "reason": "diagnosis_not_grounded"}
        assert db.query(ProjectAutonomyLearningSample).count() == 0
    finally:
        db.close()


def test_record_validated_memory_supersedes_without_deleting_history():
    db = _session()
    try:
        user, repo = _scope(db, "supersede")
        first_run = _run(
            db,
            run_id="memory-supersede-1",
            user_id=user.id,
            repo_id=repo.id,
        )
        first = diagnostic_memory.record_validated_memory(
            db,
            first_run,
            _confirmed_report(),
            outcome="completed",
            validation_passed=True,
            evidence_classification="operator_validated",
        )
        second_run = _run(
            db,
            run_id="memory-supersede-2",
            user_id=user.id,
            repo_id=repo.id,
        )
        second = diagnostic_memory.record_validated_memory(
            db,
            second_run,
            _confirmed_report(),
            outcome="completed",
            validation_passed=True,
            evidence_classification="operator_validated",
        )
        db.commit()

        rows = db.query(ProjectAutonomyLearningSample).order_by(
            ProjectAutonomyLearningSample.id
        ).all()
        assert len(rows) == 2
        assert rows[0].id == first["sample_id"]
        assert rows[0].promoted is False
        assert rows[1].id == second["sample_id"]
        assert rows[1].promoted is True
        assert json.loads(rows[1].payload_json)["supersedes_sample_id"] == rows[0].id
    finally:
        db.close()


def test_operator_memory_cannot_supersede_production_validated_memory():
    db = _session()
    try:
        user, repo = _scope(db, "trust-rank")
        production_run = _run(
            db,
            run_id="memory-production",
            user_id=user.id,
            repo_id=repo.id,
        )
        production = diagnostic_memory.record_validated_memory(
            db,
            production_run,
            _confirmed_report(),
            outcome="completed",
            validation_passed=True,
            evidence_classification="production_validated",
        )
        operator_run = _run(
            db,
            run_id="memory-lower-trust",
            user_id=user.id,
            repo_id=repo.id,
        )
        lower_trust = diagnostic_memory.record_validated_memory(
            db,
            operator_run,
            _confirmed_report(),
            outcome="completed",
            validation_passed=True,
            evidence_classification="operator_validated",
        )
        db.commit()

        rows = db.query(ProjectAutonomyLearningSample).all()
        assert production["recorded"] is True
        assert lower_trust["recorded"] is False
        assert lower_trust["reason"] == "higher_trust_memory_exists"
        assert len(rows) == 1
        assert rows[0].id == production["sample_id"]
        assert rows[0].promoted is True
    finally:
        db.close()


def test_repeat_record_updates_final_outcome_without_duplicate_memory():
    db = _session()
    try:
        user, repo = _scope(db, "outcome")
        run = _run(
            db,
            run_id="memory-outcome",
            user_id=user.id,
            repo_id=repo.id,
        )
        first = diagnostic_memory.record_validated_memory(
            db,
            run,
            _confirmed_report(),
            outcome="validated",
            validation_passed=True,
            evidence_classification="operator_validated",
        )
        final = diagnostic_memory.record_validated_memory(
            db,
            run,
            _confirmed_report(),
            outcome="merged",
            validation_passed=True,
            evidence_classification="operator_validated",
        )
        db.commit()

        row = db.query(ProjectAutonomyLearningSample).one()
        assert first["recorded"] is True
        assert final["reason"] == "already_recorded"
        assert final["outcome_updated"] is True
        assert row.outcome == "merged"
        assert json.loads(row.payload_json)["outcome"] == "merged"
    finally:
        db.close()


def test_retrieve_memories_enforces_scope_classification_safety_and_supersession():
    db = _session()
    try:
        user, repo = _scope(db, "primary")
        other_user, _ = _scope(db, "other-user")
        _, other_repo = _scope(db, "other-repo")
        eligible = _seed_memory(
            db,
            run=_run(db, run_id="eligible", user_id=user.id, repo_id=repo.id),
        )
        retrieval_secret = "TAMPERED-LESSON-SECRET-4882"
        eligible_payload = json.loads(eligible.payload_json)
        eligible_payload["abstract_lesson"] = retrieval_secret
        eligible_payload["retrieval_terms"] = [retrieval_secret, "state"]
        eligible_payload["contract_topics"].append(retrieval_secret)
        eligible.payload_json = json.dumps(eligible_payload, sort_keys=True)
        wrong_user = _seed_memory(
            db,
            run=_run(
                db,
                run_id="wrong-user",
                user_id=other_user.id,
                repo_id=repo.id,
            ),
        )
        wrong_repo = _seed_memory(
            db,
            run=_run(
                db,
                run_id="wrong-repo",
                user_id=user.id,
                repo_id=other_repo.id,
            ),
        )
        tampered_user = _seed_memory(
            db,
            run=_run(db, run_id="tampered-user", user_id=user.id, repo_id=repo.id),
        )
        tampered_user_payload = json.loads(tampered_user.payload_json)
        tampered_user_payload["user_id"] = other_user.id
        tampered_user.payload_json = json.dumps(tampered_user_payload, sort_keys=True)
        tampered_repo = _seed_memory(
            db,
            run=_run(db, run_id="tampered-repo", user_id=user.id, repo_id=repo.id),
        )
        tampered_repo_payload = json.loads(tampered_repo.payload_json)
        tampered_repo_payload["repo_id"] = other_repo.id
        tampered_repo.payload_json = json.dumps(tampered_repo_payload, sort_keys=True)
        sealed = _seed_memory(
            db,
            run=_run(db, run_id="sealed", user_id=user.id, repo_id=repo.id),
            classification="sealed_oracle",
        )
        development = _seed_memory(
            db,
            run=_run(db, run_id="development", user_id=user.id, repo_id=repo.id),
            classification="development_replay",
        )
        unpromoted = _seed_memory(
            db,
            run=_run(db, run_id="unpromoted", user_id=user.id, repo_id=repo.id),
            promoted=False,
        )
        unsafe = _seed_memory(
            db,
            run=_run(db, run_id="unsafe", user_id=user.id, repo_id=repo.id),
            contains_raw_evidence=True,
        )
        unsafe_prompt = _seed_memory(
            db,
            run=_run(db, run_id="unsafe-prompt", user_id=user.id, repo_id=repo.id),
            contains_raw_prompt=True,
        )
        unrelated = _seed_memory(
            db,
            run=_run(db, run_id="unrelated", user_id=user.id, repo_id=repo.id),
            dimension="clock",
            event_family="unspecified_transition",
            flow_classification="no_explicit_break",
            contract_topics=[],
            diagnostic_lenses=[],
        )
        old = _seed_memory(
            db,
            run=_run(db, run_id="old", user_id=user.id, repo_id=repo.id),
            promoted=False,
        )
        current = _seed_memory(
            db,
            run=_run(db, run_id="current", user_id=user.id, repo_id=repo.id),
            supersedes_sample_id=old.id,
        )
        db.commit()

        result = diagnostic_memory.retrieve_memories(
            db,
            user_id=user.id,
            repo_id=repo.id,
            problem_statement="Diagnose state queue consumer starvation",
            diagnostic_lenses=["producer_consumer_evidence_chain"],
            max_results=8,
        )

        assert {item["sample_id"] for item in result["selected"]} == {
            eligible.id,
            current.id,
        }
        assert retrieval_secret.lower() not in json.dumps(
            result["selected"],
            sort_keys=True,
        ).lower()
        excluded = {item["sample_id"]: item["reason"] for item in result["excluded"]}
        assert excluded[tampered_user.id] == "wrong_user"
        assert excluded[tampered_repo.id] == "wrong_repo"
        scoped_ids = {
            item["sample_id"] for item in [*result["selected"], *result["excluded"]]
        }
        assert wrong_user.id not in scoped_ids
        assert wrong_repo.id not in scoped_ids
        assert result["query_scope"]["database_scope_enforced"] is True
        assert excluded[sealed.id] == "forbidden_classification"
        assert excluded[development.id] == "forbidden_classification"
        assert excluded[unpromoted.id] == "not_promoted"
        assert excluded[unsafe.id] == "unsafe_payload"
        assert excluded[unsafe_prompt.id] == "unsafe_payload"
        assert excluded[unrelated.id] == "no_query_overlap"
        assert excluded[old.id] == "superseded"
    finally:
        db.close()


def test_retrieve_memories_is_hard_disabled_in_evaluation_mode():
    db = _session()
    try:
        result = diagnostic_memory.retrieve_memories(
            db,
            user_id=1,
            repo_id=1,
            problem_statement="Diagnose queue starvation",
            evaluation_mode=True,
        )

        assert result["selected"] == []
        assert result["excluded"] == [
            {"reason": "evaluation_mode_memory_disabled"}
        ]
    finally:
        db.close()


def test_retrieve_memories_handles_absent_store_without_poisoning_session():
    db = _session(include_memory=False)
    try:
        user, repo = _scope(db, "absent-store")
        run = _run(
            db,
            run_id="memory-absent-store",
            user_id=user.id,
            repo_id=repo.id,
        )
        db.commit()

        result = diagnostic_memory.retrieve_memories(
            db,
            user_id=user.id,
            repo_id=repo.id,
            problem_statement="Diagnose queue starvation",
        )
        db.add(
            ProjectAutonomyArtifact(
                run_id=run.run_id,
                artifact_type="diagnostic",
                name="post_memory_check",
                content_json="{}",
                byte_length=2,
            )
        )
        db.commit()

        assert result["selected"] == []
        assert result["excluded"] == [{"reason": "memory_store_unavailable"}]
        assert db.query(ProjectAutonomyArtifact).count() == 1
    finally:
        db.close()


def test_record_learning_promotes_only_operator_approved_diagnostics(monkeypatch):
    db = _session()
    try:
        user, repo = _scope(db, "operator-approved")
        run = _run(
            db,
            run_id="memory-operator-approved",
            user_id=user.id,
            repo_id=repo.id,
        )
        run.execution_mode = orchestrator.EXECUTION_MODE_PLAN_APPROVAL
        run.plan_status = orchestrator.PLAN_STATUS_APPROVED
        _add_diagnostic_artifact(db, run)
        monkeypatch.setattr(orchestrator, "_DIAGNOSTIC_MEMORY_ENABLED", True)
        monkeypatch.setattr(orchestrator, "_DIAGNOSTIC_EVALUATION_MODE", False)

        orchestrator._record_learning(
            db,
            run,
            outcome="validated",
            plan={"files": []},
            validation=[{"exit_code": 0, "command": "isolated tests"}],
        )
        db.commit()

        memory = (
            db.query(ProjectAutonomyLearningSample)
            .filter(ProjectAutonomyLearningSample.sample_type == "diagnostic_mechanism")
            .one()
        )
        assert memory.promoted is True
        assert json.loads(memory.payload_json)["evidence_classification"] == (
            "operator_validated"
        )
        record_artifact = (
            db.query(ProjectAutonomyArtifact)
            .filter(ProjectAutonomyArtifact.name == "diagnostic_memory_record")
            .one()
        )
        assert json.loads(record_artifact.content_json)["recorded"] is True
    finally:
        db.close()


def test_record_learning_does_not_self_promote_full_autopilot_diagnosis(monkeypatch):
    db = _session()
    try:
        user, repo = _scope(db, "self-promotion")
        run = _run(
            db,
            run_id="memory-self-promotion",
            user_id=user.id,
            repo_id=repo.id,
        )
        run.execution_mode = orchestrator.EXECUTION_MODE_FULL_AUTOPILOT
        run.plan_status = orchestrator.PLAN_STATUS_APPROVED
        _add_diagnostic_artifact(db, run)
        monkeypatch.setattr(orchestrator, "_DIAGNOSTIC_MEMORY_ENABLED", True)
        monkeypatch.setattr(orchestrator, "_DIAGNOSTIC_EVALUATION_MODE", False)

        orchestrator._record_learning(
            db,
            run,
            outcome="validated",
            plan={"files": []},
            validation=[{"exit_code": 0, "command": "isolated tests"}],
        )
        db.commit()

        assert (
            db.query(ProjectAutonomyLearningSample)
            .filter(ProjectAutonomyLearningSample.sample_type == "diagnostic_mechanism")
            .count()
            == 0
        )
        record_artifact = (
            db.query(ProjectAutonomyArtifact)
            .filter(ProjectAutonomyArtifact.name == "diagnostic_memory_record")
            .one()
        )
        assert json.loads(record_artifact.content_json) == {
            "recorded": False,
            "reason": "classification_not_retrievable",
        }
    finally:
        db.close()


def test_record_learning_never_persists_memory_in_evaluation_mode(monkeypatch):
    db = _session()
    try:
        user, repo = _scope(db, "evaluation-record")
        run = _run(
            db,
            run_id="memory-evaluation-record",
            user_id=user.id,
            repo_id=repo.id,
        )
        run.execution_mode = orchestrator.EXECUTION_MODE_PLAN_APPROVAL
        run.plan_status = orchestrator.PLAN_STATUS_APPROVED
        _add_diagnostic_artifact(db, run)
        monkeypatch.setattr(orchestrator, "_DIAGNOSTIC_MEMORY_ENABLED", True)
        monkeypatch.setattr(orchestrator, "_DIAGNOSTIC_EVALUATION_MODE", True)

        orchestrator._record_learning(
            db,
            run,
            outcome="validated",
            plan={"files": []},
            validation=[{"exit_code": 0, "command": "isolated tests"}],
        )
        db.commit()

        assert (
            db.query(ProjectAutonomyLearningSample)
            .filter(ProjectAutonomyLearningSample.sample_type == "diagnostic_mechanism")
            .count()
            == 0
        )
        record_artifact = (
            db.query(ProjectAutonomyArtifact)
            .filter(ProjectAutonomyArtifact.name == "diagnostic_memory_record")
            .one()
        )
        assert json.loads(record_artifact.content_json)["reason"] == (
            "evaluation_mode_memory_disabled"
        )
    finally:
        db.close()


def test_record_learning_respects_disabled_memory_flag(monkeypatch):
    db = _session()
    try:
        user, repo = _scope(db, "disabled-record")
        run = _run(
            db,
            run_id="memory-disabled-record",
            user_id=user.id,
            repo_id=repo.id,
        )
        run.execution_mode = orchestrator.EXECUTION_MODE_PLAN_APPROVAL
        run.plan_status = orchestrator.PLAN_STATUS_APPROVED
        _add_diagnostic_artifact(db, run)
        monkeypatch.setattr(orchestrator, "_DIAGNOSTIC_MEMORY_ENABLED", False)
        monkeypatch.setattr(orchestrator, "_DIAGNOSTIC_EVALUATION_MODE", False)

        orchestrator._record_learning(
            db,
            run,
            outcome="validated",
            plan={"files": []},
            validation=[{"exit_code": 0, "command": "isolated tests"}],
        )
        db.commit()

        assert (
            db.query(ProjectAutonomyLearningSample)
            .filter(ProjectAutonomyLearningSample.sample_type == "diagnostic_mechanism")
            .count()
            == 0
        )
        record_artifact = (
            db.query(ProjectAutonomyArtifact)
            .filter(ProjectAutonomyArtifact.name == "diagnostic_memory_record")
            .one()
        )
        assert json.loads(record_artifact.content_json)["reason"] == (
            "diagnostic_memory_disabled"
        )
    finally:
        db.close()


def test_record_learning_rejects_skipped_only_validation(monkeypatch):
    db = _session()
    try:
        user, repo = _scope(db, "skipped-validation")
        run = _run(
            db,
            run_id="memory-skipped-validation",
            user_id=user.id,
            repo_id=repo.id,
        )
        run.execution_mode = orchestrator.EXECUTION_MODE_PLAN_APPROVAL
        run.plan_status = orchestrator.PLAN_STATUS_APPROVED
        _add_diagnostic_artifact(db, run)
        monkeypatch.setattr(orchestrator, "_DIAGNOSTIC_MEMORY_ENABLED", True)
        monkeypatch.setattr(orchestrator, "_DIAGNOSTIC_EVALUATION_MODE", False)

        learning = orchestrator._record_learning(
            db,
            run,
            outcome="validated",
            plan={"files": []},
            validation=[{"exit_code": 0, "skipped": True, "command": "missing tool"}],
        )
        db.commit()

        assert learning["validation_passed"] is True
        assert learning["diagnostic_memory_validation_passed"] is False
        assert (
            db.query(ProjectAutonomyLearningSample)
            .filter(ProjectAutonomyLearningSample.sample_type == "diagnostic_mechanism")
            .count()
            == 0
        )
        record_artifact = (
            db.query(ProjectAutonomyArtifact)
            .filter(ProjectAutonomyArtifact.name == "diagnostic_memory_record")
            .one()
        )
        assert json.loads(record_artifact.content_json)["reason"] == (
            "validation_not_passed"
        )
    finally:
        db.close()


def test_local_diagnostic_reasoning_receives_only_retrieved_abstraction(monkeypatch):
    db = _session()
    try:
        user, repo = _scope(db, "reasoning")
        source_secret = "SOURCE-INCIDENT-SECRET-9917"
        source_run = _run(
            db,
            run_id="memory-reasoning-source",
            user_id=user.id,
            repo_id=repo.id,
            prompt=f"Queue consumer failed with {source_secret}",
        )
        _seed_memory(db, run=source_run)
        current_run = _run(
            db,
            run_id="memory-reasoning-current",
            user_id=user.id,
            repo_id=repo.id,
            prompt="Diagnose state queue consumer starvation",
        )
        db.commit()
        captured: dict = {}

        def fake_debate(case, _role, **_kwargs):
            captured["case"] = case
            return {
                "packet": {},
                "report": {
                    "valid": True,
                    "decision": "investigate",
                    "conclusion": {"status": "inconclusive", "dimension": "state"},
                    "next_experiments": [],
                },
                "stages": [],
                "premium_calls": 0,
            }

        monkeypatch.setattr(orchestrator, "_DIAGNOSTIC_MEMORY_ENABLED", True)
        monkeypatch.setattr(orchestrator, "_DIAGNOSTIC_EVALUATION_MODE", False)
        monkeypatch.setattr(
            orchestrator.diagnostic_reasoning,
            "run_local_diagnostic_debate",
            fake_debate,
        )

        orchestrator._run_local_diagnostic_reasoning(
            db,
            current_run,
            context={},
            repo_path=None,
            model_info={"model": None},
        )

        memories = captured["case"]["constraints"]["retrieved_mechanisms"]
        assert len(memories) == 1
        assert memories[0]["abstract_lesson"].startswith("validated_dimension=state")
        serialized = json.dumps(memories, sort_keys=True)
        assert source_secret.lower() not in serialized.lower()
        assert "source_run_id" not in serialized
        retrieval_artifact = (
            db.query(ProjectAutonomyArtifact)
            .filter(ProjectAutonomyArtifact.name == "diagnostic_memory_retrieval")
            .one()
        )
        assert source_secret.lower() not in (
            retrieval_artifact.content_json or ""
        ).lower()
    finally:
        db.close()

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.services.project_autonomy import diagnostic_probes
from app.services.project_autonomy import diagnostic_reasoning
from app.services.project_autonomy import diagnostic_runtime_evidence as runtime_evidence


def test_log_inventory_and_search_are_suffix_tail_and_root_bounded(tmp_path):
    root = tmp_path.resolve()
    logs = root / "logs"
    logs.mkdir()
    (logs / "worker.log").write_text(
        "startup ok\nqueue depth=500\nconnection refused upstream\n",
        encoding="utf-8",
    )
    (root / "app.py").write_text(
        "message = 'connection refused but this is source, not a log'\n",
        encoding="utf-8",
    )

    run = diagnostic_probes.execute_safe_probes(
        root,
        [
            {
                "probe_id": "logs",
                "kind": "log_inventory",
                "paths": ["logs"],
                "safety": "read_only",
                "dimension": "runtime",
            },
            {
                "probe_id": "connection-errors",
                "kind": "log_search",
                "paths": ["logs"],
                "query": "connection refused",
                "tail_lines": 20,
                "safety": "read_only",
                "dimension": "dependency",
            },
        ],
    )

    inventory = json.loads(run["results"][0]["output"])
    searched = json.loads(run["results"][1]["output"])
    assert inventory["log_files"][0]["path"] == "logs/worker.log"
    assert searched["matches"][0]["path"] == "logs/worker.log"
    assert "app.py" not in run["results"][1]["output"]
    assert all(item["status"] == "completed" for item in run["results"])
    assert run["evidence"][0]["dimension"] == "unknown"
    assert run["evidence"][0]["discriminating"] is False
    assert run["evidence"][1]["dimension"] == "dependency"
    assert run["evidence"][1]["kind"] == "artifact"
    assert run["evidence"][1]["discriminating"] is False
    assert run["evidence"][1]["causal_role"] == "context"
    assert run["evidence"][1]["intervention_scope"] == "none"


def test_database_url_resolution_never_falls_back_to_primary(monkeypatch):
    primary = "postgresql://writer:secret@db.example/chili"
    monkeypatch.setenv("DATABASE_URL", primary)
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("CHILI_AUTONOMY_READONLY_DATABASE_URL", raising=False)

    url, is_test, error = runtime_evidence.resolve_readonly_database_url()

    assert url == ""
    assert is_test is False
    assert "READONLY_DATABASE_URL" in error

    monkeypatch.setenv("CHILI_AUTONOMY_READONLY_DATABASE_URL", primary)
    url, _is_test, error = runtime_evidence.resolve_readonly_database_url()
    assert url == ""
    assert "distinct from DATABASE_URL" in error

    monkeypatch.setenv(
        "CHILI_AUTONOMY_READONLY_DATABASE_URL",
        "postgresql://writer:different-secret@readonly.example/chili",
    )
    url, _is_test, error = runtime_evidence.resolve_readonly_database_url()
    assert url == ""
    assert "login distinct from DATABASE_URL" in error

    monkeypatch.setenv(
        "CHILI_AUTONOMY_READONLY_DATABASE_URL",
        "postgresql://readonly.example/chili",
    )
    url, _is_test, error = runtime_evidence.resolve_readonly_database_url()
    assert url == ""
    assert "dedicated database login" in error


def test_database_errors_redact_dsn_credentials():
    rendered = runtime_evidence._safe_db_error(
        RuntimeError("connect failed postgresql://reader:super-secret@db.example/chili")
    )

    assert "super-secret" not in rendered
    assert "postgresql://[redacted]" in rendered


def test_explicit_database_url_is_accepted_only_for_test_database():
    rejected = runtime_evidence.resolve_readonly_database_url(
        "postgresql://writer:secret@localhost/chili"
    )
    accepted = runtime_evidence.resolve_readonly_database_url(
        "postgresql://tester:secret@localhost/chili_test"
    )

    assert rejected[0] == ""
    assert "_test" in rejected[2]
    assert accepted[0].endswith("/chili_test")
    assert accepted[1] is True


def test_read_only_connection_sets_transaction_and_short_timeouts():
    statements = []

    class Result:
        def scalar(self):
            return "on"

    class Transaction:
        is_active = True

        def rollback(self):
            self.is_active = False

    class Connection:
        def begin(self):
            return Transaction()

        def execute(self, statement):
            statements.append(str(statement))
            return Result()

        def close(self):
            return None

    class Engine:
        def connect(self):
            return Connection()

    connection, transaction = runtime_evidence._read_only_connection(Engine())
    transaction.rollback()
    connection.close()

    assert statements[0] == "SET TRANSACTION READ ONLY"
    assert any("statement_timeout" in value for value in statements)
    assert any("lock_timeout" in value for value in statements)
    assert statements[-1] == "SHOW transaction_read_only"


def test_production_role_must_have_select_without_write_privileges():
    class Result:
        def __init__(self, privileges):
            self._privileges = privileges

        def mappings(self):
            return self

        def one(self):
            return self._privileges

    class Connection:
        def __init__(self, privileges):
            self._privileges = privileges

        def execute(self, _statement, _parameters):
            return Result(self._privileges)

    readonly = {
        "can_select": True,
        "can_insert": False,
        "can_update": False,
        "can_delete": False,
        "can_truncate": False,
        "can_reference": False,
        "can_trigger": False,
    }
    runtime_evidence._assert_select_only_table(Connection(readonly), "trading_events")

    writer = {**readonly, "can_update": True}
    with pytest.raises(PermissionError, match="write-capable"):
        runtime_evidence._assert_select_only_table(Connection(writer), "trading_events")


def test_unclassified_log_match_does_not_inherit_model_dimension(tmp_path):
    (tmp_path / "worker.log").write_text("ERROR opaque failure code=17\n", encoding="utf-8")

    run = diagnostic_probes.execute_safe_probes(
        tmp_path,
        [
            {
                "probe_id": "opaque",
                "kind": "log_search",
                "query": "opaque failure",
                "safety": "read_only",
                "dimension": "code",
            }
        ],
    )

    assert run["evidence"][0]["dimension"] == "unknown"
    assert run["evidence"][0]["discriminating"] is False


def test_production_profile_requires_bounded_timestamp_window(monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://writer:x@localhost/chili")
    monkeypatch.setenv(
        "CHILI_AUTONOMY_READONLY_DATABASE_URL",
        "postgresql://reader:x@localhost/chili",
    )

    result = runtime_evidence.execute_db_profile({"table": "trading_events"})

    assert result["status"] == "blocked"
    assert "timestamp_column" in result["output"]
    assert "lookback_minutes" in result["output"]


def test_test_database_schema_and_count_probes_are_read_only(db):
    test_url = os.environ["TEST_DATABASE_URL"]
    root = Path.cwd()
    run = diagnostic_probes.execute_safe_probes(
        root,
        [
            {
                "probe_id": "users-schema",
                "kind": "db_schema",
                "table": "users",
                "safety": "read_only",
                "dimension": "data",
            },
            {
                "probe_id": "users-count",
                "kind": "db_profile",
                "table": "users",
                "safety": "read_only",
                "dimension": "data",
            },
        ],
        explicit_test_database_url=test_url,
    )

    schema = json.loads(run["results"][0]["output"])
    profile = json.loads(run["results"][1]["output"])
    assert run["results"][0]["status"] == "completed"
    assert run["results"][1]["status"] == "completed"
    assert schema["transaction_read_only"] is True
    assert {item["name"] for item in schema["columns"]} >= {"id", "name", "email"}
    assert profile["transaction_read_only"] is True
    assert isinstance(profile["count"], int)
    assert "postgresql://" not in json.dumps(run["results"])


def test_db_probe_schema_has_no_raw_sql_and_rejects_unsafe_identifiers():
    normalized = diagnostic_probes.normalize_probe_spec(
        {
            "probe_id": "unsafe",
            "kind": "db_profile",
            "table": "users; DROP TABLE users",
            "sql": "DELETE FROM users",
        }
    )

    assert "sql" not in normalized
    assert diagnostic_probes.validate_probe_spec(normalized, "read_only")
    assert "command" not in diagnostic_probes.PROBE_KINDS


def test_model_packet_accepts_typed_aggregate_probe_without_sql():
    packet = diagnostic_reasoning.normalize_packet(
        {
            "hypotheses": [
                {
                    "hypothesis_id": "h-data",
                    "claim": "A queue reason dominates recent events.",
                    "dimension": "data",
                    "support_evidence_ids": ["e1"],
                    "falsification": "Profile recent reasons while holding code constant.",
                }
            ],
            "experiments": [
                {
                    "experiment_id": "x-profile",
                    "hypothesis_ids": ["h-data"],
                    "changed_dimensions": ["data"],
                    "safety": "read_only",
                    "auto_execute": True,
                    "probe": {
                        "probe_id": "recent-reasons",
                        "kind": "db_profile",
                        "table": "trading_events",
                        "timestamp_column": "created_at",
                        "lookback_minutes": 60,
                        "group_by": "reason",
                        "filters": {"status": "pending"},
                    },
                }
            ],
            "conclusion": {"hypothesis_id": "h-data", "status": "provisional"},
        }
    )
    probes = diagnostic_probes.probes_from_packet(packet)

    assert len(probes) == 1
    assert probes[0]["kind"] == "db_profile"
    assert probes[0]["lookback_minutes"] == 60
    assert probes[0]["filters"] == {"status": "pending"}
    assert "sql" not in probes[0]


def test_default_runtime_followups_include_logs_and_named_table_schema():
    probes = diagnostic_probes.default_followup_probes(
        {"decision": "instrument_first"},
        [],
        (
            "Worker logs show a connection refused error while "
            "trading_autotrader_runs may be stale."
        ),
    )

    assert [item["kind"] for item in probes] == [
        "log_inventory",
        "log_search",
        "db_schema",
        "repo_state",
    ]
    assert probes[1]["query"] == "connection refused"
    assert probes[2]["table"] == "trading_autotrader_runs"


def test_default_database_followup_parses_only_explicit_bounded_profile_fields():
    probes = diagnostic_probes.default_followup_probes(
        {"decision": "instrument_first"},
        [],
        (
            "Inspect brain_activation_events with timestamp_column=created_at, "
            "lookback_minutes=120, group_by=cause."
        ),
    )

    assert [item["kind"] for item in probes[:2]] == ["db_schema", "db_profile"]
    assert probes[1]["table"] == "brain_activation_events"
    assert probes[1]["timestamp_column"] == "created_at"
    assert probes[1]["lookback_minutes"] == 120
    assert probes[1]["group_by"] == "cause"


def test_prompt_grounded_runtime_probes_are_not_starved_by_model_probe():
    defaults = diagnostic_probes.default_followup_probes(
        {"decision": "instrument_first"},
        [],
        "Worker logs show an error in trading_events.",
    )
    model = [
        {
            **diagnostic_probes.normalize_probe_spec(
                {
                    "probe_id": "model-repo-state",
                    "kind": "repo_state",
                    "dimension": "code",
                }
            ),
            "safety": "read_only",
        }
    ]

    merged = diagnostic_probes.merge_probe_sets(defaults, model, max_probes=4)

    assert [item["kind"] for item in merged[:3]] == [
        "log_inventory",
        "log_search",
        "db_schema",
    ]
    assert len(merged) == 4


def test_probe_selector_prioritizes_information_gain_and_skips_attempted_probe():
    probes = [
        {
            **diagnostic_probes.normalize_probe_spec(
                {
                    "probe_id": "inspect-repo",
                    "kind": "repo_state",
                    "dimension": "code",
                }
            ),
            "safety": "read_only",
        },
        {
            **diagnostic_probes.normalize_probe_spec(
                {
                    "probe_id": "search-provider-log",
                    "kind": "log_search",
                    "query": "connection refused",
                    "dimension": "dependency",
                }
            ),
            "safety": "read_only",
        },
        {
            **diagnostic_probes.normalize_probe_spec(
                {
                    "probe_id": "profile-events",
                    "kind": "db_profile",
                    "table": "trading_events",
                    "timestamp_column": "created_at",
                    "lookback_minutes": 60,
                    "dimension": "data",
                }
            ),
            "safety": "read_only",
        },
    ]
    report = {
        "conclusion": {"dimension": "dependency", "status": "provisional"},
        "hypothesis_results": [
            {"dimension": "dependency", "status": "provisional"},
            {"dimension": "code", "status": "untested"},
        ],
        "next_experiments": [{"dimension": "dependency"}],
    }

    first = diagnostic_probes.select_next_probe(probes, report)
    second = diagnostic_probes.select_next_probe(
        probes,
        report,
        attempted_probe_ids={"search-provider-log"},
    )

    assert first is not None
    assert first["probe_id"] == "search-provider-log"
    assert "tests_current_conclusion" in first["selection_reasons"]
    assert "kind_dimension_affinity" in first["selection_reasons"]
    assert second is not None
    assert second["probe_id"] == "inspect-repo"


def test_probe_selector_uses_structured_earliest_break_dimension():
    probes = [
        {
            **diagnostic_probes.normalize_probe_spec(
                {
                    "probe_id": "profile-state",
                    "kind": "db_profile",
                    "table": "trading_events",
                    "timestamp_column": "created_at",
                    "lookback_minutes": 60,
                    "dimension": "state",
                }
            ),
            "safety": "read_only",
        },
        {
            **diagnostic_probes.normalize_probe_spec(
                {
                    "probe_id": "inspect-code",
                    "kind": "repo_state",
                    "dimension": "code",
                }
            ),
            "safety": "read_only",
        },
    ]
    report = {
        "conclusion": {"dimension": "unknown", "status": "inconclusive"},
        "causal_timeline": {"earliest_break": {"dimension": "state"}},
    }

    selected = diagnostic_probes.select_next_probe(probes, report)

    assert selected is not None
    assert selected["probe_id"] == "profile-state"
    assert "tests_earliest_break" in selected["selection_reasons"]


def test_runtime_probe_evidence_cannot_retract_without_contrastive_intervention(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runtime_evidence,
        "execute_db_profile",
        lambda *_args, **_kwargs: {
            "status": "completed",
            "exit_code": 0,
            "output": json.dumps(
                {
                    "table": "trading_events",
                    "count": 500,
                    "groups": [{"value": "stale_pending", "count": 476}],
                    "transaction_read_only": True,
                }
            ),
            "duration_ms": 3,
        },
    )
    monkeypatch.setattr(
        runtime_evidence,
        "execute_log_search",
        lambda *_args, **_kwargs: {
            "status": "completed",
            "exit_code": 0,
            "output": json.dumps(
                {
                    "fixed_query": "queue depth",
                    "matches": [
                        {
                            "path": "logs/worker.log",
                            "tail_line": 12,
                            "text": "queue depth=500 reject stale_pending",
                        }
                    ],
                }
            ),
            "duration_ms": 2,
        },
    )
    probe_run = diagnostic_probes.execute_safe_probes(
        tmp_path,
        [
            {
                "probe_id": "queue-profile",
                "kind": "db_profile",
                "table": "trading_events",
                "timestamp_column": "created_at",
                "lookback_minutes": 60,
                "group_by": "reason",
                "safety": "read_only",
                "dimension": "state",
            },
            {
                "probe_id": "queue-log",
                "kind": "log_search",
                "query": "queue depth",
                "safety": "read_only",
                "dimension": "state",
            }
        ],
    )
    initial_case = {
        "case_id": "runtime-to-state",
        "problem_statement": "A worker stopped processing after deployment.",
        "observations": [
            {
                "evidence_id": "runtime-a",
                "statement": "The worker image changed before the stall.",
                "dimension": "runtime",
                "kind": "artifact",
                "provenance": "deploy:image",
                "independence_key": "deploy-image",
                "reliability": 0.95,
                "discriminating": True,
            },
            {
                "evidence_id": "runtime-b",
                "statement": "A restart temporarily changed the heartbeat.",
                "dimension": "runtime",
                "kind": "experiment",
                "provenance": "runtime:restart",
                "independence_key": "runtime-restart",
                "reliability": 0.9,
                "discriminating": True,
            },
        ],
    }
    runtime_packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-runtime",
                "claim": "The worker image caused the stall.",
                "dimension": "runtime",
                "support_evidence_ids": ["runtime-a", "runtime-b"],
                "falsification": "Hold queue state constant and revert only the image.",
            }
        ],
        "experiments": [],
        "conclusion": {"hypothesis_id": "h-runtime", "status": "confirmed"},
    }
    initial = diagnostic_reasoning.evaluate_packet(initial_case, runtime_packet)
    enriched = {
        **initial_case,
        "observations": [*initial_case["observations"], *probe_run["evidence"]],
    }
    state_packet = {
        "hypotheses": [
            {
                "hypothesis_id": "h-state",
                "claim": "A saturated stale queue caused the processing stall.",
                "dimension": "state",
                "support_evidence_ids": ["probe-queue-profile", "probe-queue-log"],
                "falsification": "Drain only stale pending work and observe throughput.",
            }
        ],
        "experiments": [],
        "conclusion": {"hypothesis_id": "h-state", "status": "confirmed"},
    }
    result = diagnostic_reasoning.run_local_diagnostic_debate(
        enriched,
        lambda _stage, _prompt: json.dumps(state_packet),
        stages_to_run=("judge",),
        previous_report=initial,
    )

    assert result["report"]["conclusion"]["dimension"] == "state"
    assert result["report"]["conclusion"]["status"] != "confirmed"
    assert result["report"]["retractions"] == []
    assert all(
        evidence["discriminating"] is False
        and evidence["causal_role"] == "context"
        and evidence["intervention_scope"] == "none"
        for evidence in probe_run["evidence"]
    )

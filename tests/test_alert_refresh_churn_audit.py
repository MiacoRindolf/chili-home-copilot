from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

from scripts import analyze_alert_refresh_churn as audit


def test_churn_audit_prints_all_sections_without_db(monkeypatch, capsys):
    calls: list[tuple[str, int, int | None]] = []

    def work_counts(hours: int):
        calls.append(("work", hours, None))
        return [{"event_type": "exit_variant_refresh", "status": "pending", "events": 2}]

    def diagnostic_counts(hours: int):
        calls.append(("diagnostics", hours, None))
        return [{"event_type": "exit_variant_diagnostic", "skip_reason": "no_loss_report"}]

    def top_patterns(hours: int, limit: int):
        calls.append(("patterns", hours, limit))
        return [{"scan_pattern_id": 123, "open_work": 1}]

    def top_noop_exit_patterns(hours: int, limit: int):
        calls.append(("noop", hours, limit))
        return [{"scan_pattern_id": 123, "noop_diagnostics": 1}]

    def top_noop_exit_pattern_rollups(hours: int, limit: int):
        calls.append(("noop_rollups", hours, limit))
        return [{"scan_pattern_id": 123, "distinct_fingerprints": 2}]

    def top_recert_rescue_blocker_rollups(hours: int, limit: int):
        calls.append(("recert_rollups", hours, limit))
        return [{"scan_pattern_id": 456, "blocker_diagnostics": 3}]

    def top_recert_rescue_action_rollups(hours: int, limit: int):
        calls.append(("recert_action_rollups", hours, limit))
        return [{"scan_pattern_id": 789, "action_diagnostics": 1}]

    def open_exit_work_with_recent_noop(hours: int, limit: int):
        calls.append(("open_noop", hours, limit))
        return [{"work_id": 77, "skip_reason": "no_loss_report"}]

    def open_recert_work_with_recent_blocker_diagnostic(hours: int, limit: int):
        calls.append(("open_recert", hours, limit))
        return [{"work_id": 88, "next_action": "wait_for_recert_backtest_cooldown_keep_live_blocked"}]

    def duplicate_open_refresh_work(hours: int, limit: int):
        calls.append(("duplicates", hours, limit))
        return [{"event_type": "recert_rescue_refresh", "open_work": 2}]

    def recent_duplicate_suppressions(hours: int, limit: int):
        calls.append(("suppressions", hours, limit))
        return [{"event_type": "recert_rescue_refresh", "suppressed": 1}]

    monkeypatch.setattr(audit, "_work_counts", work_counts)
    monkeypatch.setattr(audit, "_diagnostic_counts", diagnostic_counts)
    monkeypatch.setattr(audit, "_top_patterns", top_patterns)
    monkeypatch.setattr(audit, "_top_noop_exit_patterns", top_noop_exit_patterns)
    monkeypatch.setattr(
        audit,
        "_top_noop_exit_pattern_rollups",
        top_noop_exit_pattern_rollups,
    )
    monkeypatch.setattr(
        audit,
        "_top_recert_rescue_blocker_rollups",
        top_recert_rescue_blocker_rollups,
    )
    monkeypatch.setattr(
        audit,
        "_top_recert_rescue_action_rollups",
        top_recert_rescue_action_rollups,
    )
    monkeypatch.setattr(
        audit,
        "_open_exit_work_with_recent_noop",
        open_exit_work_with_recent_noop,
    )
    monkeypatch.setattr(
        audit,
        "_open_recert_work_with_recent_blocker_diagnostic",
        open_recert_work_with_recent_blocker_diagnostic,
    )
    monkeypatch.setattr(audit, "_duplicate_open_refresh_work", duplicate_open_refresh_work)
    monkeypatch.setattr(audit, "_recent_duplicate_suppressions", recent_duplicate_suppressions)
    monkeypatch.setattr(
        sys,
        "argv",
        ["analyze_alert_refresh_churn.py", "--hours", "6", "--limit", "4"],
    )

    assert audit.main() == 0

    out = capsys.readouterr().out
    assert "# alert-refresh-churn hours=6 limit=4" in out
    assert "## Alert Pressure Summary" in out
    assert "## Work Counts" in out
    assert "## Diagnostic Outcomes" in out
    assert "## Top Work-Producing Patterns" in out
    assert "## Top No-Op Exit Variant Diagnostics" in out
    assert "## Top No-Op Exit Variant Pattern Rollups" in out
    assert "## Top Recert Rescue Blocker Rollups" in out
    assert "## Top Recert Rescue Action Rollups" in out
    assert "## Open Exit Variant Work With Recent No-Op Evidence" in out
    assert "## Open Recert Work With Recent Blocker Diagnostic" in out
    assert "## Duplicate Open Refresh Work" in out
    assert "## Recent Duplicate Suppressions" in out
    assert calls == [
        ("work", 6, None),
        ("diagnostics", 6, None),
        ("patterns", 6, 4),
        ("noop", 6, 4),
        ("noop_rollups", 6, 4),
        ("recert_rollups", 6, 4),
        ("recert_action_rollups", 6, 4),
        ("open_noop", 6, 4),
        ("open_recert", 6, 4),
        ("duplicates", 6, 4),
        ("suppressions", 6, 4),
    ]


def test_alert_pressure_summary_separates_open_conflicts_from_history():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    report = {
        "work_counts": [
            {
                "event_type": "recert_rescue_refresh",
                "status": "done",
                "events": 25,
                "last_seen": now - timedelta(seconds=45),
            },
            {
                "event_type": "exit_variant_refresh",
                "status": "pending",
                "events": 2,
                "first_seen": now - timedelta(seconds=90),
            },
        ],
        "diagnostic_outcomes": [
            {
                "event_type": "recert_rescue_diagnostic",
                "events": 7,
                "last_seen": now - timedelta(seconds=30),
            },
            {
                "event_type": "exit_variant_diagnostic",
                "events": 3,
                "last_seen": now - timedelta(seconds=20),
            },
        ],
        "top_noop_exit_variant_pattern_rollups": [
            {
                "noop_diagnostics": 3,
                "last_seen": now - timedelta(seconds=25),
            },
            {
                "noop_diagnostics": 2,
                "last_seen": now - timedelta(minutes=45),
            },
        ],
        "top_recert_rescue_blocker_rollups": [
            {
                "blocker_diagnostics": 7,
                "last_seen": now - timedelta(seconds=35),
            },
            {
                "blocker_diagnostics": 4,
                "last_seen": now - timedelta(minutes=45),
            },
        ],
        "open_exit_variant_work_with_recent_noop": [
            {
                "work_id": 1,
                "work_created": now - timedelta(seconds=60),
            }
        ],
        "open_recert_work_with_recent_blocker_diagnostic": [],
        "duplicate_open_refresh_work": [{"scan_pattern_id": 123}],
        "recent_duplicate_suppressions": [
            {
                "suppressed": 4,
                "last_suppressed": now - timedelta(seconds=15),
            }
        ],
    }

    summary = audit._alert_pressure_summary(report)
    assert 90 <= summary.pop("oldest_open_work_age_seconds") <= 120
    assert 60 <= summary.pop("oldest_open_conflict_age_seconds") <= 120
    assert 15 <= summary.pop("latest_historical_noise_age_seconds") <= 45
    assert 25 <= summary.pop("latest_noop_exit_age_seconds") <= 55
    assert 35 <= summary.pop("latest_recert_blocker_age_seconds") <= 65
    assert summary == {
        "status": "attention",
        "pressure_mode": "actionable_conflict",
        "open_work_events": 2,
        "recert_open_work_events": 0,
        "exit_open_work_events": 2,
        "open_conflict_rows": 2,
        "completed_work_events": 25,
        "diagnostic_events": 10,
        "noop_exit_diagnostics": 5,
        "recert_blocker_diagnostics": 11,
        "duplicate_suppressions": 4,
        "historical_noise_events": 39,
        "fresh_signal_window_seconds": 1800,
        "fresh_noop_exit_rollups": 1,
        "fresh_recert_blocker_rollups": 1,
        "fresh_duplicate_suppression_groups": 1,
    }


def test_alert_pressure_summary_labels_historical_noise_without_attention():
    report = {
        "work_counts": [
            {
                "event_type": "recert_rescue_refresh",
                "status": "done",
                "events": 12,
                "last_seen": "2026-05-30T10:00:00",
            },
        ],
        "diagnostic_outcomes": [
            {
                "event_type": "recert_rescue_diagnostic",
                "events": 5,
                "last_seen": "2026-05-30T10:02:00",
            },
        ],
        "top_noop_exit_variant_pattern_rollups": [],
        "top_recert_rescue_blocker_rollups": [
            {
                "blocker_diagnostics": 5,
                "last_seen": "2026-05-30T10:01:00",
            },
        ],
        "open_exit_variant_work_with_recent_noop": [],
        "open_recert_work_with_recent_blocker_diagnostic": [],
        "duplicate_open_refresh_work": [],
        "recent_duplicate_suppressions": [{"suppressed": 2}],
    }

    summary = audit._alert_pressure_summary(report)
    assert summary.pop("latest_historical_noise_age_seconds") is not None
    assert summary.pop("latest_recert_blocker_age_seconds") is not None
    assert summary == {
        "status": "clear",
        "pressure_mode": "historical_noise",
        "open_work_events": 0,
        "recert_open_work_events": 0,
        "exit_open_work_events": 0,
        "open_conflict_rows": 0,
        "completed_work_events": 12,
        "diagnostic_events": 5,
        "noop_exit_diagnostics": 0,
        "recert_blocker_diagnostics": 5,
        "duplicate_suppressions": 2,
        "historical_noise_events": 19,
        "fresh_signal_window_seconds": 1800,
        "fresh_noop_exit_rollups": 0,
        "fresh_recert_blocker_rollups": 0,
        "fresh_duplicate_suppression_groups": 0,
        "oldest_open_work_age_seconds": None,
        "oldest_open_conflict_age_seconds": None,
        "latest_noop_exit_age_seconds": None,
    }


def test_alert_pressure_summary_ages_duplicate_only_conflicts():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    report = {
        "work_counts": [],
        "diagnostic_outcomes": [],
        "top_noop_exit_variant_pattern_rollups": [],
        "top_recert_rescue_blocker_rollups": [],
        "open_exit_variant_work_with_recent_noop": [],
        "open_recert_work_with_recent_blocker_diagnostic": [],
        "duplicate_open_refresh_work": [
            {
                "event_type": "recert_rescue_refresh",
                "open_work": 2,
                "oldest_open": now - timedelta(seconds=75),
            }
        ],
        "recent_duplicate_suppressions": [],
    }

    summary = audit._alert_pressure_summary(report)
    assert 75 <= summary.pop("oldest_open_conflict_age_seconds") <= 105
    assert summary["status"] == "attention"
    assert summary["pressure_mode"] == "actionable_conflict"
    assert summary["open_conflict_rows"] == 1
    assert summary["oldest_open_work_age_seconds"] is None


def test_churn_audit_handles_unavailable_database(monkeypatch, capsys):
    def unavailable(_hours: int):
        raise audit.DatabaseUnavailable("database system is starting up")

    monkeypatch.setattr(audit, "_work_counts", unavailable)
    monkeypatch.setattr(
        sys,
        "argv",
        ["analyze_alert_refresh_churn.py", "--hours", "1", "--limit", "3"],
    )

    assert audit.main() == 2

    captured = capsys.readouterr()
    assert "# alert-refresh-churn hours=1 limit=3" in captured.out
    assert "Database is not accepting read-only connections yet" in captured.err
    assert "database system is starting up" in captured.err


def test_churn_audit_json_output_without_db(monkeypatch, capsys):
    monkeypatch.setattr(
        audit,
        "_build_report",
        lambda hours, limit: {
            "hours": hours,
            "limit": limit,
            "work_counts": [{"event_type": "recert_rescue_refresh", "events": 3}],
            "diagnostic_outcomes": [],
            "top_work_producing_patterns": [],
            "top_noop_exit_variant_diagnostics": [],
            "top_noop_exit_variant_pattern_rollups": [],
            "top_recert_rescue_blocker_rollups": [],
            "top_recert_rescue_action_rollups": [],
            "open_exit_variant_work_with_recent_noop": [],
            "open_recert_work_with_recent_blocker_diagnostic": [],
            "duplicate_open_refresh_work": [],
            "recent_duplicate_suppressions": [],
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["analyze_alert_refresh_churn.py", "--hours", "12", "--limit", "5", "--json"],
    )

    assert audit.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["hours"] == 12
    assert payload["limit"] == 5
    assert payload["work_counts"][0]["event_type"] == "recert_rescue_refresh"


def test_churn_audit_json_flags_open_recert_blocker_work(monkeypatch, capsys):
    monkeypatch.setattr(
        audit,
        "_build_report",
        lambda hours, limit: {
            "hours": hours,
            "limit": limit,
            "work_counts": [],
            "diagnostic_outcomes": [],
            "top_work_producing_patterns": [],
            "top_noop_exit_variant_diagnostics": [],
            "top_noop_exit_variant_pattern_rollups": [],
            "top_recert_rescue_blocker_rollups": [],
            "top_recert_rescue_action_rollups": [],
            "open_exit_variant_work_with_recent_noop": [],
            "open_recert_work_with_recent_blocker_diagnostic": [{"work_id": 19144}],
            "duplicate_open_refresh_work": [],
            "recent_duplicate_suppressions": [],
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["analyze_alert_refresh_churn.py", "--hours", "12", "--limit", "5", "--json"],
    )

    assert audit.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["open_recert_work_with_recent_blocker_diagnostic"] == [
        {"work_id": 19144}
    ]


def test_churn_audit_json_unavailable_database(monkeypatch, capsys):
    def unavailable(_hours: int):
        raise audit.DatabaseUnavailable("database system is starting up")

    monkeypatch.setattr(audit, "_work_counts", unavailable)
    monkeypatch.setattr(
        sys,
        "argv",
        ["analyze_alert_refresh_churn.py", "--hours", "1", "--limit", "3", "--json"],
    )

    assert audit.main() == 2

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert payload["ok"] is False
    assert payload["error"] == "database_unavailable"
    assert payload["hours"] == 1
    assert payload["limit"] == 3
    assert payload["wait_seconds"] == 0
    assert "database system is starting up" in payload["detail"]


def test_churn_audit_waits_for_database(monkeypatch, capsys):
    calls = 0
    sleeps: list[float] = []

    def flaky_report(hours: int, limit: int):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise audit.DatabaseUnavailable("database system is starting up")
        return {
            "hours": hours,
            "limit": limit,
            "work_counts": [],
            "diagnostic_outcomes": [],
            "top_work_producing_patterns": [],
            "top_noop_exit_variant_diagnostics": [],
            "top_noop_exit_variant_pattern_rollups": [],
            "top_recert_rescue_blocker_rollups": [],
            "top_recert_rescue_action_rollups": [],
            "open_exit_variant_work_with_recent_noop": [],
            "open_recert_work_with_recent_blocker_diagnostic": [],
            "duplicate_open_refresh_work": [],
            "recent_duplicate_suppressions": [],
        }

    monkeypatch.setattr(audit, "_build_report", flaky_report)
    monkeypatch.setattr(audit.time, "sleep", lambda seconds: sleeps.append(seconds))
    times = iter([100.0, 100.0, 101.0])
    monkeypatch.setattr(audit.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "analyze_alert_refresh_churn.py",
            "--hours",
            "2",
            "--limit",
            "3",
            "--wait-seconds",
            "5",
            "--json",
        ],
    )

    assert audit.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["wait_seconds"] == 5
    assert calls == 2
    assert sleeps == [4.0]


def test_rows_configures_bounded_read_only_session(monkeypatch):
    executed: list[str] = []

    class FakeResult:
        def fetchall(self):
            return []

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, statement, params=None):
            executed.append(str(statement))
            return FakeResult()

        def rollback(self):
            executed.append("ROLLBACK")

    monkeypatch.setenv("CHILI_ALERT_REFRESH_CHURN_STATEMENT_TIMEOUT_MS", "3210")
    monkeypatch.setenv("CHILI_ALERT_REFRESH_CHURN_LOCK_TIMEOUT_MS", "210")
    monkeypatch.setattr(audit, "SessionLocal", lambda: FakeSession())

    assert audit._rows("SELECT 1", {}) == []

    assert executed == [
        "SET TRANSACTION READ ONLY",
        "SET LOCAL statement_timeout = 3210",
        "SET LOCAL lock_timeout = 210",
        "SET LOCAL application_name = 'chili-alert-refresh-churn-audit'",
        "SELECT 1",
        "ROLLBACK",
    ]


def test_read_only_guardrails_are_reported(monkeypatch):
    monkeypatch.setenv("CHILI_ALERT_REFRESH_CHURN_STATEMENT_TIMEOUT_MS", "4321")
    monkeypatch.setenv("CHILI_ALERT_REFRESH_CHURN_LOCK_TIMEOUT_MS", "654")
    monkeypatch.setattr(audit, "_work_counts", lambda hours: [])
    monkeypatch.setattr(audit, "_diagnostic_counts", lambda hours: [])
    monkeypatch.setattr(audit, "_top_patterns", lambda hours, limit: [])
    monkeypatch.setattr(audit, "_top_noop_exit_patterns", lambda hours, limit: [])
    monkeypatch.setattr(
        audit,
        "_top_noop_exit_pattern_rollups",
        lambda hours, limit: [],
    )
    monkeypatch.setattr(
        audit,
        "_top_recert_rescue_blocker_rollups",
        lambda hours, limit: [],
    )
    monkeypatch.setattr(
        audit,
        "_top_recert_rescue_action_rollups",
        lambda hours, limit: [],
    )
    monkeypatch.setattr(
        audit,
        "_open_exit_work_with_recent_noop",
        lambda hours, limit: [],
    )
    monkeypatch.setattr(
        audit,
        "_open_recert_work_with_recent_blocker_diagnostic",
        lambda hours, limit: [],
    )
    monkeypatch.setattr(audit, "_duplicate_open_refresh_work", lambda hours, limit: [])
    monkeypatch.setattr(audit, "_recent_duplicate_suppressions", lambda hours, limit: [])

    report = audit._build_report(hours=2, limit=3)

    assert report["read_only_guardrails"] == {
        "read_only": True,
        "statement_timeout_ms": 4321,
        "lock_timeout_ms": 654,
        "application_name": "chili-alert-refresh-churn-audit",
    }


def test_open_exit_noop_query_keeps_non_positive_skip_evidence_specific(monkeypatch):
    captured: dict[str, object] = {}

    def capture_rows(sql: str, params: dict):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(audit, "_rows", capture_rows)

    assert audit._open_exit_work_with_recent_noop(hours=3, limit=7) == []

    sql = str(captured["sql"])
    assert "d.evidence_fingerprint = w.evidence_fingerprint" in sql
    assert "d.skip_reason = ANY(:structural_exit_noop_reasons)" in sql
    assert "d.skip_reason LIKE ANY(:structural_exit_noop_prefixes)" in sql
    assert "d.skip_reason = ANY(:non_positive_exit_noop_reasons)" in sql
    assert "expected_evidence_value" in sql
    assert "calibrated_ev_after_cost_pct" in sql
    assert captured["params"] == {
        "hours": 3,
        "limit": 7,
        "structural_exit_noop_reasons": [
            "duplicate_learned_exit_label",
            "learned_stop_not_tighter_than_static",
            "learned_target_not_tighter_than_static",
            "max_active_variants",
            "missing_parent_payoff_geometry",
            "no_loss_report",
            "no_parent_returns",
            "non_positive_parent_realized_avg",
            "parent_missing_or_inactive",
        ],
        "structural_exit_noop_prefixes": [
            "edge_debt_too_negative_for_exit_child:%",
            "insufficient_parent_payoff_samples:%",
            "reward_risk_below_floor:%",
        ],
        "non_positive_exit_noop_reasons": [
            "negative_ev_no_exit_variant_birth",
            "non_positive_quality_evidence_no_exit_variant_birth",
        ],
    }


def test_top_noop_exit_patterns_include_asset_slice(monkeypatch):
    captured: dict[str, object] = {}

    def capture_rows(sql: str, params: dict):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(audit, "_rows", capture_rows)

    assert audit._top_noop_exit_patterns(hours=5, limit=8) == []

    sql = str(captured["sql"])
    assert "event_type = 'exit_variant_diagnostic'" in sql
    assert "COALESCE(e.payload->>'asset_class', '<none>') AS asset_class" in sql
    assert "GROUP BY" in sql
    assert "COALESCE(e.payload->>'asset_class', '<none>')" in sql
    assert captured["params"] == {"hours": 5, "limit": 8}


def test_top_noop_exit_pattern_rollups_fold_fingerprints(monkeypatch):
    captured: dict[str, object] = {}

    def capture_rows(sql: str, params: dict):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(audit, "_rows", capture_rows)

    assert audit._top_noop_exit_pattern_rollups(hours=5, limit=8) == []

    sql = str(captured["sql"])
    assert "event_type = 'exit_variant_diagnostic'" in sql
    assert "GROUP BY" in sql
    assert "n.scan_pattern_id" in sql
    assert "n.asset_class" in sql
    assert "n.skip_reason" in sql
    assert "count(DISTINCT n.evidence_fingerprint)" in sql
    assert "distinct_fingerprints" in sql
    assert "created_count" in sql
    assert captured["params"] == {"hours": 5, "limit": 8}


def test_top_recert_rescue_blocker_rollups_fold_repeated_actions(monkeypatch):
    captured: dict[str, object] = {}

    def capture_rows(sql: str, params: dict):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(audit, "_rows", capture_rows)

    assert audit._top_recert_rescue_blocker_rollups(hours=5, limit=8) == []

    sql = str(captured["sql"])
    assert "event_type = 'recert_rescue_diagnostic'" in sql
    assert "GROUP BY" in sql
    assert "r.scan_pattern_id" in sql
    assert "r.recert_status" in sql
    assert "r.next_action" in sql
    assert "r.source" in sql
    assert "r.asset_class" in sql
    assert "recert_backtest_refresh,asset_class" in sql
    assert "recommended_next_action" in sql
    assert "recert_backtest_refresh,reason" in sql
    assert "recert_backtest_refresh,requested" in sql
    assert "blocker_diagnostics" in sql
    assert captured["params"] == {
        "hours": 5,
        "limit": 8,
        "recert_actions": [
            "complete_oos_recert_and_quality_refresh",
            "keep_live_blocked_until_hard_recert_clears",
            "no_recert_action_needed",
            "inspect_recert_backtest_no_oos_evidence_keep_live_blocked",
            "wait_for_recert_backtest_cooldown_keep_live_blocked",
            "live_blocked_recert_debt_no_refresh",
        ],
        "recert_reasons": [
            "recent_recert_backtest_cooldown",
            "recert_backtest_refresh_already_open",
            "no_recert_refresh_needed",
        ],
        "conditional_action": "run_recert_backtest_refresh_keep_live_blocked",
    }


def test_top_recert_rescue_action_rollups_include_run_refresh_actions(monkeypatch):
    captured: dict[str, object] = {}

    def capture_rows(sql: str, params: dict):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(audit, "_rows", capture_rows)

    assert audit._top_recert_rescue_action_rollups(hours=5, limit=8) == []

    sql = str(captured["sql"])
    assert "event_type = 'recert_rescue_diagnostic'" in sql
    assert "GROUP BY" in sql
    assert "r.scan_pattern_id" in sql
    assert "r.recert_status" in sql
    assert "r.next_action" in sql
    assert "r.source" in sql
    assert "r.asset_class" in sql
    assert "recert_backtest_refresh,asset_class" in sql
    assert "action_diagnostics" in sql
    assert "recommended_next_action" in sql
    assert "run_recert_backtest_refresh_keep_live_blocked" not in sql
    assert captured["params"] == {"hours": 5, "limit": 8}


def test_open_recert_query_reports_wait_or_inspect_actions(monkeypatch):
    captured: dict[str, object] = {}

    def capture_rows(sql: str, params: dict):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(audit, "_rows", capture_rows)

    assert audit._open_recert_work_with_recent_blocker_diagnostic(hours=4, limit=9) == []

    sql = str(captured["sql"])
    assert "event_type = 'recert_rescue_refresh'" in sql
    assert "event_type = 'recert_rescue_diagnostic'" in sql
    assert "recommended_next_action" in sql
    assert "recert_backtest_refresh,reason" in sql
    assert "recert_backtest_refresh,requested" in sql
    assert captured["params"] == {
        "hours": 4,
        "limit": 9,
        "recert_actions": [
            "complete_oos_recert_and_quality_refresh",
            "keep_live_blocked_until_hard_recert_clears",
            "no_recert_action_needed",
            "inspect_recert_backtest_no_oos_evidence_keep_live_blocked",
            "wait_for_recert_backtest_cooldown_keep_live_blocked",
            "live_blocked_recert_debt_no_refresh",
        ],
        "recert_reasons": [
            "recent_recert_backtest_cooldown",
            "recert_backtest_refresh_already_open",
            "no_recert_refresh_needed",
        ],
        "conditional_action": "run_recert_backtest_refresh_keep_live_blocked",
    }


def test_duplicate_open_refresh_work_groups_refresh_churn(monkeypatch):
    captured: dict[str, object] = {}

    def capture_rows(sql: str, params: dict):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(audit, "_rows", capture_rows)

    assert audit._duplicate_open_refresh_work(hours=8, limit=11) == []

    sql = str(captured["sql"])
    assert "event_type = ANY(:event_types)" in sql
    assert "status IN ('pending', 'retry_wait', 'processing')" in sql
    assert "status," in sql
    assert "GROUP BY" in sql
    assert "COALESCE(open_work.payload->>'asset_class', '<none>')" in sql
    assert "COALESCE(open_work.payload->>'source', '<none>')" in sql
    assert "HAVING count(*) > 1" in sql
    assert "count(DISTINCT open_work.dedupe_key)" in sql
    assert captured["params"] == {
        "event_types": ["recert_rescue_refresh", "exit_variant_refresh"],
        "hours": 8,
        "limit": 11,
    }


def test_work_counts_groups_by_payload_expression(monkeypatch):
    captured: dict[str, object] = {}

    def capture_rows(sql: str, params: dict):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(audit, "_rows", capture_rows)

    assert audit._work_counts(hours=2) == []

    sql = str(captured["sql"])
    assert "GROUP BY event_type, status, COALESCE(payload->>'source', '<none>')" in sql
    assert captured["params"] == {
        "event_types": ["recert_rescue_refresh", "exit_variant_refresh"],
        "hours": 2,
    }


def test_recent_duplicate_suppressions_reports_coalescing_effect(monkeypatch):
    captured: dict[str, object] = {}

    def capture_rows(sql: str, params: dict):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(audit, "_rows", capture_rows)

    assert audit._recent_duplicate_suppressions(hours=12, limit=6) == []

    sql = str(captured["sql"])
    assert "duplicate_open_work_suppressed_reason" in sql
    assert "duplicate_open_work_suppressed" in sql
    assert "event_type = ANY(:event_types)" in sql
    assert captured["params"] == {
        "event_types": ["recert_rescue_refresh", "exit_variant_refresh"],
        "hours": 12,
        "limit": 6,
    }

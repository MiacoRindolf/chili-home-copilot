from __future__ import annotations

import json
import sys

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

    def open_exit_work_with_recent_noop(hours: int, limit: int):
        calls.append(("open_noop", hours, limit))
        return [{"work_id": 77, "skip_reason": "no_loss_report"}]

    monkeypatch.setattr(audit, "_work_counts", work_counts)
    monkeypatch.setattr(audit, "_diagnostic_counts", diagnostic_counts)
    monkeypatch.setattr(audit, "_top_patterns", top_patterns)
    monkeypatch.setattr(audit, "_top_noop_exit_patterns", top_noop_exit_patterns)
    monkeypatch.setattr(
        audit,
        "_open_exit_work_with_recent_noop",
        open_exit_work_with_recent_noop,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["analyze_alert_refresh_churn.py", "--hours", "6", "--limit", "4"],
    )

    assert audit.main() == 0

    out = capsys.readouterr().out
    assert "# alert-refresh-churn hours=6 limit=4" in out
    assert "## Work Counts" in out
    assert "## Diagnostic Outcomes" in out
    assert "## Top Work-Producing Patterns" in out
    assert "## Top No-Op Exit Variant Diagnostics" in out
    assert "## Open Exit Variant Work With Recent No-Op Evidence" in out
    assert calls == [
        ("work", 6, None),
        ("diagnostics", 6, None),
        ("patterns", 6, 4),
        ("noop", 6, 4),
        ("open_noop", 6, 4),
    ]


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
            "open_exit_variant_work_with_recent_noop": [],
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
    assert "database system is starting up" in payload["detail"]


def test_open_exit_noop_query_keeps_non_positive_skip_evidence_specific(monkeypatch):
    captured: dict[str, object] = {}

    def capture_rows(sql: str, params: dict):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(audit, "_rows", capture_rows)

    assert audit._open_exit_work_with_recent_noop(hours=3, limit=7) == []

    sql = str(captured["sql"])
    structural_clause = sql.split("d.skip_reason IN", 1)[1].split(")", 1)[0]
    assert "d.evidence_fingerprint = w.evidence_fingerprint" in sql
    assert "non_positive_quality_evidence_no_exit_variant_birth" not in structural_clause
    assert captured["params"] == {"hours": 3, "limit": 7}

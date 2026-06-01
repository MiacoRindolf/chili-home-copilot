from __future__ import annotations

from datetime import date

from app.config import settings
from app.services.trading.pattern_survival.decisions import (
    demote_policy,
    run_pattern_survival_demote_pass,
)


class _Result:
    def __init__(self, rows=None, row=None):
        self.rows = rows or []
        self.row = row

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.row


class _Session:
    def __init__(self):
        self.sqls: list[str] = []
        self.params: list[dict] = []
        self.commits = 0
        self.rollbacks = 0
        self.streak_updates: dict[int, int] = {}
        self.lifecycle_updates: dict[int, str] = {}
        self.pattern_rows = [
            (101, "promoted", 0),
            (102, "pilot_promoted", 1),
        ]

    def execute(self, stmt, params=None):
        sql = str(stmt)
        p = dict(params or {})
        self.sqls.append(sql)
        self.params.append(p)

        if "FROM scan_patterns" in sql and "ORDER BY id" in sql:
            return _Result(rows=self.pattern_rows)
        if "FROM pattern_survival_predictions" in sql:
            pid = int(p["p"])
            return _Result(row=(0.2, f"model-{pid}", date(2026, 5, 1), False, 0.5))
        if "SET survival_at_risk_streak_days" in sql:
            self.streak_updates[int(p["p"])] = int(p["s"])
        if "SET lifecycle_stage = :ls" in sql:
            self.lifecycle_updates[int(p["p"])] = str(p["ls"])
        return _Result()

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_demote_pass_inspects_promoted_and_pilot_lifecycles(monkeypatch) -> None:
    db = _Session()
    monkeypatch.setattr(settings, "chili_pattern_survival_classifier_enabled", True)
    monkeypatch.setattr(settings, "chili_pattern_survival_decisions_enabled", False)
    monkeypatch.setattr(settings, "chili_pattern_survival_demote_enabled", False)

    out = run_pattern_survival_demote_pass(db)

    assert out["inspected"] == 2
    assert out["streak_updated"] == 2
    assert out["demoted"] == 0
    assert db.streak_updates == {101: 1, 102: 2}
    enum_sql = db.sqls[0]
    assert "'live'" in enum_sql
    assert "'challenged'" in enum_sql
    assert "'promoted'" in enum_sql
    assert "'pilot_promoted'" in enum_sql


def test_demote_policy_derisks_promoted_and_pilot_to_challenged() -> None:
    for stage in ("promoted", "pilot_promoted"):
        out = demote_policy(
            p=0.2,
            current_lifecycle=stage,
            current_streak=1,
            threshold=0.3,
            streak_required=3,
        )

        assert out["proposed_lifecycle"] == "challenged"
        assert out["streak_days_after"] == 2
        assert out["would_apply"] is True


def test_demote_pass_applies_low_survival_to_promoted_and_pilot(monkeypatch) -> None:
    db = _Session()
    monkeypatch.setattr(settings, "chili_pattern_survival_classifier_enabled", True)
    monkeypatch.setattr(settings, "chili_pattern_survival_decisions_enabled", False)
    monkeypatch.setattr(settings, "chili_pattern_survival_demote_enabled", True)

    out = run_pattern_survival_demote_pass(db)

    assert out["inspected"] == 2
    assert out["demoted"] == 2
    assert db.streak_updates == {101: 1, 102: 2}
    assert db.lifecycle_updates == {101: "challenged", 102: "challenged"}

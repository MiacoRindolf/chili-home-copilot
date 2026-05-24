from __future__ import annotations

from typing import Any

from app.services.trading.momentum_neural import repair_plan as rp


def test_momentum_truth_repair_plan_sequences_dry_run_stages(monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []

    def _record(name: str, payload: dict[str, Any]):
        def _inner(_db, **kwargs):
            calls.append((name, dict(kwargs)))
            return {"ok": True, "dry_run": True, **payload}

        return _inner

    monkeypatch.setattr(
        rp,
        "evolution_credit_diagnostics",
        _record(
            "credit",
            {
                "total": 10,
                "credited": 3,
                "blocked": 7,
                "credit_rate": 0.3,
                "reingest_required": 1,
                "recommended_repairs": [{"reason_code": "economic_ledger_parity_missing", "n": 4}],
            },
        ),
    )
    monkeypatch.setattr(
        rp,
        "repair_packet_snapshot_seals",
        _record("packet_snapshots", {"candidate_count": 2}),
    )
    monkeypatch.setattr(
        rp,
        "repair_automation_ledger_packet_links",
        _record("automation_ledger_links", {"candidate_count": 3}),
    )
    monkeypatch.setattr(
        rp,
        "reconcile_missing_automation_outcome_parity",
        _record("automation_parity", {"candidate_count": 5}),
    )
    monkeypatch.setattr(
        rp,
        "regrade_momentum_outcome_evolution_credit",
        _record("regrade", {"candidate_count": 4, "upgraded_to_training_grade": 4}),
    )
    monkeypatch.setattr(
        rp,
        "reingest_regraded_momentum_outcomes",
        _record("reingest", {"candidate_count": 6}),
    )

    out = rp.momentum_truth_repair_plan(
        object(),
        days=45,
        user_id=7,
        limit=123,
    )

    assert out["ok"] is True
    assert out["mode"] == "dry_run_plan"
    assert out["trade_count_impact"] == "none"
    assert out["policy_effect"] == "read_only_no_execution_change"
    assert out["window_days"] == 45
    assert out["lookback_hours"] == 45 * 24
    assert out["limit"] == 123
    assert out["user_id"] == 7
    assert out["credit"]["blocked"] == 7
    assert out["credit"]["recommended_repairs"][0]["reason_code"] == "economic_ledger_parity_missing"
    assert out["summary"] == {
        "prerequisite_repair_candidates": 10,
        "training_grade_upgrades_ready_now": 4,
        "neural_reingest_ready_now": 6,
        "actionable_stage_count": 5,
    }
    assert [stage["stage"] for stage in out["sequence"]] == [
        "packet_snapshot_seals",
        "automation_ledger_packet_links",
        "automation_ledger_parity",
        "evolution_credit_regrade",
        "evolution_reingest",
    ]
    assert [stage["actionable_count"] for stage in out["sequence"]] == [2, 3, 5, 4, 6]

    call_map = {name: kwargs for name, kwargs in calls}
    assert call_map["credit"] == {"days": 45, "user_id": 7, "limit": 123}
    assert call_map["packet_snapshots"] == {
        "lookback_hours": 45 * 24,
        "user_id": 7,
        "limit": 123,
        "dry_run": True,
    }
    assert call_map["automation_ledger_links"]["dry_run"] is True
    assert call_map["automation_parity"] == {
        "days": 45,
        "user_id": 7,
        "limit": 123,
        "dry_run": True,
    }
    assert call_map["regrade"]["dry_run"] is True
    assert call_map["reingest"]["dry_run"] is True

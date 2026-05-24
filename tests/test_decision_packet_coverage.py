from __future__ import annotations

from datetime import datetime

import pytest

from app.services.trading.decision_packet_coverage import (
    decision_packet_coverage_summary,
    repair_automation_ledger_packet_links,
    repair_directional_outcome_packet_links,
    repair_packet_snapshot_seals,
    repair_trade_packet_links_from_proposals,
)
from app.models.trading import TradingDecisionPacket


class _Result:
    def __init__(self, payload):
        self._payload = payload

    def mappings(self):
        return self

    def one(self):
        assert isinstance(self._payload, dict)
        return self._payload

    def all(self):
        if isinstance(self._payload, list):
            return self._payload
        return [self._payload]


class _FakeDb:
    def __init__(self, rows: dict[str, dict] | None = None, fail_on: str | None = None):
        self.rows = rows or {}
        self.fail_on = fail_on
        self.calls: list[tuple[str, dict]] = []
        self.objects: dict[tuple[type, int], object] = {}
        self.commits = 0
        self.rollbacks = 0

    def execute(self, clause, params):
        sql = str(clause)
        self.calls.append((sql, dict(params or {})))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("simulated query failure")
        if "repair_candidates:directional_outcomes" in sql:
            return _Result(self.rows.get("repair_candidates_directional_outcomes", []))
        if "repair_apply:directional_outcomes" in sql:
            return _Result(self.rows.get("repair_apply_directional_outcomes", []))
        if "repair_candidates:automation_ledger" in sql:
            return _Result(self.rows.get("repair_candidates_automation_ledger", []))
        if "repair_apply:automation_ledger" in sql:
            return _Result(self.rows.get("repair_apply_automation_ledger", []))
        if "repair_candidates:trade_packets" in sql:
            return _Result(self.rows.get("repair_candidates_trade_packets", []))
        if "repair_apply:trade_packets" in sql:
            return _Result(self.rows.get("repair_apply_trade_packets", []))
        if "repair_candidates:packet_snapshots" in sql:
            return _Result(self.rows.get("repair_candidates_packet_snapshots", []))
        if "coverage_examples:alerts_signal" in sql:
            return _Result(self.rows.get("examples_alerts_signal", []))
        if "coverage_examples:directional_outcomes" in sql:
            return _Result(self.rows.get("examples_directional_outcomes", []))
        if "coverage_examples:trade_packets" in sql:
            return _Result(self.rows.get("examples_trade_packets", []))
        if "coverage_examples:automation_entry_fills" in sql:
            return _Result(self.rows.get("examples_automation_entry_fills", []))
        if "coverage_examples:economic_ledger_fills" in sql:
            return _Result(self.rows.get("examples_economic_ledger_fills", []))
        if "coverage_examples:packet_snapshots" in sql:
            return _Result(self.rows.get("examples_packet_snapshots", []))
        if "GROUP BY a.alert_type" in sql:
            return _Result(self.rows.get("alerts_by_type", []))
        if "FROM trading_alerts a" in sql and "pattern_alert_directional_outcome" not in sql:
            return _Result(self.rows.get("alerts", {}))
        if "FROM pattern_alert_directional_outcome p" in sql:
            return _Result(self.rows.get("directional_outcomes", {}))
        if "FROM trading_trades t" in sql:
            return _Result(self.rows.get("trade_packets", {}))
        if "FROM trading_automation_simulated_fills" in sql:
            return _Result(self.rows.get("automation_entry_fills", {}))
        if "GROUP BY e.source, e.event_type" in sql:
            return _Result(self.rows.get("economic_ledger_by_source_event", []))
        if "FROM trading_economic_ledger e" in sql:
            return _Result(self.rows.get("economic_ledger_fills", {}))
        if "FROM trading_decision_packets p" in sql:
            return _Result(self.rows.get("packet_snapshots", {}))
        raise AssertionError(f"unexpected SQL: {sql}")

    def get(self, model, id_):
        return self.objects.get((model, int(id_)))

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_decision_packet_coverage_summary_reports_soft_lineage_gaps():
    db = _FakeDb(
        {
            "alerts": {
                "total": 10,
                "linked": 8,
                "signal_total": 4,
                "signal_linked": 3,
            },
            "directional_outcomes": {"total": 5, "linked": 5},
            "trade_packets": {"total": 2, "linked": 1},
            "automation_entry_fills": {"total": 0, "linked": 0},
            "economic_ledger_fills": {"total": 6, "linked": 4},
            "alerts_by_type": [
                {"alert_type": "pattern_breakout_imminent", "total": 3, "linked": 2},
                {"alert_type": "strategy_proposed", "total": 1, "linked": 1},
            ],
            "economic_ledger_by_source_event": [
                {"source": "automation", "event_type": "entry_fill", "total": 4, "linked": 3},
                {"source": "automation", "event_type": "exit_fill", "total": 2, "linked": 1},
            ],
            "examples_alerts_signal": [
                {
                    "alert_id": 10,
                    "alert_type": "pattern_breakout_imminent",
                    "ticker": "AAPL",
                    "scan_pattern_id": 7,
                    "created_at": datetime(2026, 5, 24, 12, 0, 0),
                }
            ],
            "examples_trade_packets": [
                {
                    "trade_id": 99,
                    "ticker": "MSFT",
                    "broker_source": "manual",
                    "status": "open",
                    "entry_date": datetime(2026, 5, 24, 12, 5, 0),
                }
            ],
            "examples_economic_ledger_fills": [
                {
                    "ledger_event_id": 33,
                    "source": "automation",
                    "event_type": "entry_fill",
                    "ticker": "BTC-USD",
                    "trade_id": -12,
                    "paper_trade_id": None,
                    "created_at": datetime(2026, 5, 24, 12, 10, 0),
                }
            ],
            "packet_snapshots": {"total": 9, "linked": 9},
        }
    )

    out = decision_packet_coverage_summary(db, lookback_hours=72, user_id=42, example_limit=2)

    assert out["ok"] is True
    assert out["mode"] == "audit_only"
    assert out["lookback_hours"] == 72
    assert out["user_id"] == 42
    assert out["status"] == "red"
    assert out["surfaces"]["alerts_all"]["coverage"] == pytest.approx(0.8)
    assert out["surfaces"]["alerts_all"]["status"] == "yellow"
    assert out["surfaces"]["alerts_signal"]["coverage"] == pytest.approx(0.75)
    assert out["surfaces"]["alerts_signal"]["status"] == "red"
    assert out["surfaces"]["directional_outcomes"]["status"] == "green"
    assert out["surfaces"]["automation_entry_fills"]["status"] == "unknown"
    assert out["surfaces"]["economic_ledger_fills"]["coverage"] == pytest.approx(0.6667)
    assert out["surfaces"]["economic_ledger_fills"]["status"] == "red"
    alert_breakdown = out["breakdowns"]["alerts_signal_by_type"][0]
    assert alert_breakdown["alert_type"] == "pattern_breakout_imminent"
    assert alert_breakdown["coverage"] == pytest.approx(0.6667)
    ledger_breakdown = out["breakdowns"]["economic_ledger_fills_by_source_event"][1]
    assert ledger_breakdown["source"] == "automation"
    assert ledger_breakdown["event_type"] == "exit_fill"
    assert ledger_breakdown["missing"] == 1
    fixes = out["recommended_next_fixes"]
    assert any(
        f["surface"] == "trade_packets" and f["missing"] == 1
        for f in fixes
    )
    assert any(
        f["surface"] == "economic_ledger_fills" and f["key"] == "automation:entry_fill"
        for f in fixes
    )
    assert any(
        f["surface"] == "alerts_signal" and f["key"] == "pattern_breakout_imminent"
        for f in fixes
    )
    assert out["missing_examples"]["alerts_signal"][0]["alert_id"] == 10
    assert out["missing_examples"]["alerts_signal"][0]["created_at"].endswith("Z")
    assert out["missing_examples"]["economic_ledger_fills"][0]["ledger_event_id"] == 33
    assert any(call_params.get("example_limit") == 2 for _, call_params in db.calls)
    assert all(call_params["user_id"] == 42 for _, call_params in db.calls if "user_id" in call_params)


def test_decision_packet_coverage_summary_is_partial_when_a_table_is_missing():
    db = _FakeDb(
        {
            "alerts": {
                "total": 1,
                "linked": 1,
                "signal_total": 1,
                "signal_linked": 1,
            },
            "trade_packets": {"total": 0, "linked": 0},
            "automation_entry_fills": {"total": 0, "linked": 0},
            "economic_ledger_fills": {"total": 0, "linked": 0},
            "alerts_by_type": [],
            "economic_ledger_by_source_event": [],
            "examples_alerts_signal": [],
            "examples_trade_packets": [],
            "examples_automation_entry_fills": [],
            "examples_economic_ledger_fills": [],
            "examples_packet_snapshots": [],
            "packet_snapshots": {"total": 1, "linked": 1},
        },
        fail_on="pattern_alert_directional_outcome",
    )

    out = decision_packet_coverage_summary(db)

    assert out["ok"] is False
    assert "directional_outcomes" in out["errors"]
    assert out["surfaces"]["alerts_signal"]["status"] == "green"
    assert out["surfaces"]["packet_snapshots"]["coverage"] == pytest.approx(1.0)
    assert out["breakdowns"]["alerts_signal_by_type"] == []
    assert out["missing_examples"]["alerts_signal"] == []
    assert out["recommended_next_fixes"] == []


def test_repair_directional_outcome_packet_links_dry_run_lists_candidates():
    db = _FakeDb(
        {
            "repair_candidates_directional_outcomes": [
                {
                    "outcome_id": 22,
                    "alert_id": 11,
                    "ticker": "AAPL",
                    "scan_pattern_id": 7,
                    "decision_packet_id": 123,
                    "evaluated_at": datetime(2026, 5, 24, 13, 0, 0),
                }
            ]
        }
    )

    out = repair_directional_outcome_packet_links(
        db,
        lookback_hours=720,
        user_id=42,
        limit=5,
        dry_run=True,
    )

    assert out["ok"] is True
    assert out["dry_run"] is True
    assert out["candidate_count"] == 1
    assert out["applied_count"] == 0
    assert out["candidates"][0]["outcome_id"] == 22
    assert out["candidates"][0]["evaluated_at"].endswith("Z")
    assert db.commits == 0
    assert any(call_params.get("user_id") == 42 for _, call_params in db.calls)


def test_repair_directional_outcome_packet_links_apply_updates_candidates():
    db = _FakeDb(
        {
            "repair_candidates_directional_outcomes": [
                {
                    "outcome_id": 22,
                    "alert_id": 11,
                    "ticker": "AAPL",
                    "scan_pattern_id": 7,
                    "decision_packet_id": 123,
                    "evaluated_at": datetime(2026, 5, 24, 13, 0, 0),
                }
            ],
            "repair_apply_directional_outcomes": [
                {
                    "outcome_id": 22,
                    "alert_id": 11,
                    "ticker": "AAPL",
                    "scan_pattern_id": 7,
                    "decision_packet_id": 123,
                    "evaluated_at": datetime(2026, 5, 24, 13, 0, 0),
                }
            ],
        }
    )

    out = repair_directional_outcome_packet_links(
        db,
        lookback_hours=720,
        limit=5,
        dry_run=False,
    )

    assert out["ok"] is True
    assert out["dry_run"] is False
    assert out["candidate_count"] == 1
    assert out["applied_count"] == 1
    assert out["applied"][0]["decision_packet_id"] == 123
    assert db.commits == 1
    assert db.rollbacks == 0


def test_repair_automation_ledger_packet_links_dry_run_lists_candidates():
    db = _FakeDb(
        {
            "repair_candidates_automation_ledger": [
                {
                    "ledger_event_id": 44,
                    "source": "automation",
                    "event_type": "entry_fill",
                    "ticker": "BTC-USD",
                    "trade_id": -12,
                    "session_id": 12,
                    "decision_packet_id": 321,
                    "created_at": datetime(2026, 5, 24, 14, 0, 0),
                }
            ]
        }
    )

    out = repair_automation_ledger_packet_links(
        db,
        lookback_hours=720,
        user_id=42,
        limit=5,
        dry_run=True,
    )

    assert out["ok"] is True
    assert out["dry_run"] is True
    assert out["candidate_count"] == 1
    assert out["applied_count"] == 0
    assert out["candidates"][0]["ledger_event_id"] == 44
    assert out["candidates"][0]["created_at"].endswith("Z")
    assert db.commits == 0
    assert any(call_params.get("user_id") == 42 for _, call_params in db.calls)


def test_repair_automation_ledger_packet_links_apply_updates_candidates():
    db = _FakeDb(
        {
            "repair_candidates_automation_ledger": [
                {
                    "ledger_event_id": 44,
                    "source": "automation",
                    "event_type": "entry_fill",
                    "ticker": "BTC-USD",
                    "trade_id": -12,
                    "session_id": 12,
                    "decision_packet_id": 321,
                    "created_at": datetime(2026, 5, 24, 14, 0, 0),
                }
            ],
            "repair_apply_automation_ledger": [
                {
                    "ledger_event_id": 44,
                    "source": "automation",
                    "event_type": "entry_fill",
                    "ticker": "BTC-USD",
                    "trade_id": -12,
                    "session_id": 12,
                    "decision_packet_id": 321,
                    "created_at": datetime(2026, 5, 24, 14, 0, 0),
                }
            ],
        }
    )

    out = repair_automation_ledger_packet_links(
        db,
        lookback_hours=720,
        limit=5,
        dry_run=False,
    )

    assert out["ok"] is True
    assert out["dry_run"] is False
    assert out["candidate_count"] == 1
    assert out["applied_count"] == 1
    assert out["applied"][0]["decision_packet_id"] == 321
    assert db.commits == 1
    assert db.rollbacks == 0


def test_repair_trade_packet_links_from_proposals_dry_run_lists_candidates():
    db = _FakeDb(
        {
            "repair_candidates_trade_packets": [
                {
                    "trade_id": 77,
                    "ticker": "AAPL",
                    "strategy_proposal_id": 55,
                    "decision_packet_id": 444,
                    "entry_date": datetime(2026, 5, 24, 15, 0, 0),
                }
            ]
        }
    )

    out = repair_trade_packet_links_from_proposals(
        db,
        lookback_hours=720,
        user_id=42,
        limit=5,
        dry_run=True,
    )

    assert out["ok"] is True
    assert out["dry_run"] is True
    assert out["candidate_count"] == 1
    assert out["applied_count"] == 0
    assert out["candidates"][0]["trade_id"] == 77
    assert out["candidates"][0]["entry_date"].endswith("Z")
    assert db.commits == 0
    assert any(call_params.get("user_id") == 42 for _, call_params in db.calls)


def test_repair_trade_packet_links_from_proposals_apply_updates_candidates():
    db = _FakeDb(
        {
            "repair_candidates_trade_packets": [
                {
                    "trade_id": 77,
                    "ticker": "AAPL",
                    "strategy_proposal_id": 55,
                    "decision_packet_id": 444,
                    "entry_date": datetime(2026, 5, 24, 15, 0, 0),
                }
            ],
            "repair_apply_trade_packets": [
                {
                    "decision_packet_id": 444,
                    "trade_id": 77,
                    "ticker": "AAPL",
                    "strategy_proposal_id": 55,
                    "entry_date": datetime(2026, 5, 24, 15, 0, 0),
                }
            ],
        }
    )

    out = repair_trade_packet_links_from_proposals(
        db,
        lookback_hours=720,
        limit=5,
        dry_run=False,
    )

    assert out["ok"] is True
    assert out["dry_run"] is False
    assert out["candidate_count"] == 1
    assert out["applied_count"] == 1
    assert out["applied"][0]["trade_id"] == 77
    assert db.commits == 1
    assert db.rollbacks == 0


def test_repair_packet_snapshot_seals_dry_run_lists_candidates():
    db = _FakeDb(
        {
            "repair_candidates_packet_snapshots": [
                {
                    "decision_packet_id": 555,
                    "chosen_ticker": "AAPL",
                    "source_surface": "alert_pattern_breakout",
                    "outcome_status": "pending",
                    "created_at": datetime(2026, 5, 24, 16, 0, 0),
                }
            ]
        }
    )

    out = repair_packet_snapshot_seals(
        db,
        lookback_hours=720,
        user_id=42,
        limit=5,
        dry_run=True,
    )

    assert out["ok"] is True
    assert out["dry_run"] is True
    assert out["candidate_count"] == 1
    assert out["applied_count"] == 0
    assert out["candidates"][0]["decision_packet_id"] == 555
    assert out["candidates"][0]["created_at"].endswith("Z")
    assert db.commits == 0
    assert any(call_params.get("user_id") == 42 for _, call_params in db.calls)


def test_repair_packet_snapshot_seals_apply_uses_canonical_sealer():
    pkt = TradingDecisionPacket(
        id=555,
        created_at=datetime(2026, 5, 24, 16, 0, 0),
        user_id=42,
        chosen_ticker="AAPL",
        decision_type="manual_signal",
        execution_mode="shadow",
        deployment_stage="promoted",
        source_surface="alert_pattern_breakout",
        outcome_status="pending",
        shadow_advisory_only=True,
        allocator_input_json={},
        research_vs_live_context_json={},
    )
    db = _FakeDb(
        {
            "repair_candidates_packet_snapshots": [
                {
                    "decision_packet_id": 555,
                    "chosen_ticker": "AAPL",
                    "source_surface": "alert_pattern_breakout",
                    "outcome_status": "pending",
                    "created_at": datetime(2026, 5, 24, 16, 0, 0),
                }
            ]
        }
    )
    db.objects[(TradingDecisionPacket, 555)] = pkt

    out = repair_packet_snapshot_seals(
        db,
        lookback_hours=720,
        limit=5,
        dry_run=False,
    )

    assert out["ok"] is True
    assert out["dry_run"] is False
    assert out["candidate_count"] == 1
    assert out["applied_count"] == 1
    assert out["applied"][0]["decision_packet_id"] == 555
    assert len(out["applied"][0]["fingerprint_sha256"]) == 64
    assert pkt.allocator_input_json["decision_snapshot"]["snapshot_id"] == out["applied"][0]["snapshot_id"]
    assert pkt.research_vs_live_context_json["decision_snapshot"]["snapshot_id"] == out["applied"][0]["snapshot_id"]
    assert db.commits == 1
    assert db.rollbacks == 0

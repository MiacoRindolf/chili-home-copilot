from __future__ import annotations

from datetime import datetime

from app.models.trading import MomentumAutomationOutcome
from app.services.trading.momentum_neural import feedback_query


class _TrackedOutcome:
    def __init__(
        self,
        *,
        row_id: int,
        contributes: bool,
        reason_codes: list[str] | None = None,
        mode: str = "paper",
        requires_reingest: bool = False,
        contribution_applied: bool = False,
    ) -> None:
        self.id = row_id
        self.session_id = 100 + row_id
        self.symbol = "SOL-USD"
        self.variant_id = 7
        self.execution_family = "impulse"
        self.mode = mode
        self.terminal_state = "closed"
        self.outcome_class = "strategy_exit"
        self.realized_pnl_usd = 1.25
        self.return_bps = 12.5
        self.hold_seconds = 60
        self.exit_reason = "target"
        self.evidence_weight = 1.0
        self.governance_context_json = {"governance": "ok"}
        self.terminal_at = datetime(2026, 6, 1, 12, 0)
        self.created_at = datetime(2026, 6, 1, 12, row_id)
        self._summary = {
            "evolution_credit": {
                "contributes_to_evolution": contributes,
                "reason_codes": reason_codes or [],
            },
            "evolution_credit_regrade_v1": {
                "requires_reingest": requires_reingest,
                "reingested_at_utc": None,
            },
            "evolution_ingest_v1": {
                "contribution_applied_at_utc": "2026-06-01T12:00:00Z" if contribution_applied else None,
            },
        }
        self._contributes = contributes
        self.contrib_reads = 0
        self.summary_reads = 0

    @property
    def contributes_to_evolution(self) -> bool:
        self.contrib_reads += 1
        return self._contributes

    @property
    def extracted_summary_json(self) -> dict:
        self.summary_reads += 1
        return self._summary


class _FakeQuery:
    def __init__(self, rows: list[_TrackedOutcome]) -> None:
        self.rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, _limit: int):
        return self

    def all(self) -> list[_TrackedOutcome]:
        return self.rows

    def one_or_none(self) -> _TrackedOutcome | None:
        return self.rows[0] if self.rows else None


class _FakeDb:
    def __init__(self, rows: list[_TrackedOutcome]) -> None:
        self.rows = rows

    def query(self, model):
        assert model is MomentumAutomationOutcome
        return _FakeQuery(self.rows)


def test_evolution_credit_diagnostics_counts_credit_in_existing_row_pass(monkeypatch) -> None:
    rows = [
        _TrackedOutcome(row_id=1, contributes=True, mode="paper"),
        _TrackedOutcome(row_id=2, contributes=False, reason_codes=["missing_entry_decision_packet"], mode="live"),
        _TrackedOutcome(row_id=3, contributes=False, mode="paper"),
    ]
    monkeypatch.setattr(feedback_query, "momentum_outcomes_table_present", lambda _db: True)

    out = feedback_query.evolution_credit_diagnostics(_FakeDb(rows), days=30, limit=10)

    assert out["total"] == 3
    assert out["credited"] == 1
    assert out["blocked"] == 2
    assert out["reason_counts"] == [
        {"reason_code": "missing_entry_decision_packet", "n": 1},
        {"reason_code": "credit_reason_missing", "n": 1},
    ]
    assert rows[0].contrib_reads == 1
    assert rows[1].contrib_reads == 1
    assert rows[2].contrib_reads == 1
    assert [row.summary_reads for row in rows] == [1, 1, 1]


def test_outcome_brief_reuses_loaded_summary_for_reingest_state() -> None:
    row = _TrackedOutcome(row_id=4, contributes=True, requires_reingest=True)

    brief = feedback_query._outcome_brief(row)

    assert brief["contributes_to_evolution"] is True
    assert brief["reingest_required"] is True
    assert brief["evolution_credit_regrade"]["requires_reingest"] is True
    assert row.contrib_reads == 1
    assert row.summary_reads == 1


def test_outcome_brief_respects_applied_ingest_marker() -> None:
    row = _TrackedOutcome(row_id=5, contributes=True, requires_reingest=True, contribution_applied=True)

    brief = feedback_query._outcome_brief(row)

    assert brief["reingest_required"] is False
    assert row.contrib_reads == 1
    assert row.summary_reads == 1


def test_session_feedback_row_reuses_raw_summary(monkeypatch) -> None:
    row = _TrackedOutcome(row_id=6, contributes=True, requires_reingest=True)
    monkeypatch.setattr(feedback_query, "momentum_outcomes_table_present", lambda _db: True)

    out = feedback_query.get_session_feedback_row(_FakeDb([row]), session_id=106)

    assert out is not None
    assert out["session_id"] == 106
    assert out["extracted_summary_json"] is row._summary
    assert out["reingest_required"] is True
    assert row.contrib_reads == 1
    assert row.summary_reads == 1


def test_symbol_variant_feedback_summary_reuses_paper_live_slice(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    def fake_symbol_agg(_db, **kwargs):
        calls.append(("symbol", kwargs))
        return {"n": 3, "mean_return_bps": 12.0}

    def fake_slices(_db, **kwargs):
        calls.append(("slices", kwargs))
        return {
            "paper": {"n": 2, "mean_return_bps": 10.0},
            "live": {"n": 1, "mean_return_bps": -20.0},
        }

    monkeypatch.setattr(feedback_query, "aggregate_recent_outcomes_for_symbol_variant", fake_symbol_agg)
    monkeypatch.setattr(feedback_query, "paper_vs_live_performance_slices", fake_slices)

    out = feedback_query.get_symbol_variant_feedback_summary(object(), symbol=" sol-usd ", variant_id=7, days=21)

    assert calls == [
        ("symbol", {"symbol": "SOL-USD", "variant_id": 7, "days": 21}),
        ("slices", {"variant_id": 7, "days": 21}),
    ]
    assert out == {
        "symbol": "SOL-USD",
        "variant_id": 7,
        "symbol_variant_window": {"n": 3, "mean_return_bps": 12.0},
        "variant_paper_slice": {"n": 2, "mean_return_bps": 10.0},
        "variant_live_slice": {"n": 1, "mean_return_bps": -20.0},
    }

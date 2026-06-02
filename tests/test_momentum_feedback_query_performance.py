from __future__ import annotations

from datetime import datetime

from app.models.trading import MomentumAutomationOutcome, MomentumStrategyVariant
from app.services.trading.momentum_neural import feedback_query


class _FakeBindDb:
    bind = object()


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
    def __init__(self, rows: list) -> None:
        self.rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, _limit: int):
        return self

    def all(self) -> list:
        return self.rows

    def one_or_none(self) -> _TrackedOutcome | None:
        return self.rows[0] if self.rows else None


class _FakeDb:
    def __init__(self, rows: list[_TrackedOutcome]) -> None:
        self.rows = rows
        self.full_outcome_queries = 0
        self.full_variant_queries = 0
        self.variant_column_queries = 0
        self.recent_list_column_queries = 0
        self.diagnostic_column_queries = 0
        self.session_column_queries = 0

    def query(self, *models):
        if len(models) == 1 and models[0] is MomentumAutomationOutcome:
            self.full_outcome_queries += 1
            return _FakeQuery(self.rows)
        if len(models) == 1 and models[0] is MomentumStrategyVariant:
            self.full_variant_queries += 1
            return _FakeQuery([_tracked_variant_object()])
        if all(model is not MomentumAutomationOutcome for model in models) and len(models) == 6:
            self.variant_column_queries += 1
            return _FakeQuery([_tracked_variant_column_tuple()])
        assert all(model is not MomentumAutomationOutcome for model in models)
        if len(models) == 18:
            self.session_column_queries += 1
            return _FakeQuery([_tracked_session_column_tuple(row) for row in self.rows])
        if len(models) == 17:
            self.recent_list_column_queries += 1
            return _FakeQuery([_tracked_outcome_column_tuple(row) for row in self.rows])
        if len(models) == 9:
            self.diagnostic_column_queries += 1
            return _FakeQuery([_tracked_diagnostic_column_tuple(row) for row in self.rows])
        raise AssertionError(f"unexpected query column count: {len(models)}")


def _tracked_variant_object():
    class _Variant:
        id = 7
        family = "impulse"
        variant_key = "impulse"
        version = 3
        label = "Impulse v3"
        execution_family = "momentum"

    return _Variant()


def _tracked_variant_column_tuple() -> tuple:
    variant = _tracked_variant_object()
    return (
        variant.id,
        variant.family,
        variant.variant_key,
        variant.version,
        variant.label,
        variant.execution_family,
    )


def _tracked_outcome_column_tuple(row: _TrackedOutcome) -> tuple:
    return (
        row.id,
        row.session_id,
        row.symbol,
        row.variant_id,
        row.execution_family,
        row.mode,
        row.terminal_state,
        row.outcome_class,
        row.realized_pnl_usd,
        row.return_bps,
        row.hold_seconds,
        row.exit_reason,
        row.evidence_weight,
        row._contributes,
        row._summary,
        row.terminal_at,
        row.created_at,
    )


def _tracked_diagnostic_column_tuple(row: _TrackedOutcome) -> tuple:
    return (
        row.id,
        row.session_id,
        row.symbol,
        row.mode,
        row.execution_family,
        row.outcome_class,
        row._contributes,
        row._summary,
        row.terminal_at,
    )


def _tracked_session_column_tuple(row: _TrackedOutcome) -> tuple:
    return (
        row.id,
        row.session_id,
        row.symbol,
        row.variant_id,
        row.execution_family,
        row.mode,
        row.terminal_state,
        row.outcome_class,
        row.realized_pnl_usd,
        row.return_bps,
        row.hold_seconds,
        row.exit_reason,
        row.evidence_weight,
        row._contributes,
        row._summary,
        row.terminal_at,
        row.created_at,
        row.governance_context_json,
    )


def test_momentum_outcomes_table_present_uses_targeted_has_table(monkeypatch) -> None:
    class _Inspector:
        def __init__(self) -> None:
            self.has_table_calls: list[str] = []

        def has_table(self, name: str) -> bool:
            self.has_table_calls.append(name)
            return name == "momentum_automation_outcomes"

        def get_table_names(self):
            raise AssertionError("full table-name scan should not be used")

    inspector = _Inspector()
    monkeypatch.setattr(feedback_query, "sa_inspect", lambda _bind: inspector)

    assert feedback_query.momentum_outcomes_table_present(_FakeBindDb()) is True
    assert inspector.has_table_calls == ["momentum_automation_outcomes"]


def test_momentum_outcomes_table_present_keeps_table_list_fallback(monkeypatch) -> None:
    class _Inspector:
        def get_table_names(self):
            return ["users", "momentum_automation_outcomes"]

    monkeypatch.setattr(feedback_query, "sa_inspect", lambda _bind: _Inspector())

    assert feedback_query.momentum_outcomes_table_present(_FakeBindDb()) is True


def test_evolution_credit_diagnostics_counts_credit_in_existing_row_pass(monkeypatch) -> None:
    rows = [
        _TrackedOutcome(row_id=1, contributes=True, mode="paper"),
        _TrackedOutcome(row_id=2, contributes=False, reason_codes=["missing_entry_decision_packet"], mode="live"),
        _TrackedOutcome(row_id=3, contributes=False, mode="paper"),
    ]
    db = _FakeDb(rows)
    monkeypatch.setattr(feedback_query, "momentum_outcomes_table_present", lambda _db: True)

    out = feedback_query.evolution_credit_diagnostics(db, days=30, limit=10)

    assert out["total"] == 3
    assert out["credited"] == 1
    assert out["blocked"] == 2
    assert out["reason_counts"] == [
        {"reason_code": "missing_entry_decision_packet", "n": 1},
        {"reason_code": "credit_reason_missing", "n": 1},
    ]
    assert db.full_outcome_queries == 0
    assert db.diagnostic_column_queries == 1
    assert [row.contrib_reads for row in rows] == [0, 0, 0]
    assert [row.summary_reads for row in rows] == [0, 0, 0]


def test_list_recent_momentum_outcomes_uses_column_rows(monkeypatch) -> None:
    rows = [
        _TrackedOutcome(row_id=7, contributes=True, requires_reingest=True),
        _TrackedOutcome(row_id=8, contributes=False, reason_codes=["missing_entry_decision_packet"]),
    ]
    db = _FakeDb(rows)
    monkeypatch.setattr(feedback_query, "momentum_outcomes_table_present", lambda _db: True)

    out = feedback_query.list_recent_momentum_outcomes(db, limit=10, symbol=" sol-usd ")

    assert [row["session_id"] for row in out] == [107, 108]
    assert out[0]["reingest_required"] is True
    assert out[1]["evolution_credit_reason_codes"] == ["missing_entry_decision_packet"]
    assert db.full_outcome_queries == 0
    assert db.recent_list_column_queries == 1
    assert [row.contrib_reads for row in rows] == [0, 0]
    assert [row.summary_reads for row in rows] == [0, 0]


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
    db = _FakeDb([row])
    monkeypatch.setattr(feedback_query, "momentum_outcomes_table_present", lambda _db: True)

    out = feedback_query.get_session_feedback_row(db, session_id=106)

    assert out is not None
    assert out["session_id"] == 106
    assert out["extracted_summary_json"] is row._summary
    assert out["governance_context_json"] == {"governance": "ok"}
    assert out["reingest_required"] is True
    assert db.full_outcome_queries == 0
    assert db.session_column_queries == 1
    assert row.contrib_reads == 0
    assert row.summary_reads == 0


def test_variant_feedback_summary_uses_variant_column_row(monkeypatch) -> None:
    db = _FakeDb([])
    monkeypatch.setattr(
        feedback_query,
        "paper_vs_live_performance_slices",
        lambda _db, **kwargs: {"variant_id": kwargs["variant_id"], "paper": {}, "live": {}},
    )
    monkeypatch.setattr(
        feedback_query,
        "evolution_summary_for_operator",
        lambda _db, **kwargs: {"variant_id": kwargs["variant_id"], "trace": []},
    )

    out = feedback_query.get_variant_feedback_summary(db, variant_id=7, days=14)

    assert out["variant"] == {
        "id": 7,
        "family": "impulse",
        "strategy_family": "impulse",
        "variant_key": "impulse",
        "version": 3,
        "label": "Impulse v3",
        "execution_family": "momentum",
    }
    assert out["paper_vs_live"] == {"variant_id": 7, "paper": {}, "live": {}}
    assert out["evolution"] == {"variant_id": 7, "trace": []}
    assert db.full_variant_queries == 0
    assert db.variant_column_queries == 1


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

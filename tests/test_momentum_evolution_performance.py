from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from app.models.trading import MomentumAutomationOutcome, MomentumStrategyVariant, MomentumSymbolViability
from app.services.trading.momentum_neural import evolution
from app.services.trading.momentum_neural.evolution import (
    _aggregate_from_stats_row,
    _aggregate_rows_by_mode,
    _aggregate_stats_rows_by_mode,
    _outcome_return_stats,
    _return_bps_stats_from_row,
)


class _TrackingOutcomes:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        return iter(self.rows)


def test_outcome_return_stats_single_pass_with_null_returns() -> None:
    rows = _TrackingOutcomes(
        [
            None,
            (Decimal("12.5"),),
            SimpleNamespace(return_bps=-42.5),
            SimpleNamespace(return_bps=0.0),
            SimpleNamespace(return_bps=10.0),
        ]
    )

    n, wins, mean_bps = _outcome_return_stats(rows)

    assert rows.iterations == 1
    assert n == 4
    assert wins == 2
    assert mean_bps == -5.0


class _FakeQuery:
    def __init__(self, result):
        self.result = result

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def group_by(self, *_args, **_kwargs):
        return self

    def limit(self, _limit: int):
        return self

    def one_or_none(self):
        if isinstance(self.result, list):
            return self.result[0] if self.result else None
        return self.result

    def all(self):
        return self.result


def _aggregate_outcome_rows(rows: list[SimpleNamespace]) -> SimpleNamespace:
    n = 0
    w_sum = 0.0
    wrb = 0.0
    wp = 0.0
    gr = 0
    for row in rows:
        evidence_weight, return_bps, realized_pnl_usd, outcome_class = row
        n += 1
        weight = float(evidence_weight or 1.0)
        w_sum += weight
        if return_bps is not None:
            wrb += float(return_bps) * weight
        if realized_pnl_usd is not None:
            wp += float(realized_pnl_usd) * weight
        if outcome_class in ("governance_exit", "risk_block", "stale_data_abort"):
            gr += 1
    return SimpleNamespace(
        n=n,
        weight_sum=w_sum,
        weighted_return_bps_sum=wrb,
        weighted_pnl_sum=wp,
        governance_or_risk_count=gr,
    )


def _aggregate_outcome_rows_by_mode(rows: list[SimpleNamespace]) -> list[SimpleNamespace]:
    grouped = {
        "paper": [],
        "live": [],
    }
    for row in rows:
        mode = str(row[0] or "").lower()
        if mode in grouped:
            grouped[mode].append(row[1:])
    out = []
    for mode, bucket in grouped.items():
        stats = _aggregate_outcome_rows(bucket)
        if stats.n:
            out.append(SimpleNamespace(mode=mode, **stats.__dict__))
    return out


class _FakeDb:
    def __init__(self, outcome_rows: list[SimpleNamespace] | None = None) -> None:
        self.variant = SimpleNamespace(is_active=True, updated_at=None)
        self.viability_rows = [
            SimpleNamespace(
                paper_eligible=True,
                live_eligible=True,
                evidence_window_json={},
                explain_json={},
                viability_score=0.5,
                updated_at=None,
            )
        ]
        self.outcome_rows = outcome_rows or []
        self.outcome_query_count = 0
        self.refinement_column_query_count = 0
        self.return_bps_query_count = 0
        self.return_bps_stats_query_count = 0
        self.paper_live_column_query_count = 0
        self.paper_live_aggregate_query_count = 0
        self.aggregate_column_query_count = 0

    def query(self, *models):
        if len(models) == 1 and models[0] is MomentumStrategyVariant:
            return _FakeQuery(self.variant)
        if len(models) == 1 and models[0] is MomentumSymbolViability:
            return _FakeQuery(self.viability_rows)
        if len(models) == 1 and models[0] is MomentumAutomationOutcome:
            self.outcome_query_count += 1
            return _FakeQuery(self.outcome_rows)
        if (
            len(models) == 3
            and models[0] is MomentumAutomationOutcome.return_bps
            and models[1] is MomentumAutomationOutcome.hold_seconds
            and models[2] is MomentumAutomationOutcome.mode
        ):
            self.refinement_column_query_count += 1
            return _FakeQuery(self.outcome_rows)
        if len(models) == 1 and models[0] is MomentumAutomationOutcome.return_bps:
            self.return_bps_query_count += 1
            return _FakeQuery(self.outcome_rows)
        if len(models) == 3:
            self.return_bps_stats_query_count += 1
            n, wins, mean_bps = evolution._return_bps_column_stats(self.outcome_rows)
            return _FakeQuery(SimpleNamespace(n=n, wins=wins, mean_bps=mean_bps))
        if (
            len(models) == 6
            and models[0] is MomentumAutomationOutcome.mode
            and models[1] is not MomentumAutomationOutcome.evidence_weight
        ):
            self.paper_live_aggregate_query_count += 1
            return _FakeQuery(_aggregate_outcome_rows_by_mode(self.outcome_rows))
        if (
            len(models) == 5
            and models[0] is MomentumAutomationOutcome.mode
            and models[1] is MomentumAutomationOutcome.evidence_weight
            and models[2] is MomentumAutomationOutcome.return_bps
            and models[3] is MomentumAutomationOutcome.realized_pnl_usd
            and models[4] is MomentumAutomationOutcome.outcome_class
        ):
            self.paper_live_column_query_count += 1
            return _FakeQuery(self.outcome_rows)
        if len(models) == 5 and models[0] is not MomentumAutomationOutcome.mode:
            self.aggregate_column_query_count += 1
            return _FakeQuery(_aggregate_outcome_rows(self.outcome_rows))
        raise AssertionError(f"unexpected query models {models!r}")


def test_maybe_kill_underperforming_variant_uses_return_only_stats(monkeypatch) -> None:
    db = _FakeDb(
        [
            (None,),
            (-80.0,),
            (-70.0,),
            (-60.0,),
            (-50.0,),
            (-40.0,),
        ]
    )
    feedback: list[dict] = []
    monkeypatch.setattr(evolution, "record_feedback_ingestion_trace", lambda _db, payload: feedback.append(payload))

    out = evolution.maybe_kill_underperforming_variant(db, variant_id=22)

    assert out["killed"] is True
    assert out["win_rate"] == 0.0
    assert out["mean_return_bps"] == -60.0
    assert db.variant.is_active is False
    assert db.viability_rows[0].paper_eligible is False
    assert db.viability_rows[0].live_eligible is False
    assert db.outcome_query_count == 0
    assert db.return_bps_query_count == 0
    assert db.return_bps_stats_query_count == 1
    assert feedback[0]["sample_size"] == 5


def test_maybe_pause_symbol_variant_after_losses_uses_return_only_rows() -> None:
    db = _FakeDb([(-12.0,), (-20.0,), (-5.0,)])
    outcome = SimpleNamespace(symbol=" sol-usd ", variant_id=7)

    evolution.maybe_pause_symbol_variant_after_losses(db, outcome)

    assert db.outcome_query_count == 0
    assert db.return_bps_query_count == 1
    assert db.viability_rows[0].explain_json["variant_symbol_pause_until_utc"]
    assert db.viability_rows[0].updated_at is not None


def test_return_bps_stats_from_row_handles_tuple_object_and_empty() -> None:
    assert _return_bps_stats_from_row((4, 2, -5.0)) == (4, 2, -5.0)
    assert _return_bps_stats_from_row(SimpleNamespace(n=3, wins=1, mean_bps=12.5)) == (3, 1, 12.5)
    assert _return_bps_stats_from_row((0, 0, None)) == (0, 0, None)
    assert _return_bps_stats_from_row(None) == (0, 0, None)


def test_recent_refinement_outcomes_use_column_only_rows() -> None:
    rows = [
        SimpleNamespace(return_bps=20.0, hold_seconds=120, mode="paper"),
        SimpleNamespace(return_bps=-40.0, hold_seconds=240, mode="live"),
        SimpleNamespace(return_bps=30.0, hold_seconds=180, mode="paper"),
        SimpleNamespace(return_bps=45.0, hold_seconds=150, mode="live"),
    ]
    db = _FakeDb(rows)

    out = evolution._recent_outcomes_for_variant(db, variant_id=7)
    refined, meta = evolution.refine_strategy_params({"entry_viability_min": 0.52}, out)

    assert db.outcome_query_count == 0
    assert db.refinement_column_query_count == 1
    assert len(out) == 4
    assert meta["sample_size"] == 4
    assert "entry_viability_min" in refined


def test_aggregate_rows_by_mode_separates_paper_and_live_once() -> None:
    rows = _TrackingOutcomes(
        [
            ("paper", 1.0, 20.0, 2.0, ""),
            SimpleNamespace(mode="live", evidence_weight=2.0, return_bps=-30.0, realized_pnl_usd=-4.0, outcome_class="risk_block"),
            SimpleNamespace(mode="paper", evidence_weight=3.0, return_bps=40.0, realized_pnl_usd=None, outcome_class=""),
            ("shadow", 1.0, 999.0, 999.0, "risk_block"),
        ]
    )

    by_mode = _aggregate_rows_by_mode(rows)

    assert rows.iterations == 1
    assert by_mode["paper"]["n"] == 2
    assert by_mode["paper"]["weighted_return_bps_sum"] == 140.0
    assert by_mode["paper"]["mean_return_bps"] == 35.0
    assert by_mode["live"]["n"] == 1
    assert by_mode["live"]["weighted_return_bps_sum"] == -60.0
    assert by_mode["live"]["weighted_pnl_sum"] == -8.0
    assert by_mode["live"]["governance_or_risk_count"] == 1


def test_recent_outcome_aggregates_use_metric_column_queries() -> None:
    db = _FakeDb(
        [
            (1.0, 20.0, 2.0, ""),
            (2.0, -30.0, -4.0, "risk_block"),
        ]
    )

    variant = evolution.aggregate_recent_outcomes_for_variant(db, variant_id=7, days=14, mode="paper")
    symbol = evolution.aggregate_recent_outcomes_for_symbol_variant(db, symbol=" sol-usd ", variant_id=7, days=14)

    assert db.outcome_query_count == 0
    assert db.aggregate_column_query_count == 2
    assert variant["n"] == 2
    assert variant["weight_sum"] == 3.0
    assert variant["weighted_return_bps_sum"] == -40.0
    assert variant["weighted_pnl_sum"] == -6.0
    assert variant["governance_or_risk_count"] == 1
    assert symbol == variant


def test_aggregate_from_stats_row_handles_tuple_object_and_empty() -> None:
    assert _aggregate_from_stats_row((2, 3.0, -40.0, -6.0, 1)) == {
        "n": 2,
        "weighted_return_bps_sum": -40.0,
        "weighted_pnl_sum": -6.0,
        "weight_sum": 3.0,
        "mean_return_bps": -13.3333,
        "governance_or_risk_count": 1,
    }
    assert _aggregate_from_stats_row(
        SimpleNamespace(
            n=1,
            weight_sum=2.0,
            weighted_return_bps_sum=10.0,
            weighted_pnl_sum=4.0,
            governance_or_risk_count=0,
        )
    )["mean_return_bps"] == 5.0
    assert _aggregate_from_stats_row(None)["n"] == 0


def test_aggregate_stats_rows_by_mode_handles_tuple_object_and_empty() -> None:
    by_mode = _aggregate_stats_rows_by_mode(
        [
            ("paper", 2, 3.0, 140.0, 2.0, 0),
            SimpleNamespace(
                mode="live",
                n=1,
                weight_sum=2.0,
                weighted_return_bps_sum=-60.0,
                weighted_pnl_sum=-8.0,
                governance_or_risk_count=1,
            ),
            ("shadow", 1, 1.0, 999.0, 999.0, 1),
        ]
    )

    assert by_mode["paper"]["n"] == 2
    assert by_mode["paper"]["mean_return_bps"] == 46.6667
    assert by_mode["live"]["n"] == 1
    assert by_mode["live"]["governance_or_risk_count"] == 1
    assert _aggregate_stats_rows_by_mode([])["paper"]["n"] == 0


def test_paper_vs_live_performance_slices_queries_outcomes_once() -> None:
    db = _FakeDb(
        [
            ("paper", 1.0, 20.0, 2.0, ""),
            ("live", 1.0, -80.0, -8.0, "risk_block"),
        ]
    )

    out = evolution.paper_vs_live_performance_slices(db, variant_id=7, days=14)

    assert db.outcome_query_count == 0
    assert db.paper_live_column_query_count == 0
    assert db.paper_live_aggregate_query_count == 1
    assert out["variant_id"] == 7
    assert out["paper"]["n"] == 1
    assert out["paper"]["mean_return_bps"] == 20.0
    assert out["live"]["n"] == 1
    assert out["live"]["mean_return_bps"] == -80.0
    assert out["live_sample_caution"] is True


def test_apply_outcome_feedback_uses_single_paper_live_query() -> None:
    db = _FakeDb(
        [
            ("paper", 1.0, 40.0, 4.0, ""),
            ("live", 1.0, -90.0, -9.0, "risk_block"),
        ]
    )
    outcome = SimpleNamespace(
        symbol="sol-usd",
        variant_id=7,
        mode="live",
        evidence_weight=1.0,
        return_bps=-90.0,
        outcome_class="risk_block",
        session_id=123,
        terminal_at=datetime(2026, 6, 1, 12, 30),
    )

    evolution.apply_outcome_feedback_to_viability(db, outcome)

    via = db.viability_rows[0]
    assert db.outcome_query_count == 0
    assert db.paper_live_column_query_count == 0
    assert db.paper_live_aggregate_query_count == 1
    assert via.updated_at is not None
    live = via.evidence_window_json["neural_feedback_v1"]["live"]
    assert live["n"] == 1
    assert live["weighted_return_bps_sum"] == -90.0
    assert live["hint"] == "caution_tiny_live_sample"

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import inspect as python_inspect

import pytest
from sqlalchemy import inspect as sqlalchemy_inspect

from app.db import engine
from app.models.trading import MomentumStrategyVariant, MomentumSymbolViability
from app.services.trading.momentum_neural import (
    captured_paper_initial_candidate_reader as reader_module,
)
from app.services.trading.momentum_neural import (
    captured_paper_initial_admission as initial,
)
from app.services.trading.momentum_neural.captured_paper_initial_provider import (
    CapturedPaperInitialCandidateReadPort,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 16, 13, 0, 0, 123456, tzinfo=UTC)


def _variant(
    row_id: int,
    *,
    active: bool,
    key: str,
) -> MomentumStrategyVariant:
    return MomentumStrategyVariant(
        id=row_id,
        family="captured_paper",
        variant_key=key,
        version=1,
        label=f"Candidate {key}",
        params_json={"setup_family": "first_dip", "key": key},
        is_active=active,
        execution_family="alpaca_spot",
        parent_variant_id=None,
        refinement_meta_json={"source": "candidate-reader-test"},
        scan_pattern_id=None,
        created_at=NOW.replace(tzinfo=None),
        updated_at=NOW.replace(tzinfo=None),
    )


def _viability(
    row_id: int,
    *,
    variant_id: int,
    symbol: str = "CAND",
    scope: str = "symbol",
    paper_eligible: bool,
    live_eligible: bool,
) -> MomentumSymbolViability:
    return MomentumSymbolViability(
        id=row_id,
        symbol=symbol,
        scope=scope,
        variant_id=variant_id,
        viability_score=0.73,
        paper_eligible=paper_eligible,
        live_eligible=live_eligible,
        freshness_ts=(NOW - timedelta(seconds=1)).replace(tzinfo=None),
        regime_snapshot_json={"regime": "momentum"},
        execution_readiness_json={"captured": True},
        explain_json={"source": "candidate-reader-test"},
        evidence_window_json={"coverage": "complete"},
        source_node_id="candidate_reader_test",
        correlation_id=f"candidate-{row_id}",
        created_at=NOW.replace(tzinfo=None),
        updated_at=NOW.replace(tzinfo=None),
    )


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class _FakeQuery:
    def __init__(self, rows, *, failure=None):
        self.rows = list(rows)
        self.failure = failure
        self.join_calls = []
        self.filter_calls = []
        self.order_calls = []

    def join(self, *args):
        self.join_calls.append(args)
        return self

    def filter(self, *args):
        self.filter_calls.append(args)
        return self

    def order_by(self, *args):
        self.order_calls.append(args)
        return self

    def all(self):
        if self.failure is not None:
            raise self.failure
        return list(self.rows)


class _FakeSession:
    def __init__(self, rows, *, read_at=NOW, query_failure=None):
        self.query_object = _FakeQuery(rows, failure=query_failure)
        self.read_at = read_at
        self.execute_calls = []
        self.query_calls = []
        self.expunge_calls = []
        self.rollback_calls = 0
        self.close_calls = 0

    def execute(self, statement, parameters=None):
        sql = str(statement)
        self.execute_calls.append((sql, dict(parameters or {})))
        if "LEAST(transaction_timestamp()" in sql:
            return _ScalarResult(self.read_at)
        return _ScalarResult(None)

    def query(self, *entities):
        self.query_calls.append(entities)
        return self.query_object

    def expunge(self, row):
        self.expunge_calls.append(row)

    def rollback(self):
        self.rollback_calls += 1

    def close(self):
        self.close_calls += 1


def _install_fake_session(monkeypatch, fake):
    calls = []

    def build_session(*, bind, expire_on_commit):
        assert bind is engine
        assert expire_on_commit is False
        calls.append((bind, expire_on_commit))
        return fake

    monkeypatch.setattr(reader_module, "Session", build_session)
    return calls


def test_reader_is_strict_read_only_protocol_and_detaches_complete_ordered_set(
    monkeypatch,
):
    inactive = _variant(11, active=False, key="inactive")
    active = _variant(7, active=True, key="active")
    inactive_viability = _viability(
        110,
        variant_id=inactive.id,
        paper_eligible=False,
        live_eligible=False,
    )
    active_viability = _viability(
        70,
        variant_id=active.id,
        paper_eligible=True,
        live_eligible=True,
    )
    fake = _FakeSession(
        [
            (inactive_viability, inactive),
            (active_viability, active),
        ],
        read_at=NOW - timedelta(milliseconds=1),
    )
    session_calls = _install_fake_session(monkeypatch, fake)
    reader = reader_module.SqlAlchemyCapturedPaperInitialCandidateReader(engine)

    result = reader.read_candidates(
        user_id=41,
        symbol="CAND",
        decision_at=NOW,
    )

    assert isinstance(reader, CapturedPaperInitialCandidateReadPort)
    assert reader.network_fallback_allowed is False
    assert reader.mutation_allowed is False
    assert result.user_id == 41
    assert result.symbol == "CAND"
    assert result.read_at == NOW - timedelta(milliseconds=1)
    assert [row.variant.id for row in result.rows] == [7, 11]
    assert [row.viability.id for row in result.rows] == [70, 110]
    assert result.rows[1].variant.is_active is False
    assert result.rows[1].viability.paper_eligible is False
    assert result.rows[1].viability.live_eligible is False
    assert initial.captured_paper_initial_variant_sha256(result.rows[0].variant)
    assert initial.captured_paper_initial_viability_sha256(
        result.rows[0].viability
    )
    assert session_calls == [(engine, False)]
    assert fake.query_calls == [
        (MomentumSymbolViability, MomentumStrategyVariant)
    ]
    assert len(fake.query_object.join_calls) == 1
    assert len(fake.query_object.filter_calls) == 1
    assert len(fake.query_object.order_calls) == 1
    assert fake.expunge_calls == [
        active_viability,
        active,
        inactive_viability,
        inactive,
    ]
    assert fake.rollback_calls == 1
    assert fake.close_calls == 1
    assert fake.execute_calls[0][0] == (
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    )
    assert fake.execute_calls[1][1] == {"decision_at": NOW}


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        (
            {"user_id": True, "symbol": "CAND", "decision_at": NOW},
            "initial_candidate_reader_user_id_invalid",
        ),
        (
            {"user_id": 41, "symbol": "cand", "decision_at": NOW},
            "initial_candidate_reader_symbol_invalid",
        ),
        (
            {
                "user_id": 41,
                "symbol": "CAND",
                "decision_at": NOW.replace(tzinfo=None),
            },
            "initial_candidate_reader_decision_at_invalid",
        ),
    ],
)
def test_invalid_route_is_rejected_before_session_open(monkeypatch, kwargs, reason):
    monkeypatch.setattr(
        reader_module,
        "Session",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("session must not open")
        ),
    )
    reader = reader_module.SqlAlchemyCapturedPaperInitialCandidateReader(engine)

    with pytest.raises(
        reader_module.CapturedPaperInitialCandidateReaderUnavailable,
        match=reason,
    ):
        reader.read_candidates(**kwargs)


def test_query_failure_rolls_back_and_closes_without_detached_result(monkeypatch):
    fake = _FakeSession([], query_failure=RuntimeError("read failed"))
    _install_fake_session(monkeypatch, fake)
    reader = reader_module.SqlAlchemyCapturedPaperInitialCandidateReader(engine)

    with pytest.raises(RuntimeError, match="read failed"):
        reader.read_candidates(user_id=41, symbol="CAND", decision_at=NOW)

    assert fake.expunge_calls == []
    assert fake.rollback_calls == 1
    assert fake.close_calls == 1


def test_source_has_no_commit_mutation_network_provider_or_broker_fallback():
    source = python_inspect.getsource(reader_module)
    forbidden = (
        "db.commit(",
        "db.add(",
        "db.delete(",
        "requests.",
        "httpx.",
        "AlpacaSpotAdapter",
        "iqfeed",
        "Massive",
    )
    assert all(value not in source for value in forbidden)
    assert "REPEATABLE READ, READ ONLY" in source
    assert "MomentumSymbolViability.paper_eligible" not in source
    assert "MomentumSymbolViability.live_eligible" not in source
    assert "MomentumStrategyVariant.is_active" not in source


def test_real_db_reader_returns_active_and_inactive_rows_without_mutation(db):
    user_id = 41
    inactive = _variant(0, active=False, key="real-inactive")
    active = _variant(0, active=True, key="real-active")
    excluded = _variant(0, active=True, key="real-excluded")
    for row in (inactive, active, excluded):
        row.id = None
        db.add(row)
    db.flush()
    exact_inactive = _viability(
        0,
        variant_id=int(inactive.id),
        paper_eligible=False,
        live_eligible=False,
    )
    exact_active = _viability(
        0,
        variant_id=int(active.id),
        paper_eligible=True,
        live_eligible=True,
    )
    wrong_scope = _viability(
        0,
        variant_id=int(excluded.id),
        scope="sector",
        paper_eligible=True,
        live_eligible=True,
    )
    wrong_symbol = _viability(
        0,
        variant_id=int(active.id),
        symbol="OTHER",
        paper_eligible=True,
        live_eligible=True,
    )
    for row in (exact_inactive, exact_active, wrong_scope, wrong_symbol):
        row.id = None
        db.add(row)
    db.commit()
    before = {
        int(row.id): (
            row.symbol,
            row.scope,
            row.variant_id,
            row.viability_score,
            row.paper_eligible,
            row.live_eligible,
            dict(row.execution_readiness_json),
        )
        for row in db.query(MomentumSymbolViability).all()
    }
    before_variants = {
        int(row.id): (
            row.family,
            row.variant_key,
            row.version,
            row.is_active,
            row.execution_family,
            dict(row.params_json),
        )
        for row in db.query(MomentumStrategyVariant).all()
    }
    decision_at = datetime.now(UTC)
    reader = reader_module.SqlAlchemyCapturedPaperInitialCandidateReader(engine)

    result = reader.read_candidates(
        user_id=user_id,
        symbol="CAND",
        decision_at=decision_at,
    )

    assert result.user_id == user_id
    assert result.symbol == "CAND"
    assert result.read_at.tzinfo is not None
    assert result.read_at <= decision_at
    assert [row.variant.id for row in result.rows] == sorted(
        (int(inactive.id), int(active.id))
    )
    assert {row.variant.is_active for row in result.rows} == {False, True}
    assert {row.viability.paper_eligible for row in result.rows} == {False, True}
    assert all(sqlalchemy_inspect(row.variant).detached for row in result.rows)
    assert all(sqlalchemy_inspect(row.viability).detached for row in result.rows)
    db.rollback()
    after = {
        int(row.id): (
            row.symbol,
            row.scope,
            row.variant_id,
            row.viability_score,
            row.paper_eligible,
            row.live_eligible,
            dict(row.execution_readiness_json),
        )
        for row in db.query(MomentumSymbolViability).all()
    }
    after_variants = {
        int(row.id): (
            row.family,
            row.variant_key,
            row.version,
            row.is_active,
            row.execution_family,
            dict(row.params_json),
        )
        for row in db.query(MomentumStrategyVariant).all()
    }
    assert after == before
    assert after_variants == before_variants

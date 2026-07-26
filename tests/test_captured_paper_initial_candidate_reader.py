from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import inspect as python_inspect
import uuid

import pytest
from sqlalchemy import event, inspect as sqlalchemy_inspect, text

from app.db import engine
from app.models.captured_paper_selection_frontier import (
    CapturedPaperSelectionFrontier,
    CapturedPaperSelectionFrontierEvent,
    CapturedPaperSelectionRouteState,
)
from app.models.trading import MomentumStrategyVariant, MomentumSymbolViability
from app.services.trading.momentum_neural import (
    captured_paper_initial_candidate_reader as reader_module,
)
from app.services.trading.momentum_neural import (
    captured_paper_initial_admission as initial,
)
from app.services.trading.momentum_neural import (
    captured_paper_selection_producer as producer,
)
from app.services.trading.momentum_neural.captured_paper_initial_provider import (
    CapturedPaperInitialCandidateReadPort,
)
from app.services.trading.momentum_neural.captured_paper_selection_producer import (
    CapturedPaperSelectionAuthority,
    CapturedPaperSelectionBatch,
    CapturedPaperSelectionObservation,
    CapturedPaperSelectionRouteStateUpdate,
    CapturedPaperSelectionVariantBinding,
)
from app.services.trading.momentum_neural.captured_paper_variant_binding import (
    BINDING_META_KEY,
    BINDING_META_SCHEMA_VERSION,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 16, 13, 0, 0, 123456, tzinfo=UTC)
ACCOUNT_ID = "11111111-1111-4111-8111-111111111111"
ACTIVATION_GENERATION = "22222222-2222-4222-8222-222222222222"
SOURCE_GENERATION = "33333333-3333-4333-8333-333333333333"
POLICY_SHA = "a" * 64
SETTINGS_SHA = "b" * 64
CODE_SHA = "c" * 64
PLAN_SHA = "d" * 64
QUEUE_SHA = "e" * 64
COVERAGE_SHA = "f" * 64


def _binding_marker(*, family: str, source_id: int) -> dict:
    return {
        "schema_version": BINDING_META_SCHEMA_VERSION,
        "account_scope": "alpaca:paper",
        "execution_family": "alpaca_spot",
        "expected_account_id": ACCOUNT_ID,
        "activation_generation": ACTIVATION_GENERATION,
        "source_variant_id": source_id,
        "source_variant_sha256": "9" * 64,
        "source_family": family,
        "source_version": 1,
        "policy_sha256": POLICY_SHA,
        "settings_projection_sha256": SETTINGS_SHA,
        "code_build_sha256": CODE_SHA,
        "plan_sha256": PLAN_SHA,
        "bound_at": (NOW - timedelta(minutes=1)).isoformat().replace(
            "+00:00", "Z"
        ),
        "strategy_params_overridden": False,
        "paper_order_submission_authorized": False,
        "live_cash_authorized": False,
        "real_money_authorized": False,
    }


def _variant(row_id: int, *, family: str) -> MomentumStrategyVariant:
    source_id = row_id + 10_000
    return MomentumStrategyVariant(
        id=row_id,
        family=family,
        variant_key=f"captured_paper:{family}",
        version=1,
        label=f"Candidate {family}",
        params_json={"setup_family": family},
        is_active=True,
        execution_family="alpaca_spot",
        parent_variant_id=source_id,
        refinement_meta_json={
            "source": "candidate-reader-test",
            BINDING_META_KEY: _binding_marker(
                family=family,
                source_id=source_id,
            ),
        },
        scan_pattern_id=None,
        created_at=(NOW - timedelta(minutes=1)).replace(tzinfo=None),
        updated_at=(NOW - timedelta(minutes=1)).replace(tzinfo=None),
    )


def _authority(variants: tuple[MomentumStrategyVariant, ...]):
    bindings = tuple(
        CapturedPaperSelectionVariantBinding(
            variant_id=int(variant.id),
            family=str(variant.family),
            version=int(variant.version),
            variant_key=str(variant.variant_key),
            target_after_sha256=initial.captured_paper_initial_variant_sha256(
                variant
            ),
        )
        for variant in variants
    )
    return CapturedPaperSelectionAuthority(
        expected_account_id=ACCOUNT_ID,
        activation_generation=ACTIVATION_GENERATION,
        policy_sha256=POLICY_SHA,
        settings_projection_sha256=SETTINGS_SHA,
        code_build_sha256=CODE_SHA,
        variant_bindings=bindings,
    )


def _eligible_route_update(observation: CapturedPaperSelectionObservation):
    return CapturedPaperSelectionRouteStateUpdate(
        source_sequence=observation.source_sequence,
        source_event_at=observation.source_event_at,
        source_available_at=observation.source_available_at,
        symbol=observation.symbol,
        variant_id=observation.variant_id,
        state=producer.ROUTE_ELIGIBLE,
        evidence_sha256=observation.observation_sha256,
        bundle_sha256=producer._hash_json(
            {"bundle_sequence": observation.source_sequence}
        ),
        scoring_authority_sha256=producer._hash_json(
            {"scoring_sequence": observation.source_sequence}
        ),
        score_result_sha256=producer._hash_json(
            {"result_sequence": observation.source_sequence}
        ),
    )


def _unavailable_route_update(
    *,
    sequence: int,
    symbol: str,
    variant_id: int,
    event_at: datetime,
    available_at: datetime,
    reason: str = "fundamentals_receipt_unavailable",
):
    result_sha256 = producer._hash_json(
        {
            "status": "COVERAGE_UNAVAILABLE",
            "source_sequence": sequence,
            "symbol": symbol,
            "variant_id": variant_id,
            "reason": reason,
        }
    )
    return CapturedPaperSelectionRouteStateUpdate(
        source_sequence=sequence,
        source_event_at=event_at,
        source_available_at=available_at,
        symbol=symbol,
        variant_id=variant_id,
        state=producer.ROUTE_COVERAGE_UNAVAILABLE,
        evidence_sha256=result_sha256,
        bundle_sha256=producer._hash_json({"bundle_sequence": sequence}),
        scoring_authority_sha256=producer._hash_json(
            {"scoring_sequence": sequence}
        ),
        score_result_sha256=result_sha256,
        reason_codes=(reason,),
    )


def _selection_state(
    *,
    families: tuple[str, ...] = ("first_dip", "breakout"),
    eligible: tuple[bool, ...] = (True, False),
):
    variants = tuple(
        _variant(700 + index, family=family)
        for index, family in enumerate(families)
    )
    authority = _authority(variants)
    initial_values = {
        **producer._authority_values(authority),
        "last_source_sequence": 0,
        "last_source_event_at": None,
        "last_source_available_at": None,
        "last_batch_sha256": None,
        "status": "ready",
        "gap_count": 0,
        "version": 1,
        "event_sequence": 0,
        "last_event_sha256": None,
    }
    initial_values["frontier_sha256"] = producer._hash_json(
        producer._frontier_body(initial_values)
    )
    initial_frontier = producer._receipt_from_values(901, initial_values)
    observations = tuple(
        CapturedPaperSelectionObservation(
            source_sequence=index,
            source_event_at=NOW - timedelta(seconds=4 - index / 10),
            source_available_at=NOW - timedelta(seconds=2 - index / 10),
            symbol="CAND",
            variant_id=int(variant.id),
            viability_score=0.80 - index / 10,
            paper_eligible=bool(eligible[index - 1]),
            live_eligible=bool(eligible[index - 1]),
            regime_snapshot_json={"regime": "momentum"},
            execution_readiness_json={"captured": True},
            explain_json={"source": "captured-test"},
            evidence_window_json={"coverage": "complete"},
            correlation_id=f"captured-{index}",
        )
        for index, variant in enumerate(variants, start=1)
    )
    route_updates = tuple(_eligible_route_update(row) for row in observations)
    batch = CapturedPaperSelectionBatch(
        authority_sha256=authority.authority_sha256,
        expected_frontier=initial_frontier,
        source_name="captured_queue",
        source_generation=SOURCE_GENERATION,
        queue_receipt_sha256=QUEUE_SHA,
        coverage_receipt_sha256=COVERAGE_SHA,
        source_sequence_from=0,
        source_sequence_through=len(observations),
        watermark_at=NOW - timedelta(seconds=1),
        read_at=NOW - timedelta(milliseconds=750),
        observations=observations,
        route_state_updates=route_updates,
    )
    next_state = {
        "last_source_sequence": len(observations),
        "last_source_event_at": max(row.source_event_at for row in observations),
        "last_source_available_at": max(
            row.source_available_at for row in observations
        ),
        "last_batch_sha256": batch.batch_sha256,
        "status": "ready",
        "gap_count": 0,
    }
    recorded_at = NOW - timedelta(milliseconds=500)
    event_body = {
        "schema_version": producer.EVENT_SCHEMA_VERSION,
        "frontier_id": initial_frontier.frontier_id,
        "event_sequence": initial_frontier.event_sequence + 1,
        "event_type": "batch_applied",
        "expected_version": initial_frontier.version,
        "next_version": initial_frontier.version + 1,
        "expected_frontier_sha256": initial_frontier.frontier_sha256,
        "previous_event_sha256": initial_frontier.last_event_sha256,
        "batch_sha256": batch.batch_sha256,
        "gap_sha256": None,
        "source_sequence_from": batch.source_sequence_from,
        "source_sequence_through": batch.source_sequence_through,
        "detail": {
            "authority_sha256": authority.authority_sha256,
            "source_name": batch.source_name,
            "source_generation": batch.source_generation,
            "queue_receipt_sha256": batch.queue_receipt_sha256,
            "coverage_receipt_sha256": batch.coverage_receipt_sha256,
            "watermark_at": producer._iso(batch.watermark_at),
            "read_at": producer._iso(batch.read_at),
            "observation_sha256s": [
                row.observation_sha256 for row in batch.observations
            ],
            "route_state_updates": [
                {**row.body(), "update_sha256": row.update_sha256}
                for row in batch.route_state_updates
            ],
        },
        "recorded_at": producer._iso(recorded_at),
        "next_state": next_state,
    }
    event_sha = producer._hash_json(event_body)
    current_values = {
        **producer._authority_values(authority),
        **next_state,
        "version": 2,
        "event_sequence": 1,
        "last_event_sha256": event_sha,
    }
    current_values["frontier_sha256"] = producer._hash_json(
        producer._frontier_body(current_values)
    )
    frontier = CapturedPaperSelectionFrontier(
        id=initial_frontier.frontier_id,
        **current_values,
        created_at=NOW - timedelta(minutes=1),
        updated_at=recorded_at,
    )
    event = CapturedPaperSelectionFrontierEvent(
        id=902,
        frontier_id=frontier.id,
        event_sequence=1,
        event_type="batch_applied",
        expected_version=1,
        next_version=2,
        expected_frontier_sha256=initial_frontier.frontier_sha256,
        next_frontier_sha256=frontier.frontier_sha256,
        previous_event_sha256=None,
        event_sha256=event_sha,
        batch_sha256=batch.batch_sha256,
        gap_sha256=None,
        source_sequence_from=0,
        source_sequence_through=len(observations),
        detail_canonical_json=producer._canonical_json(event_body),
        recorded_at=recorded_at,
    )
    viabilities = []
    route_states = []
    for index, (variant, observation) in enumerate(
        zip(variants, observations, strict=True), start=1
    ):
        provenance = producer._viability_provenance(
            authority,
            batch,
            observation,
        )
        viabilities.append(
            MomentumSymbolViability(
                id=1000 + index,
                symbol="CAND",
                scope="symbol",
                variant_id=int(variant.id),
                viability_score=observation.viability_score,
                paper_eligible=observation.paper_eligible,
                live_eligible=observation.live_eligible,
                freshness_ts=observation.source_available_at.replace(tzinfo=None),
                regime_snapshot_json=dict(observation.regime_snapshot_json),
                execution_readiness_json={
                    **dict(observation.execution_readiness_json),
                    producer.PROVENANCE_KEY: copy.deepcopy(provenance),
                },
                explain_json={
                    **dict(observation.explain_json),
                    producer.PROVENANCE_KEY: copy.deepcopy(provenance),
                },
                evidence_window_json={
                    **dict(observation.evidence_window_json),
                    producer.PROVENANCE_KEY: copy.deepcopy(provenance),
                },
                source_node_id=producer.SOURCE_NODE_ID,
                correlation_id=observation.correlation_id,
                created_at=recorded_at.replace(tzinfo=None),
                updated_at=recorded_at.replace(tzinfo=None),
            )
        )
        route_body = producer._route_state_body(
            authority=authority,
            update_row=route_updates[index - 1],
            batch_sha256=batch.batch_sha256,
            version=1,
        )
        route_states.append(
            CapturedPaperSelectionRouteState(
                id=1200 + index,
                **{
                    key: route_body[key]
                    for key in (
                        "account_scope",
                        "expected_account_id",
                        "activation_generation",
                        "execution_family",
                        "authority_sha256",
                        "symbol",
                        "variant_id",
                        "latest_source_sequence",
                        "state",
                        "evidence_sha256",
                        "batch_sha256",
                        "version",
                    )
                },
                source_event_at=observation.source_event_at,
                source_available_at=observation.source_available_at,
                state_sha256=producer._hash_json(route_body),
                created_at=recorded_at,
                updated_at=recorded_at,
            )
        )
    return {
        "authority": authority,
        "variants": variants,
        "initial_frontier": initial_frontier,
        "observations": observations,
        "batch": batch,
        "frontier": frontier,
        "event": event,
        "events": (event,),
        "viabilities": tuple(viabilities),
        "route_states": tuple(route_states),
    }


def _fake_batch_transition(
    *,
    authority,
    current,
    batch,
    event_id: int,
    recorded_at: datetime,
):
    latest_event_at = current.last_source_event_at
    latest_available_at = current.last_source_available_at
    if batch.route_state_updates:
        latest_event_at = max(
            row.source_event_at for row in batch.route_state_updates
        )
        latest_available_at = max(
            row.source_available_at for row in batch.route_state_updates
        )
    next_state = {
        "last_source_sequence": batch.source_sequence_through,
        "last_source_event_at": latest_event_at,
        "last_source_available_at": latest_available_at,
        "last_batch_sha256": batch.batch_sha256,
        "status": "ready",
        "gap_count": current.gap_count,
    }
    event_body = {
        "schema_version": producer.EVENT_SCHEMA_VERSION,
        "frontier_id": current.frontier_id,
        "event_sequence": current.event_sequence + 1,
        "event_type": "batch_applied",
        "expected_version": current.version,
        "next_version": current.version + 1,
        "expected_frontier_sha256": current.frontier_sha256,
        "previous_event_sha256": current.last_event_sha256,
        "batch_sha256": batch.batch_sha256,
        "gap_sha256": None,
        "source_sequence_from": batch.source_sequence_from,
        "source_sequence_through": batch.source_sequence_through,
        "detail": {
            "authority_sha256": authority.authority_sha256,
            "source_name": batch.source_name,
            "source_generation": batch.source_generation,
            "queue_receipt_sha256": batch.queue_receipt_sha256,
            "coverage_receipt_sha256": batch.coverage_receipt_sha256,
            "watermark_at": producer._iso(batch.watermark_at),
            "read_at": producer._iso(batch.read_at),
            "observation_sha256s": [
                row.observation_sha256 for row in batch.observations
            ],
            "route_state_updates": [
                {**row.body(), "update_sha256": row.update_sha256}
                for row in batch.route_state_updates
            ],
        },
        "recorded_at": producer._iso(recorded_at),
        "next_state": next_state,
    }
    event_sha = producer._hash_json(event_body)
    values = {
        **producer._authority_values(authority),
        **next_state,
        "version": current.version + 1,
        "event_sequence": current.event_sequence + 1,
        "last_event_sha256": event_sha,
    }
    values["frontier_sha256"] = producer._hash_json(
        producer._frontier_body(values)
    )
    receipt = producer._receipt_from_values(current.frontier_id, values)
    event = CapturedPaperSelectionFrontierEvent(
        id=event_id,
        frontier_id=current.frontier_id,
        event_sequence=receipt.event_sequence,
        event_type="batch_applied",
        expected_version=current.version,
        next_version=receipt.version,
        expected_frontier_sha256=current.frontier_sha256,
        next_frontier_sha256=receipt.frontier_sha256,
        previous_event_sha256=current.last_event_sha256,
        event_sha256=event_sha,
        batch_sha256=batch.batch_sha256,
        gap_sha256=None,
        source_sequence_from=batch.source_sequence_from,
        source_sequence_through=batch.source_sequence_through,
        detail_canonical_json=producer._canonical_json(event_body),
        recorded_at=recorded_at,
    )
    return receipt, event


def _two_batch_selection_state():
    state = _selection_state()
    authority = state["authority"]
    variants = state["variants"]
    observations = state["observations"]
    initial_frontier = state["initial_frontier"]
    batch_a = CapturedPaperSelectionBatch(
        authority_sha256=authority.authority_sha256,
        expected_frontier=initial_frontier,
        source_name="captured_queue",
        source_generation=SOURCE_GENERATION,
        queue_receipt_sha256="1" * 64,
        coverage_receipt_sha256="2" * 64,
        source_sequence_from=0,
        source_sequence_through=1,
        watermark_at=NOW - timedelta(milliseconds=1500),
        read_at=NOW - timedelta(milliseconds=1400),
        observations=(observations[0],),
        route_state_updates=(_eligible_route_update(observations[0]),),
    )
    receipt_a, event_a = _fake_batch_transition(
        authority=authority,
        current=initial_frontier,
        batch=batch_a,
        event_id=910,
        recorded_at=NOW - timedelta(milliseconds=1300),
    )
    batch_b = CapturedPaperSelectionBatch(
        authority_sha256=authority.authority_sha256,
        expected_frontier=receipt_a,
        source_name="captured_queue",
        source_generation=SOURCE_GENERATION,
        queue_receipt_sha256="3" * 64,
        coverage_receipt_sha256="4" * 64,
        source_sequence_from=1,
        source_sequence_through=2,
        watermark_at=NOW - timedelta(milliseconds=900),
        read_at=NOW - timedelta(milliseconds=800),
        observations=(observations[1],),
        route_state_updates=(_eligible_route_update(observations[1]),),
    )
    receipt_b, event_b = _fake_batch_transition(
        authority=authority,
        current=receipt_a,
        batch=batch_b,
        event_id=911,
        recorded_at=NOW - timedelta(milliseconds=500),
    )
    frontier = CapturedPaperSelectionFrontier(
        id=receipt_b.frontier_id,
        account_scope=receipt_b.account_scope,
        expected_account_id=receipt_b.expected_account_id,
        activation_generation=receipt_b.activation_generation,
        execution_family=receipt_b.execution_family,
        authority_sha256=receipt_b.authority_sha256,
        policy_sha256=receipt_b.policy_sha256,
        settings_projection_sha256=receipt_b.settings_projection_sha256,
        code_build_sha256=receipt_b.code_build_sha256,
        variant_set_sha256=receipt_b.variant_set_sha256,
        last_source_sequence=receipt_b.last_source_sequence,
        last_source_event_at=receipt_b.last_source_event_at,
        last_source_available_at=receipt_b.last_source_available_at,
        last_batch_sha256=receipt_b.last_batch_sha256,
        status=receipt_b.status,
        gap_count=receipt_b.gap_count,
        version=receipt_b.version,
        event_sequence=receipt_b.event_sequence,
        frontier_sha256=receipt_b.frontier_sha256,
        last_event_sha256=receipt_b.last_event_sha256,
        created_at=NOW - timedelta(minutes=1),
        updated_at=NOW - timedelta(milliseconds=500),
    )
    viabilities = []
    route_states = []
    for index, (variant, observation, batch, recorded_at) in enumerate(
        (
            (
                variants[0],
                observations[0],
                batch_a,
                NOW - timedelta(milliseconds=1300),
            ),
            (
                variants[1],
                observations[1],
                batch_b,
                NOW - timedelta(milliseconds=500),
            ),
        ),
        start=1,
    ):
        provenance = producer._viability_provenance(
            authority,
            batch,
            observation,
        )
        viabilities.append(
            MomentumSymbolViability(
                id=1100 + index,
                symbol="CAND",
                scope="symbol",
                variant_id=int(variant.id),
                viability_score=observation.viability_score,
                paper_eligible=observation.paper_eligible,
                live_eligible=observation.live_eligible,
                freshness_ts=observation.source_available_at.replace(
                    tzinfo=None
                ),
                regime_snapshot_json=dict(observation.regime_snapshot_json),
                execution_readiness_json={
                    **dict(observation.execution_readiness_json),
                    producer.PROVENANCE_KEY: copy.deepcopy(provenance),
                },
                explain_json={
                    **dict(observation.explain_json),
                    producer.PROVENANCE_KEY: copy.deepcopy(provenance),
                },
                evidence_window_json={
                    **dict(observation.evidence_window_json),
                    producer.PROVENANCE_KEY: copy.deepcopy(provenance),
                },
                source_node_id=producer.SOURCE_NODE_ID,
                correlation_id=observation.correlation_id,
                created_at=recorded_at.replace(tzinfo=None),
                updated_at=recorded_at.replace(tzinfo=None),
            )
        )
        route_update = batch.route_state_updates[0]
        route_body = producer._route_state_body(
            authority=authority,
            update_row=route_update,
            batch_sha256=batch.batch_sha256,
            version=1,
        )
        route_states.append(
            CapturedPaperSelectionRouteState(
                id=1300 + index,
                **{
                    key: route_body[key]
                    for key in (
                        "account_scope",
                        "expected_account_id",
                        "activation_generation",
                        "execution_family",
                        "authority_sha256",
                        "symbol",
                        "variant_id",
                        "latest_source_sequence",
                        "state",
                        "evidence_sha256",
                        "batch_sha256",
                        "version",
                    )
                },
                source_event_at=route_update.source_event_at,
                source_available_at=route_update.source_available_at,
                state_sha256=producer._hash_json(route_body),
                created_at=recorded_at,
                updated_at=recorded_at,
            )
        )
    return {
        **state,
        "batch_a": batch_a,
        "batch_b": batch_b,
        "frontier": frontier,
        "event": event_b,
        "events": (event_a, event_b),
        "viabilities": tuple(viabilities),
        "route_states": tuple(route_states),
    }


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

    def _result(self):
        if self.failure is not None:
            raise self.failure
        return list(self.rows)

    def all(self):
        return self._result()

    def one_or_none(self):
        rows = self._result()
        if len(rows) > 1:
            raise AssertionError("fake one_or_none received several rows")
        return rows[0] if rows else None

    def first(self):
        rows = self._result()
        return rows[0] if rows else None


class _FakeSession:
    def __init__(self, state, *, read_at=NOW, pair_failure=None):
        self.state = state
        self.read_at = read_at
        self.pair_failure = pair_failure
        self.execute_calls = []
        self.query_calls = []
        self.query_objects = []
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
        failure = None
        if entities == (CapturedPaperSelectionFrontier,):
            rows = [self.state["frontier"]]
        elif entities == (CapturedPaperSelectionFrontierEvent,):
            rows = list(self.state["events"])
        elif entities == (MomentumStrategyVariant,):
            rows = list(self.state["variants"])
        elif entities == (CapturedPaperSelectionRouteState,):
            rows = list(self.state["route_states"])
        elif entities == (MomentumSymbolViability, MomentumStrategyVariant):
            rows = list(
                reversed(
                    list(
                        zip(
                            self.state["viabilities"],
                            self.state["variants"],
                            strict=True,
                        )
                    )
                )
            )
            failure = self.pair_failure
        else:  # pragma: no cover - a new production query must update the fake
            raise AssertionError(f"unexpected query entities: {entities!r}")
        query = _FakeQuery(rows, failure=failure)
        self.query_objects.append(query)
        return query

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


def test_reader_is_strict_read_only_and_returns_only_current_bound_rows(monkeypatch):
    state = _selection_state()
    fake = _FakeSession(state, read_at=NOW)
    session_calls = _install_fake_session(monkeypatch, fake)
    reader = reader_module.SqlAlchemyCapturedPaperInitialCandidateReader(
        engine,
        authority=state["authority"],
    )

    result = reader.read_candidates(user_id=41, symbol="CAND", decision_at=NOW)

    assert isinstance(reader, CapturedPaperInitialCandidateReadPort)
    assert reader.network_fallback_allowed is False
    assert reader.mutation_allowed is False
    assert result.user_id == 41
    assert result.symbol == "CAND"
    assert [row.variant.id for row in result.rows] == [700, 701]
    assert [row.viability.id for row in result.rows] == [1001, 1002]
    assert result.rows[1].viability.paper_eligible is False
    assert result.rows[1].viability.live_eligible is False
    assert session_calls == [(engine, False)]
    assert fake.query_calls == [
        (CapturedPaperSelectionFrontier,),
        (CapturedPaperSelectionFrontierEvent,),
        (MomentumStrategyVariant,),
        (CapturedPaperSelectionRouteState,),
        (MomentumSymbolViability, MomentumStrategyVariant),
    ]
    assert fake.rollback_calls == 1
    assert fake.close_calls == 1
    assert fake.execute_calls[0][0] == (
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    )
    assert fake.execute_calls[1][1] == {"decision_at": NOW}


def test_reader_keeps_rows_from_each_hash_bound_ancestor_batch(monkeypatch):
    state = _two_batch_selection_state()
    fake = _FakeSession(state, read_at=NOW)
    _install_fake_session(monkeypatch, fake)
    reader = reader_module.SqlAlchemyCapturedPaperInitialCandidateReader(
        engine,
        authority=state["authority"],
    )

    result = reader.read_candidates(
        user_id=41,
        symbol="CAND",
        decision_at=NOW,
    )

    assert [row.variant.id for row in result.rows] == [700, 701]
    assert [
        row.viability.execution_readiness_json[producer.PROVENANCE_KEY][
            "batch_sha256"
        ]
        for row in result.rows
    ] == [state["batch_a"].batch_sha256, state["batch_b"].batch_sha256]
    assert fake.rollback_calls == 1
    assert fake.close_calls == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "row_not_ancestor",
        "chain_tampered",
        "missing_middle_event",
        "canonical_event_tampered",
    ],
)
def test_reader_fails_closed_for_nonancestor_or_tampered_chain(
    monkeypatch,
    mutation,
):
    state = _two_batch_selection_state()
    if mutation == "row_not_ancestor":
        for field in (
            "execution_readiness_json",
            "explain_json",
            "evidence_window_json",
        ):
            getattr(state["viabilities"][0], field)[producer.PROVENANCE_KEY][
                "batch_sha256"
            ] = "5" * 64
    elif mutation == "chain_tampered":
        state["events"][0].next_frontier_sha256 = "5" * 64
    elif mutation == "missing_middle_event":
        state["events"] = (state["events"][1],)
    else:
        state["events"][0].detail_canonical_json = (
            state["events"][0].detail_canonical_json.replace(
                '"source_name":"captured_queue"',
                '"source_name":"forged_queue"',
            )
        )
    fake = _FakeSession(state, read_at=NOW)
    _install_fake_session(monkeypatch, fake)
    reader = reader_module.SqlAlchemyCapturedPaperInitialCandidateReader(
        engine,
        authority=state["authority"],
    )

    with pytest.raises(
        reader_module.CapturedPaperInitialCandidateReaderUnavailable
    ):
        reader.read_candidates(user_id=41, symbol="CAND", decision_at=NOW)

    assert fake.expunge_calls == []
    assert fake.rollback_calls == 1
    assert fake.close_calls == 1


def test_constructor_requires_exact_selection_authority_before_session_open(monkeypatch):
    monkeypatch.setattr(
        reader_module,
        "Session",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("session must not open")
        ),
    )
    with pytest.raises(
        reader_module.CapturedPaperInitialCandidateReaderUnavailable,
        match="initial_candidate_reader_authority_invalid",
    ):
        reader_module.SqlAlchemyCapturedPaperInitialCandidateReader(
            engine,
            authority=None,
        )


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
def test_invalid_route_is_rejected_before_session_open(
    monkeypatch, kwargs, reason
):
    state = _selection_state(families=("first_dip",), eligible=(True,))
    monkeypatch.setattr(
        reader_module,
        "Session",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("session must not open")
        ),
    )
    reader = reader_module.SqlAlchemyCapturedPaperInitialCandidateReader(
        engine,
        authority=state["authority"],
    )
    with pytest.raises(
        reader_module.CapturedPaperInitialCandidateReaderUnavailable,
        match=reason,
    ):
        reader.read_candidates(**kwargs)


@pytest.mark.parametrize("gap_state", ["status", "count"])
def test_gap_frontier_rejects_all_rows(monkeypatch, gap_state):
    state = _selection_state(families=("first_dip",), eligible=(True,))
    if gap_state == "status":
        state["frontier"].status = "gap"
    else:
        state["frontier"].gap_count = 1
    fake = _FakeSession(state)
    _install_fake_session(monkeypatch, fake)
    reader = reader_module.SqlAlchemyCapturedPaperInitialCandidateReader(
        engine,
        authority=state["authority"],
    )
    with pytest.raises(
        reader_module.CapturedPaperInitialCandidateReaderUnavailable,
        match="initial_candidate_frontier_not_ready",
    ):
        reader.read_candidates(user_id=41, symbol="CAND", decision_at=NOW)
    assert fake.expunge_calls == []
    assert fake.rollback_calls == 1
    assert fake.close_calls == 1


@pytest.mark.parametrize("mutation", ["old_batch", "wrong_source", "split_copy"])
def test_old_or_nonproducer_provenance_rejects_entire_read(monkeypatch, mutation):
    state = _selection_state(families=("first_dip",), eligible=(True,))
    viability = state["viabilities"][0]
    if mutation == "old_batch":
        for field in (
            "execution_readiness_json",
            "explain_json",
            "evidence_window_json",
        ):
            getattr(viability, field)[producer.PROVENANCE_KEY][
                "batch_sha256"
            ] = "0" * 64
    elif mutation == "wrong_source":
        viability.source_node_id = "legacy_viability"
    else:
        viability.explain_json[producer.PROVENANCE_KEY][
            "coverage_receipt_sha256"
        ] = "0" * 64
    fake = _FakeSession(state)
    _install_fake_session(monkeypatch, fake)
    reader = reader_module.SqlAlchemyCapturedPaperInitialCandidateReader(
        engine,
        authority=state["authority"],
    )
    with pytest.raises(
        reader_module.CapturedPaperInitialCandidateReaderUnavailable
    ):
        reader.read_candidates(user_id=41, symbol="CAND", decision_at=NOW)
    assert fake.expunge_calls == []


@pytest.mark.parametrize(
    "mutation",
    [
        "variant_key",
        "variant_generation",
        "frontier_account",
        "frontier_policy",
        "provenance_settings",
        "provenance_code",
    ],
)
def test_account_generation_policy_settings_code_and_clone_drift_fail_closed(
    monkeypatch,
    mutation,
):
    state = _selection_state(families=("first_dip",), eligible=(True,))
    variant = state["variants"][0]
    viability = state["viabilities"][0]
    if mutation == "variant_key":
        variant.variant_key = "first_dip"
    elif mutation == "variant_generation":
        variant.refinement_meta_json[BINDING_META_KEY][
            "activation_generation"
        ] = str(uuid.uuid4())
    elif mutation == "frontier_account":
        state["frontier"].expected_account_id = str(uuid.uuid4())
    elif mutation == "frontier_policy":
        state["frontier"].policy_sha256 = "0" * 64
    else:
        key = (
            "settings_projection_sha256"
            if mutation == "provenance_settings"
            else "code_build_sha256"
        )
        for field in (
            "execution_readiness_json",
            "explain_json",
            "evidence_window_json",
        ):
            getattr(viability, field)[producer.PROVENANCE_KEY][key] = "0" * 64
    fake = _FakeSession(state)
    _install_fake_session(monkeypatch, fake)
    reader = reader_module.SqlAlchemyCapturedPaperInitialCandidateReader(
        engine,
        authority=state["authority"],
    )

    with pytest.raises(
        reader_module.CapturedPaperInitialCandidateReaderUnavailable
    ):
        reader.read_candidates(user_id=41, symbol="CAND", decision_at=NOW)

    assert fake.expunge_calls == []
    assert fake.rollback_calls == 1
    assert fake.close_calls == 1


def test_query_failure_rolls_back_and_closes_without_detached_result(monkeypatch):
    state = _selection_state(families=("first_dip",), eligible=(True,))
    fake = _FakeSession(state, pair_failure=RuntimeError("read failed"))
    _install_fake_session(monkeypatch, fake)
    reader = reader_module.SqlAlchemyCapturedPaperInitialCandidateReader(
        engine,
        authority=state["authority"],
    )

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


def test_real_db_reader_returns_only_exact_current_captured_row_without_mutation(db):
    family = f"fd_{uuid.uuid4().hex[:8]}"
    source = MomentumStrategyVariant(
        family=family,
        variant_key=family,
        version=1,
        label="Canonical source",
        params_json={"setup_family": "first_dip"},
        is_active=True,
        execution_family="alpaca_spot",
        parent_variant_id=None,
        refinement_meta_json={"source": "real-reader-test"},
        scan_pattern_id=None,
        created_at=(NOW - timedelta(minutes=2)).replace(tzinfo=None),
        updated_at=(NOW - timedelta(minutes=2)).replace(tzinfo=None),
    )
    db.add(source)
    db.flush()
    marker = _binding_marker(family=family, source_id=int(source.id))
    marker["source_variant_sha256"] = initial.captured_paper_initial_variant_sha256(
        source
    )
    target = MomentumStrategyVariant(
        family=family,
        variant_key=f"captured_paper:{family}",
        version=1,
        label=source.label,
        params_json=dict(source.params_json),
        is_active=True,
        execution_family="alpaca_spot",
        parent_variant_id=int(source.id),
        refinement_meta_json={
            **dict(source.refinement_meta_json),
            BINDING_META_KEY: marker,
        },
        scan_pattern_id=None,
        created_at=(NOW - timedelta(minutes=1)).replace(tzinfo=None),
        updated_at=(NOW - timedelta(minutes=1)).replace(tzinfo=None),
    )
    legacy = MomentumStrategyVariant(
        family=f"legacy_{family}",
        variant_key=f"legacy_{family}",
        version=1,
        label="Legacy",
        params_json={},
        is_active=True,
        execution_family="alpaca_spot",
        parent_variant_id=None,
        refinement_meta_json={},
        scan_pattern_id=None,
        created_at=(NOW - timedelta(minutes=1)).replace(tzinfo=None),
        updated_at=(NOW - timedelta(minutes=1)).replace(tzinfo=None),
    )
    db.add_all((target, legacy))
    db.flush()
    authority = _authority((target,))
    initial_frontier = producer.ensure_captured_paper_selection_frontier(
        db,
        authority=authority,
        initialized_at=NOW - timedelta(seconds=10),
    )
    observation = CapturedPaperSelectionObservation(
        source_sequence=1,
        source_event_at=NOW - timedelta(seconds=4),
        source_available_at=NOW - timedelta(seconds=3),
        symbol="CAND",
        variant_id=int(target.id),
        viability_score=0.73,
        paper_eligible=True,
        live_eligible=True,
        regime_snapshot_json={"regime": "momentum"},
        execution_readiness_json={"captured": True},
        explain_json={"source": "real-reader-test"},
        evidence_window_json={"coverage": "complete"},
        correlation_id="real-reader-test",
    )
    batch = CapturedPaperSelectionBatch(
        authority_sha256=authority.authority_sha256,
        expected_frontier=initial_frontier,
        source_name="captured_queue",
        source_generation=SOURCE_GENERATION,
        queue_receipt_sha256=QUEUE_SHA,
        coverage_receipt_sha256=COVERAGE_SHA,
        source_sequence_from=0,
        source_sequence_through=1,
        watermark_at=NOW - timedelta(seconds=2),
        read_at=NOW - timedelta(seconds=1),
        observations=(observation,),
        route_state_updates=(_eligible_route_update(observation),),
    )
    first_result = producer.apply_captured_paper_selection_batch(
        db,
        authority=authority,
        batch=batch,
        recorded_at=NOW - timedelta(milliseconds=500),
    )
    db.add(
        MomentumSymbolViability(
            symbol="CAND",
            scope="symbol",
            variant_id=int(legacy.id),
            viability_score=0.99,
            paper_eligible=True,
            live_eligible=True,
            freshness_ts=(NOW - timedelta(seconds=1)).replace(tzinfo=None),
            regime_snapshot_json={},
            execution_readiness_json={"legacy": True},
            explain_json={},
            evidence_window_json={},
            source_node_id="legacy_viability",
            correlation_id="legacy-reader-test",
            created_at=(NOW - timedelta(seconds=1)).replace(tzinfo=None),
            updated_at=(NOW - timedelta(seconds=1)).replace(tzinfo=None),
        )
    )
    db.commit()
    before = db.query(MomentumSymbolViability).count()
    decision_at = datetime.now(UTC)
    reader = reader_module.SqlAlchemyCapturedPaperInitialCandidateReader(
        engine,
        authority=authority,
    )

    result = reader.read_candidates(
        user_id=41,
        symbol="CAND",
        decision_at=decision_at,
    )

    assert [row.variant.id for row in result.rows] == [int(target.id)]
    assert result.rows[0].viability.source_node_id == producer.SOURCE_NODE_ID
    assert sqlalchemy_inspect(result.rows[0].variant).detached
    assert sqlalchemy_inspect(result.rows[0].viability).detached
    db.rollback()
    assert db.query(MomentumSymbolViability).count() == before

    statements: list[str] = []

    def record_statement(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(str(statement).lower())

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        repeated = reader.read_candidates(
            user_id=41,
            symbol="CAND",
            decision_at=decision_at,
        )
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)
    assert len(repeated.rows) == 1
    assert not any(
        "from captured_paper_selection_frontier_events" in statement
        for statement in statements
    )

    # A newly constructed reader (the restart boundary) must inventory and
    # fully verify the immutable prefix rather than trusting process memory.
    statements.clear()
    restarted_reader = (
        reader_module.SqlAlchemyCapturedPaperInitialCandidateReader(
            engine,
            authority=authority,
        )
    )
    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        restarted = restarted_reader.read_candidates(
            user_id=41,
            symbol="CAND",
            decision_at=decision_at,
        )
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)
    assert len(restarted.rows) == 1
    assert any(
        "from captured_paper_selection_frontier_events" in statement
        for statement in statements
    )

    # A later route-local coverage failure must supersede CAND without
    # suppressing another symbol on the same intended strategy.  The stale
    # viability row deliberately remains in place to exercise the reader fence.
    other = CapturedPaperSelectionObservation(
        source_sequence=2,
        source_event_at=NOW - timedelta(milliseconds=400),
        source_available_at=NOW - timedelta(milliseconds=300),
        symbol="SAFE",
        variant_id=int(target.id),
        viability_score=0.81,
        paper_eligible=True,
        live_eligible=True,
        regime_snapshot_json={"regime": "momentum"},
        execution_readiness_json={"captured": True},
        explain_json={"source": "real-reader-test"},
        evidence_window_json={"coverage": "complete"},
        correlation_id="real-reader-other",
    )
    batch_other = CapturedPaperSelectionBatch(
        authority_sha256=authority.authority_sha256,
        expected_frontier=first_result.frontier,
        source_name="captured_queue",
        source_generation=SOURCE_GENERATION,
        queue_receipt_sha256=producer._hash_json({"queue": 2}),
        coverage_receipt_sha256=producer._hash_json({"coverage": 2}),
        source_sequence_from=1,
        source_sequence_through=2,
        watermark_at=NOW - timedelta(milliseconds=250),
        read_at=NOW - timedelta(milliseconds=200),
        observations=(other,),
        route_state_updates=(_eligible_route_update(other),),
    )
    other_result = producer.apply_captured_paper_selection_batch(
        db,
        authority=authority,
        batch=batch_other,
        recorded_at=NOW - timedelta(milliseconds=100),
    )
    unavailable_update = _unavailable_route_update(
        sequence=3,
        symbol="CAND",
        variant_id=int(target.id),
        event_at=NOW,
        available_at=NOW + timedelta(milliseconds=100),
    )
    unavailable_batch = CapturedPaperSelectionBatch(
        authority_sha256=authority.authority_sha256,
        expected_frontier=other_result.frontier,
        source_name="captured_queue",
        source_generation=SOURCE_GENERATION,
        queue_receipt_sha256=producer._hash_json({"queue": 3}),
        coverage_receipt_sha256=producer._hash_json({"coverage": 3}),
        source_sequence_from=2,
        source_sequence_through=3,
        watermark_at=NOW + timedelta(milliseconds=100),
        read_at=NOW + timedelta(milliseconds=200),
        observations=(),
        route_state_updates=(unavailable_update,),
    )
    unavailable_result = producer.apply_captured_paper_selection_batch(
        db,
        authority=authority,
        batch=unavailable_batch,
        recorded_at=NOW + timedelta(milliseconds=300),
    )
    db.commit()

    stale_cand = reader.read_candidates(
        user_id=41,
        symbol="CAND",
        decision_at=decision_at,
    )
    unaffected_other = reader.read_candidates(
        user_id=41,
        symbol="SAFE",
        decision_at=decision_at,
    )
    assert stale_cand.rows == ()
    assert [row.viability.symbol for row in unaffected_other.rows] == ["SAFE"]
    tombstone = (
        db.query(CapturedPaperSelectionRouteState)
        .filter(CapturedPaperSelectionRouteState.symbol == "CAND")
        .one()
    )
    assert tombstone.state == producer.ROUTE_COVERAGE_UNAVAILABLE
    assert tombstone.latest_source_sequence == 3
    assert unavailable_result.viability_upserts == 0
    assert unavailable_result.route_state_upserts == 1
    protected_tables = (
        "adaptive_risk_opportunity_claims",
        "adaptive_risk_reservations",
        "captured_paper_post_commit_outbox",
    )
    assert {
        table_name: db.execute(
            text(f"SELECT count(*) FROM {table_name}")
        ).scalar_one()
        for table_name in protected_tables
    } == {table_name: 0 for table_name in protected_tables}

    restored = CapturedPaperSelectionObservation(
        source_sequence=4,
        source_event_at=NOW + timedelta(milliseconds=400),
        source_available_at=NOW + timedelta(milliseconds=500),
        symbol="CAND",
        variant_id=int(target.id),
        viability_score=0.93,
        paper_eligible=True,
        live_eligible=True,
        regime_snapshot_json={"regime": "momentum"},
        execution_readiness_json={"captured": True},
        explain_json={"source": "real-reader-test-restored"},
        evidence_window_json={"coverage": "complete"},
        correlation_id="real-reader-restored",
    )
    restored_batch = CapturedPaperSelectionBatch(
        authority_sha256=authority.authority_sha256,
        expected_frontier=unavailable_result.frontier,
        source_name="captured_queue",
        source_generation=SOURCE_GENERATION,
        queue_receipt_sha256=producer._hash_json({"queue": 4}),
        coverage_receipt_sha256=producer._hash_json({"coverage": 4}),
        source_sequence_from=3,
        source_sequence_through=4,
        watermark_at=NOW + timedelta(milliseconds=500),
        read_at=NOW + timedelta(milliseconds=600),
        observations=(restored,),
        route_state_updates=(_eligible_route_update(restored),),
    )
    producer.apply_captured_paper_selection_batch(
        db,
        authority=authority,
        batch=restored_batch,
        recorded_at=NOW + timedelta(milliseconds=700),
    )
    db.commit()
    restored_read = reader.read_candidates(
        user_id=41,
        symbol="CAND",
        decision_at=decision_at,
    )
    assert len(restored_read.rows) == 1
    assert restored_read.rows[0].viability.viability_score == pytest.approx(0.93)
    restored_state = (
        db.query(CapturedPaperSelectionRouteState)
        .filter(CapturedPaperSelectionRouteState.symbol == "CAND")
        .one()
    )
    assert restored_state.state == producer.ROUTE_ELIGIBLE
    assert restored_state.latest_source_sequence == 4
    assert {
        table_name: db.execute(
            text(f"SELECT count(*) FROM {table_name}")
        ).scalar_one()
        for table_name in protected_tables
    } == {table_name: 0 for table_name in protected_tables}

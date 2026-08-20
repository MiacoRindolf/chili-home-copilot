from __future__ import annotations

import copy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


def test_pipeline_rolls_back_probe_transaction_before_persistence(monkeypatch):
    from app.services.trading import market_data
    from app.services.trading.momentum_neural import persistence
    from app.services.trading.momentum_neural import pipeline as P

    class _FakeDB:
        def __init__(self):
            self.poisoned = False
            self.rollbacks = 0

        def rollback(self):
            self.rollbacks += 1
            self.poisoned = False

    class _FakeViability:
        def to_public_dict(self):
            return {
                "family_id": "impulse_breakout",
                "viability": 0.7,
                "paper_eligible": True,
                "live_eligible": True,
                "warnings": [],
            }

    db = _FakeDB()
    states = {}
    observed = {}
    family = SimpleNamespace(
        family_id="impulse_breakout",
        label="Impulse breakout",
        entry_style="pullback",
        default_stop_logic="risk",
        default_exit_logic="strength",
    )

    def _poisoning_book_imbalance(_symbol, db=None):
        db.poisoned = True
        return None

    def _state_after_clean_transaction(seen_db, _node_id):
        assert seen_db is db
        assert seen_db.poisoned is False
        assert seen_db.rollbacks >= 1
        state = SimpleNamespace(local_state={}, last_activated_at=None, updated_at=None)
        states[_node_id] = state
        return state

    def _persist_after_clean_transaction(seen_db, **_kwargs):
        assert seen_db is db
        assert seen_db.poisoned is False
        observed["persisted_at"] = _kwargs.get("observed_at")
        return 1

    def _record_trace(seen_db, *, snapshot, observed_at=None, **_kwargs):
        assert seen_db is db
        assert snapshot.get("top_family_id") == "impulse_breakout"
        observed["trace_at"] = observed_at

    monkeypatch.setattr(market_data, "fetch_ohlcv_df", lambda *_a, **_k: None)
    monkeypatch.setattr(P, "_live_book_imbalance", _poisoning_book_imbalance)
    monkeypatch.setattr(P, "_live_ofi_microprice", lambda *_a, **_k: (None, None))
    monkeypatch.setattr(P, "_live_trade_flow", lambda *_a, **_k: None)
    monkeypatch.setattr(P, "iter_momentum_families", lambda: [family])
    monkeypatch.setattr(P, "score_viability", lambda *_a, **_k: _FakeViability())
    monkeypatch.setattr(P, "get_or_create_state", _state_after_clean_transaction)
    monkeypatch.setattr(
        P,
        "_get_or_create_hub_state_for_update",
        lambda seen_db: _state_after_clean_transaction(
            seen_db,
            P.HUB_NODE_ID,
        ),
    )
    monkeypatch.setattr(P, "record_evolution_trace", _record_trace)
    monkeypatch.setattr(persistence, "persist_neural_momentum_tick", _persist_after_clean_transaction)

    decision_at = datetime(2026, 7, 13, 13, 5, tzinfo=timezone.utc)
    result = P.run_momentum_neural_tick(
        db,
        meta={"tickers": ["MOVE"]},
        decision_as_of_utc=decision_at,
    )

    assert result["persistence_ok"] is True
    assert db.rollbacks == 1
    expected = decision_at.replace(tzinfo=None)
    assert observed == {"persisted_at": expected, "trace_at": expected}
    assert states[P.HUB_NODE_ID].last_activated_at == expected
    assert states[P.HUB_NODE_ID].updated_at == expected
    assert states[P.HUB_NODE_ID].local_state["last_tick_utc"] == expected.isoformat()
    assert states[P.VIABILITY_NODE_ID].last_activated_at == expected
    assert states[P.VIABILITY_NODE_ID].updated_at == expected
    assert states[P.VIABILITY_NODE_ID].local_state["last_tick_utc"] == expected.isoformat()


def test_ortex_handoff_cap_rejection_rolls_back_and_escapes_persistence_guard(
    monkeypatch,
):
    from app.services.trading import market_data
    from app.services.trading.momentum_neural import persistence
    from app.services.trading.momentum_neural import pipeline as P
    from app.services.trading.momentum_neural.ortex_handoff_history import (
        OrtexHandoffHistoryError,
        OrtexHandoffHistoryMetrics,
        OrtexHandoffReason,
        ortex_handoff_runtime_metrics,
    )

    class _FakeDB:
        def __init__(self):
            self.poisoned = False
            self.rollbacks = 0

        def rollback(self):
            self.rollbacks += 1
            self.poisoned = False

    class _FakeViability:
        def to_public_dict(self):
            return {
                "family_id": "impulse_breakout",
                "viability": 0.7,
                "paper_eligible": True,
                "live_eligible": True,
                "warnings": [],
            }

    db = _FakeDB()
    hub = SimpleNamespace(
        local_state={"sentinel": "must-survive"},
        last_activated_at=None,
        updated_at=None,
    )
    family = SimpleNamespace(
        family_id="impulse_breakout",
        label="Impulse breakout",
        entry_style="pullback",
        default_stop_logic="risk",
        default_exit_logic="strength",
    )
    persistence_calls = []

    monkeypatch.setattr(market_data, "fetch_ohlcv_df", lambda *_a, **_k: None)
    monkeypatch.setattr(P, "_live_book_imbalance", lambda *_a, **_k: None)
    monkeypatch.setattr(P, "_live_ofi_microprice", lambda *_a, **_k: (None, None))
    monkeypatch.setattr(P, "_live_trade_flow", lambda *_a, **_k: None)
    monkeypatch.setattr(P, "iter_momentum_families", lambda: [family])
    monkeypatch.setattr(P, "score_viability", lambda *_a, **_k: _FakeViability())
    monkeypatch.setattr(
        P,
        "_validate_ortex_batch_status",
        lambda *_a, **_k: (True, "ortex_batch_status_valid"),
    )
    monkeypatch.setattr(
        P,
        "_get_or_create_hub_state_for_update",
        lambda seen_db: hub,
    )
    monkeypatch.setattr(
        P,
        "get_or_create_state",
        lambda *_a, **_k: SimpleNamespace(
            local_state={}, last_activated_at=None, updated_at=None
        ),
    )
    monkeypatch.setattr(P, "record_evolution_trace", lambda *_a, **_k: None)
    monkeypatch.setattr(
        persistence,
        "persist_neural_momentum_tick",
        lambda *_a, **_k: persistence_calls.append(True),
    )
    attempted = OrtexHandoffHistoryMetrics(
        retention_seconds=120,
        max_entries=1,
        max_canonical_bytes=1024,
        entry_count=2,
        canonical_bytes=900,
        entries_sha256="0" * 64,
        pruned_expired_total=0,
    )

    def reject_cap(*_args, **_kwargs):
        raise OrtexHandoffHistoryError(
            OrtexHandoffReason.COUNT_CAP_EXCEEDED,
            metrics=attempted,
        )

    monkeypatch.setattr(P, "stage_ortex_handoff_publication", reject_cap)
    before_metrics = ortex_handoff_runtime_metrics()
    decision_at = datetime(2026, 8, 19, 16, 0, tzinfo=timezone.utc)
    prepared = {
        "batch_sha256": "a" * 64,
        "decision_at": decision_at.isoformat(),
        "complete": False,
        "members": [],
    }

    with pytest.raises(OrtexHandoffHistoryError) as caught:
        P.run_momentum_neural_tick(
            db,
            meta={
                "tickers": ["MOVE"],
                P.ORTEX_SQUEEZE_BATCH_STATUS_KEY: prepared,
            },
            decision_as_of_utc=decision_at,
        )

    assert caught.value.reason is OrtexHandoffReason.COUNT_CAP_EXCEEDED
    # One rollback clears best-effort probes; the second is the mandatory
    # fail-closed rollback of the locked hub/viability transaction.
    assert db.rollbacks == 2
    assert hub.local_state == {"sentinel": "must-survive"}
    assert persistence_calls == []
    after_metrics = ortex_handoff_runtime_metrics()
    assert after_metrics.cap_rejects == before_metrics.cap_rejects + 1
    assert (
        after_metrics.count_cap_rejects
        == before_metrics.count_cap_rejects + 1
    )


@pytest.mark.parametrize(
    "failure_point",
    (
        "hub_assignment",
        "pool_lookup",
        "pool_assignment",
        "evolution_trace",
        "viability_exception",
        "viability_empty",
    ),
)
def test_ortex_post_stage_failures_rollback_before_caller_flush(
    monkeypatch,
    failure_point: str,
) -> None:
    from app.services.trading import market_data
    from app.services.trading.momentum_neural import persistence
    from app.services.trading.momentum_neural import pipeline as P
    from app.services.trading.momentum_neural.ortex_handoff_history import (
        ORTEX_BATCH_STATUS_KEY,
        OrtexHandoffHistoryError,
        OrtexHandoffReason,
    )

    decision_at = datetime(2026, 8, 19, 16, 0, tzinfo=timezone.utc)
    first = {
        "schema_version": "chili.ortex.squeeze-fuel-batch.v1",
        "decision_at": decision_at.isoformat(),
        "complete": True,
        "quota_policy_sha256": "c" * 64,
        "selected_symbols": ["MOVE"],
        "members": [],
        "members_sha256": "d" * 64,
        "batch_sha256": "a" * 64,
    }
    second = {**first, "batch_sha256": "b" * 64}
    caller_owned_second = copy.deepcopy(second)
    original_hub_state = {
        "sentinel": "durable-before-publication",
        ORTEX_BATCH_STATUS_KEY: first,
    }

    class _TransactionalState:
        def __init__(
            self,
            local_state: dict,
            *,
            fail_assignment: bool = False,
        ) -> None:
            self._local_state = copy.deepcopy(local_state)
            self._durable_local_state = copy.deepcopy(local_state)
            self.fail_assignment = fail_assignment
            self.publication_assignments = 0
            self.last_activated_at = None
            self.updated_at = None

        @property
        def local_state(self) -> dict:
            return self._local_state

        @local_state.setter
        def local_state(self, value: dict) -> None:
            self._local_state = copy.deepcopy(value)
            current = self._local_state.get(ORTEX_BATCH_STATUS_KEY)
            if isinstance(current, dict) and current.get("batch_sha256") == "b" * 64:
                self.publication_assignments += 1
            if self.fail_assignment:
                self.fail_assignment = False
                raise RuntimeError("injected state assignment failure")

        def rollback(self) -> None:
            self._local_state = copy.deepcopy(self._durable_local_state)

        def commit(self) -> None:
            self._durable_local_state = copy.deepcopy(self._local_state)

    hub = _TransactionalState(
        original_hub_state,
        fail_assignment=(failure_point == "hub_assignment"),
    )
    pool = _TransactionalState(
        {},
        fail_assignment=(failure_point == "pool_assignment"),
    )

    class _FakeDB:
        def __init__(self) -> None:
            self.rollbacks = 0
            self.flushes = 0
            self.commits = 0

        def rollback(self) -> None:
            self.rollbacks += 1
            hub.rollback()
            pool.rollback()

        def flush(self) -> None:
            self.flushes += 1

        def commit(self) -> None:
            self.commits += 1
            hub.commit()
            pool.commit()

    class _FakeViability:
        def to_public_dict(self) -> dict:
            return {
                "family_id": "impulse_breakout",
                "viability": 0.7,
                "paper_eligible": True,
                "live_eligible": True,
                "warnings": [],
            }

    family = SimpleNamespace(
        family_id="impulse_breakout",
        label="Impulse breakout",
        entry_style="pullback",
        default_stop_logic="risk",
        default_exit_logic="strength",
    )
    db = _FakeDB()
    persistence_calls = []
    persisted_reference_hashes = []

    monkeypatch.setattr(market_data, "fetch_ohlcv_df", lambda *_a, **_k: None)
    monkeypatch.setattr(P, "_live_book_imbalance", lambda *_a, **_k: None)
    monkeypatch.setattr(P, "_live_ofi_microprice", lambda *_a, **_k: (None, None))
    monkeypatch.setattr(P, "_live_trade_flow", lambda *_a, **_k: None)
    monkeypatch.setattr(P, "iter_momentum_families", lambda: [family])
    monkeypatch.setattr(P, "score_viability", lambda *_a, **_k: _FakeViability())

    def _validate_detached_snapshot(status, **_kwargs):
        assert status is not caller_owned_second
        caller_owned_second["batch_sha256"] = "e" * 64
        return True, "ortex_batch_status_valid"

    monkeypatch.setattr(P, "_validate_ortex_batch_status", _validate_detached_snapshot)
    monkeypatch.setattr(P, "_get_or_create_hub_state_for_update", lambda _db: hub)

    def _get_pool(_db, _node_id):
        if failure_point == "pool_lookup":
            raise RuntimeError("injected pool lookup failure")
        return pool

    monkeypatch.setattr(P, "get_or_create_state", _get_pool)

    def _record_trace(*_args, **_kwargs) -> None:
        if failure_point == "evolution_trace":
            raise RuntimeError("injected evolution trace failure")

    monkeypatch.setattr(P, "record_evolution_trace", _record_trace)

    def _persist(*_args, **_kwargs):
        persistence_calls.append(True)
        extra = _kwargs["features"].to_public_dict()["extra"]
        persisted_reference_hashes.append(
            extra[P.ORTEX_SQUEEZE_BATCH_STATUS_KEY]["batch_sha256"]
        )
        if failure_point == "viability_exception":
            raise RuntimeError("injected viability persistence failure")
        if failure_point == "viability_empty":
            return 0
        return 1

    monkeypatch.setattr(persistence, "persist_neural_momentum_tick", _persist)

    with pytest.raises(Exception) as caught:
        P.run_momentum_neural_tick(
            db,
            meta={
                "tickers": ["MOVE"],
                P.ORTEX_SQUEEZE_BATCH_STATUS_KEY: caller_owned_second,
            },
            decision_as_of_utc=decision_at,
        )

    if failure_point == "viability_empty":
        assert isinstance(caught.value, OrtexHandoffHistoryError)
        assert (
            caught.value.reason
            is OrtexHandoffReason.VIABILITY_PUBLICATION_EMPTY
        )
    else:
        assert isinstance(caught.value, RuntimeError)
    assert db.rollbacks == 2
    assert caller_owned_second["batch_sha256"] == "e" * 64
    assert hub.publication_assignments == 1
    assert hub.local_state == original_hub_state
    assert persistence_calls == (
        [True]
        if failure_point in {"viability_exception", "viability_empty"}
        else []
    )
    assert persisted_reference_hashes == (
        ["b" * 64]
        if failure_point in {"viability_exception", "viability_empty"}
        else []
    )

    # Simulate activation_runner catching the error, then flushing/committing
    # the same session. The explicit rollback must leave no hub-only manifest.
    db.flush()
    db.commit()
    assert hub._durable_local_state == original_hub_state


def test_replay_pipeline_guard_fails_before_provider_or_db_mutation(monkeypatch):
    from app.services.trading import market_data
    from app.services.trading.momentum_neural import live_runner as lr
    from app.services.trading.momentum_neural import pipeline as P

    class _UntouchedDB:
        def __getattr__(self, name):
            raise AssertionError(f"replay pipeline touched DB before preflight: {name}")

    provider_calls = []

    def _forbidden_provider(*args, **kwargs):
        provider_calls.append((args, kwargs))
        raise AssertionError("live OHLCV provider reached during replay preflight")

    monkeypatch.setattr(market_data, "fetch_ohlcv_df", _forbidden_provider)
    decision_at = datetime(2026, 7, 13, 13, 5)
    with lr.replay_clock(decision_at):
        with pytest.raises(
            P.ReplayPipelineInputUnavailableError,
            match="selection_pipeline inputs are unavailable",
        ):
            P.run_momentum_neural_tick(
                _UntouchedDB(),
                meta={"tickers": ["CLRO"]},
                decision_as_of_utc=decision_at,
            )

    assert provider_calls == []


def test_evolution_trace_uses_explicit_replay_observation_time(monkeypatch):
    from app.services.trading.momentum_neural import evolution

    state = SimpleNamespace(local_state={}, updated_at=None)
    monkeypatch.setattr(
        evolution,
        "get_or_create_state",
        lambda _db, _node_id: state,
    )
    observed_at = datetime(2026, 7, 13, 13, 5, tzinfo=timezone.utc)

    evolution.record_evolution_trace(
        object(),
        snapshot={
            "top_family_id": "impulse_breakout",
            "top_viability": 0.91,
            "session_label": "premarket",
        },
        observed_at=observed_at,
    )

    expected = observed_at.replace(tzinfo=None)
    assert state.updated_at == expected
    assert state.local_state["trace"] == [
        {
            "at_utc": expected.isoformat(),
            "top_family": "impulse_breakout",
            "top_viability": 0.91,
            "regime_session": "premarket",
        }
    ]

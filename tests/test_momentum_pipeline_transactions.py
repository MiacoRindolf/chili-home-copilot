from __future__ import annotations

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

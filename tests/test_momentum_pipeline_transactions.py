from __future__ import annotations

from types import SimpleNamespace


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
        return SimpleNamespace(local_state={}, last_activated_at=None, updated_at=None)

    def _persist_after_clean_transaction(seen_db, **_kwargs):
        assert seen_db is db
        assert seen_db.poisoned is False
        return 1

    monkeypatch.setattr(market_data, "fetch_ohlcv_df", lambda *_a, **_k: None)
    monkeypatch.setattr(P, "_live_book_imbalance", _poisoning_book_imbalance)
    monkeypatch.setattr(P, "_live_ofi_microprice", lambda *_a, **_k: (None, None))
    monkeypatch.setattr(P, "_live_trade_flow", lambda *_a, **_k: None)
    monkeypatch.setattr(P, "iter_momentum_families", lambda: [family])
    monkeypatch.setattr(P, "score_viability", lambda *_a, **_k: _FakeViability())
    monkeypatch.setattr(P, "get_or_create_state", _state_after_clean_transaction)
    monkeypatch.setattr(P, "record_evolution_trace", lambda *_a, **_k: None)
    monkeypatch.setattr(persistence, "persist_neural_momentum_tick", _persist_after_clean_transaction)

    result = P.run_momentum_neural_tick(db, meta={"tickers": ["MOVE"]})

    assert result["persistence_ok"] is True
    assert db.rollbacks == 1

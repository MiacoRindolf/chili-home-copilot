from __future__ import annotations

from pathlib import Path

from app.services.trading import management_envelopes
from app.services.trading.management_envelopes import MANAGEMENT_ENVELOPES_RELATION
from app.services.trading.venue import coinbase_orphan_adopt


class _Result:
    def mappings(self):
        return self

    def all(self):
        return []


class _Db:
    def __init__(self):
        self.sql: list[str] = []
        self.params: list[dict] = []

    def execute(self, sql, params=None):
        self.sql.append(str(sql))
        self.params.append(dict(params or {}))
        return _Result()


def test_coinbase_orphan_candidates_use_management_envelopes_relation():
    db = _Db()

    rows = management_envelopes.load_coinbase_orphan_adoption_candidates(
        db,
        adoptable_states=("intent", "terminal_reject"),
    )

    assert rows == []
    sql = db.sql[-1]
    assert f"JOIN {MANAGEMENT_ENVELOPES_RELATION} t ON t.id = bi.trade_id" in sql
    assert "JOIN trading_trades" not in sql
    assert "FROM trading_trades" not in sql
    assert "LOWER(COALESCE(t.broker_source, '')) = 'coinbase'" in sql
    assert db.params[-1] == {"states": ["intent", "terminal_reject"]}


def test_coinbase_orphan_adoption_no_longer_has_raw_trade_reader():
    src = Path("app/services/trading/venue/coinbase_orphan_adopt.py").read_text(
        encoding="utf-8"
    )

    assert "JOIN trading_trades" not in src
    assert "FROM trading_trades" not in src


def test_coinbase_orphan_loader_maps_envelope_rows(monkeypatch):
    def fake_candidates(db, *, adoptable_states):
        assert adoptable_states == coinbase_orphan_adopt._ADOPTABLE_STATES
        return [
            {
                "intent_id": "11",
                "trade_id": "22",
                "ticker": "btc",
                "quantity": "3.5",
                "intent_state": "INTENT",
                "broker_source": "COINBASE",
            },
            {
                "intent_id": "bad",
                "trade_id": "ignored",
                "ticker": "bad",
                "quantity": "bad",
                "intent_state": "intent",
                "broker_source": "coinbase",
            },
        ]

    monkeypatch.setattr(
        coinbase_orphan_adopt,
        "load_coinbase_orphan_adoption_candidates",
        fake_candidates,
    )

    rows = coinbase_orphan_adopt._load_naked_coinbase_intents(object())

    assert len(rows) == 1
    assert rows[0].intent_id == 11
    assert rows[0].trade_id == 22
    assert rows[0].ticker == "BTC"
    assert rows[0].quantity == 3.5
    assert rows[0].intent_state == "intent"
    assert rows[0].broker_source == "coinbase"

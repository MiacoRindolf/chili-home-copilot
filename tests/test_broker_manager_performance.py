from __future__ import annotations

from app.services import broker_manager


def test_combined_positions_can_skip_equity_sort(monkeypatch) -> None:
    monkeypatch.setattr(broker_manager.broker_service, "is_connected", lambda: True)
    monkeypatch.setattr(broker_manager.coinbase_service, "is_connected", lambda: False)
    monkeypatch.setattr(
        broker_manager.broker_service,
        "get_positions",
        lambda: [
            {"ticker": "LOW", "equity": 10.0},
            {"ticker": "HIGH", "equity": 20.0},
        ],
    )
    monkeypatch.setattr(
        broker_manager.broker_service,
        "get_crypto_positions",
        lambda: [{"ticker": "MID", "equity": 15.0}],
    )

    sorted_positions = broker_manager.get_combined_positions()
    unsorted_positions = broker_manager.get_combined_positions(sort_by_equity=False)

    assert [p["ticker"] for p in sorted_positions] == ["HIGH", "MID", "LOW"]
    assert [p["ticker"] for p in unsorted_positions] == ["LOW", "HIGH", "MID"]
    assert all(p["broker_source"] == broker_manager.BROKER_ROBINHOOD for p in unsorted_positions)


def test_duplicate_position_check_skips_unneeded_sort(monkeypatch) -> None:
    calls: list[bool] = []

    def fake_positions(*, fresh: bool = False, sort_by_equity: bool = True) -> list[dict]:
        calls.append(sort_by_equity)
        return [
            {"ticker": "AAPL", "quantity": 1, "broker_source": broker_manager.BROKER_ROBINHOOD},
            {"ticker": "AAPL", "quantity": 2, "broker_source": broker_manager.BROKER_COINBASE},
        ]

    monkeypatch.setattr(broker_manager, "get_combined_positions", fake_positions)

    assert broker_manager.check_duplicate_position("aapl") == [
        broker_manager.BROKER_ROBINHOOD,
        broker_manager.BROKER_COINBASE,
    ]
    assert calls == [False]

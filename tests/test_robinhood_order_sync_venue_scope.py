from __future__ import annotations

from types import SimpleNamespace

from app.services import broker_service


def test_robinhood_entry_order_sync_candidate_excludes_coinbase():
    assert (
        broker_service._is_robinhood_entry_order_sync_candidate(
            SimpleNamespace(broker_source="coinbase")
        )
        is False
    )


def test_robinhood_entry_order_sync_candidate_allows_robinhood_and_legacy_rows():
    assert (
        broker_service._is_robinhood_entry_order_sync_candidate(
            SimpleNamespace(broker_source="robinhood")
        )
        is True
    )
    assert (
        broker_service._is_robinhood_entry_order_sync_candidate(
            SimpleNamespace(broker_source=None)
        )
        is True
    )

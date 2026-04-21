"""Tests for Robinhood VenueAdapter (robinhood_spot)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.trading.venue.protocol import NormalizedOrder, NormalizedTicker
from app.services.trading.venue.robinhood_spot import (
    RobinhoodSpotAdapter,
    _normalize_rh_order,
    _to_ticker,
)


# ── Helpers ────────────────────────────────────────────────────────────


def _mock_quote(bid: float = 150.0, ask: float = 150.10, last: float = 150.05) -> dict:
    return {
        "bid_price": str(bid),
        "ask_price": str(ask),
        "last_trade_price": str(last),
        "bid_size": "100",
        "ask_size": "200",
    }


def _mock_rh_order(
    order_id: str = "abc123",
    state: str = "filled",
    side: str = "buy",
    qty: float = 10.0,
    avg_price: float = 150.0,
) -> dict:
    return {
        "id": order_id,
        "state": state,
        "side": side,
        "cumulative_quantity": str(qty),
        "average_price": str(avg_price),
        "symbol": "AAPL",
        "type": "market",
        "created_at": "2024-01-01T10:00:00Z",
    }


# ── Unit tests ─────────────────────────────────────────────────────────


def test_to_ticker_strips_usd():
    assert _to_ticker("AAPL-USD") == "AAPL"
    assert _to_ticker("AAPL") == "AAPL"
    assert _to_ticker("btc-usd") == "BTC"
    assert _to_ticker("  msft  ") == "MSFT"


def test_normalize_rh_order_filled():
    od = _mock_rh_order(state="filled", qty=10, avg_price=150.0)
    norm = _normalize_rh_order(od)
    assert isinstance(norm, NormalizedOrder)
    assert norm.order_id == "abc123"
    assert norm.status == "open"  # map_rh_status("filled") -> "open"
    assert norm.filled_size == 10.0
    assert norm.average_filled_price == 150.0
    assert norm.side == "buy"
    assert norm.product_id == "AAPL"


def test_normalize_rh_order_cancelled():
    od = _mock_rh_order(state="cancelled")
    norm = _normalize_rh_order(od)
    assert norm.status == "cancelled"


def test_normalize_rh_order_queued():
    od = _mock_rh_order(state="queued")
    norm = _normalize_rh_order(od)
    assert norm.status == "working"


@patch("app.services.trading.venue.robinhood_spot.rh", create=True)
def test_is_enabled_false_when_config_off(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "chili_robinhood_spot_adapter_enabled", False, raising=False)
    adapter = RobinhoodSpotAdapter()
    assert adapter.is_enabled() is False


@patch("app.services.broker_service.is_connected", return_value=True)
def test_is_enabled_true_when_config_on(mock_conn, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "chili_robinhood_spot_adapter_enabled", True, raising=False)
    adapter = RobinhoodSpotAdapter()
    assert adapter.is_enabled() is True


@patch("robin_stocks.robinhood.stocks.get_quotes")
@patch("app.services.broker_service.is_connected", return_value=True)
def test_get_best_bid_ask(mock_conn, mock_quotes, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "chili_robinhood_spot_adapter_enabled", True, raising=False)
    mock_quotes.return_value = [_mock_quote(bid=150.0, ask=150.10, last=150.05)]

    adapter = RobinhoodSpotAdapter()
    tick, fresh = adapter.get_best_bid_ask("AAPL")

    assert tick is not None
    assert isinstance(tick, NormalizedTicker)
    assert tick.bid == 150.0
    assert tick.ask == 150.10
    assert tick.mid == pytest.approx(150.05)
    assert tick.spread_abs == pytest.approx(0.10)
    assert tick.spread_bps is not None and tick.spread_bps > 0
    assert tick.product_id == "AAPL"


@patch("robin_stocks.robinhood.stocks.get_quotes")
@patch("app.services.broker_service.is_connected", return_value=True)
def test_get_best_bid_ask_returns_none_on_empty(mock_conn, mock_quotes, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "chili_robinhood_spot_adapter_enabled", True, raising=False)
    mock_quotes.return_value = [None]

    adapter = RobinhoodSpotAdapter()
    tick, fresh = adapter.get_best_bid_ask("AAPL")
    assert tick is None


@patch("app.services.broker_service.place_buy_order")
@patch("app.services.broker_service.is_connected", return_value=True)
def test_place_market_order_buy(mock_conn, mock_buy, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "chili_robinhood_spot_adapter_enabled", True, raising=False)
    mock_buy.return_value = {"ok": True, "order_id": "ord1", "state": "filled", "raw": {}}

    adapter = RobinhoodSpotAdapter()
    result = adapter.place_market_order(product_id="AAPL", side="buy", base_size="10")

    assert result["ok"] is True
    assert result["order_id"] == "ord1"
    # Adapter forwards market/extended-hours overrides as kwargs (None when unset).
    mock_buy.assert_called_once_with(
        "AAPL", 10.0,
        order_type="market",
        market_hours_override=None,
        extended_hours_override=None,
    )


@patch("app.services.broker_service.place_sell_order")
@patch("app.services.broker_service.is_connected", return_value=True)
def test_place_market_order_sell(mock_conn, mock_sell, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "chili_robinhood_spot_adapter_enabled", True, raising=False)
    mock_sell.return_value = {"ok": True, "order_id": "ord2", "state": "filled", "raw": {}}

    adapter = RobinhoodSpotAdapter()
    result = adapter.place_market_order(product_id="AAPL", side="sell", base_size="5")

    assert result["ok"] is True
    mock_sell.assert_called_once_with(
        "AAPL", 5.0,
        order_type="market",
        market_hours_override=None,
        extended_hours_override=None,
    )


@patch("app.services.broker_service.place_buy_order")
@patch("app.services.broker_service.is_connected", return_value=True)
def test_place_market_order_duplicate_client_order_id_short_circuits(
    mock_conn, mock_buy, db, monkeypatch
):
    """P0.1 — the idempotency store must refuse a retry BEFORE the adapter
    calls the broker. A network-flake retry of an already-submitted order
    would otherwise double-buy.
    """
    from sqlalchemy import text

    from app.config import settings
    from app.services.trading.venue import idempotency_store

    monkeypatch.setattr(settings, "chili_robinhood_spot_adapter_enabled", True, raising=False)
    mock_buy.return_value = {"ok": True, "order_id": "ord-dupe-1", "raw": {}}

    cid = "test-dupe-check-1"
    db.execute(
        text("DELETE FROM venue_order_idempotency WHERE client_order_id = :k"),
        {"k": cid},
    )
    db.commit()
    idempotency_store.reset_for_tests()

    adapter = RobinhoodSpotAdapter()

    first = adapter.place_market_order(
        product_id="AAPL", side="buy", base_size="1", client_order_id=cid,
    )
    assert first["ok"] is True
    assert mock_buy.call_count == 1

    # Second call with the same client_order_id must NOT reach the broker.
    second = adapter.place_market_order(
        product_id="AAPL", side="buy", base_size="1", client_order_id=cid,
    )
    assert second["ok"] is False
    assert second.get("error") == "duplicate_client_order_id"
    assert mock_buy.call_count == 1, "broker was called again on duplicate — idempotency failed"

    db.execute(
        text("DELETE FROM venue_order_idempotency WHERE client_order_id = :k"),
        {"k": cid},
    )
    db.commit()
    idempotency_store.reset_for_tests()


@patch("app.services.broker_service.get_order_by_id")
@patch("app.services.broker_service.is_connected", return_value=True)
def test_get_order_found(mock_conn, mock_get, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "chili_robinhood_spot_adapter_enabled", True, raising=False)
    mock_get.return_value = _mock_rh_order(order_id="xyz", state="filled")

    adapter = RobinhoodSpotAdapter()
    order, fresh = adapter.get_order("xyz")

    assert order is not None
    assert order.order_id == "xyz"
    assert order.status == "open"  # filled -> open in chili mapping


@patch("app.services.broker_service.get_order_by_id")
@patch("app.services.broker_service.is_connected", return_value=True)
def test_get_order_not_found(mock_conn, mock_get, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "chili_robinhood_spot_adapter_enabled", True, raising=False)
    mock_get.return_value = None

    adapter = RobinhoodSpotAdapter()
    order, fresh = adapter.get_order("nonexistent")
    assert order is None


def test_execution_family_registry_includes_robinhood():
    from app.services.trading.execution_family_registry import (
        EXECUTION_FAMILY_ROBINHOOD_SPOT,
        is_momentum_automation_implemented,
        resolve_live_spot_adapter_factory,
    )

    assert is_momentum_automation_implemented(EXECUTION_FAMILY_ROBINHOOD_SPOT)
    factory = resolve_live_spot_adapter_factory(EXECUTION_FAMILY_ROBINHOOD_SPOT)
    assert factory is RobinhoodSpotAdapter


# ── Fault-injection (Phase B) ───────────────────────────────────────────
#
# The four scenarios below pin the RH adapter's behavior under upstream
# failure modes that previously only surfaced in prod. They complement
# (not replace) the DB-level dup-CID test above, so the adapter's
# contract is pinned against timeouts, broker-side 429s, partial fills,
# and the in-process dup-CID fast path.


def _purge_idempotency_cid(db, cid: str) -> None:
    """Delete any leftover ``venue_order_idempotency`` rows for this CID.

    Fault-injection tests below use the real DB idempotency table (shared
    by all adapter tests) but do NOT use the per-test ``db`` fixture's
    TRUNCATE — they're fast unit tests that pin adapter behavior. This
    helper ensures a clean slate for the specific CIDs each test uses.
    """
    from sqlalchemy import text as _sql_text
    db.execute(
        _sql_text("DELETE FROM venue_order_idempotency WHERE client_order_id = :cid"),
        {"cid": cid},
    )
    db.commit()


@patch("app.services.broker_service.place_buy_order")
@patch("app.services.broker_service.is_connected", return_value=True)
def test_place_market_order_timeout_propagates(mock_conn, mock_buy, db, monkeypatch):
    """RH adapter does NOT wrap broker_service; a TimeoutError bubbles out.

    This pins today's behavior so the Phase B `_current_price` retry loop
    (which DOES catch TimeoutError and retries) is unambiguously the layer
    that handles transient upstream timeouts — the adapter stays dumb.
    """
    from app.config import settings
    from app.services.trading.venue.robinhood_spot import (
        reset_duplicate_client_order_guard_for_tests,
    )

    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_robinhood_spot_adapter_enabled", True, raising=False)
    mock_buy.side_effect = TimeoutError("RH upstream timeout after 30s")

    adapter = RobinhoodSpotAdapter()
    with pytest.raises(TimeoutError, match="upstream timeout"):
        adapter.place_market_order(product_id="AAPL", side="buy", base_size="10")


@patch("app.services.broker_service.place_buy_order")
@patch("app.services.broker_service.is_connected", return_value=True)
def test_place_market_order_broker_returns_429(mock_conn, mock_buy, db, monkeypatch):
    """Broker-side 429 (upstream rate limit) surfaces as ok=False with the
    raw error intact. REJECTED state gets recorded; no idempotency row is
    committed (so the caller can legitimately retry after backoff).
    """
    from app.config import settings
    from app.services.trading.venue.robinhood_spot import (
        reset_duplicate_client_order_guard_for_tests,
    )

    cid = "cid-429-1"
    _purge_idempotency_cid(db, cid)
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_robinhood_spot_adapter_enabled", True, raising=False)
    mock_buy.return_value = {
        "ok": False,
        "error": "rate_limited_by_broker",
        "status_code": 429,
        "raw": {"detail": "Request throttled"},
    }

    adapter = RobinhoodSpotAdapter()
    out = adapter.place_market_order(
        product_id="AAPL", side="buy", base_size="1", client_order_id=cid,
    )
    assert out["ok"] is False
    assert out["error"] == "rate_limited_by_broker"
    assert out["status_code"] == 429
    _purge_idempotency_cid(db, cid)


@patch("app.services.broker_service.get_order_by_id")
@patch("app.services.broker_service.is_connected", return_value=True)
def test_get_order_partial_fill_reports_working(mock_conn, mock_get, monkeypatch):
    """A partially-filled RH order maps to status='working' (via map_rh_status)
    with filled_size reflecting the partial. Sizers downstream must see
    both the in-flight size and the known fill, not a terminal 'open'.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "chili_robinhood_spot_adapter_enabled", True, raising=False)
    mock_get.return_value = _mock_rh_order(
        order_id="partial-1",
        state="partially_filled",
        qty=4.0,  # 4 of 10 filled
        avg_price=150.25,
    )

    adapter = RobinhoodSpotAdapter()
    order, _fresh = adapter.get_order("partial-1")

    assert order is not None
    assert order.order_id == "partial-1"
    assert order.status == "working"  # partially_filled maps to working, not open
    assert order.filled_size == 4.0
    assert order.average_filled_price == 150.25


@patch("app.services.broker_service.place_buy_order")
@patch("app.services.broker_service.is_connected", return_value=True)
def test_place_market_order_duplicate_cid_intra_process(mock_conn, mock_buy, db, monkeypatch):
    """In-process memory guard: two back-to-back calls with the same CID
    inside the same adapter instance must short-circuit the second call,
    even when the DB idempotency row lookup has not yet been written
    (network-partition scenario). Complements the DB-level dup-CID test
    above by exercising the fast-path branch in is_duplicate().
    """
    from app.config import settings
    from app.services.trading.venue.robinhood_spot import (
        reset_duplicate_client_order_guard_for_tests,
    )

    cid = "intra-proc-cid-1"
    _purge_idempotency_cid(db, cid)
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_robinhood_spot_adapter_enabled", True, raising=False)
    mock_buy.return_value = {"ok": True, "order_id": "ord-intra-1", "state": "queued", "raw": {}}

    adapter = RobinhoodSpotAdapter()
    first = adapter.place_market_order(
        product_id="AAPL", side="buy", base_size="1", client_order_id=cid,
    )
    assert first["ok"] is True
    assert mock_buy.call_count == 1

    # Second call with the same CID — must NOT hit the broker again.
    second = adapter.place_market_order(
        product_id="AAPL", side="buy", base_size="1", client_order_id=cid,
    )
    assert second["ok"] is False
    assert second["error"] == "duplicate_client_order_id"
    assert mock_buy.call_count == 1, "broker hit twice — in-process guard missed"

    _purge_idempotency_cid(db, cid)
    reset_duplicate_client_order_guard_for_tests()

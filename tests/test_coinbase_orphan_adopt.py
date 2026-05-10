"""Tests for the Coinbase orphan-stop adoption pass.

f-coinbase-orphan-stop-adoption (2026-05-10).

Covers the matching, persistence, and skip logic of
:func:`app.services.trading.venue.coinbase_orphan_adopt.adopt_coinbase_orphan_stops`.
A stub :class:`_StubCoinbaseAdapter` plays the role of
``CoinbaseSpotAdapter`` so the suite is hermetic — no broker creds, no
network. The DB-side state is exercised against the real test database
via the standard ``db`` fixture (per ``conftest.py``).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import pytest
from sqlalchemy import text

from app.models.trading import BracketIntent, Trade
from app.services.trading.venue.coinbase_orphan_adopt import (
    adopt_coinbase_orphan_stops,
)
from app.services.trading.venue.protocol import (
    FreshnessMeta,
    NormalizedOrder,
    VenueAdapterError,
)


# ── Stub adapter ───────────────────────────────────────────────────────


class _StubCoinbaseAdapter:
    """Minimal stand-in for :class:`CoinbaseSpotAdapter`. The adoption pass
    only ever calls :meth:`list_open_orders` on the adapter, so that's all
    we implement.

    Set ``raise_on_list=True`` to simulate adapter / API failure — the
    pass is required to propagate :class:`VenueAdapterError` rather than
    silently return ok=True.
    """

    def __init__(
        self,
        orders: list[NormalizedOrder],
        *,
        raise_on_list: bool = False,
    ):
        self._orders = orders
        self._raise = raise_on_list

    def list_open_orders(
        self, *, product_id: Optional[str] = None, limit: int = 50
    ) -> tuple[list[NormalizedOrder], FreshnessMeta]:
        if self._raise:
            raise VenueAdapterError("simulated adapter failure")
        # The adoption pass passes product_id=None and gets back EVERYTHING
        # the adapter returns; filtering happens inside the pass.
        return list(self._orders), FreshnessMeta(retrieved_at_utc=datetime.utcnow())


# ── Helpers ────────────────────────────────────────────────────────────


def _make_stop_order(
    *,
    order_id: str,
    product_id: str,
    base_size: str,
    side: str = "sell",
    order_type: str = "STOP_LIMIT",
    status: str = "OPEN",
) -> NormalizedOrder:
    return NormalizedOrder(
        order_id=order_id,
        client_order_id=None,
        product_id=product_id,
        side=side,
        status=status,
        order_type=order_type,
        filled_size=0.0,
        average_filled_price=None,
        created_time=None,
        raw={
            "order_id": order_id,
            "product_id": product_id,
            "side": side.upper(),
            "status": status,
            "order_type": order_type,
            "base_size": base_size,
        },
    )


def _seed_open_coinbase_trade(
    db,
    *,
    ticker: str,
    quantity: float,
    user_id: Optional[int] = None,
    entry_price: float = 1.0,
) -> Trade:
    trade = Trade(
        user_id=user_id,
        ticker=ticker,
        direction="long",
        entry_price=entry_price,
        quantity=quantity,
        status="open",
        broker_source="coinbase",
        stop_loss=entry_price * 0.95,
    )
    db.add(trade)
    db.flush()
    return trade


def _seed_intent(
    db,
    *,
    trade: Trade,
    intent_state: str,
    broker_stop_order_id: Optional[str] = None,
    broker_source: Optional[str] = "coinbase",
) -> BracketIntent:
    intent = BracketIntent(
        trade_id=trade.id,
        user_id=trade.user_id,
        ticker=trade.ticker,
        direction=trade.direction,
        quantity=trade.quantity,
        entry_price=trade.entry_price,
        stop_price=trade.entry_price * 0.95,
        target_price=trade.entry_price * 1.10,
        intent_state=intent_state,
        shadow_mode=False,
        broker_source=broker_source,
        broker_stop_order_id=broker_stop_order_id,
        payload_json={},
    )
    db.add(intent)
    db.flush()
    return intent


def _intent_state(db, intent_id: int) -> tuple[str, Optional[str]]:
    """Return ``(intent_state, broker_stop_order_id)`` for a row, freshly
    queried so we see post-commit state."""
    db.expire_all()
    row = db.execute(text(
        "SELECT intent_state, broker_stop_order_id "
        "FROM trading_bracket_intents WHERE id = :id"
    ), {"id": int(intent_id)}).fetchone()
    return (str(row[0]), row[1] if row else None)


# ── Tests ──────────────────────────────────────────────────────────────


def test_happy_match_single_intent_single_order(db):
    """One naked intent + one open SELL stop-limit, qty matches → adopted."""
    trade = _seed_open_coinbase_trade(db, ticker="AERGO", quantity=2400.0)
    intent = _seed_intent(db, trade=trade, intent_state="intent")
    db.commit()

    adapter = _StubCoinbaseAdapter([
        _make_stop_order(
            order_id="b3c14ef6", product_id="AERGO-USD", base_size="2400",
        ),
    ])

    report = adopt_coinbase_orphan_stops(db, adapter=adapter, dry_run=False)

    assert report["ok"] is True
    assert report["dry_run"] is False
    assert len(report["adoptions"]) == 1
    assert report["adoptions"][0]["intent_id"] == intent.id
    assert report["adoptions"][0]["broker_stop_order_id"] == "b3c14ef6"
    assert report["adoptions"][0]["new_state"] == "reconciled"
    assert report["adoptions"][0]["applied"] is True

    state, order_id = _intent_state(db, intent.id)
    assert state == "reconciled"
    assert order_id == "b3c14ef6"


def test_qty_mismatch_skips_with_log(db):
    """Local qty 100, broker qty 200 → skip with reason qty_mismatch."""
    trade = _seed_open_coinbase_trade(db, ticker="ACX", quantity=100.0)
    intent = _seed_intent(db, trade=trade, intent_state="intent")
    db.commit()

    adapter = _StubCoinbaseAdapter([
        _make_stop_order(
            order_id="d1b91a9c", product_id="ACX-USD", base_size="200",
        ),
    ])

    report = adopt_coinbase_orphan_stops(db, adapter=adapter, dry_run=False)

    assert report["adoptions"] == []
    assert any(s["reason"] == "qty_mismatch" for s in report["skipped"])
    state, order_id = _intent_state(db, intent.id)
    assert state == "intent"
    assert order_id is None


def test_multiple_intents_same_ticker_skips(db):
    """Two naked intents for the same ticker → skip with multiple_intents."""
    t1 = _seed_open_coinbase_trade(db, ticker="ACX", quantity=100.0)
    t2 = _seed_open_coinbase_trade(db, ticker="ACX", quantity=100.0)
    i1 = _seed_intent(db, trade=t1, intent_state="intent")
    i2 = _seed_intent(db, trade=t2, intent_state="intent")
    db.commit()

    adapter = _StubCoinbaseAdapter([
        _make_stop_order(order_id="o1", product_id="ACX-USD", base_size="100"),
    ])

    report = adopt_coinbase_orphan_stops(db, adapter=adapter, dry_run=False)

    assert report["adoptions"] == []
    skips = [s for s in report["skipped"] if s["reason"] == "multiple_intents"]
    assert len(skips) == 1
    assert sorted(skips[0]["intent_ids"]) == sorted([i1.id, i2.id])
    # Neither row should have been touched.
    for iid in (i1.id, i2.id):
        state, order_id = _intent_state(db, iid)
        assert state == "intent"
        assert order_id is None


def test_multiple_orders_same_ticker_skips(db):
    """Two open Coinbase stops for the same ticker → skip with multiple_orders."""
    trade = _seed_open_coinbase_trade(db, ticker="RARE", quantity=50.0)
    intent = _seed_intent(db, trade=trade, intent_state="intent")
    db.commit()

    adapter = _StubCoinbaseAdapter([
        _make_stop_order(order_id="oA", product_id="RARE-USD", base_size="50"),
        _make_stop_order(order_id="oB", product_id="RARE-USD", base_size="50"),
    ])

    report = adopt_coinbase_orphan_stops(db, adapter=adapter, dry_run=False)

    assert report["adoptions"] == []
    skips = [s for s in report["skipped"] if s["reason"] == "multiple_orders"]
    assert len(skips) == 1
    assert sorted(skips[0]["order_ids"]) == ["oA", "oB"]
    state, order_id = _intent_state(db, intent.id)
    assert state == "intent"
    assert order_id is None


def test_paper_trade_excluded(db):
    """Intent with broker_source IS NULL never appears in the candidate set."""
    # Seed a paper trade — broker_source NULL. We can't go through
    # _seed_open_coinbase_trade because it sets broker_source='coinbase';
    # construct directly.
    trade = Trade(
        user_id=None, ticker="PAPER", direction="long", entry_price=1.0,
        quantity=10.0, status="open", broker_source=None,
    )
    db.add(trade)
    db.flush()
    intent = _seed_intent(
        db, trade=trade, intent_state="intent", broker_source=None,
    )
    db.commit()

    adapter = _StubCoinbaseAdapter([
        _make_stop_order(order_id="x", product_id="PAPER-USD", base_size="10"),
    ])

    report = adopt_coinbase_orphan_stops(db, adapter=adapter, dry_run=False)

    # Intent is excluded from naked_intents_examined entirely.
    assert report["naked_intents_examined"] == 0
    # And nothing is adopted (no naked intent to match).
    assert report["adoptions"] == []
    state, order_id = _intent_state(db, intent.id)
    assert order_id is None


def test_intent_already_has_order_id_excluded(db):
    """Intent with broker_stop_order_id already set never appears."""
    trade = _seed_open_coinbase_trade(db, ticker="ALREADY", quantity=10.0)
    intent = _seed_intent(
        db, trade=trade, intent_state="confirmed_at_broker",
        broker_stop_order_id="prev_id",
    )
    db.commit()

    adapter = _StubCoinbaseAdapter([
        _make_stop_order(order_id="new_id", product_id="ALREADY-USD", base_size="10"),
    ])

    report = adopt_coinbase_orphan_stops(db, adapter=adapter, dry_run=False)

    assert report["naked_intents_examined"] == 0
    state, order_id = _intent_state(db, intent.id)
    # Order id unchanged.
    assert order_id == "prev_id"


def test_terminal_reject_uses_auto_reconcile_bypass(db):
    """A naked intent in terminal_reject is moved to reconciled via the
    audited bypass writer."""
    trade = _seed_open_coinbase_trade(db, ticker="STUCK", quantity=42.0)
    intent = _seed_intent(db, trade=trade, intent_state="terminal_reject")
    db.commit()

    adapter = _StubCoinbaseAdapter([
        _make_stop_order(order_id="bypass_id", product_id="STUCK-USD", base_size="42"),
    ])

    report = adopt_coinbase_orphan_stops(db, adapter=adapter, dry_run=False)

    assert len(report["adoptions"]) == 1
    assert report["adoptions"][0]["prev_state"] == "terminal_reject"
    assert report["adoptions"][0]["new_state"] == "reconciled"
    state, order_id = _intent_state(db, intent.id)
    assert state == "reconciled"
    assert order_id == "bypass_id"


def test_intent_state_filter_excludes_closed_and_reconciled(db):
    """Closed and reconciled rows are not adoption candidates."""
    t1 = _seed_open_coinbase_trade(db, ticker="DONE1", quantity=5.0)
    t2 = _seed_open_coinbase_trade(db, ticker="DONE2", quantity=5.0)
    i_closed = _seed_intent(db, trade=t1, intent_state="closed")
    i_recon = _seed_intent(db, trade=t2, intent_state="reconciled")
    db.commit()

    adapter = _StubCoinbaseAdapter([
        _make_stop_order(order_id="x1", product_id="DONE1-USD", base_size="5"),
        _make_stop_order(order_id="x2", product_id="DONE2-USD", base_size="5"),
    ])

    report = adopt_coinbase_orphan_stops(db, adapter=adapter, dry_run=False)

    assert report["naked_intents_examined"] == 0
    assert report["adoptions"] == []
    # Both intents untouched.
    s1, o1 = _intent_state(db, i_closed.id)
    s2, o2 = _intent_state(db, i_recon.id)
    assert s1 == "closed" and o1 is None
    assert s2 == "reconciled" and o2 is None


def test_dry_run_reports_but_does_not_write(db):
    """dry_run=True surfaces planned adoption but persists nothing."""
    trade = _seed_open_coinbase_trade(db, ticker="DRY", quantity=99.0)
    intent = _seed_intent(db, trade=trade, intent_state="intent")
    db.commit()

    adapter = _StubCoinbaseAdapter([
        _make_stop_order(order_id="dryrun_id", product_id="DRY-USD", base_size="99"),
    ])

    report = adopt_coinbase_orphan_stops(db, adapter=adapter, dry_run=True)

    assert report["dry_run"] is True
    assert len(report["adoptions"]) == 1
    assert report["adoptions"][0]["applied"] is False
    state, order_id = _intent_state(db, intent.id)
    # No DB mutation under dry_run.
    assert state == "intent"
    assert order_id is None


def test_adapter_unreachable_raises_not_swallowed(db):
    """When the adapter raises, the pass propagates VenueAdapterError —
    it does NOT silently return ok=True."""
    trade = _seed_open_coinbase_trade(db, ticker="DOWN", quantity=10.0)
    _seed_intent(db, trade=trade, intent_state="intent")
    db.commit()

    adapter = _StubCoinbaseAdapter([], raise_on_list=True)

    with pytest.raises(VenueAdapterError):
        adopt_coinbase_orphan_stops(db, adapter=adapter, dry_run=False)


def test_buy_orders_excluded(db):
    """An open BUY order on the same product never matches a naked intent."""
    trade = _seed_open_coinbase_trade(db, ticker="ONLYBUY", quantity=10.0)
    intent = _seed_intent(db, trade=trade, intent_state="intent")
    db.commit()

    adapter = _StubCoinbaseAdapter([
        _make_stop_order(
            order_id="buy_id", product_id="ONLYBUY-USD", base_size="10",
            side="buy",
        ),
    ])

    report = adopt_coinbase_orphan_stops(db, adapter=adapter, dry_run=False)

    # The BUY stop is filtered out of open_stop_orders_examined; the
    # naked intent is left untouched (no-op per brief edge case
    # "no Coinbase orphan order for an intent").
    assert report["open_stop_orders_examined"] == 0
    assert report["adoptions"] == []
    state, order_id = _intent_state(db, intent.id)
    assert state == "intent" and order_id is None


def test_non_stop_order_excluded(db):
    """A SELL LIMIT (target order) never matches; only stop-limit is adopted."""
    trade = _seed_open_coinbase_trade(db, ticker="LIMIT", quantity=10.0)
    intent = _seed_intent(db, trade=trade, intent_state="intent")
    db.commit()

    # SELL LIMIT — same side, but order_type lacks 'stop_limit'.
    adapter = _StubCoinbaseAdapter([
        _make_stop_order(
            order_id="lim_id", product_id="LIMIT-USD", base_size="10",
            side="sell", order_type="LIMIT",
        ),
    ])

    report = adopt_coinbase_orphan_stops(db, adapter=adapter, dry_run=False)

    assert report["open_stop_orders_examined"] == 0
    assert report["adoptions"] == []
    state, order_id = _intent_state(db, intent.id)
    assert state == "intent" and order_id is None

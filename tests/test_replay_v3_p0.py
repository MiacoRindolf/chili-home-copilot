"""Replay v3 P0 — sim-clock ContextVar on ``_utcnow`` + MockBrokerAdapter skeleton.

These tests pin the TWO independently-testable, provably-inert P0 pieces:

  P0a — the simulated clock chokepoint (``live_runner._utcnow`` + ``replay_clock``):
        prod (no sim clock) is BYTE-IDENTICAL to ``datetime.utcnow()``; the ContextVar
        injects/nests/auto-resets (on normal exit AND on exception) so a frozen clock can
        never leak into a real lane.

  P0b — ``MockBrokerAdapter`` conforms to the ``VenueAdapter`` protocol, fills
        deterministically at a recorded NBBO via the pure paper-fill math, and rejects a
        no-bbo place.

Pure (no DB) — see docs/DESIGN/REPLAY_V3_LIVE_FSM_SIM.md §4 (P0).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural.replay_mock_broker import (
    MockBrokerAdapter,
    RecordedQuote,
    make_mock_broker_factory,
)
from app.services.trading.venue.protocol import (
    FreshnessMeta,
    NormalizedOrder,
    NormalizedTicker,
    VenueAdapter,
)

# A fixed sim instant (naive-UTC, the prod ``_utcnow`` shape).
_T = datetime(2026, 6, 29, 13, 30, 0)


# ── P0a: the simulated clock ─────────────────────────────────────────────────────
def test_utcnow_no_sim_clock_is_real_now_naive_utc():
    """With NO sim clock (prod ALWAYS), ``_utcnow`` returns the real ``datetime.utcnow()``:
    naive (tz-unaware), within a hair of the real wall clock — the byte-identical path."""
    assert lr._SIM_NOW.get() is None  # default unset
    before = datetime.utcnow()
    got = lr._utcnow()
    after = datetime.utcnow()
    assert got.tzinfo is None  # naive UTC, exactly as datetime.utcnow()
    assert before <= got <= after  # same real clock, same code path


def test_utcnow_no_sim_clock_matches_datetime_utcnow_identity(monkeypatch):
    """Prove the no-sim-clock branch is LITERALLY ``datetime.utcnow()`` — patch utcnow to a
    sentinel and confirm ``_utcnow`` returns it unchanged when the ContextVar is unset."""
    sentinel = datetime(2000, 1, 2, 3, 4, 5)

    class _FrozenDT(datetime):
        @classmethod
        def utcnow(cls):  # type: ignore[override]
            return sentinel

    monkeypatch.setattr(lr, "datetime", _FrozenDT)
    assert lr._SIM_NOW.get() is None
    assert lr._utcnow() == sentinel


def test_replay_clock_injects_sim_time():
    with lr.replay_clock(_T):
        assert lr._utcnow() == _T
        # the aware-UTC + ET helpers DERIVE from the same chokepoint
        assert lr._utcnow_aware() == _T.replace(tzinfo=timezone.utc)
        assert lr._now_in_tz(timezone.utc).replace(tzinfo=None) == _T
    # auto-reset after the block
    assert lr._SIM_NOW.get() is None


def test_replay_clock_tz_aware_input_normalized_to_naive_utc():
    aware = datetime(2026, 6, 29, 13, 30, 0, tzinfo=timezone.utc)
    with lr.replay_clock(aware):
        got = lr._utcnow()
        assert got.tzinfo is None
        assert got == _T


def test_replay_clock_nests_and_restores_outer():
    outer = datetime(2026, 6, 29, 13, 0, 0)
    inner = datetime(2026, 6, 29, 14, 0, 0)
    with lr.replay_clock(outer):
        assert lr._utcnow() == outer
        with lr.replay_clock(inner):
            assert lr._utcnow() == inner
        # inner exit restores OUTER, not None
        assert lr._utcnow() == outer
    assert lr._SIM_NOW.get() is None


def test_replay_clock_resets_on_exception():
    with pytest.raises(RuntimeError):
        with lr.replay_clock(_T):
            assert lr._utcnow() == _T
            raise RuntimeError("boom")
    # the finally-block restored the prior (None) value despite the exception
    assert lr._SIM_NOW.get() is None
    # and prod is back to real now
    assert lr._utcnow().tzinfo is None
    assert abs((datetime.utcnow() - lr._utcnow()).total_seconds()) < 2.0


def test_set_reset_sim_clock_token_roundtrip():
    token = lr.set_sim_clock(_T)
    try:
        assert lr._utcnow() == _T
    finally:
        lr.reset_sim_clock(token)
    assert lr._SIM_NOW.get() is None


# ── P0b: MockBrokerAdapter ───────────────────────────────────────────────────────
def test_mock_broker_conforms_to_venue_adapter_protocol():
    m = MockBrokerAdapter()
    assert isinstance(m, VenueAdapter)  # @runtime_checkable structural conformance
    # the methods the runner calls per tick exist and are callable
    for name in (
        "is_enabled",
        "get_best_bid_ask",
        "get_ticker",
        "get_product",
        "get_order",
        "place_market_order",
        "place_limit_order_gtc",
        "cancel_order",
        "get_account_snapshot",
    ):
        assert callable(getattr(m, name)), name


def test_mock_broker_is_enabled_default():
    assert MockBrokerAdapter().is_enabled() is True
    assert MockBrokerAdapter(enabled=False).is_enabled() is False


def test_mock_broker_bbo_from_injected_quote_stamped_at_sim_clock():
    m = MockBrokerAdapter()
    m.set_clock(_T)
    m.set_quote("UPC", RecordedQuote(bid=10.0, ask=10.04, last=10.02))
    ticker, fresh = m.get_best_bid_ask("UPC")
    assert isinstance(ticker, NormalizedTicker)
    assert isinstance(fresh, FreshnessMeta)
    assert ticker.bid == 10.0 and ticker.ask == 10.04
    assert ticker.mid == pytest.approx(10.02)
    # freshness stamped at the sim clock (sim-to-sim comparison in the runner)
    assert fresh.retrieved_at_utc.replace(tzinfo=None) == _T


def test_mock_broker_deterministic_entry_fill_at_recorded_ask():
    """A long buy crosses the recorded ASK (zero slippage default) via the pure paper math."""
    m = MockBrokerAdapter()
    m.set_clock(_T)
    m.set_quote("UPC", RecordedQuote(bid=10.0, ask=10.04))
    r = m.place_market_order(product_id="UPC", side="buy", base_size="100")
    assert r["ok"] is True
    assert r["raw"]["fill_price"] == pytest.approx(10.04)
    assert r["raw"]["filled_size"] == pytest.approx(100.0)
    # the resting order resolves to a terminal FILLED with the same fill
    o, _ = m.get_order(r["order_id"])
    assert isinstance(o, NormalizedOrder)
    assert o.status == "filled"
    assert o.filled_size == pytest.approx(100.0)
    assert o.average_filled_price == pytest.approx(10.04)


def test_mock_broker_exit_fill_crosses_bid():
    m = MockBrokerAdapter()
    m.set_clock(_T)
    m.set_quote("UPC", RecordedQuote(bid=10.0, ask=10.04))
    r = m.place_market_order(product_id="UPC", side="sell", base_size="100")
    assert r["ok"] is True
    assert r["raw"]["fill_price"] == pytest.approx(10.0)  # crosses the bid


def test_mock_broker_slippage_applied_symmetrically():
    """Non-zero slippage widens the entry (above ask) and the exit (below bid)."""
    m = MockBrokerAdapter(slippage_bps=10.0)  # 10 bps of mid
    m.set_clock(_T)
    m.set_quote("UPC", RecordedQuote(bid=10.0, ask=10.04))
    mid = 10.02
    slip = mid * 10.0 / 10_000.0
    buy = m.place_market_order(product_id="UPC", side="buy", base_size="1")
    sell = m.place_market_order(product_id="UPC", side="sell", base_size="1")
    assert buy["raw"]["fill_price"] == pytest.approx(10.04 + slip)
    assert sell["raw"]["fill_price"] == pytest.approx(10.0 - slip)


def test_mock_broker_no_bbo_returns_none_and_rejects_place():
    m = MockBrokerAdapter()
    m.set_clock(_T)
    # no quote injected for this product ⇒ no_bbo
    ticker, fresh = m.get_best_bid_ask("RVMDW")
    assert ticker is None
    assert isinstance(fresh, FreshnessMeta)
    r = m.place_market_order(product_id="RVMDW", side="buy", base_size="100")
    assert r["ok"] is False
    assert r["error"] == "no_bbo"


def test_mock_broker_clear_quote_reverts_to_no_bbo():
    m = MockBrokerAdapter()
    m.set_clock(_T)
    m.set_quote("UPC", RecordedQuote(bid=10.0, ask=10.04))
    assert m.get_best_bid_ask("UPC")[0] is not None
    m.clear_quote("UPC")
    assert m.get_best_bid_ask("UPC")[0] is None


def test_mock_broker_invalid_quote_treated_as_no_bbo():
    m = MockBrokerAdapter()
    m.set_clock(_T)
    m.set_quote("UPC", RecordedQuote(bid=0.0, ask=10.0))  # invalid bid
    assert m.get_best_bid_ask("UPC")[0] is None
    assert m.place_market_order(product_id="UPC", side="buy", base_size="1")["ok"] is False


def test_mock_broker_deterministic_order_ids_no_wallclock():
    """Identical inputs ⇒ identical order ids (monotonic counter, no UUID/wall-clock)."""
    ids = []
    for _ in range(2):
        m = MockBrokerAdapter()
        m.set_clock(_T)
        m.set_quote("UPC", RecordedQuote(bid=10.0, ask=10.04))
        r = m.place_market_order(product_id="UPC", side="buy", base_size="100")
        ids.append(r["order_id"])
    assert ids[0] == ids[1]  # deterministic across instances


def test_mock_broker_cancel_always_accepts():
    m = MockBrokerAdapter()
    out = m.cancel_order("replay_mock-00000001")
    assert out["ok"] is True


def test_make_mock_broker_factory_returns_same_singleton():
    m = MockBrokerAdapter()
    factory = make_mock_broker_factory(m)
    assert factory() is m
    assert factory() is m

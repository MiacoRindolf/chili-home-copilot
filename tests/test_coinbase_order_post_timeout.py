"""[64] review fix: an order POST that hits the TRADING-client bound says so.

The first revision of [64] bound the shared Coinbase client (orders included) by the
arm cadence, so a POST Coinbase accepted but answered after 10 s came back as a bare
``{ok: False, error: 'Read timed out'}``: a terminal reject in the runner while the
order filled at the venue. The fix scopes the cadence bound to the PROBE client and
gives the TRADING client the slowest recorded order ack (11.849 s over the 2,879
recorded coinbase submits) — see tests/test_coinbase_service_bounded_connect.py.

These tests pin the adapter envelope when that (much wider) bound still fires:
  - the receipt names the bound, its binding and derivation, and whether the request
    may have reached the venue (ConnectTimeout = never left);
  - it is deliberately NOT ``submit_outcome="indeterminate"``: that value engages the
    runner's client-id reconcile, which needs a lookup Coinbase does not offer and
    would read "unreadable" forever — blocking every later exit of the position.

Runnable: pytest tests/test_coinbase_order_post_timeout.py -v
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
import requests

from app.services import coinbase_service as cbs
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.venue import (
    CoinbaseSpotAdapter,
    FreshnessMeta,
    NormalizedProduct,
    reset_duplicate_client_order_guard_for_tests,
)
from app.services.trading.venue import coinbase_spot as cs_mod


class _TimeoutClient:
    """Every order POST raises ``exc``; records which call was attempted."""

    timeout = 11.849

    def __init__(self, exc: BaseException):
        self._exc = exc
        self.calls: list[str] = []

    def _raise(self, name):
        def _f(**_kw):
            self.calls.append(name)
            raise self._exc

        return _f

    def __getattr__(self, name):
        if name.startswith(("market_order_", "limit_order_gtc_", "stop_limit_order_gtc_")):
            return self._raise(name)
        raise AttributeError(name)


def _adapter(client) -> CoinbaseSpotAdapter:
    prod = NormalizedProduct(
        product_id="BTC-USD",
        base_currency="BTC",
        quote_currency="USD",
        status="online",
        trading_disabled=False,
        cancel_only=False,
        limit_only=False,
        post_only=False,
        auction_mode=False,
        base_increment=0.00000001,
        quote_increment=0.01,
    )
    fresh = FreshnessMeta(retrieved_at_utc=datetime.now(timezone.utc), max_age_seconds=15.0)
    ad = CoinbaseSpotAdapter(client_factory=lambda: client)
    ad.is_enabled = lambda: True  # type: ignore[method-assign]
    ad._require_client = lambda: client  # type: ignore[method-assign]
    ad.get_product = lambda pid: (prod, fresh)  # type: ignore[assignment]
    return ad


@pytest.fixture(autouse=True)
def _clean(db):
    cs_mod.reset_product_info_cache_for_tests()
    reset_duplicate_client_order_guard_for_tests()
    yield
    cs_mod.reset_product_info_cache_for_tests()


def _assert_receipt(res, *, cid: str, call: str, reached: bool):
    assert res["ok"] is False
    assert res["client_order_id"] == cid
    r = res["order_post_timeout"]
    assert r["call"] == call
    assert r["request_may_have_reached_venue"] is reached
    assert r["phase"] == ("after_connect" if reached else "connect")
    assert r["order_timeout_s"] == 11.849  # the bound the client that POSTed carries
    assert r["order_timeout_binding"] == "coinbase_order_ack_span_max_s"
    assert "n=2879" in r["order_timeout_derivation"]
    # NOT the Alpaca-only indeterminate class (would block every later exit on Coinbase).
    assert "submit_outcome" not in res
    assert lr._is_indeterminate_alpaca_submit(res, "coinbase_spot") is False


def test_market_sell_read_timeout_envelope_carries_the_bound(monkeypatch):
    client = _TimeoutClient(requests.exceptions.ReadTimeout("Read timed out."))
    res = _adapter(client).place_market_order(
        product_id="BTC-USD", side="sell", base_size="0.001", client_order_id="t64-mkt-1"
    )
    assert client.calls == ["market_order_sell"]
    _assert_receipt(res, cid="t64-mkt-1", call="place_market_order", reached=True)


def test_limit_sell_connect_timeout_never_left(monkeypatch):
    client = _TimeoutClient(requests.exceptions.ConnectTimeout("connect timed out"))
    res = _adapter(client).place_limit_order_gtc(
        product_id="BTC-USD",
        side="sell",
        base_size="0.001",
        limit_price="50000",
        client_order_id="t64-lmt-1",
    )
    assert client.calls == ["limit_order_gtc_sell"]
    _assert_receipt(res, cid="t64-lmt-1", call="place_limit_order_gtc", reached=False)


def test_stop_limit_sell_read_timeout_envelope(monkeypatch):
    client = _TimeoutClient(requests.exceptions.ReadTimeout("Read timed out."))
    res = _adapter(client).place_stop_limit_order_gtc(
        product_id="BTC-USD",
        side="sell",
        base_size="0.001",
        stop_price="49000",
        limit_price="48900",
        client_order_id="t64-stp-1",
    )
    assert client.calls == ["stop_limit_order_gtc_sell"]
    _assert_receipt(res, cid="t64-stp-1", call="place_stop_limit_order_gtc", reached=True)


def test_scale_limit_marker_is_not_pinned_by_a_coinbase_timeout(monkeypatch):
    """The runner keeps ``scale_limit_place_intent`` ONLY for ``submit_outcome=
    indeterminate``; a pinned marker on Coinbase (no client-id truth lookup) would make
    every later exit read the sibling as UNREADABLE and refuse to release."""
    client = _TimeoutClient(requests.exceptions.ReadTimeout("Read timed out."))
    res = _adapter(client).place_limit_order_gtc(
        product_id="BTC-USD",
        side="sell",
        base_size="0.001",
        limit_price="50000",
        client_order_id="t64-lmt-2",
    )
    le = {"scale_limit_place_intent": {"client_order_id": "t64-lmt-2", "kind": "scale"}}
    commits: list = []
    monkeypatch.setattr(lr, "_commit_le", lambda sess, le_: commits.append(dict(le_)))
    lr._clear_scale_limit_place_intent_if_determinate(object(), le, res)
    assert "scale_limit_place_intent" not in le
    assert commits, "the marker drop must be committed"


def test_order_timeout_receipt_falls_back_to_configured_bound_without_a_client(monkeypatch):
    monkeypatch.setattr(cbs, "_client", None)
    r = cbs.order_timeout_receipt()
    assert r["order_timeout_s"] == cbs._COINBASE_ORDER_ACK_SPAN_MAX_S == 11.849

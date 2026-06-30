"""Short-side lane — P0 feasibility (docs/DESIGN/SHORT_SIDE_LANE.md).

P0 is the foundational, fully-ISOLATED, PAPER-only step: plumb SHORT support through
the Alpaca adapter and prove a paper short OPENS (SELL_TO_OPEN → short position) and
COVERS (BUY_TO_CLOSE → flat). NO momentum-lane wiring (that's P1).

These tests mock the Alpaca client (no real network) but use the REAL alpaca-py request
models + ``PositionIntent`` enum (alpaca-py 0.43.4 is installed) so the position_intent
mapping is asserted against the actual SDK, exactly as a live order would carry it.

Load-bearing invariants proved here:
  1. a SELL_TO_OPEN entry carries ``OrderSide.SELL`` + ``PositionIntent.SELL_TO_OPEN``
  2. a BUY_TO_CLOSE cover carries ``OrderSide.BUY`` + ``PositionIntent.BUY_TO_CLOSE``
  3. a mocked paper short OPENS (qty goes negative) then COVERS (back to flat)
  4. the LONG path is BYTE-IDENTICAL when position_intent is omitted (no field set)
  5. the ``alpaca_short`` execution family is registered + isolated + paper-only
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from alpaca.trading.enums import OrderSide, PositionIntent

from app.services.trading.execution_family_registry import (
    EXECUTION_FAMILY_ALPACA_SHORT,
    EXECUTION_FAMILY_ALPACA_SPOT,
    DOCUMENTED_EXECUTION_FAMILIES,
    IMPLEMENTED_MOMENTUM_AUTOMATION_FAMILIES,
    asset_class_of_execution_family,
    execution_family_supports_asset_class,
    momentum_runner_supports_execution_family,
    normalize_execution_family,
    resolve_live_spot_adapter_factory,
    venue_for_execution_family,
)
from app.services.trading.governance import REAL_DAILY_LOSS_FAMILIES
from app.services.trading.venue import alpaca_spot as mod
from app.services.trading.venue.alpaca_spot import AlpacaSpotAdapter


# ── fixtures: a fake Alpaca TradingClient capturing the submitted order_data ──
class _FakeOrder:
    """Mimics the alpaca-py Order echo the adapter reads off submit_order."""

    def __init__(self, *, order_id, status, position_intent=None):
        self.id = order_id
        self.status = status
        self.client_order_id = None
        self.position_intent = position_intent


class _FakeTradingClient:
    """Captures every submitted ``order_data`` and tracks a single-symbol position
    qty so OPEN (sell-to-open ⇒ negative) → COVER (buy-to-close ⇒ flat) is observable."""

    def __init__(self):
        self.submitted = []
        self._qty = 0.0  # signed paper position (negative = short)

    def submit_order(self, *, order_data):
        self.submitted.append(order_data)
        qty = float(getattr(order_data, "qty", 0.0) or 0.0)
        side = getattr(order_data, "side", None)
        # paper fill model: SELL reduces (opens short), BUY increases (covers)
        self._qty += qty if side == OrderSide.BUY else -qty
        return _FakeOrder(
            order_id=f"ord-{len(self.submitted)}",
            status="filled",
            position_intent=getattr(order_data, "position_intent", None),
        )

    def position_qty(self):
        return self._qty


@pytest.fixture
def fake_client(monkeypatch):
    fc = _FakeTradingClient()
    mod.reset_clients_for_tests()
    monkeypatch.setattr(mod, "_trading_client", lambda: fc)
    yield fc
    mod.reset_clients_for_tests()


# ── (1) intent mapping: SELL_TO_OPEN entry ────────────────────────────────────
def test_sell_to_open_request_has_correct_side_and_intent(fake_client):
    ad = AlpacaSpotAdapter()
    res = ad.place_limit_order_gtc(
        product_id="ABCD", side="sell", base_size="100", limit_price="5.00",
        position_intent="sell_to_open",
    )
    assert res["ok"] is True
    assert res["position_intent"] == PositionIntent.SELL_TO_OPEN.value
    req = fake_client.submitted[-1]
    assert req.side == OrderSide.SELL
    assert req.position_intent == PositionIntent.SELL_TO_OPEN
    assert float(req.qty) == 100.0


# ── (2) intent mapping: BUY_TO_CLOSE cover ────────────────────────────────────
def test_buy_to_close_request_has_correct_side_and_intent(fake_client):
    ad = AlpacaSpotAdapter()
    res = ad.place_market_order(
        product_id="ABCD", side="buy", base_size="100",
        position_intent="buy_to_close",
    )
    assert res["ok"] is True
    assert res["position_intent"] == PositionIntent.BUY_TO_CLOSE.value
    req = fake_client.submitted[-1]
    assert req.side == OrderSide.BUY
    assert req.position_intent == PositionIntent.BUY_TO_CLOSE


def test_intent_accepts_enum_directly(fake_client):
    ad = AlpacaSpotAdapter()
    ad.place_limit_order_gtc(
        product_id="ABCD", side="sell", base_size="10", limit_price="5.00",
        position_intent=PositionIntent.SELL_TO_OPEN,
    )
    assert fake_client.submitted[-1].position_intent == PositionIntent.SELL_TO_OPEN


# ── (3) full lifecycle: paper short OPENS then COVERS ─────────────────────────
def test_paper_short_opens_and_covers(fake_client):
    ad = AlpacaSpotAdapter()
    assert fake_client.position_qty() == 0.0  # flat to start

    # ENTRY: sell-to-open ⇒ position goes NEGATIVE (a short)
    open_res = ad.place_limit_order_gtc(
        product_id="ABCD", side="sell", base_size="100", limit_price="5.00",
        position_intent="sell_to_open",
    )
    assert open_res["ok"] is True
    assert fake_client.position_qty() == -100.0

    # COVER: buy-to-close ⇒ back to FLAT
    cover_res = ad.place_market_order(
        product_id="ABCD", side="buy", base_size="100",
        position_intent="buy_to_close",
    )
    assert cover_res["ok"] is True
    assert fake_client.position_qty() == 0.0

    # two orders: SELL_TO_OPEN then BUY_TO_CLOSE
    assert [r.position_intent for r in fake_client.submitted] == [
        PositionIntent.SELL_TO_OPEN, PositionIntent.BUY_TO_CLOSE,
    ]


# ── (4) long path is BYTE-IDENTICAL when intent is omitted ────────────────────
def test_long_path_request_byte_identical_without_intent(fake_client):
    """A normal long buy (no position_intent) must build a request with NO
    position_intent field set — identical to the pre-short adapter behavior."""
    ad = AlpacaSpotAdapter()
    res = ad.place_limit_order_gtc(
        product_id="ABCD", side="buy", base_size="100", limit_price="5.00",
    )
    assert res["ok"] is True
    assert "position_intent" not in res  # not surfaced for the long path
    req = fake_client.submitted[-1]
    assert req.side == OrderSide.BUY
    # the field is left at the SDK default (None) — the request the venue receives is
    # byte-identical to today's long order.
    assert getattr(req, "position_intent", None) is None


def test_long_market_path_byte_identical_without_intent(fake_client):
    ad = AlpacaSpotAdapter()
    ad.place_market_order(product_id="ABCD", side="buy", base_size="50")
    req = fake_client.submitted[-1]
    assert req.side == OrderSide.BUY
    assert getattr(req, "position_intent", None) is None


def test_resolve_position_intent_none_returns_none():
    ad = AlpacaSpotAdapter()
    assert ad._resolve_position_intent(None) is None
    assert ad._resolve_position_intent("sell_to_open") == PositionIntent.SELL_TO_OPEN
    assert ad._resolve_position_intent("BUY_TO_CLOSE") == PositionIntent.BUY_TO_CLOSE
    assert ad._resolve_position_intent("garbage") is None


# ── SSR / borrow rejection surfacing (defer-not-retry) ────────────────────────
def test_ssr_rejection_surfaced_distinctly(monkeypatch):
    mod.reset_clients_for_tests()

    class _RejectingClient:
        def submit_order(self, *, order_data):
            raise RuntimeError("order rejected: short sale restricted (Regulation SHO uptick)")

    monkeypatch.setattr(mod, "_trading_client", lambda: _RejectingClient())
    ad = AlpacaSpotAdapter()
    res = ad.place_limit_order_gtc(
        product_id="ABCD", side="sell", base_size="100", limit_price="5.00",
        position_intent="sell_to_open",
    )
    assert res["ok"] is False
    assert res.get("reject_kind") == "ssr"
    mod.reset_clients_for_tests()


def test_borrow_rejection_surfaced_distinctly(monkeypatch):
    mod.reset_clients_for_tests()

    class _RejectingClient:
        def submit_order(self, *, order_data):
            raise RuntimeError("not shortable: no borrow available (HTB)")

    monkeypatch.setattr(mod, "_trading_client", lambda: _RejectingClient())
    ad = AlpacaSpotAdapter()
    res = ad.place_limit_order_gtc(
        product_id="ABCD", side="sell", base_size="100", limit_price="5.00",
        position_intent="sell_to_open",
    )
    assert res["ok"] is False
    assert res.get("reject_kind") == "borrow"
    mod.reset_clients_for_tests()


# ── account / asset short surfacing ───────────────────────────────────────────
def test_account_snapshot_surfaces_shorting_capability(monkeypatch):
    mod.reset_clients_for_tests()

    class _Acct:
        equity = "10000"; buying_power = "40000"; cash = "10000"
        status = "ACTIVE"; shorting_enabled = True; multiplier = "4"

    class _AcctClient:
        def get_account(self):
            return _Acct()

    monkeypatch.setattr(mod, "_trading_client", lambda: _AcctClient())
    snap = AlpacaSpotAdapter().get_account_snapshot()
    assert snap["ok"] is True
    assert snap["shorting_enabled"] is True
    assert snap["multiplier"] == 4.0
    mod.reset_clients_for_tests()


def test_get_product_surfaces_borrow_fields(monkeypatch):
    mod.reset_clients_for_tests()

    class _Asset:
        tradable = True; status = "active"; fractionable = False
        min_trade_increment = None; min_order_size = None; price_increment = None
        exchange = "NASDAQ"; shortable = True; easy_to_borrow = False

    class _AssetClient:
        def get_asset(self, sym):
            return _Asset()

    monkeypatch.setattr(mod, "_trading_client", lambda: _AssetClient())
    prod, _ = AlpacaSpotAdapter().get_product("ABCD")
    assert prod is not None
    assert prod.raw["shortable"] is True
    assert prod.raw["easy_to_borrow"] is False
    mod.reset_clients_for_tests()


# ── (5) execution-family: alpaca_short is registered, isolated, paper-only ─────
def test_alpaca_short_family_registered_and_isolated():
    assert EXECUTION_FAMILY_ALPACA_SHORT == "alpaca_short"
    # registered + implemented automation
    assert EXECUTION_FAMILY_ALPACA_SHORT in DOCUMENTED_EXECUTION_FAMILIES
    assert EXECUTION_FAMILY_ALPACA_SHORT in IMPLEMENTED_MOMENTUM_AUTOMATION_FAMILIES
    assert momentum_runner_supports_execution_family("alpaca_short") is True
    assert normalize_execution_family("ALPACA_SHORT") == "alpaca_short"
    # equity asset class, isolated from the long alpaca_spot family
    assert asset_class_of_execution_family("alpaca_short") == "equity"
    assert execution_family_supports_asset_class("alpaca_short", "equity") is True
    assert execution_family_supports_asset_class("alpaca_short", "crypto") is False
    assert EXECUTION_FAMILY_ALPACA_SHORT != EXECUTION_FAMILY_ALPACA_SPOT


def test_alpaca_short_routes_to_alpaca_adapter_and_venue():
    factory = resolve_live_spot_adapter_factory("alpaca_short")
    assert factory is AlpacaSpotAdapter
    assert venue_for_execution_family("alpaca_short") == "alpaca"


def test_alpaca_short_is_paper_only_excluded_from_real_daily_loss():
    """P0 is PAPER-only by construction: the short family is NOT in the real-money
    daily-loss family set (no real-money path until a later phase)."""
    assert EXECUTION_FAMILY_ALPACA_SHORT not in REAL_DAILY_LOSS_FAMILIES
    # the long alpaca family is likewise excluded (paper soak posture)
    assert EXECUTION_FAMILY_ALPACA_SPOT not in REAL_DAILY_LOSS_FAMILIES


def test_short_lane_flag_default_off():
    """The master gate ships DEFAULT-OFF (the one deliberate dark flag — un-soaked,
    dangerous, no triggers wired yet)."""
    from app.config import settings
    assert settings.chili_momentum_short_lane_enabled is False

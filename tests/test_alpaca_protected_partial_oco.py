"""Ang Alpaca ay MAY OCO. Ang CHILI ang wala.

ANG MALING PREMISE (natuklasan 2026-08-27). Sinusupil ng ``live_runner`` ang
BAWAT partial sa Alpaca dahil sa isang komento::

    # Recertification posture: Alpaca has no OCO contract here. A resting
    # partial SELL and a full-qty deadman would reserve overlapping shares;
    # keep the broker stop and use only full-position software exits.

Totoo iyon tungkol sa KODIGO at MALI tungkol sa API. Nasa SDK ang lahat --
``OrderClass.OCO``, ``TakeProfitRequest``, ``StopLossRequest``, at ang mga field
na ``order_class`` / ``take_profit`` / ``stop_loss`` sa ``OrderRequest`` -- at ang
paghahanap ng ``order_class`` sa buong ``app/`` ay nagbalik ng **ZERO hits**, sa
kahit anong venue. Hindi limitasyon ng broker ang harang; hindi lang ito
naipatupad kailanman, tapos ipinaliwanag bilang limitasyon.

ANG NASUKAT NA HALAGA (2026-08-26, DAIC)::

    entry 6.14 x 95sh   scale grid rungs 6.3675 (50%) at 6.5000 (25%)

    rung          unang naabot        share doon o pataas   kailangan
    6.3675        13:30:04.66              622,528              47
    6.5000        13:30:21.04              283,554              24

    47 @ 6.3675 = +$12.10 | 24 @ 6.50 = +$9.36 | natira nag-trail = -$2.21
    => ~+$19.25 laban sa TUNAY na -$11.58

Hindi kailanman naging hadlang ang liquidity -- 10,000x ang sobra. Ang
``alpaca_scale_out_suppressed_for_deadman`` ay pumutok sa **5 sa 5** na buhay na
fill mula 2026-08-21, kasama ang CDTG ngayong 2026-08-26 na 7 ms BAGO pa
maitala ang ``live_entry_filled``.

⚠️ HINDI PA ITO NAPAPATUNAYAN LABAN SA BUHAY NA BROKER. Sinusunod ng bawat field
ang nakadokumentong kontrata, pero walang order na naipadala gamit ito. Kaya ang
``ok: False`` na may sariling teksto ng broker ay UNANG-URING resulta: ang
tumatawag ay DAPAT ituring ang pagtanggi bilang "bumalik sa buong-posisyong
gawi", hindi kailanman bilang dahilan para iwang walang proteksyon ang share.

Runnable: pytest tests/test_alpaca_protected_partial_oco.py -v
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from app.services.trading.venue.alpaca_spot import AlpacaSpotAdapter


class _Leg:
    def __init__(self, oid, cid, otype, limit=None, stop=None):
        self.id = oid
        self.client_order_id = cid
        self.order_type = type("_T", (), {"value": otype})()
        self.limit_price = limit
        self.stop_price = stop


class _Order:
    def __init__(self, legs=None):
        self.id = "parent-oid"
        self.status = type("_S", (), {"value": "new"})()
        self.legs = legs or []


class _Client:
    """Kinukuha ang eksaktong request na naipadala, walang network."""

    def __init__(self, order=None, raises=None):
        self.seen = []
        self._order = order if order is not None else _Order()
        self._raises = raises

    def submit_order(self, req=None, **kw):
        self.seen.append(req if req is not None else kw.get("order_data"))
        if self._raises is not None:
            raise self._raises
        return self._order


def _adapter(monkeypatch, client):
    a = AlpacaSpotAdapter()
    monkeypatch.setattr(a, "_account_client", lambda: client)
    return a


def _place(a, **over):
    kw = dict(
        product_id="DAIC",
        base_size="47",
        take_profit_price=6.3675,
        stop_price=5.8525,
        client_order_id="cid-partial-1",
    )
    kw.update(over)
    return a.place_protected_partial_oco(**kw)


# ── Ang pangunahing kaso ─────────────────────────────────────────────────────


def test_it_builds_an_oco_with_BOTH_legs(monkeypatch):
    """ANG BUONG PUNTO. Ang tranche ay may sariling take-profit AT sariling stop,
    kaya PROTEKTADO ito mula sa sandaling ito ay umiral -- hindi tulad ng hubad na
    resting limit, na siyang dahilan kung bakit tinanggihan ang mas naunang
    disenyo."""
    from alpaca.trading.enums import OrderClass, OrderSide

    c = _Client()
    out = _place(_adapter(monkeypatch, c))
    assert out["ok"] is True, out
    req = c.seen[0]
    assert req.order_class == OrderClass.OCO
    assert req.side == OrderSide.SELL
    assert req.take_profit is not None and float(req.take_profit.limit_price) > 0
    assert req.stop_loss is not None and float(req.stop_loss.stop_price) > 0
    assert float(req.take_profit.limit_price) > float(req.stop_loss.stop_price)


def test_the_child_leg_ids_are_captured(monkeypatch):
    """⚠️ ITO ANG PAGTUTOL NA PUMATAY SA BAWAT MAS NAUNANG DISENYO NG PARTIAL:
    "hindi kayang PANGALANAN, i-certify, o muling AMPUNIN ni CHILI ang stop leg
    ng OCO". Nakatayo ang buong kaligtasan ng lane sa mahigpit na
    client-order-id na pagkakakilanlan, at ang child leg ay may id mula sa
    broker. Kunin ang mga ito SA PAGLALAGAY para maitala ng session -- kung wala
    ito ay hindi mapapangalanan ang stop leg at kaya hindi maaampon muli
    pagkatapos ng restart. At ang lane na ito ay NA-RESTART habang may hawak na
    posisyon (CDTG 191sh, 2026-08-26 11:45:05,
    reason=orphaned_runner_process_exited)."""
    legs = [
        _Leg("leg-tp", "cid-tp", "limit", limit="6.37"),
        _Leg("leg-sl", "cid-sl", "stop", stop="5.85"),
    ]
    c = _Client(order=_Order(legs=legs))
    out = _place(_adapter(monkeypatch, c))
    got = {l["order_id"]: l for l in out["legs"]}
    assert set(got) == {"leg-tp", "leg-sl"}
    assert got["leg-tp"]["order_type"] == "limit"
    assert got["leg-sl"]["order_type"] == "stop"
    assert got["leg-sl"]["stop_price"] == "5.85"


def test_no_legs_returned_is_not_an_error_but_is_visible(monkeypatch):
    """Ang broker ay maaaring hindi magbalik ng nested leg sa unang tugon. Hindi
    iyon pagkabigo -- pero dapat itong MAKITA ng tumatawag para makapag-desisyon
    itong bumawi sa pamamagitan ng magulang na id."""
    out = _place(_adapter(monkeypatch, _Client(order=_Order(legs=[]))))
    assert out["ok"] is True
    assert out["legs"] == []
    assert out["order_id"] == "parent-oid"


# ── Ang mga pagtanggi (bawat isa ay pumipigil sa isang tunay na paraan ng talo) ─


def test_a_take_profit_at_or_below_the_stop_is_refused(monkeypatch):
    """Hindi iyon pag-aani -- karera iyon papuntang flush. Tanggihan ang utos sa
    halip na ipahayag ito."""
    c = _Client()
    out = _place(_adapter(monkeypatch, c), take_profit_price=5.80, stop_price=5.85)
    assert out["ok"] is False
    assert out["error"] == "alpaca_partial_take_profit_not_above_stop"
    assert out["pre_submit_blocked"] is True
    assert c.seen == [], "hindi dapat umabot sa broker"


def test_a_fractional_tranche_is_refused(monkeypatch):
    """Kaparehong bantay ng deadman stop: hindi pa sertipikado ang fractional."""
    c = _Client()
    out = _place(_adapter(monkeypatch, c), base_size="47.5")
    assert out["ok"] is False
    assert out["error"] == "alpaca_fractional_partial_not_certified"
    assert c.seen == []


@pytest.mark.parametrize("over", [
    {"product_id": "BTC-USD"},
    {"client_order_id": ""},
    {"client_order_id": None},
    {"base_size": "0"},
    {"stop_price": 0.0},
    {"take_profit_price": 0.0},
])
def test_an_uncertified_instruction_never_reaches_the_broker(monkeypatch, over):
    """⚠️ FAIL-CLOSED. Ang bawat isa sa mga ito ay dapat hindi umabot sa
    transport -- hindi umaasa sa broker para tanggihan tayo."""
    c = _Client()
    out = _place(_adapter(monkeypatch, c), **over)
    assert out["ok"] is False
    assert out.get("pre_submit_blocked") is True
    assert c.seen == []


# ── Ang oras ng araw ─────────────────────────────────────────────────────────


def test_regular_hours_uses_gtc(monkeypatch):
    from alpaca.trading.enums import TimeInForce

    c = _Client()
    _place(_adapter(monkeypatch, c))
    assert c.seen[0].time_in_force == TimeInForce.GTC


def test_premarket_uses_LIMIT_plus_DAY_plus_extended_hours(monkeypatch):
    """⚠️ Tinatanggihan ng Alpaca ang extended_hours maliban kung LIMIT + DAY.
    At ang premarket ay EKSAKTONG kung saan mahalaga ito: sadyang nag-e-entry ang
    lane doon (ang DAIC entry noong 2026-08-26 ay 09:16 ET), ang market order ay
    tahimik na kinakansela, at ang deadman STOP ay WALANG extended_hours na field
    kahit isa kaya inert ito. Ang resting limit ang TANGING instrumentong
    gumagana doon."""
    from alpaca.trading.enums import TimeInForce

    c = _Client()
    out = _place(_adapter(monkeypatch, c), extended_hours=True)
    assert out["ok"] is True
    assert c.seen[0].time_in_force == TimeInForce.DAY
    assert c.seen[0].extended_hours is True
    assert out["order_request"]["extended_hours"] is True


# ── Ang kontrata ng pagbawi ──────────────────────────────────────────────────


def test_a_broker_rejection_is_a_first_class_result(monkeypatch):
    """⚠️ ANG DIREKSYON NG KALIGTASAN. Hindi pa napapatunayan ang OCO laban sa
    buhay na broker. Ang pagtanggi ay DAPAT magbalik ng malinis na ok:False na
    may teksto ng broker, para makabalik ang tumatawag sa buong-posisyong gawi
    ngayong araw -- hindi kailanman umulit o umiwan ng share na walang
    proteksyon."""
    c = _Client(raises=RuntimeError("insufficient qty available for order"))
    out = _place(_adapter(monkeypatch, c))
    assert out["ok"] is False
    assert "insufficient qty" in out["error"]
    assert out["client_order_id"] == "cid-partial-1"
    assert out["stop_price"] is not None, (
        "ang presyong sinubukan ay dapat lumitaw sa pagkabigo, kung hindi ay "
        "hindi mabubuo ng operator ang nangyari"
    )


def test_the_quantities_can_never_overlap_by_construction():
    """⚠️ ANG MISMONG DAHILAN NG PAGSUPIL. Ang komento ay nagsasabing "would
    reserve overlapping shares". Ang disenyo ay deadman(Q-f) + oco(f) = Q --
    eksakto ang posisyon, walang sobrang reserba. Ang pagsusuring ito ay
    nagpapanatiling nakasulat ang aritmetika sa tabi ng kodigo."""
    Q, f = 95, 47
    assert (Q - f) + f == Q


# ── Bantay sa istruktura ─────────────────────────────────────────────────────


def test_the_method_does_not_shrink_the_deadman_itself():
    """⚠️ Ang method na ito ay naglalagay ng ISANG order. Kung sinubukan din
    nitong kanselahin o baguhin ang deadman ay magkakaroon ito ng dalawang
    mutation na walang transaction sa pagitan -- at ang bintana sa pagitan nila
    ang eksaktong sandaling maaaring mawalan ng proteksyon ang posisyon. Ang
    pagkakasunod ay pag-aari ng tumatawag, nang sadya."""
    src = pathlib.Path(
        AlpacaSpotAdapter.__module__.replace(".", "/") + ".py"
    )
    import app.services.trading.venue.alpaca_spot as mod

    tree = ast.parse(pathlib.Path(mod.__file__).read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
        and n.name == "place_protected_partial_oco"
    )
    calls = {
        n.func.attr for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    for forbidden in ("cancel_order", "cancel_order_by_id", "place_deadman_stop"):
        assert forbidden not in calls, (
            "ang %s ay hindi dapat mag-mutate ng deadman; pag-aari ng tumatawag "
            "ang pagkakasunod" % forbidden
        )

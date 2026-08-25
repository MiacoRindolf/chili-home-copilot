"""Ang action history ay dapat magpakita ng dahilan ng risk block (2026-08-25).

ANG PUWANG. Ang ``_event_detail`` ay naghahanap ng ``reason``, ``wait_reason``,
``trigger_reason``, ``setup_reason``, ``exit_reason``, ``outcome_class``,
``window``, ``state``, ``status`` -- pero **ang risk evaluation ay gumagamit ng
``errors``**, na wala sa listahan.

Kaya ganito ang lumalabas sa website::

    09:44  JEM    live blocked by risk · wide bbo spread      <- may dahilan
    09:45  BEEM   paper blocked by risk                       <- WALA
    09:45  XRPZ   paper blocked by risk
    09:45  XRP    paper blocked by risk
    ... walong linya, wala ni isang dahilan

Samantalang ang payload ay may laman na pala sa buong panahon::

    {"severity": "block",
     "errors": ["Not paper-eligible per neural viability."],
     "evaluated_at_utc": "..."}

Ang bunga ay hindi kosmetiko. Isang PERPEKTONG LEGITIMONG block ang mukhang
misteryo sa operator, at kinailangan ng paghukay sa DB para lang makita ang
dahilang nandoon na.

⚠️ ANG DATOS AY HINDI NAGBABAGO. Purong display ito -- binabasa lang ang isang
susing naisusulat na.

Runnable: pytest tests/test_action_history_shows_risk_errors.py -v
"""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.live_monitor import _event_detail


def test_a_risk_block_now_shows_its_reason():
    """ANG PANGUNAHING KASO -- ang eksaktong payload mula sa buhay na DB."""
    payload = {
        "severity": "block",
        "errors": ["Not paper-eligible per neural viability."],
        "evaluated_at_utc": "2026-08-25T13:47:59.307020+00:00",
    }
    assert _event_detail(payload) == "Not paper-eligible per neural viability."


def test_multiple_errors_are_all_shown():
    """Ang operator ay dapat makita ang LAHAT ng dahilan, hindi ang una lamang."""
    got = _event_detail({"errors": ["Kill switch active.", "Drawdown breaker tripped."]})
    assert got == "Kill switch active.; Drawdown breaker tripped."


def test_reason_still_WINS_over_errors():
    """⚠️ WALANG REGRESSION SA LIVE. Ang live-side na block ay nag-e-emit ng
    `reason` at iyon ang mas tumpak; hindi ito dapat masapawan ng `errors`."""
    got = _event_detail({"reason": "wide bbo spread", "errors": ["something else"]})
    assert got == "wide bbo spread"


def test_a_bare_string_error_also_works():
    """May ilang tumatawag na nagpapasa ng isang string sa halip na listahan."""
    assert _event_detail({"errors": "Not eligible."}) == "Not eligible."


@pytest.mark.parametrize("payload", [
    {"errors": []},
    {"errors": None},
    {"errors": ["", "   "]},
    {},
])
def test_an_empty_errors_field_yields_nothing_not_a_blank_separator(payload):
    """⚠️ Ang isang walang lamang `errors` ay dapat magbalik ng None, hindi ng isang
    walang lamang string -- kung hindi ay magrerender ang UI ng nakabitin na ' · '."""
    assert _event_detail(payload) is None


def test_the_detail_is_capped():
    """Ang mahabang listahan ng error ay hindi dapat sumira sa hanay."""
    got = _event_detail({"errors": ["x" * 300]})
    assert got is not None
    assert len(got) <= 120


def test_detector_rejects_still_work():
    """Ang naunang fallback ay dapat manatili."""
    got = _event_detail({"detector_rejects": {"volume_below_1p5x_avg": 12}})
    assert got == "volume_below_1p5x_avg: 12"


def test_errors_is_checked_BEFORE_detector_rejects():
    """Ang tahasang dahilan ng risk ay mas malapit sa katotohanan kaysa sa isang
    bilang ng detector reject."""
    got = _event_detail({
        "errors": ["Kill switch active."],
        "detector_rejects": {"volume_below_1p5x_avg": 12},
    })
    assert got == "Kill switch active."

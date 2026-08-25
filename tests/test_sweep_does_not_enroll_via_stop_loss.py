"""Ang maintenance sweep ay hindi dapat mag-ENROLL sa pamamagitan ng pagsulat ng stop.

ANG NATITIRANG KALAHATI (2026-08-24). Ang ``live_autopilot_trade_filter()`` ay may
LIMANG OR-branch, at **bawat writer ng alinman sa lima ay isang enrollment path**::

    auto_trader_version == "v1"   -- sinasadya
    scan_pattern_id               -- broker-sync backfill   -> naisara sa #1149
    related_alert_id              -- broker-sync backfill   -> naisara sa #1149
    stop_loss                     -- maintenance sweep      -> ITO
    take_profit                   -- maintenance sweep      -> ITO

⚠️ Ang **#1145 ay nag-bound sa HALAGA** na isinusulat ng sweep sa ``stop_loss``
(para hindi bumagsak ang R denominator sa isang 54% na stop). **HINDI nito
pinigilan ang ENROLLMENT.** Ang ``_apply_stop_to_trade`` ay nagsusulat pa rin nang
walang kundisyon kapag ``is_pattern_linked`` ay False -- kaya patuloy nitong
inililipat ang mga hindi pinamamahalaang row PAPASOK sa live sell scope; mas
matino lang ang numero.

ANG DALOY. ``broker_position_price_monitor`` (default ON, kada 5 minuto) ->
``evaluate_all`` na sinasala LAMANG sa ``Trade.status == "open"`` -- walang lane,
walang pattern-link -> ``_apply_stop_to_trade`` -> ``Trade.stop_loss`` ->
``live_autopilot_trade_filter()`` -> ``auto_trader_monitor.py:603`` ->
``:854 qty = float(t.quantity or 0)`` -- ang BUONG posisyon.

ANG AYOS. Isang row-level na katumbas ng SQL filter
(``autopilot_scope.trade_is_in_live_autopilot_scope``), na tinatawag bago ang
anumang pagsulat. Ang tamang tanong bago magsulat ay hindi *"wasto ba ang halaga"*
kundi **"gagawin ba nitong pinamamahalaan ang isang hindi pinamamahalaang row"**.

Runnable: pytest tests/test_sweep_does_not_enroll_via_stop_loss.py -v
"""
from __future__ import annotations

import inspect

import pytest

from app.services.trading.autopilot_scope import (
    LIVE_SCOPE_PRESENCE_FIELDS,
    live_autopilot_trade_filter,
    trade_is_in_live_autopilot_scope,
)


class _FakeDB:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)


class _Trade:
    def __init__(self, **kw) -> None:
        self.ticker = "SOFI"
        self.stop_loss = None
        self.take_profit = None
        self.trail_stop = None
        self.high_watermark = None
        self.related_alert_id = None
        self.scan_pattern_id = None
        self.auto_trader_version = None
        for k, v in kw.items():
            setattr(self, k, v)


def _result(new_stop: float):
    from app.services.trading.stop_engine import StopDecisionResult

    kw = {p: None for p in inspect.signature(StopDecisionResult).parameters}
    kw["new_stop"] = new_stop
    if "watermark_updated" in kw:
        kw["watermark_updated"] = False
    return StopDecisionResult(**kw)


def _apply(trade):
    from app.services.trading.stop_engine import _apply_stop_to_trade

    _apply_stop_to_trade(_FakeDB(), trade, _result(13.20))
    return trade


# ── ang row-level na tseke ────────────────────────────────────────────────

def test_an_unmanaged_row_is_out_of_scope():
    assert trade_is_in_live_autopilot_scope(_Trade()) is False


@pytest.mark.parametrize("field", LIVE_SCOPE_PRESENCE_FIELDS)
def test_any_present_field_puts_the_row_in_scope(field):
    assert trade_is_in_live_autopilot_scope(_Trade(**{field: 1})) is True


def test_v1_is_in_scope_even_with_every_field_empty():
    assert trade_is_in_live_autopilot_scope(_Trade(auto_trader_version="v1")) is True


def test_the_python_check_matches_the_SQL_filter():
    """⚠️ ANG BANTAY SA DRIFT. Dalawang anyo ng iisang tanong; kung maghihiwalay
    sila, ang isang writer ay muling makakapag-enroll nang tahimik."""
    sql = str(live_autopilot_trade_filter())
    for field in LIVE_SCOPE_PRESENCE_FIELDS:
        assert field in sql, f"{field} ay nasa Python na tseke pero wala sa SQL"
    assert "auto_trader_version" in sql


# ── ang tunay na epekto sa write path ────────────────────────────────────

def test_the_sweep_does_NOT_write_a_stop_onto_an_unmanaged_row():
    """ANG PANGUNAHING BANTAY. Ang pagsulat na ito ang mismong enrollment."""
    t = _apply(_Trade())
    assert t.stop_loss is None
    assert t.take_profit is None


@pytest.mark.parametrize(
    "kw",
    [
        {"auto_trader_version": "v1"},
        {"scan_pattern_id": 7},
        {"related_alert_id": 42},
        {"take_profit": 15.5},
    ],
)
def test_an_already_managed_row_still_gets_its_stop(kw):
    """Hindi dapat mawala ang stop management para sa mga tunay na CHILI row."""
    t = _apply(_Trade(**kw))
    assert t.stop_loss == 13.20


def test_the_guard_runs_before_any_write():
    """Bantayan ang posisyon -- ang bantay na nasa ilalim ng unang pagsulat ay
    walang silbi."""
    from app.services.trading import stop_engine

    src = inspect.getsource(stop_engine._apply_stop_to_trade)
    assert src.index("trade_is_in_live_autopilot_scope") < src.index("trade.stop_loss =")


def test_1145_value_bound_is_still_in_place():
    """Ang dalawang ayos ay magkapatid, hindi magkapalit: ang #1145 ay nag-bound
    sa HALAGA, ito ay sa ENROLLMENT. Pareho silang kailangan."""
    from app.services.trading import stop_engine

    assert hasattr(stop_engine, "_bound_maintenance_stop_distance")
    src = inspect.getsource(stop_engine._compute_initial_stop)
    assert "_bound_maintenance_stop_distance" in src

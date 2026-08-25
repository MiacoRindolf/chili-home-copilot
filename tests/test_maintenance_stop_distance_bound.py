"""Ang maintenance sweep ay hindi dapat magsulat ng stop na hindi kayang abutin.

ANG PUWANG (natuklasan 2026-08-24 ng audit ng PR #732 -- ibang depekto ito kaysa
sa iminungkahi ng #732, at sa ibang landas).

``stop_engine._compute_initial_stop`` ang NAG-IISANG producer ng stop para sa
isang trade na wala nito, at naaabot ito mula sa BUHAY na 5-minutong sweep
(``broker_position_price_monitor``, default ON; ang ``evaluate_all`` ay
sinasala LAMANG sa ``Trade.status == "open"`` -- walang lane filter, walang
pattern-link filter). Nagsusulat ito ng WALANG HANGGANANG ATR-multiple na stop,
at WALA sa pagitan nito at ng nakaupong broker order ang muling nagsusuri sa
fractional cap na ipinapatupad na ng codebase na ito sa entry.

⚠️ DALAWANG PINSALA, at PAREHONG SIZING-INDEPENDENT -- kaya hindi sapat ang
"sinasalo na ng risk-first sizing ang malayong stop". Ang mga row na ito ay
BUKAS NANG POSISYON (orphan / broker-adopted); walang sizer na sangkot.

1. SCOPE ENROLLMENT. Ang ``autopilot_scope.live_autopilot_trade_filter`` ay isang
   ``or_(...)`` na naglalaman ng ``Trade.stop_loss.isnot(None)`` (:48). Ang isang
   broker-adopted na row na NULL ang ``related_alert_id``/``stop_loss`` ay nasa
   LABAS ng live execution monitor. Sa sandaling isulat ng sweep ang stop, ang row
   ay PUMAPASOK sa scope at ang numerong iyon ang nagiging operative exit level
   nito. At ang never-widen guard ng ``_apply_stop_to_trade`` ay
   ``if is_pattern_linked and trade.stop_loss is not None:`` -- PAREHONG mali para
   sa mga row na ito, kaya walang kundisyon ang pagsulat.

2. R-DENOMINATOR COLLAPSE. ``R = abs(entry - stop)``;
   ``current_r = (price - entry) / R``; ``BREAKEVEN_R = 1.0``; ``TRAILING_R = 2.0``.
   Sa 54% na stop, ang break-even ay nangangailangan ng +54% at ang chandelier
   trail ng +109%. Habambuhay na hindi napamamahalaan ang posisyon.

⚠️ ANG PAGKAKAIBA SA #732. Sa ENTRY path ay MAPANGANIB ang clamp: risk-first ang
sizing (``qty = max_loss / stop_distance``), kaya ang pagsisikip ng stop ay
BUMIBILI ng laki -- ang pag-clamp ng 84% tungong 25% ay nagtatriple ng posisyon sa
pinakapabagu-bagong pangalan. Iyon ang dahilan ng pagsasara ng #732. DITO ay
UMIIRAL NA ang posisyon, kaya hindi makakadagdag ng exposure ang clamp.

Runnable: pytest tests/test_maintenance_stop_distance_bound.py -v
"""
from __future__ import annotations

import inspect

import pytest

from app.services.trading.stop_engine import _bound_maintenance_stop_distance as bound


def _rr(entry: float, sl: float, tp: float, long_side: bool = True) -> float:
    risk = (entry - sl) if long_side else (sl - entry)
    reward = (tp - entry) if long_side else (entry - tp)
    return reward / risk


def test_a_stop_inside_the_cap_is_untouched():
    """ANG 99% NA LANDAS. Ito ang dapat na wala talagang epekto."""
    assert bound(entry=10.0, direction="long", sl=9.0, tp=12.0, is_crypto=False) == (9.0, 12.0)


def test_a_stock_stop_past_the_cap_is_bounded():
    """ANG EKSAKTONG KASO: 54% -> ang 30% na stock cap."""
    sl, _tp = bound(entry=10.0, direction="long", sl=4.6, tp=16.0, is_crypto=False)
    assert sl == pytest.approx(7.0)


def test_the_reward_risk_ratio_is_preserved_exactly():
    """⚠️ Ang pagsisikip ng stop nang HINDI ini-scale ang target ay tahimik na
    magpapataas ng R:R at magpapabaluktot sa bawat downstream na desisyon."""
    before = _rr(10.0, 4.6, 16.0)
    sl, tp = bound(entry=10.0, direction="long", sl=4.6, tp=16.0, is_crypto=False)
    assert _rr(10.0, sl, tp) == pytest.approx(before)


def test_the_cap_is_asset_aware_not_a_single_number():
    """Ang 54% ay lampas sa stock (30%) pero NASA LOOB ng crypto (60%)."""
    assert bound(entry=10.0, direction="long", sl=4.6, tp=16.0, is_crypto=True) == (4.6, 16.0)
    sl, _ = bound(entry=10.0, direction="long", sl=3.0, tp=20.0, is_crypto=True)
    assert sl == pytest.approx(4.0)  # 70% -> 60%


def test_short_side_bounds_in_the_other_direction():
    sl, tp = bound(entry=10.0, direction="short", sl=15.4, tp=4.0, is_crypto=False)
    assert sl == pytest.approx(13.0)
    assert _rr(10.0, sl, tp, long_side=False) == pytest.approx(_rr(10.0, 15.4, 4.0, long_side=False))


def test_a_wrong_sided_stop_is_left_alone():
    """Ibang bug iyon at hindi dapat itago rito."""
    assert bound(entry=10.0, direction="long", sl=11.0, tp=12.0, is_crypto=False) == (11.0, 12.0)


@pytest.mark.parametrize(
    "kw",
    [
        {"entry": 0.0, "sl": 4.6, "tp": 16.0},
        {"entry": 10.0, "sl": 0.0, "tp": 16.0},
        {"entry": -1.0, "sl": 4.6, "tp": 16.0},
    ],
)
def test_unusable_input_fails_OPEN(kw):
    """⚠️ Hinding-hindi ito dapat maging dahilan kung bakit NABIGONG maisulat ang
    isang stop. Ang kawalan ng stop ay mas masahol kaysa sa malawak na stop."""
    out = bound(direction="long", is_crypto=False, **kw)
    assert out == (kw["sl"], kw["tp"])


def test_it_reuses_the_shipped_cap_and_invents_no_constant():
    """⚠️ Walang magic number. Ang cap ay ang naipasa at nasusubukan nang
    `_max_execution_stop_loss_fraction` (stock 30 / crypto 60; 0 = naka-disable)."""
    src = inspect.getsource(bound)
    assert "_max_execution_stop_loss_fraction" in src
    for magic in ("0.30", "0.60", "30.0", "60.0"):
        assert magic not in src, f"naka-hard-code ang cap na {magic}"


def test_the_producer_actually_calls_the_bound():
    """Bantayan ang call site -- walang silbi ang helper kung hindi tinatawag."""
    from app.services.trading import stop_engine

    src = inspect.getsource(stop_engine._compute_initial_stop)
    assert "_bound_maintenance_stop_distance" in src
    # dapat mangyari BAGO ang tick normalization na nagsusulat ng huling presyo
    assert src.index("_bound_maintenance_stop_distance") < src.index("_norm_price")


def test_settings_is_imported_locally_inside_the_helper():
    """⚠️ Ang `settings` ay WALA sa module scope dito. Kung wala ang lokal na
    import, ang NameError ay lalamunin ng fail-open at TAHIMIK na hindi gagana ang
    bound -- eksaktong nangyari sa unang draft, at nahuli lang sa pagsubok."""
    src = inspect.getsource(bound)
    assert "from ...config import settings as _settings" in src
    assert "_max_execution_stop_loss_fraction(\n            _settings" in src

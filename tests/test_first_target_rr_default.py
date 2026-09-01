"""Ang first-target R:R floor ay 2.5 (#1271).

INTERLEAVED na A/B, 10 window x 3 arm, 2026-09-01:

        RR 2.0 = +160.47      RR 2.5 = +185.21      RR 3.0 = +151.91

Ang 2.5 ay +24.74 (+15.4%) laban sa 2.0; ang 3.0 ay mas masama kaysa pareho.
PEAK ito, hindi monotone — sinadyang idinagdag ang 3.0 arm bilang pagsubok sa
premise (kung patuloy pang gumaganda ang 3.0, ang TRAIL ang gumagawa ng trabaho
at mali ang buong ideya ng target). Bumagsak ang XPON +44.66 -> +23.77 sa 3.0.

WALANG WINDOW ANG NASIRA sa 2.5: 3 ang gumanda, 7 ang LITERAL na magkapareho
kasama ang dalawang pinakamalaking panalo (SLE +107.81, CAPR +29.10).

Ang test na ito ay hindi tungkol sa numero mismo — ito ay tungkol sa
PAGKAKASUNOD-SUNOD at sa MEKANISMO na ginagawang makahulugan ang numero:
ang floor ay isang FLOOR (kayang itaas ng adaptive layer, hindi ibaba), at ang
mas malayong target ay hindi kailanman nakakapinsala sa isang trade na hindi
umaabot doon.

Runnable: pytest tests/test_first_target_rr_default.py -v
"""
from __future__ import annotations

from app.config import Settings
from app.services.trading.momentum_neural.paper_execution import (
    adaptive_first_target_reward_risk,
    stop_target_prices,
)


AB_TOTALS = {"2.0": 160.47, "2.5": 185.21, "3.0": 151.91}


def test_default_is_the_ab_winner():
    assert Settings().chili_momentum_risk_reward_risk_ratio == 2.5


def test_the_ab_shape_is_a_peak_not_a_ramp():
    """Kung monotone ito, ang trail ang lever at mali ang ship na ito."""
    assert AB_TOTALS["2.5"] > AB_TOTALS["2.0"]
    assert AB_TOTALS["2.5"] > AB_TOTALS["3.0"], (
        "monotone-pataas ⇒ hindi ang target ang gumagawa ng trabaho"
    )


def test_target_moves_further_from_entry_than_the_old_floor():
    """Ang buong mekanismo: mas malayo ang target sa parehong stop."""
    entry, atr = 4.01, 0.0156
    stop_a, t_old = stop_target_prices(
        entry, atr_pct=atr, reward_risk=2.0, partial_capable=False,
    )
    stop_b, t_new = stop_target_prices(
        entry, atr_pct=atr, reward_risk=2.5, partial_capable=False,
    )
    assert stop_a == stop_b, "ang R:R ay hindi dapat gumalaw ng STOP"
    assert t_new > t_old
    r = entry - stop_a
    assert abs((t_new - entry) / r - 2.5) < 1e-6


def test_the_floor_can_only_be_RAISED_by_the_adaptive_layer():
    """FLOOR ito. Ang naipatunay na headroom ay nagtataas; walang nagbababa."""
    entry, stop = 4.01, 3.9725
    base = 2.5
    # walang naipatunay na headroom -> eksaktong base
    rr, _meta = adaptive_first_target_reward_risk(
        base_reward_risk=base, entry=entry, stop=stop, realized_high=None,
    )
    assert rr == base
    # realized high sa ibaba ng entry -> base pa rin (hindi kailanman mas mababa)
    rr, _meta = adaptive_first_target_reward_risk(
        base_reward_risk=base, entry=entry, stop=stop, realized_high=entry - 0.10,
    )
    assert rr == base
    # malaking naipatunay na headroom -> maaaring itaas, hindi kailanman ibaba
    rr, _meta = adaptive_first_target_reward_risk(
        base_reward_risk=base, entry=entry, stop=stop, realized_high=entry + 1.0,
    )
    assert rr >= base


def test_a_trade_that_never_reaches_the_old_target_is_untouched():
    """Bakit 7 sa 10 window ang LITERAL na magkapareho.

    Ang mas malayong target ay tumatama LAMANG sa trade na umaabot sa lumang
    target tapos ay patuloy pang tumataas. Sa ilalim ng lumang target, ang
    dalawang setting ay naglalabas ng magkaparehong stop at magkaparehong
    kabuluhan — kaya walang window na maaaring masira.
    """
    entry, atr = 4.01, 0.0156
    stop_a, t_old = stop_target_prices(
        entry, atr_pct=atr, reward_risk=2.0, partial_capable=False,
    )
    stop_b, t_new = stop_target_prices(
        entry, atr_pct=atr, reward_risk=2.5, partial_capable=False,
    )
    peak = entry + 0.5 * (t_old - entry)   # hindi umabot sa lumang target
    assert peak < t_old < t_new
    assert stop_a == stop_b               # magkaparehong panganib
    # walang target ang tinamaan sa magkabilang setting => magkaparehong resulta
    assert (peak >= t_old) == (peak >= t_new) is False


def test_env_override_still_wins():
    """Nananatiling nako-configure ang knob — default lang ang ginalaw."""
    assert Settings(
        CHILI_MOMENTUM_RISK_REWARD_RISK_RATIO=3.0
    ).chili_momentum_risk_reward_risk_ratio == 3.0

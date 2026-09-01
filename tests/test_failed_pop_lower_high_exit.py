"""Failed-pop MOMENTUM-BREAK exit (#1261) — doktrina ni Ross 2026-08-31.

"Once you have a stock that's gone up but is not moving higher, usually it's
because sellers have come in." Ang aktwal niyang tinitingnan: TULOY-TULOY na
GREEN na 10-segundong candle; ang unang pula pagkatapos ng green run = tapos
na ang leg.

SINUKAT sa 6 tunay na trade (08-31/09-01): green-run break +448.99 vs aktwal
(mekanikal na lower-high +419.63; green+L2 filter +356.28 — ang L2 ay
nagpapabagal). SSM 09-01: lower-high lumabas sa 4.01 (+0); green break sa
4.12 (+47.63) laban sa aktwal na −25.98.

RUNNER PROTECTION (itinama matapos mahuli ng sariling test ang baligtad na
unang disenyo): HINDI profit gate — ang pulang bar ay dapat magsara sa IBABA
ng LOW ng naunang bar (tunay na pagbasag ng istruktura). Ang pulang bar sa
loob ng malakas na run ay ingay; hawak pa rin.

Runnable: pytest tests/test_failed_pop_lower_high_exit.py -v
"""
from __future__ import annotations

from app.services.trading.momentum_neural.paper_execution import (
    failed_pop_momentum_break_exit,
)


def _call(**over):
    # 4 green bars tapos isang pula — ang SSM shape.
    kw = dict(
        enabled=True,
        bar_closes_opens=[
            (4.05, 4.01), (4.09, 4.05), (4.13, 4.09), (4.15, 4.13),
            (4.12, 4.15),
        ],
        made_new_high=True,
        current_price=4.12,
        avg_entry=4.01,
        stop_price=3.97,
        prior_bar_low=4.13,
        min_green_run=2,
    )
    kw.update(over)
    return failed_pop_momentum_break_exit(**kw)


def test_ssm_shape_fires_after_green_run():
    fire, dbg = _call()
    assert fire is True
    assert dbg["green_run"] == 4


def test_still_green_does_not_fire():
    """Habang berde ang huling bar, hawak — hindi pa tapos ang leg."""
    fire, _ = _call(bar_closes_opens=[
        (4.05, 4.01), (4.09, 4.05), (4.13, 4.09), (4.17, 4.13),
    ])
    assert fire is False


def test_short_green_run_does_not_fire():
    """Isang green lang bago ang pula = ingay, hindi leg."""
    fire, dbg = _call(bar_closes_opens=[(3.99, 4.01), (4.05, 4.01), (4.02, 4.05)])
    assert fire is False
    assert dbg["green_run"] == 1


def test_red_bar_not_breaking_prior_low_is_noise():
    """Pulang bar na HINDI bumasag ng prior low = ingay sa loob ng run —
    hawak pa rin (ito ang runner protection, hindi ang profit gate)."""
    fire, dbg = _call(prior_bar_low=4.05)   # close 4.12 >= 4.05
    assert fire is False
    assert dbg["skipped"] == "red_bar_did_not_break_prior_low"


def test_no_prior_low_still_fires():
    fire, _ = _call(prior_bar_low=None)
    assert fire is True


def test_no_new_high_does_not_fire():
    """Walang pop = walang failed pop."""
    fire, _ = _call(made_new_high=False)
    assert fire is False


def test_flag_off_is_noop():
    assert _call(enabled=False)[0] is False


def test_missing_bars_fails_open():
    assert _call(bar_closes_opens=None)[0] is False
    assert _call(bar_closes_opens=[(4.1, 4.0)])[0] is False


def test_missing_prices_fail_open():
    assert _call(current_price=None)[0] is False
    assert _call(avg_entry=None)[0] is False


def test_no_stop_still_fires():
    fire, dbg = _call(stop_price=None)
    assert fire is True
    assert "r_now" not in dbg


def test_move2_shape_fires():
    """MOVE#2 08-31: green run tapos pula, malayo pa sa rung."""
    fire, _ = _call(
        bar_closes_opens=[
            (15.09, 15.05), (15.13, 15.09), (15.16, 15.13), (15.08, 15.16),
        ],
        current_price=15.08, avg_entry=15.05, stop_price=14.90,
        prior_bar_low=15.10,
    )
    assert fire is True

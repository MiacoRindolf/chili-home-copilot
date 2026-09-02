"""Burst-window exit: labas ~60s pagkatapos magsimula ang burst (#1275).

NASUKAT sa **968 na kaso sa 5 magkakahiwalay na araw**, 26 na pangalan.
Kondisyon: NASA LOOB NA TAYO bago ang burst (entry = close 2 min bago ang
1-min bar na tumaas >=1.5%):

    paglabas sa   45s     60s     90s    120s    300s
    average      2.15%   3.01%   2.81%   2.77%   2.47%

    08-26  160 kaso  60s=3.03  panalo 93%
    08-27  140 kaso  60s=3.76  panalo 85%
    08-28   89 kaso  60s=2.64  panalo 91%
    08-31  178 kaso  60s=3.20  panalo 86%
    09-01  401 kaso  60s=2.74  panalo 86%
    LAHAT  968 kaso  60s=3.01  panalo 88% (852/968)

Ang 60s ang peak sa **4 sa 5 araw**, at PAREHO ang hugis sa bawat araw:
45s < 60s > 90s > 120s > 300s. NET pagkatapos ng spread (median 53.9 bps):
+2.47% sa isang crossing, +1.93% sa dalawa.

ANG BUMAGSAK — lahat natalo ng orasang ito: trailing stop 0.5/1.0/1.5/2.5%,
volume conditioning (<5x..>=40x), tape-speed cadence decay, at ang CUSUM sa
SIGNED FLOW (+1.74% lamang) na siyang literature Signal 1.

Runnable: pytest tests/test_burst_window_exit.py -v
"""
from __future__ import annotations

from app.config import Settings
from app.services.trading.momentum_neural.live_runner import (
    _BURST_TRACK_MAX_SAMPLES,
    _FRESHNESS_FAIL_OPEN_EXIT_REASONS,
    _burst_track_push,
    burst_window_decision as decide,
)

K = dict(min_move_pct=1.5, lookback_s=60.0, decision_s=45.0)
FLAT = [[0.0, 4.00], [10.0, 4.00], [20.0, 4.01]]


def test_burst_arms_on_a_real_move():
    """+2.0% sa loob ng lookback ⇒ armado ang orasan, hindi pa lumalabas."""
    fire, started, dbg = decide(
        FLAT, now_epoch=30.0, price=4.08, burst_started_epoch=None, **K
    )
    assert fire is False, "ang pag-arma ay HINDI paglabas"
    assert started == 30.0
    assert dbg["burst_detected"] is True
    assert dbg["move_pct"] == 2.0


def test_a_quiet_tape_never_arms():
    fire, started, dbg = decide(
        FLAT, now_epoch=30.0, price=4.02, burst_started_epoch=None, **K
    )
    assert (fire, started) == (False, None)
    assert "burst_detected" not in dbg


def test_the_clock_fires_at_the_decision_point():
    """45s ang desisyon — hindi 44.9s."""
    assert decide(FLAT, now_epoch=74.9, price=4.10, burst_started_epoch=30.0, **K)[0] is False
    assert decide(FLAT, now_epoch=75.0, price=4.10, burst_started_epoch=30.0, **K)[0] is True
    assert decide(FLAT, now_epoch=90.0, price=4.10, burst_started_epoch=30.0, **K)[0] is True


def test_the_decision_point_is_45s_because_of_measured_latency():
    """Ang 60s ay ang optimum ng PRESYO; ang 45s ay ang desisyon.

    Nasukat 2026-09-01 ang latency ng desisyon->fill: AUUD 14.4s, GYGY 8.7s,
    WETO 15.7s. Ang pagpapaputok sa 45s ay naglalapag ng FILL sa ~60s.
    """
    s = Settings()
    assert s.chili_momentum_burst_exit_decision_seconds == 45.0
    MEASURED_LATENCY = (8.7, 15.7)
    for lat in MEASURED_LATENCY:
        landing = s.chili_momentum_burst_exit_decision_seconds + lat
        assert 50.0 <= landing <= 65.0, landing


def test_the_arm_is_sticky_across_a_dip():
    """Kapag armado, hindi na ito bina-back-out ng helper kahit bumaba ang presyo."""
    fire, started, _ = decide(
        FLAT, now_epoch=50.0, price=3.90, burst_started_epoch=30.0, **K
    )
    assert started == 30.0
    assert fire is False          # 20s pa lamang
    assert decide(FLAT, now_epoch=76.0, price=3.90, burst_started_epoch=30.0, **K)[0] is True


def test_lookback_low_is_the_reference_not_the_first_sample():
    """Ang sanggunian ay ang PINAKAMABABA sa loob ng lookback."""
    tr = [[0.0, 4.20], [10.0, 3.90], [20.0, 4.00]]
    _f, _s, dbg = decide(tr, now_epoch=30.0, price=3.97, burst_started_epoch=None, **K)
    assert dbg["lookback_low"] == 3.90
    assert round(dbg["move_pct"], 2) == 1.79


def test_samples_outside_the_lookback_are_ignored():
    """Ang lumang mababang presyo ay hindi dapat gumawa ng pekeng burst."""
    tr = [[0.0, 2.00], [260.0, 4.00]]     # ang 2.00 ay 300s na; ang 4.00 ay 40s
    _f, started, dbg = decide(
        tr, now_epoch=300.0, price=4.05, burst_started_epoch=None, **K
    )
    assert dbg["lookback_low"] == 4.00, "ang 2.00 ay lampas sa 60s lookback"
    assert started is None, "1.25% laban sa 4.00 — kulang sa 1.5% na hangganan"


def test_an_empty_lookback_window_reports_no_reference():
    """Lahat ng sample ay luma na ⇒ walang sanggunian, walang burst."""
    tr = [[0.0, 2.00], [10.0, 2.10]]
    _f, started, dbg = decide(
        tr, now_epoch=300.0, price=4.05, burst_started_epoch=None, **K
    )
    assert started is None
    assert dbg == {"no_reference": True}


def test_fails_closed_on_garbage():
    """Walang posisyong lumalabas dahil sa bug."""
    for bad in (None, "kalokohan", float("nan"), 0.0, -1.0):
        fire, _s, _d = decide(FLAT, now_epoch=75.0, price=bad, burst_started_epoch=30.0, **K)
        assert fire is False, bad
    assert decide(None, now_epoch=30.0, price=4.08, burst_started_epoch=None, **K)[0] is False
    assert decide([], now_epoch=30.0, price=4.08, burst_started_epoch=None, **K)[0] is False


def test_track_is_bounded():
    le: dict = {}
    for i in range(_BURST_TRACK_MAX_SAMPLES + 10):
        _burst_track_push(le, now_epoch=float(i), price=4.0 + i * 0.001)
    assert len(le["burst_track"]) == _BURST_TRACK_MAX_SAMPLES


def test_track_ignores_unusable_prices():
    le: dict = {}
    _burst_track_push(le, now_epoch=1.0, price=4.0)
    for bad in (None, 0.0, -1.0, float("nan"), "x"):
        _burst_track_push(le, now_epoch=2.0, price=bad)
    assert le["burst_track"] == [[1.0, 4.0]]


def test_the_exit_reason_fails_open_at_freshness_seams():
    """60 segundo ang bintana — kung mahuhuli ito ng freshness gate, wala na."""
    assert "burst_window_exit" in _FRESHNESS_FAIL_OPEN_EXIT_REASONS


def test_ships_on_after_the_event_study():
    """ON (#1277). Ang mapagpasyang ebidensya: event-study sa TUNAY na anim na
    pasok ng 2026-09-01 gamit ang mismong helper — SSM -25.98 -> +30.31
    (ang magandang pasok ay nagiging PANALO), kabuuan -124.66 -> -66.94
    (+57.72). Replay A/B: 7/8 walang galaw, LIDR +0.98, ZERO control na
    nasira."""
    s = Settings()
    assert s.chili_momentum_burst_exit_enabled is True
    assert s.chili_momentum_burst_exit_min_move_pct == 1.5
    assert s.chili_momentum_burst_exit_lookback_seconds == 60.0

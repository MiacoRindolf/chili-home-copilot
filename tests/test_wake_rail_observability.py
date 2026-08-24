"""Ang IQFeed wake rail ay dapat maiulat ang sarili (2026-08-24).

NASUKAT NA PUWANG. Ang rail ay buhay sa TRANSPORT level -- sinukat: **174
notification/segundo** sa ``momentum_iqfeed_l1``, at tumatakbo ang listener
(``[iqfeed_wake] started -- LISTEN``). Pero ang aktwal na per-session tick
cadence ay **10.4s p50** (DAIC 10.4 · NTRB 10.3 · GGR 10.4 · INCR 9.4) --
EKSAKTONG scheduler-batch interval, hindi kailanman ang 2s na wake spacing.
Kaya walang nagigising.

Hindi masabi KUNG BAKIT: ang ``_notifies``/``_wakes`` ay binibilang pero WALANG
endpoint, WALANG log, at WALANG consumer ng ``stats()`` kahit saan sa repo. Ang
unang pangangailangan ay isang NUMERO, hindi isang hula.

Runnable: pytest tests/test_wake_rail_observability.py -v
"""
from __future__ import annotations

import logging

from app.services.trading.momentum_neural import iqfeed_wake_listener as wl


class _Tracker:
    def __init__(self, n: int):
        self._by_symbol = {f"S{i}": [{"session_id": i}] for i in range(n)}


def _listener(tracked: int = 3):
    obj = wl.IqfeedWakeListener.__new__(wl.IqfeedWakeListener)
    obj._notifies = 0
    obj._wakes = 0
    obj._last_report_at = 0.0
    obj._started_at = 0.0
    obj._tracker = lambda: _Tracker(tracked)
    return obj


def test_the_report_names_notifies_wakes_and_tracked(caplog):
    lis = _listener(tracked=4)
    lis._notifies = 5000
    lis._wakes = 0
    with caplog.at_level(logging.INFO, logger=wl._log.name):
        lis._maybe_report()
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "notifies=5000" in text
    assert "wakes=0" in text
    assert "tracked_sessions=4" in text


def test_it_reports_at_most_once_per_window(caplog):
    """174 notify/s ⇒ ang per-event logging ay isang storm."""
    lis = _listener()
    with caplog.at_level(logging.INFO, logger=wl._log.name):
        for _ in range(50):
            lis._maybe_report()
    lines = [r for r in caplog.records if "rail notifies=" in r.getMessage()]
    assert len(lines) == 1, f"dapat isang linya kada window, nakita {len(lines)}"


def test_a_broken_tracker_still_reports(caplog):
    """Ang observability ay hinding-hindi dapat ang bagay na sumisira."""
    lis = _listener()

    def _boom():
        raise RuntimeError("wala ang tracker")

    lis._tracker = _boom
    with caplog.at_level(logging.INFO, logger=wl._log.name):
        lis._maybe_report()
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "tracked_sessions=-1" in text, "dapat mag-ulat ng unknown, hindi sumabog"


def test_the_report_window_is_bounded():
    assert 1.0 <= wl._REPORT_EVERY_S <= 300.0

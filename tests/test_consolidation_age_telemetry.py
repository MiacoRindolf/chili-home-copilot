"""Consolidation-age telemetry (Ross 2026-08-17 Aral #14/#21).

"The longer consolidated without rolling over, it's good" — ang bigong unang
IPST entry ni Ross ay kulang sa consolidation, at ang panalong re-entry ay may
hinog na base. Sukat muna ([[feedback_evolve_not_devolve]]): itinatak ang edad
ng breakout/watch level sa bawat candidate fire; ang tilt ay desisyon
pagkatapos ng discrimination measurement (winners vs losers sa age).
"""
from __future__ import annotations

import time

import app.services.trading.momentum_neural.live_runner as lr


def test_stamp_sets_timestamp_on_new_level():
    le: dict = {}
    lr._stamp_level_set_at(le, "breakout_level_price", 7.36)
    assert le["breakout_level_price"] == 7.36
    assert le.get("breakout_level_price_set_at_utc")


def test_same_level_does_not_reset_age():
    """Ang muling paglapat ng PAREHONG level ay hindi nagre-reset ng edad —
    ito mismo ang consolidation semantics."""
    le: dict = {}
    lr._stamp_level_set_at(le, "breakout_level_price", 7.36)
    first = le["breakout_level_price_set_at_utc"]
    time.sleep(0.01)
    lr._stamp_level_set_at(le, "breakout_level_price", 7.36)
    assert le["breakout_level_price_set_at_utc"] == first
    # sub-0.1% na galaw = parehong level pa rin
    lr._stamp_level_set_at(le, "breakout_level_price", 7.3601)
    assert le["breakout_level_price_set_at_utc"] == first


def test_materially_new_level_resets_age():
    le: dict = {}
    lr._stamp_level_set_at(le, "breakout_level_price", 7.36)
    first = le["breakout_level_price_set_at_utc"]
    time.sleep(0.01)
    lr._stamp_level_set_at(le, "breakout_level_price", 7.80)
    assert le["breakout_level_price"] == 7.80
    assert le["breakout_level_price_set_at_utc"] >= first


def test_age_reader_semantics():
    le: dict = {}
    assert lr._level_age_seconds(le, "breakout_level_price") is None
    lr._stamp_level_set_at(le, "breakout_level_price", 7.36)
    age = lr._level_age_seconds(le, "breakout_level_price")
    assert age is not None and 0.0 <= age < 5.0
    le["watch_break_level_set_at_utc"] = "hindi-petsa"
    assert lr._level_age_seconds(le, "watch_break_level") is None

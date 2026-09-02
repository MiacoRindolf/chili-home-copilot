"""Entry-advance wake: ang pre-entry session ay ginigising ng BAGONG HIGH (#1282).

ANG PUWANG (na-map 2026-09-02). Sa deployed batch window (LOOP_ENABLED=false,
10s cadence, sukat na 2.3-8s kada pass, paminsang 30s) ang bawat pre-entry
state ay umuusad LANG sa pulse. May push rail na (pg LISTEN momentum_iqfeed_l1
~174 notify/s + WS bus) pero ang `_SessionCrossTracker.crossed` ay gumigising
lamang ng: pending exit (kada tick), held position (stop/target cross o
bagong high), PENDING_ENTRY (kada tick), at WATCHING sa `watch_break_level`
cross. Ang ARMED_PENDING_RUNNER, QUEUED_LIVE at LIVE_ENTRY_CANDIDATE ay hindi
magigising ng tick kailanman; ang WATCHING na walang level (velocity, volume,
VWAP na trigger) ay pareho. Ang AUUD 09-01 ay nag-evaluate kada ~10s
(candidate_detected 11:06:51, :07:05, :07:16, :07:26...).

ANG LUNAS: ang parehong bagong-high na wake na ginagamit na ng held position
(seed muna, saka bawat bagong high, 2s spacing kada session) — dahil ang
momentum entry ay pumuputok habang UMAAKYAT ang presyo. May global na
hangganan kada segundo dahil sa bukas ay sabay-sabay ang 20-40 pre-entry
session; lampas doon ay ang batch ang safety net. Dispatch hint lamang.

Runnable: pytest tests/test_entry_advance_wake.py -v
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services.trading.momentum_neural import ignition_loop as il
from app.services.trading.momentum_neural.live_fsm import (
    STATE_ARMED_PENDING_RUNNER,
    STATE_LIVE_ENTERED,
    STATE_LIVE_ENTRY_CANDIDATE,
    STATE_LIVE_PENDING_ENTRY,
    STATE_QUEUED_LIVE,
    STATE_WATCHING_LIVE,
)

PRE_ENTRY = (
    STATE_ARMED_PENDING_RUNNER,
    STATE_QUEUED_LIVE,
    STATE_WATCHING_LIVE,
    STATE_LIVE_ENTRY_CANDIDATE,
)


def _tracker(entries_by_symbol: dict):
    t = il._SessionCrossTracker()
    t._by_symbol = dict(entries_by_symbol)
    return t


def _q(mid=None, bid=None):
    return SimpleNamespace(bid=bid, mid=mid, last=None)


@pytest.fixture(autouse=True)
def _wide_budget(monkeypatch):
    monkeypatch.setattr(
        il.settings, "chili_momentum_entry_advance_wake_max_per_second", 50.0,
        raising=False,
    )
    monkeypatch.setattr(
        il.settings, "chili_momentum_entry_advance_wake_enabled", True, raising=False,
    )


@pytest.mark.parametrize("state", PRE_ENTRY)
def test_pre_entry_session_wakes_on_each_new_high_after_the_seed(state):
    """ANG PANGUNAHIN: seed (tahimik), bagong high (gising), pullback/pantay (tahimik)."""
    t = _tracker({"XLAB": [{"session_id": 1, "state": state}]})
    assert t.crossed("XLAB", _q(mid=4.00)) == [], "ang unang tick ay seed lamang"
    assert t.crossed("XLAB", _q(mid=4.01)) == [1]
    assert t.crossed("XLAB", _q(mid=3.95)) == [], "pullback: walang wake"
    assert t.crossed("XLAB", _q(mid=4.01)) == [], "pantay sa high: hindi bagong high"
    assert t.crossed("XLAB", _q(mid=4.02)) == [1]


def test_bid_stands_in_when_there_is_no_mid():
    t = _tracker({"XLAB": [{"session_id": 3, "state": STATE_LIVE_ENTRY_CANDIDATE}]})
    assert t.crossed("XLAB", _q(bid=4.00)) == []
    assert t.crossed("XLAB", _q(bid=4.03)) == [3]


def test_level_cross_still_wakes_watching_on_a_reclaim_below_the_high():
    """Ang reclaim ng level pagkatapos ng pullback ay HINDI bagong running high —
    ang umiiral na level-cross wake ang humuhuli nito, at nananatili."""
    t = _tracker({"XLAB": [{
        "session_id": 2, "state": STATE_WATCHING_LIVE, "watch_break_level": 3.90,
    }]})
    assert t.crossed("XLAB", _q(mid=3.80)) == []      # seed, sa ilalim ng level
    assert t.crossed("XLAB", _q(mid=3.95)) == [2]     # bagong high AT level cross
    assert t.crossed("XLAB", _q(mid=3.70)) == []
    assert t.crossed("XLAB", _q(mid=3.91)) == [2]     # reclaim: level cross lamang


def test_kill_switch_silences_only_the_advance_wake(monkeypatch):
    monkeypatch.setattr(
        il.settings, "chili_momentum_entry_advance_wake_enabled", False, raising=False,
    )
    t = _tracker({
        "XLAB": [
            {"session_id": 10, "state": STATE_WATCHING_LIVE},
            {"session_id": 11, "state": STATE_WATCHING_LIVE, "watch_break_level": 3.90},
            {"session_id": 12, "state": STATE_LIVE_PENDING_ENTRY},
            {"session_id": 13, "state": STATE_LIVE_ENTERED, "stop_px": 3.0, "target_px": 9.0},
        ],
    })
    t._hi[13] = 4.00
    hits = t.crossed("XLAB", _q(mid=4.00, bid=3.99))
    assert 10 not in hits
    assert hits == [11, 12]                       # level cross + pending entry: buhay
    hits = t.crossed("XLAB", _q(mid=4.05, bid=4.04))
    assert 10 not in hits, "patay ang advance wake"
    assert 13 in hits, "ang held bagong-high wake ay hindi apektado"


def test_the_per_second_budget_caps_advance_wakes_globally(monkeypatch):
    """Sa bukas: 5 simbolo, sabay-sabay bagong high, cap=2 ⇒ 2 lang ang gising;
    ang susunod na segundo ay bagong budget. Ang batch ang safety net."""
    monkeypatch.setattr(
        il.settings, "chili_momentum_entry_advance_wake_max_per_second", 2.0,
        raising=False,
    )
    clock = {"t": 1000.0}
    monkeypatch.setattr(il.time, "monotonic", lambda: clock["t"])
    syms = ["A", "B", "C", "D", "E"]
    t = _tracker({s: [{"session_id": i, "state": STATE_WATCHING_LIVE}] for i, s in enumerate(syms)})
    for s in syms:
        t.crossed(s, _q(mid=1.00))                # seed lahat
    hits = [h for s in syms for h in t.crossed(s, _q(mid=1.01))]
    assert len(hits) == 2, hits
    clock["t"] += 1.1
    hits = [h for s in syms for h in t.crossed(s, _q(mid=1.02))]
    assert len(hits) == 2, "bagong segundo, bagong budget"


def test_the_budget_never_touches_held_or_level_cross_wakes(monkeypatch):
    monkeypatch.setattr(
        il.settings, "chili_momentum_entry_advance_wake_max_per_second", 0.5,
        raising=False,
    )
    clock = {"t": 2000.0}
    monkeypatch.setattr(il.time, "monotonic", lambda: clock["t"])
    t = _tracker({
        "P": [{"session_id": 20, "state": STATE_LIVE_ENTRY_CANDIDATE}],
        "Q": [{"session_id": 21, "state": STATE_LIVE_ENTRY_CANDIDATE}],
        "H": [{"session_id": 22, "state": STATE_LIVE_ENTERED, "stop_px": 1.0, "target_px": 9.0}],
        "W": [{"session_id": 23, "state": STATE_WATCHING_LIVE, "watch_break_level": 1.50}],
    })
    t.crossed("P", _q(mid=1.00)); t.crossed("Q", _q(mid=1.00))
    t._hi[22] = 2.00
    assert t.crossed("P", _q(mid=1.01)) == [20]       # kinuha ang buong budget
    assert t.crossed("Q", _q(mid=1.01)) == []         # ubos na
    assert t.crossed("H", _q(mid=2.01, bid=2.00)) == [22], "held: hindi saklaw"
    assert t.crossed("W", _q(mid=1.60)) == [23], "level cross: hindi saklaw"


def test_the_pre_entry_high_carries_into_the_held_state():
    """Iisang running high kada session: pagkatapos ng fill ay hindi na kailangan
    ng bagong seed — ang unang bagong high bilang held ay gumigising agad."""
    t = _tracker({"XLAB": [{"session_id": 30, "state": STATE_WATCHING_LIVE}]})
    t.crossed("XLAB", _q(mid=4.00))
    assert t.crossed("XLAB", _q(mid=4.05)) == [30]
    t._by_symbol = {"XLAB": [{
        "session_id": 30, "state": STATE_LIVE_ENTERED, "stop_px": 3.8, "target_px": 5.0,
    }]}
    assert t.crossed("XLAB", _q(mid=4.06, bid=4.05)) == [30]


def test_unusable_prices_never_seed_or_wake():
    t = _tracker({"XLAB": [{"session_id": 40, "state": STATE_QUEUED_LIVE}]})
    for bad in (None, 0.0, -1.0, "x"):
        assert t.crossed("XLAB", _q(mid=bad)) == []
    assert 40 not in t._hi


def test_ships_on():
    s = Settings()
    assert s.chili_momentum_entry_advance_wake_enabled is True
    assert s.chili_momentum_entry_advance_wake_max_per_second == 6.0

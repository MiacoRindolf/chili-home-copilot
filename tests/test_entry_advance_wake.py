"""Entry-advance wake: ang pre-entry session ay ginigising ng BAGONG HIGH (#1282).

ANG PUWANG (na-map 2026-09-02). Sa deployed batch window (LOOP_ENABLED=false,
10s cadence, sukat na 2.3-8s kada pass, paminsang 30s) ang bawat pre-entry
state ay umuusad LANG sa pulse. May push rail na (pg LISTEN momentum_iqfeed_l1:
449k notify / 1,201 wake sa 7,500s noong 09-01, + WS bus) pero ang
`_SessionCrossTracker.crossed` ay gumigising lamang ng: pending exit (kada
tick), held position (stop/target cross o bagong high), PENDING_ENTRY (kada
tick), at WATCHING sa `watch_break_level` cross. Ang QUEUED_LIVE at
LIVE_ENTRY_CANDIDATE ay hindi magigising ng tick kailanman; ang WATCHING na
walang level (velocity, volume, VWAP na trigger) ay pareho. Ang AUUD 09-01 ay
nag-evaluate kada ~10s (candidate_detected 11:06:51, :07:05, :07:16, :07:26).

ANG LUNAS: ang parehong bagong-high na wake na ginagamit na ng held position
(seed muna, saka bawat bagong high, 2s spacing kada session) — dahil ang
momentum entry ay pumuputok habang UMAAKYAT ang presyo. Ang tracker ay
nagmamarka lang ng HINT; ang budget (token bucket) at ang concurrency cap ay
kinukuha sa `_spawn_session_wake` PAGKATAPOS ng spacing at single-flight —
kung hindi, ang isang mainit na pangalan ay uubos ng budget nang walang isa
mang spawn (unang adversarial review). Dispatch hint lamang; ang FSM ang
nagpapasya.

Runnable: pytest tests/test_entry_advance_wake.py -v
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.config import Settings
from app.services.trading.momentum_neural import captured_paper_dispatcher as cpd
from app.services.trading.momentum_neural import ignition_loop as il
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural.live_fsm import (
    STATE_ARMED_PENDING_RUNNER,
    STATE_LIVE_ENTERED,
    STATE_LIVE_ENTRY_CANDIDATE,
    STATE_LIVE_PENDING_ENTRY,
    STATE_QUEUED_LIVE,
    STATE_WATCHING_LIVE,
)

PRE_ENTRY = (STATE_QUEUED_LIVE, STATE_WATCHING_LIVE, STATE_LIVE_ENTRY_CANDIDATE)


def _tracker(entries_by_symbol: dict):
    t = il._SessionCrossTracker()
    t._by_symbol = dict(entries_by_symbol)
    return t


def _q(mid=None, bid=None):
    return SimpleNamespace(bid=bid, mid=mid, last=None)


class _FakeThread:
    spawned: list[tuple[tuple, dict]] = []

    def __init__(self, target=None, args=(), kwargs=None, name=None, daemon=None):
        self._rec = (args, dict(kwargs or {}))

    def start(self):
        _FakeThread.spawned.append(self._rec)


@pytest.fixture(autouse=True)
def _clean_module_state(monkeypatch):
    """Module-level admission state + malawak na default para sa bawat test."""
    il._advance_hint.clear()
    il._adv_running.clear()
    il._session_wake_last.clear()
    il._wake_inflight.clear()
    il._adv_tokens = -1.0
    il._adv_last_refill = 0.0
    il._adv_last_refresh = 0.0
    _FakeThread.spawned = []
    for name, val in (
        ("chili_momentum_entry_advance_wake_enabled", True),
        ("chili_momentum_entry_advance_wake_max_per_second", 50.0),
        ("chili_momentum_entry_advance_wake_max_inflight", 64),
        ("chili_momentum_session_tick_wake_enabled", True),
        ("chili_momentum_session_tick_wake_min_spacing_s", 2.0),
    ):
        monkeypatch.setattr(il.settings, name, val, raising=False)
    yield
    il._advance_hint.clear()
    il._adv_running.clear()


def _spawn(sid) -> bool:
    with patch.object(il.threading, "Thread", _FakeThread):
        return il._spawn_session_wake(sid)


def _spawned_advance_flags() -> list[bool]:
    return [bool(kw.get("advance")) for _a, kw in _FakeThread.spawned]


# ── 1. ang tracker: seed, bagong high, hint ──────────────────────────────────

@pytest.mark.parametrize("state", PRE_ENTRY)
def test_pre_entry_session_wakes_on_each_new_high_after_the_seed(state):
    """ANG PANGUNAHIN: seed (tahimik), bagong high (gising + hint), pullback/pantay (tahimik)."""
    t = _tracker({"XLAB": [{"session_id": 1, "state": state}]})
    assert t.crossed("XLAB", _q(mid=4.00)) == [], "ang unang tick ay seed lamang"
    assert 1 not in il._advance_hint
    assert t.crossed("XLAB", _q(mid=4.01)) == [1]
    assert 1 in il._advance_hint, "ang hit ay may kasamang advance hint"
    assert t.crossed("XLAB", _q(mid=3.95)) == [], "pullback: walang wake"
    assert t.crossed("XLAB", _q(mid=4.01)) == [], "pantay sa high: hindi bagong high"
    assert t.crossed("XLAB", _q(mid=4.02)) == [1]


def test_bid_stands_in_when_there_is_no_mid():
    t = _tracker({"XLAB": [{"session_id": 3, "state": STATE_LIVE_ENTRY_CANDIDATE}]})
    assert t.crossed("XLAB", _q(bid=4.00)) == []
    assert t.crossed("XLAB", _q(bid=4.03)) == [3]


def test_armed_pending_runner_is_not_in_the_advance_set():
    """Ginigising na ito sa sandali ng arm; ang tick-wake nito ay magbabayad ng
    buong quote gate para sa isang one-line na state bump."""
    assert STATE_ARMED_PENDING_RUNNER not in il._SESSION_PRE_ENTRY_STATES
    t = _tracker({"XLAB": [{"session_id": 9, "state": STATE_ARMED_PENDING_RUNNER}]})
    assert t.crossed("XLAB", _q(mid=4.00)) == []
    assert t.crossed("XLAB", _q(mid=4.50)) == []
    assert 9 not in t._hi


def test_level_cross_still_wakes_watching_on_a_reclaim_below_the_high():
    """Ang reclaim ng level pagkatapos ng pullback ay HINDI bagong running high —
    ang umiiral na level-cross wake ang humuhuli nito (walang advance hint)."""
    t = _tracker({"XLAB": [{
        "session_id": 2, "state": STATE_WATCHING_LIVE, "watch_break_level": 3.90,
    }]})
    assert t.crossed("XLAB", _q(mid=3.80)) == []      # seed, sa ilalim ng level
    assert t.crossed("XLAB", _q(mid=3.95)) == [2]     # bagong high AT level cross
    assert t._hi[2] == 3.95
    assert 2 not in il._advance_hint, "level cross ang gumising, hindi advance"
    assert t.crossed("XLAB", _q(mid=3.70)) == []
    assert t.crossed("XLAB", _q(mid=3.91)) == [2]     # reclaim: level cross lamang
    assert t._hi[2] == 3.95, "hindi bumababa ang running high"
    assert t.crossed("XLAB", _q(mid=3.85)) == [], "sa ilalim ng level AT ng high"


def test_the_running_high_ratchets_even_when_the_level_cross_wakes():
    """Kung hindi, ang held branch pagkatapos ng fill ay magmamana ng lumang seed
    at gigising sa bawat uptick sa ILALIM ng entry (pangalawang review, #3)."""
    t = _tracker({"XLAB": [{
        "session_id": 4, "state": STATE_WATCHING_LIVE, "watch_break_level": 3.90,
    }]})
    t.crossed("XLAB", _q(mid=3.80))
    assert t.crossed("XLAB", _q(mid=4.00)) == [4] and t._hi[4] == 4.00
    assert t.crossed("XLAB", _q(mid=4.10)) == [4] and t._hi[4] == 4.10
    t._by_symbol = {"XLAB": [{
        "session_id": 4, "state": STATE_LIVE_ENTERED, "stop_px": 3.8, "target_px": 6.0,
    }]}
    assert t.crossed("XLAB", _q(mid=4.05, bid=4.04)) == [], "sa ilalim ng tunay na high"
    assert t.crossed("XLAB", _q(mid=4.11, bid=4.10)) == [4]


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
    assert hits == [11, 12]                       # level cross + pending entry: buhay
    hits = t.crossed("XLAB", _q(mid=4.05, bid=4.04))
    assert 10 not in hits, "patay ang advance wake"
    assert 13 in hits, "ang held bagong-high wake ay hindi apektado"
    assert t._hi[10] == 4.05, "nagra-ratchet pa rin ang high kahit patay ang wake"


def test_the_pre_entry_high_carries_into_the_held_state():
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


# ── 2. admission sa spawn: budget PAGKATAPOS ng spacing/single-flight ────────

def _hint(*sids):
    for s in sids:
        il._mark_advance_hint(int(s), il.time.monotonic())


def test_an_advance_wake_spawns_tagged_and_takes_one_token(monkeypatch):
    monkeypatch.setattr(
        il.settings, "chili_momentum_entry_advance_wake_max_per_second", 2.0, raising=False,
    )
    _hint(1)
    assert _spawn(1) is True
    assert _spawned_advance_flags() == [True]
    assert il._adv_tokens == pytest.approx(1.0)
    assert 1 in il._adv_running and 1 in il._wake_inflight


def test_a_spacing_rejection_does_not_burn_a_token(monkeypatch):
    """Review #1: ang mainit na pangalan (10 uptick/s) ay hindi dapat umubos ng
    budget nang walang spawn."""
    monkeypatch.setattr(
        il.settings, "chili_momentum_entry_advance_wake_max_per_second", 2.0, raising=False,
    )
    _hint(1); assert _spawn(1) is True
    tokens_after_first = il._adv_tokens
    for _ in range(10):                            # 10 bagong high sa loob ng 2s
        _hint(1)
        assert _spawn(1) is False, "spacing ang tumanggi"
    assert il._adv_tokens == pytest.approx(tokens_after_first), "walang token na nasunog"
    assert 1 not in il._advance_hint, "kinonsumo ang hint sa bawat pagtatangka"
    _hint(2); assert _spawn(2) is True, "may natitira pa para sa ibang session"


def test_the_token_bucket_refills_at_the_configured_rate(monkeypatch):
    monkeypatch.setattr(
        il.settings, "chili_momentum_entry_advance_wake_max_per_second", 2.0, raising=False,
    )
    clock = {"t": 1000.0}
    monkeypatch.setattr(il.time, "monotonic", lambda: clock["t"])
    for sid in (1, 2, 3, 4, 5):
        _hint(sid)
    spawned = [_spawn(sid) for sid in (1, 2, 3, 4, 5)]
    assert spawned == [True, True, False, False, False], "burst = 2"
    for sid in (3, 4, 5):
        assert sid not in il._wake_inflight, "binawi ang inflight ng tinanggihan"
        assert sid not in il._session_wake_last, "binawi ang spacing stamp"
    clock["t"] += 1.0                              # +2 token
    for sid in (3, 4, 5):
        _hint(sid)
    assert [_spawn(sid) for sid in (3, 4, 5)] == [True, True, False]


def test_the_concurrency_cap_bounds_running_advance_wakes(monkeypatch):
    """Review #2b: ang rate cap ay hindi humahangga sa concurrency."""
    monkeypatch.setattr(
        il.settings, "chili_momentum_entry_advance_wake_max_inflight", 1, raising=False,
    )
    _hint(1); assert _spawn(1) is True
    _hint(2); assert _spawn(2) is False
    assert il._adv_stats["rejected_inflight"] >= 1
    il._advance_done(1)
    _hint(2); assert _spawn(2) is True


def test_non_advance_wakes_never_touch_the_budget(monkeypatch):
    """Held/level-cross/pending wakes: walang hint ⇒ walang token, walang cap."""
    monkeypatch.setattr(
        il.settings, "chili_momentum_entry_advance_wake_max_per_second", 1.0, raising=False,
    )
    monkeypatch.setattr(
        il.settings, "chili_momentum_entry_advance_wake_max_inflight", 1, raising=False,
    )
    _hint(1); assert _spawn(1) is True             # ubos na: 0 token, 1/1 slot
    assert _spawn(7) is True, "walang hint = hindi saklaw ng budget"
    assert _spawn(8) is True
    assert _spawned_advance_flags() == [True, False, False]


def test_a_stale_hint_is_not_an_advance_wake(monkeypatch):
    clock = {"t": 5000.0}
    monkeypatch.setattr(il.time, "monotonic", lambda: clock["t"])
    il._mark_advance_hint(1, clock["t"])
    clock["t"] += il._ADVANCE_HINT_TTL_S + 0.1
    assert _spawn(1) is True
    assert _spawned_advance_flags() == [False]


# ── 3. ang wake tick mismo ───────────────────────────────────────────────────

def test_post_wake_refresh_is_debounced_for_advance_wakes_only(monkeypatch):
    """Review #2a: 6 wake/s × buong inventory reload = aksaya; isa kada ~1s."""
    clock = {"t": 9000.0}
    monkeypatch.setattr(il.time, "monotonic", lambda: clock["t"])
    refreshes: list = []
    monkeypatch.setattr(il, "_post_wake_session_refresh", lambda: refreshes.append(1))
    with patch.object(cpd, "run_live_runner_tick_two_phase", return_value=True), \
         patch.object(lr, "consume_entry_fsm_continuation", return_value=False):
        il._adv_running.add(1)
        il._wake_runner_tick(1, advance=True)
        assert 1 not in il._adv_running, "binitawan ang slot"
        il._wake_runner_tick(2, advance=True)
        il._wake_runner_tick(3, advance=True)
        assert len(refreshes) == 1, "isang refresh lang sa loob ng 1s"
        il._wake_runner_tick(4)                    # hindi advance: hindi ginalaw
        assert len(refreshes) == 2
        clock["t"] += 1.1
        il._wake_runner_tick(5, advance=True)
        assert len(refreshes) == 3


def test_ships_on():
    s = Settings()
    assert s.chili_momentum_entry_advance_wake_enabled is True
    assert s.chili_momentum_entry_advance_wake_max_per_second == 6.0
    assert s.chili_momentum_entry_advance_wake_max_inflight == 8

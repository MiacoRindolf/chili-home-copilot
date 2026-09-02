"""Bailout wake: ang paglipat sa BAILOUT ay gumigising AGAD ng exit pulse (#1281).

NASUKAT, AUUD 2026-09-01 (session 19337, mula sa trading_automation_events):

    11:11:31.687        live_bailout  max_loss_circuit floor=1.0863 unrl=-27.55
    11:11:35.794 +4.1s  unang exit pricing         <-- naghintay ng pulse
    11:11:35.885        deadman phase-1 freeze
    11:11:37.853 +2.0s  pangalawang pricing        <-- exit continuation (#1111)
    11:11:46.067 +8.2s  live_exit_filled 1.030127 = -44.01 (5.17% sa ilalim ng floor)

Ang max-loss circuit (at ang 12 pang bailout site) ay lumilipat sa BAILOUT at
BUMABALIK mula sa tick; ang mismong submit ay nasa `st == STATE_LIVE_BAILOUT`
na sangay ng SUSUNOD na invocation. Sa batch mode iyon ay 10-30s ang layo. Ang
tick-cross tracker ay gumigising lang sa bagong stop cross o bagong high --
hindi dahil pumutok ang circuit -- kaya ang +4.1s ng AUUD ay swerte pa.

Ito ang eksaktong kapatid ng exit continuation: WALANG bagong awtoridad. Ang
na-dispatch na tick ay muling nagbabasa ng sariwang quote, dumadaan sa halt
gate ng BAILOUT handler, at ang FSM pa rin ang nagpapasya.

Runnable: pytest tests/test_bailout_wake.py -v
"""
from __future__ import annotations

import ast
import inspect
from types import SimpleNamespace

from app.config import Settings
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural.live_fsm import (
    STATE_LIVE_BAILOUT,
    STATE_LIVE_ENTERED,
)


def _bailout_transition_call_sites() -> tuple[set[str], int]:
    """(mga function na tumatawag ng _safe_transition(..., STATE_LIVE_BAILOUT),
    bilang ng tawag sa _transition_to_bailout) -- AST, hindi regex."""
    tree = ast.parse(inspect.getsource(lr))
    direct_in: set[str] = set()
    helper_calls = 0

    def _mentions_bailout(call: ast.Call) -> bool:
        vals = list(call.args) + [kw.value for kw in call.keywords]
        return any(isinstance(v, ast.Name) and v.id == "STATE_LIVE_BAILOUT" for v in vals)

    def _walk(node: ast.AST, fn: str) -> None:
        nonlocal helper_calls
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _walk(child, child.name)
                continue
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                if child.func.id == "_safe_transition" and _mentions_bailout(child):
                    direct_in.add(fn)
                elif child.func.id == "_transition_to_bailout":
                    helper_calls += 1
            _walk(child, fn)

    _walk(tree, "<module>")
    return direct_in, helper_calls


def test_every_bailout_transition_goes_through_the_waking_helper():
    """AST source guard na may POSITIBONG assertion sa magkabilang panig."""
    direct_in, helper_calls = _bailout_transition_call_sites()
    assert direct_in == {"_transition_to_bailout"}, (
        f"may direktang BAILOUT transition sa labas ng helper: {sorted(direct_in)}"
    )
    assert helper_calls >= 10, f"inaasahan ang 13 site, nakita {helper_calls}"


def test_helper_transitions_first_then_wakes(monkeypatch):
    """Transition MUNA (nakatala na ang state), saka ang gising."""
    calls: list = []
    monkeypatch.setattr(
        lr, "_safe_transition",
        lambda db, sess, new_state: calls.append(("transition", new_state)),
    )
    monkeypatch.setattr(
        lr, "_schedule_dispatch_wake",
        lambda sid, *, delay_s, name, enabled: (
            calls.append(("wake", sid, delay_s, name, enabled)) or True
        ),
    )
    sess = SimpleNamespace(id=19337, state=STATE_LIVE_ENTERED)
    lr._transition_to_bailout(object(), sess)
    assert calls == [
        ("transition", STATE_LIVE_BAILOUT),
        ("wake", 19337, lr._BAILOUT_WAKE_DELAY_S, "bailout-wake", True),
    ]


def test_a_wake_failure_never_blocks_the_transition(monkeypatch):
    """Ang gising ay hint lamang; ang BAILOUT ay dapat maitala kahit sumabog ito."""
    calls: list = []
    monkeypatch.setattr(
        lr, "_safe_transition",
        lambda db, sess, new_state: calls.append(new_state),
    )

    def _boom(*_a, **_k):
        raise RuntimeError("timer thread refused")

    monkeypatch.setattr(lr, "_schedule_dispatch_wake", _boom)
    lr._transition_to_bailout(object(), SimpleNamespace(id=7, state=STATE_LIVE_ENTERED))
    assert calls == [STATE_LIVE_BAILOUT]


def test_the_kill_switch_is_its_own(monkeypatch):
    """Ang pagpatay sa bailout wake ay hindi humahawak sa ibang waker at kabaligtaran."""
    seen: list = []
    monkeypatch.setattr(lr, "_safe_transition", lambda db, sess, new_state: None)
    monkeypatch.setattr(
        lr, "_schedule_dispatch_wake",
        lambda sid, *, delay_s, name, enabled: seen.append(enabled) or enabled,
    )
    monkeypatch.setattr(
        lr, "settings",
        SimpleNamespace(
            chili_momentum_bailout_wake_enabled=False,
            chili_momentum_exit_continuation_wake_enabled=True,
        ),
    )
    lr._transition_to_bailout(object(), SimpleNamespace(id=1, state=STATE_LIVE_ENTERED))
    assert seen == [False]
    assert lr._schedule_exit_continuation(1) is True, "hindi apektado ang kapatid"


def test_the_delay_is_the_measured_continuation_precedent():
    """0.5s: ang parehong delay na nasukat na gumagana sa exit continuation
    (AUUD: freeze -> pangalawang pricing 2.0s kasama ang tick)."""
    assert lr._BAILOUT_WAKE_DELAY_S == lr._EXIT_CONTINUATION_WAKE_DELAY_S == 0.5


def test_no_timer_is_armed_under_pytest():
    """Walang wall-clock timer sa test/replay -- determinism ng replay."""
    assert lr._schedule_bailout_wake(19337) is False
    assert 19337 not in lr._stop_confirm_wake_inflight


def test_ships_on():
    assert Settings().chili_momentum_bailout_wake_enabled is True

"""Batch-mode IQFeed pg-LISTEN wake consumer (2026-08-23).

Ang tick-cross wake (#1109) ay sumasakay sa Massive price bus: walang bus tick
= walang wake, at ang crossing ay naghihintay ng 10-30s batch. Sa manipis na
premarket na tape — ang pinaka-kumikitang sesyon ng lane — madalas ang IQFeed
bridge lang ang may tick. Ito ang batch-mode consumer ng channel na iyon.

Runnable: pytest tests/test_iqfeed_wake_listener.py -v
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import patch

from app.services.trading.momentum_neural import ignition_loop as il
from app.services.trading.momentum_neural import iqfeed_wake_listener as iwl
from app.services.trading.momentum_neural.live_fsm import (
    STATE_LIVE_ENTERED,
    STATE_WATCHING_LIVE,
)


# ── duck quote: null-safe, hindi gumagawa ng presyo ────────────────────────

def test_duck_quote_mid_from_both_sides():
    q = iwl._duck_quote({"bid": 10.0, "ask": 10.2})
    assert q.bid == 10.0 and q.ask == 10.2
    assert abs(q.mid - 10.1) < 1e-9


def test_duck_quote_bid_only_has_no_fabricated_mid():
    q = iwl._duck_quote({"bid": 10.0, "ask": None})
    assert q.bid == 10.0 and q.mid is None


def test_duck_quote_ask_only_yields_no_usable_reference():
    """Ang ask-only ay hindi dapat maging pekeng mid — mag-i-trigger iyon ng
    maling watch-break wake."""
    q = iwl._duck_quote({"bid": None, "ask": 10.2})
    assert q.mid is None and q.bid is None


def test_duck_quote_rejects_empty_and_bad_values():
    assert iwl._duck_quote({"bid": None, "ask": None}) is None
    assert iwl._duck_quote({"bid": 0, "ask": 0}) is None
    assert iwl._duck_quote({"bid": "junk", "ask": "junk"}) is None


def test_duck_quote_is_readable_by_the_cross_tracker():
    """Ang parehong crossed() ng bus path ang gagamitin — dapat magkasya."""
    t = il._SessionCrossTracker()
    t._by_symbol = {"SDOT": [
        {"session_id": 5, "state": STATE_LIVE_ENTERED, "stop_px": 16.76},
    ]}
    q = iwl._duck_quote({"symbol": "SDOT", "bid": 16.70, "ask": 16.80})
    assert t.crossed("SDOT", q) == [5]


# ── channel validation ─────────────────────────────────────────────────────

def test_channel_refused_when_not_identifier_shaped(monkeypatch):
    from app.config import settings as real_settings

    monkeypatch.setattr(
        real_settings,
        "chili_momentum_live_runner_loop_iqfeed_notify_channel",
        "bad; DROP TABLE x",
        raising=False,
    )
    assert iwl.IqfeedWakeListener()._channel() is None


def test_channel_accepted_for_default(monkeypatch):
    from app.config import settings as real_settings

    monkeypatch.setattr(
        real_settings,
        "chili_momentum_live_runner_loop_iqfeed_notify_channel",
        "momentum_iqfeed_l1",
        raising=False,
    )
    assert iwl.IqfeedWakeListener()._channel() == "momentum_iqfeed_l1"


def test_listen_statement_is_validated_before_interpolation():
    src = inspect.getsource(iwl.IqfeedWakeListener)
    assert "_CHANNEL_RE.fullmatch" in src
    # ang channel ay galing LANG sa _channel(), na nagva-validate
    assert 'cur.execute(f"LISTEN {channel};")' in src
    assert "channel = self._channel()" in inspect.getsource(
        iwl.IqfeedWakeListener.start
    )


# ── notify handling ────────────────────────────────────────────────────────

def _note(payload: str):
    return SimpleNamespace(payload=payload)


def test_handle_batch_wakes_crossing_session():
    listener = iwl.IqfeedWakeListener()
    tracker = il._SessionCrossTracker()
    tracker._by_symbol = {"SDOT": [
        {"session_id": 9, "state": STATE_LIVE_ENTERED, "stop_px": 16.76},
    ]}
    woken: list[int] = []
    with patch.object(il, "get_ignition_loop",
                      return_value=SimpleNamespace(_sessions=tracker)), \
         patch.object(il, "_spawn_session_wake",
                      side_effect=lambda sid: woken.append(sid) or True):
        listener._handle_batch([
            _note('{"symbol":"SDOT","bid":16.70,"ask":16.80}')
        ])
    assert woken == [9]
    assert listener.health()["wakes"] == 1


def test_handle_batch_ignores_untracked_symbol():
    listener = iwl.IqfeedWakeListener()
    tracker = il._SessionCrossTracker()
    woken: list[int] = []
    with patch.object(il, "get_ignition_loop",
                      return_value=SimpleNamespace(_sessions=tracker)), \
         patch.object(il, "_spawn_session_wake",
                      side_effect=lambda sid: woken.append(sid) or True):
        listener._handle_batch([
            _note('{"symbol":"ZZZZ","bid":1.0,"ask":1.1}')
        ])
    assert woken == []


def test_handle_batch_coalesces_storm_to_newest_per_symbol():
    """Isang tape storm ay hindi dapat magpatakbo ng crossed() kada print."""
    listener = iwl.IqfeedWakeListener()
    seen: list[float] = []

    class _Tracker:
        def crossed(self, symbol, quote):
            seen.append(quote.bid)
            return []

    with patch.object(il, "get_ignition_loop",
                      return_value=SimpleNamespace(_sessions=_Tracker())), \
         patch.object(il, "_spawn_session_wake", return_value=True):
        listener._handle_batch([
            _note('{"symbol":"HUIZ","bid":9.0,"ask":9.1}'),
            _note('{"symbol":"HUIZ","bid":9.2,"ask":9.3}'),
            _note('{"symbol":"HUIZ","bid":9.4,"ask":9.5}'),
        ])
    assert seen == [9.4]  # ang PINAKABAGO lang


def test_handle_batch_survives_malformed_payloads():
    listener = iwl.IqfeedWakeListener()
    tracker = il._SessionCrossTracker()
    tracker._by_symbol = {"OK": [
        {"session_id": 3, "state": STATE_WATCHING_LIVE, "watch_break_level": 1.0},
    ]}
    woken: list[int] = []
    with patch.object(il, "get_ignition_loop",
                      return_value=SimpleNamespace(_sessions=tracker)), \
         patch.object(il, "_spawn_session_wake",
                      side_effect=lambda sid: woken.append(sid) or True):
        listener._handle_batch([
            _note("not json at all"),
            _note("[1,2,3]"),
            _note('{"no_symbol":true}'),
            _note('{"symbol":"","bid":1.0}'),
            _note('{"symbol":"OK","bid":1.4,"ask":1.6}'),
        ])
    assert woken == [3]


def test_handle_batch_noop_when_ignition_loop_unavailable():
    listener = iwl.IqfeedWakeListener()
    with patch.object(il, "get_ignition_loop", side_effect=RuntimeError("no loop")):
        listener._handle_batch([_note('{"symbol":"X","bid":1.0,"ask":1.1}')])
    assert listener.health()["wakes"] == 0


# ── lifecycle ──────────────────────────────────────────────────────────────

def test_start_is_noop_when_disabled(monkeypatch):
    from app.config import settings as real_settings

    monkeypatch.setattr(
        real_settings, "chili_momentum_iqfeed_wake_listener_enabled", False,
        raising=False,
    )
    listener = iwl.IqfeedWakeListener()
    with patch.object(iwl.threading, "Thread") as mock_thread:
        listener.start()
        mock_thread.assert_not_called()
    assert listener.health()["running"] is False


def test_reconnect_backoff_is_bounded_and_increasing():
    assert iwl._RECONNECT_BACKOFF_S == tuple(sorted(iwl._RECONNECT_BACKOFF_S))
    assert iwl._RECONNECT_BACKOFF_S[0] >= 1.0
    assert iwl._RECONNECT_BACKOFF_S[-1] <= 60.0


def test_scheduler_starts_listener_in_batch_branch():
    from app.services import trading_scheduler as ts

    src = inspect.getsource(ts)
    lo = src.index("start_iqfeed_wake_listener")
    seg = src[max(0, lo - 1500):lo]
    # dapat nasa parehong batch branch kung saan sinisimulan ang ignition scorer
    assert "start_ignition_loop" in seg

"""Per-session live-runner isolation — ONE lane crashing (e.g. a first-day Alpaca tick error)
must NOT block the OTHER lanes (RH equities, Coinbase crypto) and must not stop the process.
`_dispatch_live_runner_ticks` contains a stray raise PER SESSION (both the serial and the
threadpool paths), and `_tick_one` already swallows its own errors on its own DB session. This
guards the operator's requirement (2026-06-09): an Alpaca error tomorrow keeps everything else
running. (project_momentum_lane)"""

from __future__ import annotations

from app.services.trading_scheduler import _dispatch_live_runner_ticks


def test_crashing_session_does_not_block_others_parallel():
    seen = []

    def tick_one(sid):
        seen.append(sid)
        if sid == 99:
            raise RuntimeError("simulated Alpaca tick crash")  # one lane explodes
        return (True, 7)  # the others are healthy

    ticked, timings = _dispatch_live_runner_ticks([99, 100, 101], workers=3, tick_one=tick_one)
    assert ticked == 2, ticked                     # the two healthy sessions still ticked
    assert timings[99] == 0                         # the crash was CONTAINED (False, 0)
    assert timings[100] == 7 and timings[101] == 7  # healthy sessions reported normally
    assert sorted(seen) == [99, 100, 101]           # every session was attempted


def test_crashing_session_does_not_block_others_serial():
    def tick_one(sid):
        if sid == 99:
            raise RuntimeError("crash")
        return (True, 5)

    ticked, timings = _dispatch_live_runner_ticks([99, 100], workers=1, tick_one=tick_one)
    assert ticked == 1
    assert timings[99] == 0 and timings[100] == 5


def test_all_healthy_all_ticked():
    ticked, timings = _dispatch_live_runner_ticks([1, 2, 3], workers=2, tick_one=lambda sid: (True, 3))
    assert ticked == 3
    assert all(v == 3 for v in timings.values())

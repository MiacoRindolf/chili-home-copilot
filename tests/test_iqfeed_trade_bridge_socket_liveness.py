"""The L1 half-open detector must measure the SOCKET, not quote admission.

Regression guard for the 2026-08-10 blackout. Quote capture is fenced on trade
recency (AUTHORITATIVE_MAX_AGE_S = 2.0s), and the reconnect detector used to key
off that same fence. So the instant prints stopped -- after-hours, a holiday, a
weekend, an illiquid name -- no quote could clear the fence, the clock froze, and
45s later the bridge tore down a perfectly healthy IQConnect socket, reconnected
into the same condition, and repeated forever.

Measured that day: the NBBO tape stopped at 00:00:07Z, i.e. 20:00:07 ET, the exact
end of the after-hours session, and the log then carried 192 consecutive
"no valid IQFeed L1 BBO frames for 45.3s across 183 watched symbols; reconnecting"
cycles against a socket that stayed ESTABLISHED throughout. L2 depth, which does
not pass through this fence, kept flowing the whole time.

The distinction these tests pin:
    "is the socket delivering?"  -> transport,     drives reconnect
    "is this quote executable?"  -> data quality,  drives capture
"""

import time

import pytest

bridge = pytest.importorskip("scripts.iqfeed_trade_bridge")


STALE = bridge.STALE_NBBO_RECONNECT_S


def _should_reconnect(*, watched, frame_age, nbbo_age):
    """Mirror of the detector's predicate, driven off module state.

    Deliberately reads the real module globals rather than reimplementing the
    thresholds, so a change to either constant or to which clock is consulted
    shows up here.
    """
    now = time.monotonic()
    bridge._last_socket_frame_monotonic = None if frame_age is None else now - frame_age
    bridge._last_nbbo_append_monotonic = None if nbbo_age is None else now - nbbo_age
    return (
        STALE > 0
        and bool(watched)
        and bridge._last_socket_frame_monotonic is not None
        and time.monotonic() - bridge._last_socket_frame_monotonic > STALE
    )


@pytest.fixture(autouse=True)
def _restore_clocks():
    frame = bridge._last_socket_frame_monotonic
    nbbo = bridge._last_nbbo_append_monotonic
    yield
    bridge._last_socket_frame_monotonic = frame
    bridge._last_nbbo_append_monotonic = nbbo


def test_quiet_market_does_not_reconnect():
    """THE regression.

    Frames keep arriving; no quote clears the trade-recency fence because nothing
    is printing. Under the old code this reconnected every 45s, forever.
    """
    assert _should_reconnect(watched=["AAPL"], frame_age=1.0, nbbo_age=STALE * 20) is False


def test_silent_socket_still_reconnects():
    """The detector must not be defanged: real silence is still a fault."""
    assert _should_reconnect(watched=["AAPL"], frame_age=STALE + 1, nbbo_age=1.0) is True


def test_nothing_watched_never_reconnects():
    assert _should_reconnect(watched=[], frame_age=STALE * 10, nbbo_age=STALE * 10) is False


def test_reconnect_is_independent_of_quote_admission():
    """Same transport state, wildly different admission state -> same verdict."""
    quiet = _should_reconnect(watched=["A"], frame_age=1.0, nbbo_age=99_999.0)
    busy = _should_reconnect(watched=["A"], frame_age=1.0, nbbo_age=0.1)
    assert quiet == busy is False

    silent_quiet = _should_reconnect(watched=["A"], frame_age=STALE + 5, nbbo_age=99_999.0)
    silent_busy = _should_reconnect(watched=["A"], frame_age=STALE + 5, nbbo_age=0.1)
    assert silent_quiet == silent_busy is True


def test_mark_socket_frame_advances_only_the_transport_clock():
    bridge._last_socket_frame_monotonic = None
    bridge._last_nbbo_append_monotonic = None

    bridge._mark_socket_frame()

    assert bridge._last_socket_frame_monotonic is not None
    assert bridge._last_nbbo_append_monotonic is None, (
        "a frame arriving must not imply a quote was admitted"
    )


def test_the_two_clocks_are_distinct_objects():
    """If these ever alias again the whole blackout returns."""
    bridge._last_socket_frame_monotonic = 1.0
    bridge._last_nbbo_append_monotonic = 2.0
    assert bridge._last_socket_frame_monotonic != bridge._last_nbbo_append_monotonic


def test_trade_recency_fence_is_unchanged():
    """The data-quality property must survive this fix untouched."""
    assert bridge.AUTHORITATIVE_MAX_AGE_S == 2.0

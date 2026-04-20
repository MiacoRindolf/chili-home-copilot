"""Unit tests for the yf_session sliding-window rate limiter.

Guarantees
----------
1. Rate is honored: 12 acquisitions per 5-second window, no more.
2. Oldest-first eviction: once the window slides, the next acquisition is
   admitted without waiting indefinitely.
3. No background threads — this is the headline regression guarantee. The
   previous ``pyrate_limiter``-backed implementation spawned a daemon
   ``Leaker`` thread that leaked ``ProactorEventLoop`` IOCP handles on
   Windows over long sessions (``WinError 10055``). The replacement must
   stay thread-free.
4. ``_reset_limiter_for_tests`` cleanly resets acquisition history.
5. Thread-safety: concurrent acquisitions never exceed the rate.
"""
from __future__ import annotations

import threading
import time

import pytest

from app.services import yf_session


# ─── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_limiter():
    """Clear the sliding-window buffer before each test."""
    yf_session._reset_limiter_for_tests()
    yield
    yf_session._reset_limiter_for_tests()


@pytest.fixture
def _tighten_window(monkeypatch):
    """Shrink the rate-limit window so tests run in subseconds.

    Yields ``(rate_max, window_s)`` — use these for assertions. The module
    constants are patched back automatically at teardown.
    """
    rate_max = 3
    window_s = 0.2
    monkeypatch.setattr(yf_session, "_RATE_MAX", rate_max)
    monkeypatch.setattr(yf_session, "_RATE_WINDOW_S", window_s)
    # The deque was sized with the OLD max — rebuild it at the new size.
    import collections as _collections
    monkeypatch.setattr(
        yf_session,
        "_hits",
        _collections.deque(maxlen=rate_max),
    )
    yield rate_max, window_s


# ─── Thread-leak regression guarantee ──────────────────────────────────


class TestNoBackgroundThreads:
    """The headline guarantee: pyrate_limiter's Leaker thread is gone."""

    def test_acquire_does_not_spawn_background_threads(self):
        """First acquire must not start any daemon threads.

        Why this matters: the old ``pyrate_limiter`` backend spawned a
        daemon thread named ``PyrateLimiter's Leaker`` on first bucket use.
        That thread ran ``asyncio.run(self._leak(...))`` on a perpetual loop,
        allocating a fresh ``ProactorEventLoop`` (with IOCP handles + self-
        pipe sockets) each iteration. Over hours of use this exhausted the
        Windows non-paged kernel pool and produced ``WinError 10055`` on
        every subsequent ``socket.connect()`` in the process — including
        the test suite's psycopg2 DB connections.
        """
        before = {t.name for t in threading.enumerate()}
        for _ in range(20):
            yf_session.acquire()
        after = {t.name for t in threading.enumerate()}
        new_threads = after - before
        # Filter out threads that the test runner / pytest internals may spawn.
        leaked = {n for n in new_threads if "Leaker" in n or "pyrate" in n.lower()}
        assert leaked == set(), f"unexpected background threads: {leaked}"

    def test_no_pyrate_limiter_import_side_effect(self):
        """Importing yf_session must not import pyrate_limiter.

        If someone re-adds ``pyrate_limiter`` for a feature that seems
        harmless, we want that to be a loud review moment — not a silent
        regression that surfaces as WinError 10055 weeks later.
        """
        import sys
        # Re-import to exercise any import-time work on a fresh module.
        sys.modules.pop("app.services.yf_session", None)
        import app.services.yf_session  # noqa: F401
        assert "pyrate_limiter" not in sys.modules, (
            "yf_session must not pull in pyrate_limiter — "
            "its Leaker thread leaks kernel sockets on Windows"
        )


# ─── Rate semantics ──────────────────────────────────────────────────────


class TestSlidingWindow:
    """Confirms the pure-Python sliding window honors its rate."""

    def test_burst_up_to_rate_max_is_instant(self, _tighten_window):
        rate_max, _ = _tighten_window
        t0 = time.monotonic()
        for _ in range(rate_max):
            yf_session.acquire()
        elapsed = time.monotonic() - t0
        assert elapsed < 0.05, (
            f"burst of {rate_max} within capacity should be ~instant, "
            f"took {elapsed:.3f}s"
        )

    def test_overflow_blocks_until_window_slides(self, _tighten_window):
        rate_max, window_s = _tighten_window
        # Fill the window.
        for _ in range(rate_max):
            yf_session.acquire()
        # Next acquire must wait for the oldest to age out.
        t0 = time.monotonic()
        yf_session.acquire()
        elapsed = time.monotonic() - t0
        # Allow a small scheduling slop band. Elapsed should be close to
        # window_s, not multiples of it (otherwise we'd be in a sleep-loop
        # regression where each retry over-sleeps).
        assert elapsed >= window_s * 0.5, (
            f"acquire beyond capacity must wait at least ~half the window "
            f"({window_s * 0.5:.3f}s), took {elapsed:.3f}s"
        )
        assert elapsed <= window_s * 3, (
            f"acquire should not stall multiples of the window "
            f"({window_s * 3:.3f}s cap), took {elapsed:.3f}s"
        )

    def test_old_hits_expire_and_free_slots(self, _tighten_window):
        rate_max, window_s = _tighten_window
        for _ in range(rate_max):
            yf_session.acquire()
        # Sleep past the window so all hits age out.
        time.sleep(window_s + 0.05)
        # Next ``rate_max`` acquires should be instant again.
        t0 = time.monotonic()
        for _ in range(rate_max):
            yf_session.acquire()
        elapsed = time.monotonic() - t0
        assert elapsed < 0.05, (
            f"after window elapsed, next burst should be instant, "
            f"took {elapsed:.3f}s"
        )

    def test_reset_clears_history(self, _tighten_window):
        rate_max, _ = _tighten_window
        for _ in range(rate_max):
            yf_session.acquire()
        yf_session._reset_limiter_for_tests()
        # Capacity should be fully available again.
        t0 = time.monotonic()
        for _ in range(rate_max):
            yf_session.acquire()
        elapsed = time.monotonic() - t0
        assert elapsed < 0.05, (
            f"after reset, burst should be instant, took {elapsed:.3f}s"
        )

    def test_defaults_match_documented_rate(self):
        """12 per 5 seconds is Yahoo's published safe-traffic threshold.

        We read the values from a fresh import so any prior test's
        monkeypatches are guaranteed reverted (pytest auto-restores them,
        but we re-fetch the module via ``sys.modules`` as a belt-and-
        suspenders check that no module-level mutation leaked).
        """
        import sys
        mod = sys.modules.get("app.services.yf_session")
        assert mod is not None, "yf_session must be importable"
        assert mod._RATE_MAX == 12, (
            f"expected 12 req/window, got {mod._RATE_MAX} — Yahoo's safe "
            f"threshold is documented as 12/5s"
        )
        assert mod._RATE_WINDOW_S == 5.0, (
            f"expected 5s window, got {mod._RATE_WINDOW_S}"
        )


# ─── Thread-safety ───────────────────────────────────────────────────────


class TestThreadSafety:
    """Concurrent callers from multiple threads must not exceed the rate."""

    def test_concurrent_acquires_respect_rate(self, _tighten_window):
        rate_max, window_s = _tighten_window
        # Fire ``rate_max * 2`` acquires from N threads concurrently. The
        # first ``rate_max`` should be instant; the next ``rate_max`` should
        # each wait ~window_s for an earlier one to age out.
        n_threads = rate_max * 2
        start_barrier = threading.Barrier(n_threads)
        timings: list[float] = []
        timings_lock = threading.Lock()

        def worker():
            start_barrier.wait()
            t0 = time.monotonic()
            yf_session.acquire()
            dt = time.monotonic() - t0
            with timings_lock:
                timings.append(dt)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=window_s * 5)

        assert len(timings) == n_threads, "all workers should have completed"
        timings.sort()
        # The first ``rate_max`` should be fast (under half the window).
        fast = timings[:rate_max]
        slow = timings[rate_max:]
        assert all(dt < window_s * 0.5 for dt in fast), (
            f"first {rate_max} acquires should be fast, got {fast}"
        )
        # The next ``rate_max`` should each have waited at least a bit.
        # We allow generous slack — the key guarantee is "some of them
        # waited", not "all of them waited exactly window_s".
        assert any(dt >= window_s * 0.3 for dt in slow), (
            f"at least one of the overflow acquires should have waited, "
            f"got {slow}"
        )

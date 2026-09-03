"""DEFECT 1 — the ignition watch set fails open to ZERO on a provider outage.

`build_equity_universe` documents a fail-open contract: "any error / empty
snapshot -> [] so the caller falls back to its default universe (no regression)".
`trading_scheduler.py:5327` honours it (`scan_tickers = merged or None`).
`ignition_loop._UniverseTracker.refresh` did NOT — it installed the empty list AS
the watch set, with no fallback, no refusal and no alarm.

Measured, not assumed:
  * 2026-08-28, two windows (16:07:17-16:22:13 PT and 20:46:43-21:01:16 PT):
    705 `[massive] Full market snapshot returned no tickers` lines. Both landed
    after the RTH close, so nothing tradable was lost — but the path is live.
  * The two failure branches in `refresh` logged at DEBUG under a root logger
    pinned to INFO (app/main.py:8-9). A scan of all 3,603,800 lines of
    project_ws/AgentOps/timeshare/window_app.log found ZERO occurrences of
    "snapshot fetch failed", "universe build failed" or "fail-open to []".
  * The only universe-size line ever emitted is the one-shot at `start()`: ~74
    logged out of ~23,990 rebuilds over 11 sessions (0.31%), all at process
    start. `trading_universe_snapshots` holds 0 rows.

So the bar these tests hold the code to is the brief's own sentence: an empty
universe must be DISTINGUISHABLE from a legitimately quiet market. Every test
below fails on origin/main.

Pure unit tests — every seam is monkeypatched, no DB, no network.
"""

from __future__ import annotations

import logging

import pytest

from app.services.trading.momentum_neural import ignition_loop as IL


# ── seams ─────────────────────────────────────────────────────────────────────


def _snapshot(tickers: list[str]) -> list[dict]:
    """Healthy full-market rows for `tickers` (in band, big $-vol, +change)."""
    return [
        {
            "ticker": t,
            "lastTrade": {"p": 5.0},
            "day": {"v": 5_000_000, "h": 6.0, "l": 4.0, "o": 4.5, "c": 5.0},
            "prevDay": {"c": 4.0, "v": 4_000_000, "h": 4.2, "l": 3.8},
            "todaysChangePerc": 25.0,
        }
        for t in tickers
    ]


@pytest.fixture
def tracker(monkeypatch):
    """A tracker whose provider + screen are both injectable."""
    state: dict = {"snapshot": _snapshot(["AAA", "BBB"]), "universe": ["AAA", "BBB"]}

    def _fake_snapshot(*_a, **_k):
        v = state["snapshot"]
        if isinstance(v, Exception):
            raise v
        return v

    def _fake_build(_profile, *, snapshot=None):
        v = state["universe"]
        if isinstance(v, Exception):
            raise v
        return list(v)

    import app.services.massive_client as MC

    monkeypatch.setattr(MC, "get_full_market_snapshot", _fake_snapshot, raising=False)
    monkeypatch.setattr(IL, "build_equity_universe", _fake_build)
    return IL._UniverseTracker(), state


# ── R2: refuse the zero instead of installing it ─────────────────────────────


def test_provider_empty_body_does_not_empty_the_watch_set(tracker, caplog):
    """The 2026-08-28 shape: the snapshot returns an empty body, 705 times."""
    trk, state = tracker
    assert trk.refresh() == {"AAA", "BBB"}

    state["snapshot"] = []
    state["universe"] = []
    with caplog.at_level(logging.WARNING, logger=IL.__name__):
        held = trk.refresh()

    assert held == {"AAA", "BBB"}, "an empty provider body emptied the watch set"
    assert trk.count() == 2
    assert trk.last_outcome() == IL._UNIVERSE_SNAPSHOT_EMPTY
    assert any("universe RETAINED" in r.message for r in caplog.records)


def test_provider_raising_does_not_empty_the_watch_set(tracker, caplog):
    trk, state = tracker
    trk.refresh()

    state["snapshot"] = RuntimeError("massive down")
    state["universe"] = []
    with caplog.at_level(logging.WARNING, logger=IL.__name__):
        held = trk.refresh()

    assert held == {"AAA", "BBB"}
    assert trk.last_outcome() == IL._UNIVERSE_SNAPSHOT_ERROR


def test_screen_build_raising_does_not_empty_the_watch_set(tracker):
    trk, state = tracker
    trk.refresh()

    state["universe"] = ValueError("screen blew up")
    assert trk.refresh() == {"AAA", "BBB"}
    assert trk.last_outcome() == IL._UNIVERSE_BUILD_ERROR


def test_quiet_market_DOES_empty_the_watch_set_and_reads_differently(tracker, caplog):
    """The 03:52 ET case (08-18/08-19/08-20/08-22): zero is CORRECT there.

    A healthy snapshot in which nothing clears the bands must still empty the
    set — and must not be reported as a provider failure.
    """
    trk, state = tracker
    trk.refresh()

    state["snapshot"] = _snapshot(["AAA", "BBB"])  # provider healthy
    state["universe"] = []                          # nothing qualifies
    with caplog.at_level(logging.WARNING, logger=IL.__name__):
        assert trk.refresh() == set()

    assert trk.count() == 0
    assert trk.last_outcome() == IL._UNIVERSE_SCREEN_EMPTY
    assert not any("universe RETAINED" in r.message for r in caplog.records)


def test_degraded_and_quiet_produce_different_outcomes(tracker):
    """The brief's bar, stated as one assertion."""
    trk, state = tracker
    trk.refresh()

    state["snapshot"] = []
    state["universe"] = []
    trk.refresh()
    degraded = trk.last_outcome()

    trk._retaining_since = None
    trk._symbols = {"AAA"}
    state["snapshot"] = _snapshot(["AAA"])
    state["universe"] = []
    trk.refresh()
    quiet = trk.last_outcome()

    assert degraded != quiet
    assert degraded in IL._UNIVERSE_DEGRADED
    assert quiet not in IL._UNIVERSE_DEGRADED


def test_retention_is_bounded_and_surrender_is_loud(tracker, caplog, monkeypatch):
    """A stale universe beats an empty one for minutes, not for hours."""
    trk, state = tracker
    trk.refresh()

    clock = {"t": 1_000.0}
    monkeypatch.setattr(IL.time, "monotonic", lambda: clock["t"])

    state["snapshot"] = []
    state["universe"] = []
    assert trk.refresh() == {"AAA", "BBB"}          # retention starts

    clock["t"] += IL._UNIVERSE_RETAIN_MAX_S + 1.0
    with caplog.at_level(logging.WARNING, logger=IL.__name__):
        assert trk.refresh() == set()               # bound exceeded → surrender

    assert any("universe SURRENDERED" in r.message for r in caplog.records)


def test_kill_switch_restores_the_prior_behaviour(tracker, monkeypatch):
    trk, state = tracker
    trk.refresh()
    monkeypatch.setattr(
        IL.settings,
        "chili_momentum_universe_retain_on_provider_failure_enabled",
        False,
        raising=False,
    )
    state["snapshot"] = []
    state["universe"] = []
    assert trk.refresh() == set()


def test_cold_start_provider_failure_stays_empty(tracker):
    """Nothing to retain on the very first refresh — must not invent a set."""
    trk, state = tracker
    state["snapshot"] = []
    state["universe"] = []
    assert trk.refresh() == set()
    assert trk.last_outcome() == IL._UNIVERSE_SNAPSHOT_EMPTY


def test_retained_set_keeps_its_baselines(tracker):
    """A retained name must still SCORE — retention holds baselines too."""
    trk, state = tracker
    trk.refresh()
    assert trk.baseline_for("AAA") == 4.0

    state["snapshot"] = []
    state["universe"] = []
    trk.refresh()
    assert trk.baseline_for("AAA") == 4.0


# ── R3: the failure branches must be reachable under an INFO root logger ─────


def test_failure_branches_log_above_debug(tracker, caplog):
    """app/main.py:8-9 pins the root logger at INFO, so DEBUG is unreachable."""
    trk, state = tracker
    trk.refresh()
    state["snapshot"] = RuntimeError("massive down")
    state["universe"] = []
    with caplog.at_level(logging.INFO, logger=IL.__name__):
        trk.refresh()
    assert any(
        "snapshot fetch failed" in r.message and r.levelno >= logging.WARNING
        for r in caplog.records
    )


# ── R1/R4: the refresh LOOP must report size changes and its own failures ────


class _StubTracker:
    def __init__(self, sizes, outcomes, raises_on=()):
        self._sizes = list(sizes)
        self._outcomes = list(outcomes)
        self._raises_on = set(raises_on)
        self.calls = 0

    def refresh(self):
        idx = self.calls
        self.calls += 1
        if idx in self._raises_on:
            raise RuntimeError("refresh blew up")

    def count(self):
        i = min(max(self.calls - 1, 0), len(self._sizes) - 1)
        return self._sizes[i]

    def last_outcome(self):
        i = min(max(self.calls - 1, 0), len(self._outcomes) - 1)
        return self._outcomes[i]


def _run_loop(monkeypatch, tracker, iterations):
    """Drive `_refresh_loop` for a fixed number of iterations, no real sleeping."""
    loop = object.__new__(IL.IgnitionScoringLoop)
    loop._running = True
    loop._tracker = tracker
    loop._sessions = type("S", (), {"refresh": lambda self: None})()
    loop._sync_subscriptions = lambda: None

    clock = {"t": 0.0}
    # One extra tick: the loop sleeps BEFORE it refreshes, so `iterations + 1`
    # sleeps produce exactly `iterations` universe rebuilds.
    left = {"n": iterations + 1}

    def _sleep(_s):
        clock["t"] += IL._UNIVERSE_REFRESH_S
        left["n"] -= 1
        if left["n"] <= 0:
            loop._running = False

    monkeypatch.setattr(IL.time, "sleep", _sleep)
    monkeypatch.setattr(IL.time, "monotonic", lambda: clock["t"])
    loop._refresh_loop()
    return loop


def test_refresh_loop_logs_universe_size_on_change_only(monkeypatch, caplog):
    """~23,990 rebuilds produced 74 log lines, all at process start. Fix that —
    without adding 4,320 lines/day for a steady state."""
    trk = _StubTracker(
        sizes=[120, 120, 120, 0],
        outcomes=[IL._UNIVERSE_OK] * 3 + [IL._UNIVERSE_SNAPSHOT_EMPTY],
    )
    with caplog.at_level(logging.INFO, logger=IL.__name__):
        _run_loop(monkeypatch, trk, 4)

    lines = [r for r in caplog.records if "universe " in r.message and "symbols" in r.message]
    assert len(lines) == 2, [r.getMessage() for r in lines]
    assert lines[0].levelno == logging.INFO
    assert lines[1].levelno == logging.WARNING
    assert IL._UNIVERSE_SNAPSHOT_EMPTY in lines[1].getMessage()


def test_refresh_loop_failure_is_logged_not_swallowed(monkeypatch, caplog):
    """The THIRD silent state: refresh() raises, the watch set freezes, and the
    bare `except Exception: pass` left no trace at ANY log level."""
    trk = _StubTracker(sizes=[42], outcomes=[IL._UNIVERSE_OK], raises_on=(0,))
    with caplog.at_level(logging.INFO, logger=IL.__name__):
        _run_loop(monkeypatch, trk, 1)

    frozen = [r for r in caplog.records if "FROZEN" in r.message]
    assert frozen, "a raising refresh left no trace"
    assert frozen[0].levelno >= logging.WARNING

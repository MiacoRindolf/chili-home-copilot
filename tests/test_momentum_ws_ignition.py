"""WS/event-driven UNCAPPED momentum-universe coverage (2026-06-15).

The day's biggest movers were missing the SCORED universe (momentum_symbol_viability):
  * CUPR (+125%) — the top-50 count cap in build_equity_universe truncated 296
    screened movers to 50, and a faded leader ranked OUT of the pool.
  * RGNT (+498%) — a VERTICAL name is nowhere near its EMA9, so the
    scan_momentum_continuation gate emits NOTHING and it never gets a fresh
    per-symbol viability row even though build_equity_universe selects it.

This suite covers the two-part fix:
  (a) uncap parity            — build_equity_universe surfaces EVERY screen-passer
                                when uncapped, EXACTLY top-50 when off, prefix-stable.
  (b) RGNT-class WS scoring    — _score_symbol routes a synthetic vertical mover
                                directly into viability via run_momentum_neural_tick
                                (the EMA9 gate is bypassed).
  (c) ignition adaptivity      — _on_tick fires above the floor, silent below.
  (d) dedup/cooldown/inflight  — two ticks < cooldown apart → one dispatch.
  (e) session hygiene          — _score_symbol closes its session with rollback-in-
                                finally even when run_momentum_neural_tick raises.
  (f) bridge chunking          — uncapped bridge with 70 tickers calls the tick 3×
                                (32/32/6) with a commit between chunks.
  (g) kill-switch              — chili_momentum_ws_ignition_enabled=0 → start is a no-op.

Pure unit tests (no DB fixture): every DB seam is stubbed. Style mirrors
tests/test_momentum_auto_arm.py (monkeypatch seams) + tests/test_premarket_gap_full_universe.py.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.trading.momentum_neural import ignition_loop as IL
from app.services.trading.momentum_neural import universe as U


# ── shared fakes ──────────────────────────────────────────────────────────────


class _RecordingSession:
    """Minimal SessionLocal stand-in that records commit/rollback/close order."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def commit(self) -> None:
        self.events.append("commit")

    def rollback(self) -> None:
        self.events.append("rollback")

    def close(self) -> None:
        self.events.append("close")

    # the bridge queries TradingAutomationSession for armed-session pins; fail-open
    def query(self, *_a, **_k):  # pragma: no cover - defensive
        raise RuntimeError("no query in stub")


def _equity_snapshot(n: int) -> list[dict]:
    """n synthetic screen-passers (in band, big $-vol, +change), distinct ranks."""
    snap = []
    for i in range(n):
        snap.append(
            {
                "ticker": f"M{i:03d}",
                "lastTrade": {"p": 5.0 + i * 0.01},
                "day": {"v": 5_000_000, "h": 6.0, "l": 4.0, "o": 4.5},
                "prevDay": {"c": 4.5},
                "todaysChangePerc": 10.0 + i * 0.1,
            }
        )
    return snap


# ── (a) uncap parity ──────────────────────────────────────────────────────────


def test_uncap_off_is_exactly_top_50(monkeypatch):
    """Flag OFF ⇒ build_equity_universe truncates to profile.max_universe (50)."""
    monkeypatch.setattr(settings, "chili_momentum_universe_uncapped_enabled", False, raising=False)
    snap = _equity_snapshot(60)
    out = U.build_equity_universe(snapshot=snap)
    assert len(out) == 50  # == EQUITY_ROSS_SMALLCAP.max_universe


def test_uncap_on_surfaces_every_screen_passer_prefix_stable(monkeypatch):
    """Flag ON ⇒ EVERY screen-passer surfaces; uncapped[:50] == the capped output."""
    monkeypatch.setattr(settings, "chili_momentum_universe_uncapped_enabled", False, raising=False)
    snap = _equity_snapshot(60)
    capped = U.build_equity_universe(snapshot=snap)

    monkeypatch.setattr(settings, "chili_momentum_universe_uncapped_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_universe_hard_ceiling", 1500, raising=False)
    uncapped = U.build_equity_universe(snapshot=snap)

    assert len(uncapped) == 60  # no top-N quality cap
    assert uncapped[:50] == capped  # ranked order preserved for downstream [:N]


def test_uncap_hard_ceiling_is_the_only_bound(monkeypatch):
    """The DB-safety ceiling bounds the uncapped output (not a quality cap)."""
    monkeypatch.setattr(settings, "chili_momentum_universe_uncapped_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_universe_hard_ceiling", 10, raising=False)
    out = U.build_equity_universe(snapshot=_equity_snapshot(60))
    assert len(out) == 10


# ── (b) RGNT-class WS-only scoring (bypasses the EMA9 continuation gate) ───────


def test_score_symbol_writes_viability_for_vertical_mover(monkeypatch):
    """A vertical RGNT-class name routes straight to run_momentum_neural_tick([sym]).

    The continuation scan would emit nothing for it (nowhere near EMA9); the WS path
    scores it directly. We stub the tick and assert it was called for exactly [RGNT].
    """
    calls: list[dict] = []

    def _fake_tick(db, *, meta=None, **_k):
        calls.append(dict(meta or {}))
        return {}

    # the loop imports the tick lazily as `from .pipeline import run_momentum_neural_tick`
    from app.services.trading.momentum_neural import pipeline as P

    monkeypatch.setattr(P, "run_momentum_neural_tick", _fake_tick, raising=False)
    monkeypatch.setattr(IL, "SessionLocal", _RecordingSession, raising=False)

    loop = IL.IgnitionScoringLoop()
    loop._score_symbol("RGNT", 498.0)

    assert len(calls) == 1
    assert calls[0]["tickers"] == ["RGNT"]
    assert "RGNT" in (calls[0].get("ross_signals") or {})


# ── (c) ignition adaptivity (the single FLOOR knob) ───────────────────────────


def _loop_armed_for_tick(monkeypatch, dispatched: list[str]):
    """An IgnitionScoringLoop wired so _on_tick can dispatch (running, pooled, baseline)."""
    loop = IL.IgnitionScoringLoop()
    loop._running = True
    loop._pool = SimpleNamespace(submit=lambda fn, sym, mv: dispatched.append(sym))
    # baseline 100.0 for SYM so move% is derivable from the quote price
    loop._tracker._baseline = {"SYM": 100.0}
    loop._tracker._symbols = {"SYM"}
    return loop


def test_on_tick_fires_above_floor(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_ignition_min_pct", 3.0, raising=False)
    dispatched: list[str] = []
    loop = _loop_armed_for_tick(monkeypatch, dispatched)
    # 100 -> 110 == +10% (>= 3% floor)
    loop._on_tick("SYM", SimpleNamespace(last=110.0, mid=110.0, bid=109.9))
    assert dispatched == ["SYM"]


def test_on_tick_silent_below_floor(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_ignition_min_pct", 3.0, raising=False)
    dispatched: list[str] = []
    loop = _loop_armed_for_tick(monkeypatch, dispatched)
    # 100 -> 101 == +1% (< 3% floor)
    loop._on_tick("SYM", SimpleNamespace(last=101.0, mid=101.0, bid=100.9))
    assert dispatched == []


# ── (d) dedup / cooldown / inflight ───────────────────────────────────────────


def test_two_ticks_within_cooldown_dispatch_once(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_ignition_min_pct", 3.0, raising=False)
    dispatched: list[str] = []
    loop = _loop_armed_for_tick(monkeypatch, dispatched)
    q = SimpleNamespace(last=120.0, mid=120.0, bid=119.9)  # +20%

    loop._on_tick("SYM", q)
    # second tick immediately after (well within _SCORE_COOLDOWN_S) must not re-dispatch
    loop._on_tick("SYM", q)

    assert dispatched == ["SYM"]  # exactly one dispatch


def test_inflight_guard_blocks_concurrent_dispatch(monkeypatch):
    """A symbol already inflight is not dispatched again even past the cooldown."""
    monkeypatch.setattr(settings, "chili_momentum_ignition_min_pct", 3.0, raising=False)
    dispatched: list[str] = []
    loop = _loop_armed_for_tick(monkeypatch, dispatched)
    loop._inflight.add("SYM")  # simulate an in-progress score
    loop._on_tick("SYM", SimpleNamespace(last=120.0, mid=120.0, bid=119.9))
    assert dispatched == []


# ── (e) session hygiene (rollback-in-finally on a raising tick) ────────────────


def test_score_symbol_rolls_back_and_closes_on_raise(monkeypatch):
    """_score_symbol must rollback + close its session even when the tick raises."""
    rec = _RecordingSession()
    monkeypatch.setattr(IL, "SessionLocal", lambda: rec, raising=False)

    def _boom(db, *, meta=None, **_k):
        raise RuntimeError("tick exploded")

    from app.services.trading.momentum_neural import pipeline as P

    monkeypatch.setattr(P, "run_momentum_neural_tick", _boom, raising=False)

    loop = IL.IgnitionScoringLoop()
    loop._score_symbol("BOOM", 50.0)  # must not raise

    # at least one rollback (error path + finally), and the session is closed last.
    assert "rollback" in rec.events
    assert rec.events[-1] == "close"
    assert "commit" not in rec.events  # the tick raised → never committed
    assert "BOOM" not in loop._inflight  # inflight cleared in finally


def test_score_symbol_commits_then_closes_on_success(monkeypatch):
    """Happy path: commit on success, then a finally-rollback, then close."""
    rec = _RecordingSession()
    monkeypatch.setattr(IL, "SessionLocal", lambda: rec, raising=False)

    from app.services.trading.momentum_neural import pipeline as P

    monkeypatch.setattr(P, "run_momentum_neural_tick", lambda db, *, meta=None, **_k: {}, raising=False)

    loop = IL.IgnitionScoringLoop()
    loop._score_symbol("OK", 12.0)

    assert "commit" in rec.events
    assert rec.events[-1] == "close"
    assert "OK" not in loop._inflight


# ── (f) bridge chunking (uncapped: 70 tickers → 3 chunks, commit between) ──────


def test_uncapped_bridge_chunks_70_tickers_into_3_with_commit_between(monkeypatch):
    """_bridge_scanner_to_viability (uncapped) chunks 70 tickers 32/32/6, commit/chunk."""
    monkeypatch.setattr(settings, "chili_momentum_universe_uncapped_enabled", True, raising=False)

    import app.services.trading_scheduler as TS
    from app.services.trading.momentum_neural import pipeline as P

    tick_chunk_sizes: list[int] = []

    def _fake_tick(db, *, meta=None, **_k):
        tick_chunk_sizes.append(len(list((meta or {}).get("tickers") or [])))
        return {}

    monkeypatch.setattr(P, "run_momentum_neural_tick", _fake_tick, raising=False)
    # Neutralize the fail-open enrichment/feeder/pin seams so the chunking is exercised
    # deterministically + hermetically (each is already wrapped in try/except, but
    # snapshot_dollar_volumes would hit the network without a stub).
    from app.services.trading.momentum_neural import nbbo_tape as NT
    from app.services.trading.momentum_neural import universe as _UNI

    monkeypatch.setattr(NT, "tape_running_up_symbols", lambda db, **_k: [], raising=False)
    monkeypatch.setattr(_UNI, "snapshot_dollar_volumes", lambda *a, **k: {}, raising=False)

    db = _RecordingSession()
    results = [{"ticker": f"T{i:03d}", "direction": "up", "rvol": 5.0} for i in range(70)]

    TS._bridge_scanner_to_viability(db, results, source="test")

    # 70 → 32 + 32 + 6 == three tick calls
    assert tick_chunk_sizes == [32, 32, 6]
    # a commit BETWEEN chunks (one per successful chunk) — the idle-in-txn guard
    assert db.events.count("commit") == 3


def test_capped_bridge_single_call_when_flag_off(monkeypatch):
    """Flag OFF ⇒ the bridge keeps the single capped (top-30) call (parity)."""
    monkeypatch.setattr(settings, "chili_momentum_universe_uncapped_enabled", False, raising=False)

    import app.services.trading_scheduler as TS
    from app.services.trading.momentum_neural import pipeline as P
    from app.services.trading.momentum_neural import nbbo_tape as NT
    from app.services.trading.momentum_neural import universe as _UNI

    tick_calls: list[int] = []
    monkeypatch.setattr(
        P, "run_momentum_neural_tick",
        lambda db, *, meta=None, **_k: tick_calls.append(len(list((meta or {}).get("tickers") or []))) or {},
        raising=False,
    )
    monkeypatch.setattr(NT, "tape_running_up_symbols", lambda db, **_k: [], raising=False)
    monkeypatch.setattr(_UNI, "snapshot_dollar_volumes", lambda *a, **k: {}, raising=False)

    db = _RecordingSession()
    results = [{"ticker": f"T{i:03d}", "direction": "up", "rvol": 5.0} for i in range(70)]

    TS._bridge_scanner_to_viability(db, results, source="test")

    # exactly one call, capped at _VIABILITY_BRIDGE_MAX_TICKERS (30)
    assert len(tick_calls) == 1
    assert tick_calls[0] == TS._VIABILITY_BRIDGE_MAX_TICKERS == 30


# ── (g) kill-switch ───────────────────────────────────────────────────────────


def test_start_ignition_loop_is_noop_when_disabled(monkeypatch):
    """chili_momentum_ws_ignition_enabled=0 ⇒ start_ignition_loop never starts a loop."""
    monkeypatch.setattr(settings, "chili_autopilot_price_bus_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_ws_ignition_enabled", False, raising=False)

    started = {"n": 0}

    class _Spy(IL.IgnitionScoringLoop):
        def start(self):  # type: ignore[override]
            started["n"] += 1

    # reset the module singleton so get_ignition_loop builds our spy
    monkeypatch.setattr(IL, "_loop", None, raising=False)
    monkeypatch.setattr(IL, "IgnitionScoringLoop", _Spy, raising=False)

    IL.start_ignition_loop()
    assert started["n"] == 0


def test_loop_start_is_noop_when_disabled(monkeypatch):
    """The loop's own start() short-circuits to a no-op under the kill-switch."""
    monkeypatch.setattr(settings, "chili_momentum_ws_ignition_enabled", False, raising=False)
    loop = IL.IgnitionScoringLoop()
    loop.start()
    assert loop._running is False
    assert loop._pool is None

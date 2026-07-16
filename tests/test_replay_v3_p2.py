"""Replay v3 P2 — the ELIGIBILITY REPLAYER + the REAL risk gate (the grace A/B mechanism).

This is the CRUX of Replay v3: drive a session through a SCRIPTED ``live_eligible`` flicker
(eligible at arm → NOT-eligible at the entry instant — the UPC TOCTOU) with the GENUINE
entry-instant risk path running (``runner_boundary_risk_ok`` →
``evaluate_proposed_momentum_automation``), and prove BOTH directions of the recency-grace:

  * **grace OFF** (``chili_momentum_live_eligible_recency_grace_enabled=False``): the flicker
    at the entry instant ⇒ the ``live_eligible`` check BLOCKS ⇒ the session does NOT enter
    (a ``live_blocked_by_risk`` emitted; no ``live_entered``).
  * **grace ON** (default): the SAME flicker, but eligible-at-arm <= window + forward momentum
    present ⇒ the block is DOWNGRADED to warn ⇒ the session ENTERS (``live_entered``).

The two runs differ ONLY in the grace flag and produce OPPOSITE entry outcomes — the exact
A/B Replay v2 structurally cannot run (it never reaches the grace branch). P4 runs this same
mechanism against the REAL recorded UPC 2026-06-29 data.

Unlike P1 the driver does NOT short-circuit the gate (``risk_gate_allows`` stays ``None``):
the REAL ``evaluate_proposed_momentum_automation`` is invoked (asserted via a spy). The equity
read flows through the P2 ``risk_policy.replay_account_equity`` seam (prod byte-identical), and
forward-momentum flows through the as-of-t ``iqfeed_trade_ticks`` tape the replayer seeds.

Self-contained: seeds its own session + grid + tape in ``chili_test``; no prod ``chili`` data.
One pytest at a time (DB-truncate rule).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural import market_profile as _mp
from app.services.trading.momentum_neural import replay_eligibility as relig
from app.services.trading.momentum_neural import replay_v3 as rv3
from app.services.trading.momentum_neural import risk_evaluator as _re
from app.services.trading.momentum_neural.live_fsm import (
    STATE_LIVE_ENTERED,
    STATE_QUEUED_LIVE,
    STATE_WATCHING_LIVE,
)
from app.services.trading.momentum_neural.replay_mock_broker import MockBrokerAdapter

_BASE = datetime(2026, 6, 29, 14, 30, 0)  # 10:30 ET, RTH (naive-UTC, the _utcnow shape)


# ── hermetic network guard (same discipline as P1) ───────────────────────────────
def _install_network_guard(monkeypatch) -> None:
    """Route every heavy/real market read through a seam or neutralize the fail-open ones, so
    a green test proves the replay is hermetic (no network). Mirrors test_replay_v3_p1."""
    import app.services.trading.market_data as _md

    def _boom_fetch(*a, **k):
        raise AssertionError("NETWORK GUARD: real fetch_ohlcv_df called during replay")

    def _boom_adapter(*a, **k):
        raise AssertionError("NETWORK GUARD: real adapter factory resolved during replay")

    monkeypatch.setattr(_md, "fetch_ohlcv_df", _boom_fetch)
    monkeypatch.setattr(lr, "resolve_live_spot_adapter_factory", _boom_adapter)
    monkeypatch.setattr(lr, "_entry_pricebook_snapshot", lambda symbol: None)
    monkeypatch.setattr(lr, "_refetch_bbo_secondary", lambda symbol: None)
    import app.services.trading.momentum_neural.universe as _uni

    monkeypatch.setattr(_uni, "snapshot_dollar_volumes", lambda syms: {})
    import app.services.trading.momentum_neural.entry_features as _ef

    monkeypatch.setattr(_ef, "macro_regime_features", lambda *a, **k: {})
    import app.services.massive_client as _mc

    def _boom_snapshot(*a, **k):
        raise AssertionError("NETWORK GUARD: real market snapshot called during replay")

    monkeypatch.setattr(_mc, "get_full_market_snapshot", _boom_snapshot)


@pytest.fixture
def _enable_runner(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    # This suite isolates the eligibility-recency grace. The independent Ross
    # universe gate has its own fail-closed coverage and, when enabled, its
    # profile proof intentionally backfills live eligibility before the grace
    # branch can run.
    monkeypatch.setattr(
        settings,
        "chili_momentum_ross_equity_universe_required",
        False,
        raising=False,
    )
    monkeypatch.setattr(lr, "_venue_broker_connected", lambda ef: True)
    monkeypatch.setattr(lr, "is_kill_switch_active", lambda: False)
    # The risk evaluator imports is_kill_switch_active + get_kill_switch_status; neutralize.
    monkeypatch.setattr(_re, "is_kill_switch_active", lambda: False)
    monkeypatch.setattr(
        _re, "get_kill_switch_status", lambda: {"active": False, "reason": None}
    )
    monkeypatch.setattr(_mp, "is_tradeable_now", lambda symbol, **k: True)


def _grid_rising(symbol: str, n_rise: int = 8) -> list[rv3.RecordedNbboTick]:
    """A steadily rising tight-spread NBBO walk (entry fires + would fill). No drop — P2
    cares only about the ENTRY decision (the grace A/B), not the exit."""
    ticks: list[rv3.RecordedNbboTick] = []
    t = _BASE
    px = 10.00
    for _ in range(n_rise):
        ticks.append(rv3.RecordedNbboTick(ts=t, bid=px - 0.01, ask=px + 0.01, last=px))
        t = t + timedelta(seconds=5)
        px += 0.02
    return ticks


def _equity_provider():
    """A recorded/injected account equity basis (the P2 seam) so the atomic risk-budget
    admission + equity-relative caps don't bounce the entry on ``equity_unavailable``. Mirrors
    a stable ~$100k account; returned for every (family, flags) call shape."""
    return lambda *a, **k: 100000.0


def _build_driver(db, monkeypatch, symbol, *, grace_enabled: bool):
    """Seed a session armed live-eligible, build a scripted flicker, seed the as-of-t forward-
    momentum tape, and return (driver, eligibility, spy_calls). The flicker flips live_eligible
    False from the 2nd grid tick onward (covering the entry window); the anchor is 30s old
    (in-window). ``grace_enabled`` toggles the REAL grace flag the gate reads."""
    _install_network_guard(monkeypatch)
    monkeypatch.setattr(
        settings, "chili_momentum_live_eligible_recency_grace_enabled", grace_enabled
    )

    arm = rv3.RecordedArm(
        symbol=symbol,
        live_eligible_at_utc=(_BASE - timedelta(seconds=30)).isoformat() + "+00:00",
        viability_score=0.9,
        atr_pct=0.02,
    )
    seed = rv3.seed_replay_session(db, arm, execution_family="robinhood_spot")
    db.flush()

    grid = rv3.build_event_grid(_grid_rising(symbol))
    # FLICKER: eligible on tick 0 (queued_live -> watching_live), then live_eligible=False from
    # tick 1 onward — covering the entry instant. Grace OFF => stuck blocked; grace ON => enter.
    flicker_at = grid[1].ts
    timeline = relig.scripted_flicker_timeline(
        eligible_until=grid[0].ts, flicker_at=flicker_at
    )
    eligibility = relig.EligibilityReplayer(
        symbol=symbol, variant_id=seed.variant_id, timeline=timeline
    )
    # Forward-momentum tape covering the WHOLE grid window (buyer-aggressed ⇒ ofi_level>0,
    # slope>=0) so EVERY as-of-t read in the entry window finds fresh ticks <= t (the freshness
    # gate). Start one OFI window before the first grid tick; end at the last grid instant.
    _window_s = 15.0
    relig.seed_forward_momentum_ticks(
        db,
        symbol=symbol,
        start=grid[0].ts - timedelta(seconds=_window_s),
        as_of=grid[-1].ts,
        cadence_seconds=1.0,
    )
    db.flush()

    provider = rv3.RecordedOhlcvProvider(
        {
            "15m": rv3.synthetic_uptrend_ohlcv(),
            "5m": rv3.synthetic_uptrend_ohlcv(),
            "1m": rv3.synthetic_uptrend_ohlcv(),
        }
    )
    mock = MockBrokerAdapter(slippage_bps=0.0, venue_rt_bps=0.0, freshness_mode="wall")

    # Spy on the REAL evaluator to PROVE it was invoked (not short-circuited).
    spy_calls: list[dict] = []
    real_eval = lr.evaluate_proposed_momentum_automation

    def _spy_eval(*a, **k):
        spy_calls.append({"args": a, "kwargs": k})
        return real_eval(*a, **k)

    monkeypatch.setattr(lr, "evaluate_proposed_momentum_automation", _spy_eval)

    driver = rv3.ReplayV3Driver(
        db,
        seed,
        mock=mock,
        ohlcv_provider=provider,
        grid=grid,
        risk_gate_allows=None,  # P2: run the REAL gate
        eligibility=eligibility,
        equity_provider=_equity_provider(),
    )
    return driver, eligibility, spy_calls


# ── the P2 ACCEPTANCE TEST: the grace A/B (OFF -> blocked, ON -> enters) ───────────
def test_replay_v3_p2_grace_OFF_flicker_blocks_entry(db, monkeypatch, _enable_runner):
    """grace OFF: the eligibility flicker at the entry instant ⇒ the REAL gate's
    ``live_eligible`` check BLOCKS ⇒ the session does NOT enter."""
    symbol = "FLKR"
    driver, eligibility, spy_calls = _build_driver(
        db, monkeypatch, symbol, grace_enabled=False
    )
    result = driver.run()

    # the REAL evaluator was invoked (the gate ran, not the P1 short-circuit)
    assert spy_calls, "evaluate_proposed_momentum_automation was never invoked (gate short-circuited?)"
    # the session reached watching (tick-0 eligible) but NEVER entered (flicker blocks entry)
    assert STATE_WATCHING_LIVE in result.states_visited, result.states_visited
    assert STATE_LIVE_ENTERED not in result.states_visited, result.states_visited
    assert "live_entered" not in result.events, result.events
    assert "live_entry_filled" not in result.events, result.events
    # a risk block WAS emitted (the eligibility flicker)
    assert "live_blocked_by_risk" in result.events, result.events
    # no entry fill
    assert result.entry_fill_price is None, result.entry_fill_price
    # the eligibility replayer used the degenerate/scripted tier and actually flipped False
    assert eligibility.tier == relig.TIER_C_DEGENERATE
    assert any(not e for (_, e) in eligibility.apply_log), eligibility.apply_log


def test_replay_v3_p2_grace_ON_flicker_tolerated_enters(db, monkeypatch, _enable_runner):
    """grace ON (default): the SAME flicker, but eligible-at-arm <= window + forward momentum
    present ⇒ the block is DOWNGRADED to warn ⇒ the session ENTERS."""
    symbol = "FLKR"
    driver, eligibility, spy_calls = _build_driver(
        db, monkeypatch, symbol, grace_enabled=True
    )
    result = driver.run()

    # the REAL evaluator was invoked
    assert spy_calls, "evaluate_proposed_momentum_automation was never invoked (gate short-circuited?)"
    # the session ENTERED despite the flicker (grace tolerated it)
    assert STATE_LIVE_ENTERED in result.states_visited, result.states_visited
    assert "live_entry_submitted" in result.events, result.events
    assert "live_entry_filled" in result.events, result.events
    # the mock filled at the recorded ask region
    assert result.entry_fill_price is not None
    assert 10.0 <= result.entry_fill_price <= 10.3, result.entry_fill_price
    # the eligibility row was flickered False during the entry window (the grace tolerated it)
    assert any(not e for (_, e) in eligibility.apply_log), eligibility.apply_log


def test_replay_v3_p2_grace_AB_opposite_outcomes(db, monkeypatch, _enable_runner):
    """The load-bearing A/B in ONE assertion: the SAME scripted flicker produces OPPOSITE
    entry outcomes — grace OFF blocks, grace ON enters — differing ONLY in the grace flag.

    (Two seeds in one DB; the seed helpers use per-call unique user/variant keys so they never
    collide. A truncating fixture is not required between the two runs because each session +
    viability row is independent.)"""
    # Run A — grace OFF
    drv_off, _, spy_off = _build_driver(db, monkeypatch, "ABOFF", grace_enabled=False)
    res_off = drv_off.run()
    # Run B — grace ON
    drv_on, _, spy_on = _build_driver(db, monkeypatch, "ABON", grace_enabled=True)
    res_on = drv_on.run()

    assert spy_off and spy_on, "the REAL evaluator must run in both arms"
    # OPPOSITE outcomes:
    entered_off = STATE_LIVE_ENTERED in res_off.states_visited
    entered_on = STATE_LIVE_ENTERED in res_on.states_visited
    assert entered_off is False, ("grace OFF must NOT enter", res_off.states_visited)
    assert entered_on is True, ("grace ON must enter", res_on.states_visited)
    assert entered_off != entered_on, (entered_off, entered_on)


def test_replay_v3_p2_equity_seam_prod_byte_identical():
    """The P2 equity seam is prod byte-identical: with NO provider installed (PROD always),
    ``_account_equity_usd`` runs the real broker read path; with a provider it serves the
    injected basis with zero network. Pins the seam's prod-inertness directly (no DB)."""
    from app.services.trading.momentum_neural import risk_policy as _rp

    # PROD: no provider → the ContextVar is None (the real read path is taken).
    assert _rp._REPLAY_EQUITY.get() is None

    # REPLAY: provider installed → the injected basis is returned (no broker read).
    with _rp.replay_account_equity(lambda *a, **k: 12345.0):
        eq = _rp._account_equity_usd("robinhood_spot", apply_margin_multiple=False, prefer_equity=True)
    assert eq == 12345.0
    # a provider may return None to reproduce an equity outage
    with _rp.replay_account_equity(lambda *a, **k: None):
        eq2 = _rp._account_equity_usd("robinhood_spot")
    assert eq2 is None
    # auto-reset after the block
    assert _rp._REPLAY_EQUITY.get() is None


def test_replay_v3_p2_forward_momentum_tape_reads_as_of_t(db):
    """The as-of-t forward-momentum read (the grace's replay-native leg) sees the seeded
    buyer-aggressed tape: ``_live_flow_slope(symbol, db, as_of=last_tick)`` returns
    ofi_level>0 ∧ ofi_slope>=0 (forward momentum True)."""
    from app.services.trading.momentum_neural.pipeline import _live_flow_slope

    symbol = "TAPE"
    as_of = _BASE + timedelta(seconds=20)
    relig.seed_forward_momentum_ticks(db, symbol=symbol, as_of=as_of, n=14)
    db.flush()

    fs = _live_flow_slope(symbol, db=db, as_of=as_of)
    assert isinstance(fs, dict), fs
    assert fs.get("ofi_level") is not None and fs["ofi_level"] > 0.0, fs
    assert fs.get("ofi_slope") is not None and fs["ofi_slope"] >= 0.0, fs

    # cleanup leaves no replay rows behind
    n = relig.clear_forward_momentum_ticks(db, symbol=symbol)
    assert n == 14

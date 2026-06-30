"""Replay v3 P1 — drive ONE recorded session END-TO-END through the REAL FSM.

These tests step ``live_runner.tick_live_session`` (verbatim — NOT re-implemented) across a
small SYNTHETIC recorded grid via:

  * the P0 sim clock (``replay_clock`` → ``_utcnow`` ContextVar),
  * the P0 ``MockBrokerAdapter`` (deterministic fills off the recorded NBBO, zero network),
  * the P1 recorded-OHLCV provider seam (``replay_ohlcv_provider`` → the in-tick
    ``fetch_ohlcv_df`` wrapper).

The load-bearing asserts (docs/DESIGN/REPLAY_V3_LIVE_FSM_SIM.md §4 P1 ship gate):
  1. the FSM ADVANCES through the expected states (queued_live → watching_live →
     live_entry_candidate → live_pending_entry → live_entered → an exit terminal);
  2. the mock broker FILLED at the recorded quote;
  3. the SIM CLOCK governed every ``_utcnow()`` read (no wall-clock leak);
  4. ZERO external network calls occurred (a hard guard raises if the real
     ``fetch_ohlcv_df`` / a real adapter / a real quote fetch is hit without the seam).

Self-contained: seeds its own session + grid in ``chili_test``; does NOT read prod ``chili``
data (real-data replay vs chili_staging is P3/P4). One pytest at a time (DB-truncate rule).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural import market_profile as _mp
from app.services.trading.momentum_neural import replay_v3 as rv3
from app.services.trading.momentum_neural.live_fsm import (
    LIVE_RUNNER_TERMINAL_STATES,
    STATE_LIVE_COOLDOWN,
    STATE_LIVE_ENTERED,
    STATE_LIVE_EXITED,
    STATE_QUEUED_LIVE,
    STATE_WATCHING_LIVE,
)
from app.services.trading.momentum_neural.replay_mock_broker import MockBrokerAdapter, RecordedQuote

_BASE = datetime(2026, 6, 29, 14, 30, 0)  # 10:30 ET, RTH (naive-UTC, the _utcnow shape)


class _NetworkGuard:
    """A monkeypatch barrier proving the replay is HERMETIC. Any real OHLCV fetch, real
    adapter resolution, or real venue quote read during a replay step RAISES — so a green
    test is positive proof that every market read flowed through the replay seam / the mock."""

    def __init__(self) -> None:
        self.violations: list[str] = []

    def boom(self, what: str):
        def _raise(*a, **k):  # noqa: ANN001
            self.violations.append(what)
            raise AssertionError(f"NETWORK GUARD: real {what} called during replay")

        return _raise


def _install_network_guard(monkeypatch) -> _NetworkGuard:
    """Raise on the REAL network reads; neutralize the FAIL-OPEN auxiliary enrichment reads.

    Two classes of in-tick external I/O exist beyond ``fetch_ohlcv_df``:
      * HARD deps the replay MUST route through a seam — the heavy OHLCV read + the real
        adapter factory. These raise (a green test proves they were never hit).
      * FAIL-OPEN enrichment reads (the RH pricebook L2 snapshot + the secondary-quote
        refetch). They return None on error in prod, so they cannot break the FSM — but they
        are still network. We stub them to None so the replay is fully hermetic AND we document
        them here as the additional hidden real-time deps (design R2/R3) a future phase should
        seam (P2 wires the recorded L2 book; the refetch is only reached on a stale primary,
        which ``freshness_mode='wall'`` avoids)."""
    guard = _NetworkGuard()
    # The heavy OHLCV read: the REAL fetch must never run (the provider seam serves bars).
    import app.services.trading.market_data as _md

    monkeypatch.setattr(_md, "fetch_ohlcv_df", guard.boom("fetch_ohlcv_df"))
    # The real spot-adapter factory must never be resolved (the driver injects the mock).
    monkeypatch.setattr(
        lr, "resolve_live_spot_adapter_factory", guard.boom("resolve_live_spot_adapter_factory")
    )
    # FAIL-OPEN auxiliary reads → stub to the prod fail-open value (None / {}), hermetically.
    monkeypatch.setattr(lr, "_entry_pricebook_snapshot", lambda symbol: None)
    monkeypatch.setattr(lr, "_refetch_bbo_secondary", lambda symbol: None)
    # The liquidity-ceiling sizing reads name $-volume from the massive/yfinance market
    # snapshot (universe.snapshot_dollar_volumes) — fail-open (None ⇒ uncapped notional).
    # Stub it so the replay is hermetic (P2 wires recorded $-volume as-of-t).
    import app.services.trading.momentum_neural.universe as _uni

    monkeypatch.setattr(_uni, "snapshot_dollar_volumes", lambda syms: {})
    # entry_features.macro_regime_features reads SPY/^VIX OHLCV (the MACRO benchmark, a
    # separate module the live_runner OHLCV seam does not reach) on the post-fill feature
    # capture. Stub it (fail-open shape) so the replay is hermetic — it is feature logging,
    # not an FSM decision. A future phase can serve recorded macro bars through the same seam.
    import app.services.trading.momentum_neural.entry_features as _ef

    monkeypatch.setattr(_ef, "macro_regime_features", lambda *a, **k: {})
    # HARD BARRIER: any market-data snapshot that still escapes RAISES (positive proof).
    import app.services.massive_client as _mc

    monkeypatch.setattr(_mc, "get_full_market_snapshot", guard.boom("get_full_market_snapshot"))
    return guard


def _grid_with_entry_then_stop(symbol: str) -> list[rv3.RecordedNbboTick]:
    """A recorded NBBO walk: a few rising ticks (the entry fires + fills near 10.04), then a
    sharp drop BELOW the stop so the held position's stop fires and it exits."""
    ticks: list[rv3.RecordedNbboTick] = []
    t = _BASE
    # 1) a couple of rising, tight-spread ticks (watch → candidate → pending → entered)
    for px in (10.00, 10.02, 10.04, 10.06, 10.08, 10.10):
        ticks.append(rv3.RecordedNbboTick(ts=t, bid=px - 0.01, ask=px + 0.01, last=px))
        t = t + timedelta(seconds=5)
    # 2) a deep drop — bid well under the (~0.65*ATR) stop ⇒ stop/bailout exit
    for px in (9.30, 9.10, 9.00):
        ticks.append(rv3.RecordedNbboTick(ts=t, bid=px - 0.01, ask=px + 0.01, last=px))
        t = t + timedelta(seconds=5)
    return ticks


@pytest.fixture
def _enable_runner(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    # Venue-connectivity preflight is always-false in the test env (no creds); force connected
    # so the tick reaches the FSM logic (the #565 preflight has its own test).
    monkeypatch.setattr(lr, "_venue_broker_connected", lambda ef: True)
    # Kill switch off.
    monkeypatch.setattr(lr, "is_kill_switch_active", lambda: False)
    # The market-hours gate reads its own clock; force tradeable (RTH gate has its own tests).
    monkeypatch.setattr(_mp, "is_tradeable_now", lambda symbol, **k: True)
    # ACCOUNT EQUITY is read from the BROKER (network) by the atomic-risk-budget admission +
    # the equity-relative sizing — another hidden real-time dep (design R2). In replay it must
    # come from RECORDED equity; P1 pins a fixed sane equity so the budget admits (P2 wires the
    # recorded-equity-as-of-t feed). Patched at the source module (imported locally in the tick).
    import app.services.trading.momentum_neural.risk_policy as _rp

    monkeypatch.setattr(_rp, "_account_equity_usd", lambda *a, **k: 100000.0)


def test_replay_v3_p1_drives_one_session_end_to_end(db, monkeypatch, _enable_runner):
    symbol = "RPLY"
    guard = _install_network_guard(monkeypatch)

    arm = rv3.RecordedArm(
        symbol=symbol,
        live_eligible_at_utc=(_BASE - timedelta(seconds=30)).isoformat() + "+00:00",
        viability_score=0.9,
        atr_pct=0.02,
    )
    seed = rv3.seed_replay_session(db, arm, execution_family="robinhood_spot")
    db.flush()

    grid = rv3.build_event_grid(_grid_with_entry_then_stop(symbol))
    provider = rv3.RecordedOhlcvProvider(
        {
            "15m": rv3.synthetic_uptrend_ohlcv(),
            "5m": rv3.synthetic_uptrend_ohlcv(),
            "1m": rv3.synthetic_uptrend_ohlcv(),
        }
    )
    mock = MockBrokerAdapter(slippage_bps=0.0, venue_rt_bps=0.0, freshness_mode="wall")

    driver = rv3.ReplayV3Driver(
        db, seed, mock=mock, ohlcv_provider=provider, grid=grid, risk_gate_allows=True
    )
    result = driver.run()

    # (1) FSM advanced through the expected states (queued → watching → entered → exit).
    visited = result.states_visited
    assert STATE_QUEUED_LIVE in visited, visited
    assert STATE_WATCHING_LIVE in visited, visited
    assert STATE_LIVE_ENTERED in visited, visited
    # the entry-candidate + pending-entry edges were walked (entry was SUBMITTED then FILLED)
    assert "live_entry_candidate" in visited, visited
    assert "live_pending_entry" in visited, visited
    assert "live_entry_submitted" in result.events, result.events
    assert "live_entry_filled" in result.events, result.events
    # the position EXITED — live_exited was walked (then the runner recycles to cooldown,
    # the legitimate post-exit state). The exit fill confirms the position was flattened.
    assert STATE_LIVE_EXITED in visited, visited
    assert "live_exit_filled" in result.events, result.events
    assert result.final_state in (
        STATE_LIVE_EXITED,
        STATE_LIVE_COOLDOWN,
    ) or result.final_state in LIVE_RUNNER_TERMINAL_STATES, result.final_state

    # (2) the mock broker FILLED at the recorded quote (entry crossed the ~10.05 ask region).
    assert result.entry_fill_price is not None
    assert 10.0 <= result.entry_fill_price <= 10.2, result.entry_fill_price
    assert result.exit_fill_prices, "expected at least one exit fill"
    # the exit filled on the DROP (well below the entry) — the stop did its job
    assert min(result.exit_fill_prices) < result.entry_fill_price, result.exit_fill_prices

    # (3) ZERO network calls — the guard never fired.
    assert guard.violations == [], guard.violations
    # and the provider DID serve the in-tick OHLCV reads (proves the seam routed them)
    assert provider.call_log, "the recorded-OHLCV provider was never called"

    # (4) the sim clock governed _utcnow during the ticks; restored to wall-clock after.
    assert lr._SIM_NOW.get() is None
    assert lr._REPLAY_OHLCV_PROVIDER.get() is None


def test_replay_v3_p1_sim_clock_governs_utcnow_each_step(db, monkeypatch, _enable_runner):
    """The driver freezes ``_utcnow()`` at the grid instant for the duration of each tick —
    proving the runner's time reads are sim-governed (no wall-clock leak)."""
    symbol = "CLKG"
    _install_network_guard(monkeypatch)
    arm = rv3.RecordedArm(
        symbol=symbol,
        live_eligible_at_utc=(_BASE - timedelta(seconds=30)).isoformat() + "+00:00",
    )
    seed = rv3.seed_replay_session(db, arm)
    db.flush()

    observed: list[datetime] = []
    real_tick = lr.tick_live_session

    def _spy_tick(db_, sid, **kw):
        observed.append(lr._utcnow())  # what the runner sees as 'now' inside the tick
        return real_tick(db_, sid, **kw)

    monkeypatch.setattr(lr, "tick_live_session", _spy_tick)

    grid = rv3.build_event_grid(_grid_with_entry_then_stop(symbol))
    provider = rv3.RecordedOhlcvProvider({"15m": rv3.synthetic_uptrend_ohlcv()})
    mock = MockBrokerAdapter(freshness_mode="wall")
    driver = rv3.ReplayV3Driver(db, seed, mock=mock, ohlcv_provider=provider, grid=grid)
    driver.run()

    # every observed 'now' equals the grid instant the driver set for that step (sim-governed)
    assert observed == [tk.ts for tk in grid], (observed[:3], [t.ts for t in grid][:3])


def test_replay_v3_p1_no_provider_means_prod_byte_identical_fetch(monkeypatch):
    """With NO provider installed (PROD always), the in-tick OHLCV wrapper calls the REAL
    ``fetch_ohlcv_df`` with the EXACT same args — byte-identical. With a provider it bypasses
    the network entirely. This pins the seam's prod-inertness directly (no DB needed)."""
    import app.services.trading.market_data as _md

    calls: list[tuple] = []

    def _spy(ticker, interval="1d", period="6mo", **kw):
        calls.append((ticker, interval, period, kw))
        return "REAL_DF"

    monkeypatch.setattr(_md, "fetch_ohlcv_df", _spy)

    # PROD: no provider → forwards verbatim to the real function.
    assert lr._REPLAY_OHLCV_PROVIDER.get() is None
    out = lr._replay_aware_fetch_ohlcv_df("UPC", interval="15m", period="5d")
    assert out == "REAL_DF"
    assert calls == [("UPC", "15m", "5d", {})]

    # REPLAY: provider installed → the real fetch is never touched.
    calls.clear()
    with lr.replay_ohlcv_provider(lambda t, *, interval, period: f"REPLAY:{t}:{interval}:{period}"):
        out2 = lr._replay_aware_fetch_ohlcv_df("UPC", interval="5m", period="5d")
    assert out2 == "REPLAY:UPC:5m:5d"
    assert calls == []  # hermetic
    # auto-reset after the block
    assert lr._REPLAY_OHLCV_PROVIDER.get() is None


# ── P1 MOCK FIDELITY: resting limit / ack-delay / partial (pure, no DB) ───────────
def test_mock_resting_limit_does_not_fill_until_quote_crosses():
    """A resting BUY limit rests ``open`` while the ask is ABOVE the limit, then fills the
    instant a later recorded NBBO has the ask at/below it — not on placement."""
    m = MockBrokerAdapter(resting_limit_fills=True, freshness_mode="wall")
    m.set_clock(_BASE)
    m.set_quote("RST", RecordedQuote(bid=10.00, ask=10.06))
    # place a BUY limit at 10.03 while the ask is 10.06 ⇒ it must REST (no cross yet)
    r = m.place_limit_order_gtc(product_id="RST", side="buy", base_size="100", limit_price="10.03")
    assert r["ok"] is True and r["status"] == "open", r
    o, _ = m.get_order(r["order_id"])
    assert o.status == "open" and (o.filled_size or 0.0) == 0.0
    # the book ticks DOWN through the limit ⇒ the resting order crosses + fills
    m.set_quote("RST", rv3.RecordedQuote(bid=10.00, ask=10.02))
    o2, _ = m.get_order(r["order_id"])
    assert o2.status == "filled"
    assert o2.filled_size == pytest.approx(100.0)
    assert o2.average_filled_price == pytest.approx(10.02)  # crossed the ask


def test_mock_ack_delay_holds_open_for_n_advances():
    """``ack_delay_ticks`` holds a crossable resting limit ``open`` for N quote advances first
    — exercising the runner's pending-entry ack-poll/timeout window."""
    m = MockBrokerAdapter(resting_limit_fills=True, ack_delay_ticks=2, freshness_mode="wall")
    m.set_clock(_BASE)
    q = rv3.RecordedQuote(bid=9.99, ask=10.00)
    m.set_quote("ACK", q)
    # marketable (ask 10.00 <= limit 10.05) but ack-delayed: rests open on placement
    r = m.place_limit_order_gtc(product_id="ACK", side="buy", base_size="10", limit_price="10.05")
    assert r["status"] == "open"
    m.set_quote("ACK", q)  # advance 1 → still delayed
    assert m.get_order(r["order_id"])[0].status == "open"
    m.set_quote("ACK", q)  # advance 2 → delay exhausted, still not yet crossed this advance
    # one more advance after the delay drains ⇒ it crosses + fills
    m.set_quote("ACK", q)
    assert m.get_order(r["order_id"])[0].status == "filled"


def test_mock_partial_first_fill_then_remainder():
    """``partial_first_fill`` fills HALF on the first cross, leaving the order open for the
    remainder on the next cross — exercising the runner's partial-entry bookkeeping."""
    m = MockBrokerAdapter(resting_limit_fills=True, partial_first_fill=True, freshness_mode="wall")
    m.set_clock(_BASE)
    m.set_quote("PRT", rv3.RecordedQuote(bid=9.99, ask=10.00))
    r = m.place_limit_order_gtc(product_id="PRT", side="buy", base_size="100", limit_price="10.05")
    o, _ = m.get_order(r["order_id"])
    assert o.status == "open"
    assert o.filled_size == pytest.approx(50.0)  # half on the first cross
    # next cross fills the remainder
    m.set_quote("PRT", rv3.RecordedQuote(bid=9.99, ask=10.00))
    o2, _ = m.get_order(r["order_id"])
    assert o2.status == "filled"
    assert o2.filled_size == pytest.approx(100.0)


def test_mock_cancel_open_resting_blocks_later_cross_fill():
    """A cancelled resting order can never fill on a later cross (the ack-timeout → re-watch
    path the runner relies on)."""
    m = MockBrokerAdapter(resting_limit_fills=True, freshness_mode="wall")
    m.set_clock(_BASE)
    m.set_quote("CXL", rv3.RecordedQuote(bid=10.00, ask=10.06))
    r = m.place_limit_order_gtc(product_id="CXL", side="buy", base_size="10", limit_price="10.03")
    assert m.get_order(r["order_id"])[0].status == "open"
    m.cancel_order(r["order_id"])
    # the book now crosses, but the cancelled order must stay cancelled (no fill)
    m.set_quote("CXL", rv3.RecordedQuote(bid=10.00, ask=10.01))
    o, _ = m.get_order(r["order_id"])
    assert o.status == "cancelled"
    assert (o.filled_size or 0.0) == 0.0

"""Tests for f-fastpath-maker-only-executor (2026-05-08).

Pin the executor's maker-only / maker-first-then-taker path:

  * Mode dispatch — taker stays bit-identical at default settings;
    maker_only and maker_first_then_taker route through the new
    `_process_alert_maker`.
  * Limit-price computation — bid + tick (long) / ask - tick (short),
    never crosses.
  * 1-outstanding-per-(ticker, side) cap — second signal rejected.
  * Cancel-on-timeout (paper) — book not crossed -> cancelled,
    book crossed -> filled.
  * Adverse paper sweep — side-adjusted mid reaching our quote before
    fill cancels early without waiting for timeout.
  * `decay_miner.record_maker_outcome` called on fill.
  * Hybrid taker fallback fires after `replaced`.

Helper-level. We monkey-patch the executor's DB helpers + the
`_build_context` book peek; no DB / no broker.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text

from app.services.trading.fast_path import executor as ex_mod
from app.services.trading.fast_path.alert_direction import (
    DIRECTION_LONG,
    DIRECTION_NEUTRAL,
    DIRECTION_SHORT,
    SIDE_BUY,
    SIDE_SELL,
    direction_for_alert_type,
    spot_entry_side_for_alert_type,
)
from app.services.trading.fast_path.executor import (
    FastPathExecutor,
    _compute_maker_limit_price,
    _maker_default_tick_size,
)
from app.services.trading.fast_path.gates import (
    ExecContext,
    GateResult,
    GateRunResult,
)
from app.services.trading.fast_path.settings import FastPathSettings
from app.services.trading.fast_path.universe_status import (
    UNIVERSE_STATUS_ACTIVE,
    UNIVERSE_STATUS_SHADOW,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(*, execution_mode="taker",
                   maker_cancel_on_timeout_s=10,
                   maker_first_taker_fallback_s=5):
    return FastPathSettings(
        enabled=True,
        execution_mode=execution_mode,
        maker_cancel_on_timeout_s=maker_cancel_on_timeout_s,
        maker_first_taker_fallback_s=maker_first_taker_fallback_s,
    )


def _make_alert(ticker="BTC-USD", alert_type="imbalance_long",
                signal_score=0.85, alert_id=1):
    return {
        "id": alert_id,
        "ticker": ticker,
        "alert_type": alert_type,
        "fired_at": datetime.now(timezone.utc).replace(tzinfo=None),
        "signal_score": signal_score,
        "features": {"best_bid": 100.0, "best_ask": 100.10},
    }


def _make_ctx(*, mode="paper", best_bid=100.0, best_ask=100.10,
              spread_bps=10.0):
    return ExecContext(
        now_wall=datetime.now(timezone.utc).replace(tzinfo=None),
        best_bid=best_bid,
        best_ask=best_ask,
        spread_bps=spread_bps,
        open_positions_for_ticker=0,
        daily_notional_used_usd=0.0,
        mode=mode,
        live_authorized=False,
        engine="stub",
    )


def _make_gate_run():
    return GateRunResult(
        allow=True, deny_reason=None,
        results=[GateResult(name="dummy", allow=True, reason=None, detail={})],
    )


def _make_denied_gate_run(*gate_names: str):
    return GateRunResult(
        allow=False,
        deny_reason="negative_edge:negative_edge",
        results=[
            GateResult(name=name, allow=False, reason=name, detail={})
            for name in gate_names
        ],
    )


def _make_executor(settings, decay_miner=None):
    """Build an executor with a stubbed engine + book aggregator. The
    DB helpers are patched per-test so no real engine is needed.
    """
    engine = MagicMock(name="engine")
    book = SimpleNamespace(_books={})
    ex = FastPathExecutor(settings, engine, book, decay_miner=decay_miner)
    # Replace synchronous DB helpers with no-ops so loop.run_in_executor
    # paths don't choke.
    ex._insert_decision_sync = MagicMock(return_value=None)
    ex._insert_maker_attempt_sync = MagicMock(return_value=42)
    ex._update_maker_attempt_sync = MagicMock(return_value=None)
    return ex


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_compute_maker_limit_long_is_inside_spread():
    """buy: best_bid + tick, never crosses ask."""
    px = _compute_maker_limit_price("buy", 100.0, 100.10, 0.01)
    assert 100.0 < px < 100.10


def test_compute_maker_limit_short_is_inside_spread():
    """sell: best_ask - tick, never crosses bid."""
    px = _compute_maker_limit_price("sell", 100.0, 100.10, 0.01)
    assert 100.0 < px < 100.10


def test_compute_maker_limit_joins_top_when_no_inside_price_exists():
    """One-tick books should still get passive join prices."""
    assert _compute_maker_limit_price("buy", 100.0, 100.05, 1.0) == pytest.approx(100.0)
    assert _compute_maker_limit_price("sell", 100.0, 100.05, 1.0) == pytest.approx(100.05)


def test_compute_maker_limit_returns_zero_on_inverted_book():
    assert _compute_maker_limit_price("buy", 100.05, 100.0, 0.01) == 0.0
    assert _compute_maker_limit_price("sell", 100.05, 100.0, 0.01) == 0.0


def test_compute_maker_limit_returns_zero_on_no_quotes():
    assert _compute_maker_limit_price("buy", 0.0, 100.0, 0.01) == 0.0
    assert _compute_maker_limit_price("sell", 100.0, 0.0, 0.01) == 0.0


def test_compute_maker_limit_unknown_side_zero():
    assert _compute_maker_limit_price("flip", 100.0, 100.10, 0.01) == 0.0


def test_default_tick_size_scales_with_mid():
    """Tick is `mid * 1bp` by default — order-of-magnitude sanity."""
    assert _maker_default_tick_size(100.0) == pytest.approx(0.01)
    assert _maker_default_tick_size(0.0) == 0.0


def test_alert_direction_helpers_treat_neutral_as_spot_long_entry():
    assert direction_for_alert_type("imbalance_long") == DIRECTION_LONG
    assert direction_for_alert_type("imbalance_short") == DIRECTION_SHORT
    assert direction_for_alert_type("spread_squeeze") == DIRECTION_NEUTRAL
    assert spot_entry_side_for_alert_type("spread_squeeze") == SIDE_BUY
    assert spot_entry_side_for_alert_type("imbalance_short") == SIDE_SELL


# ---------------------------------------------------------------------------
# Mode dispatch
# ---------------------------------------------------------------------------

def test_taker_mode_does_not_invoke_maker_path():
    """Default taker mode skips _process_alert_maker entirely.

    Hard acceptance criterion: 'Default taker is bit-identical to today.'
    We assert the maker path was never entered by checking
    `_outstanding_maker` stays empty + insert_maker_attempt isn't called.
    """
    settings = _make_settings(execution_mode="taker")
    ex = _make_executor(settings)

    # Drive _process_alert directly with a paper context. We monkey-
    # patch _build_context so the per-alert call returns our ctx.
    ctx = _make_ctx()
    ex._build_context = MagicMock(return_value=ctx)
    ex._write_decision = MagicMock(return_value=None)
    # Coerce the awaitable shape the production code expects.
    async def _async_noop(*_a, **_kw):
        return None
    ex._write_decision = _async_noop

    asyncio.run(ex._process_alert(_make_alert()))

    assert ex._outstanding_maker == {}
    assert ex._insert_maker_attempt_sync.call_count == 0


def test_neutral_spread_squeeze_does_not_become_spot_short(monkeypatch):
    """Neutral alert names must not become sell entries by default."""
    settings = _make_settings(execution_mode="taker")
    ex = _make_executor(settings)
    ctx = _make_ctx(spread_bps=1.0)
    ex._build_context = MagicMock(return_value=ctx)
    writes = []

    monkeypatch.setattr(ex_mod, "run_gates", lambda *_a, **_kw: _make_gate_run())

    async def run():
        async def _record(*a, **kw):
            writes.append(kw)
            return None

        ex._write_decision = _record
        await ex._process_alert(_make_alert(alert_type="spread_squeeze"))

    asyncio.run(run())

    assert len(writes) == 1
    assert writes[0]["decision"] == "paper_fill"
    assert writes[0]["reject_reason"] is None
    assert writes[0]["side"] == SIDE_BUY


def test_shadow_maker_probe_bypasses_learned_denials(monkeypatch):
    """Shadow maker probes refresh maker-filled evidence despite pooled blocks."""
    settings = replace(
        _make_settings(execution_mode="maker_only"),
        universe_rotation_enabled=True,
    )
    ex = _make_executor(settings)
    ex._build_context = MagicMock(return_value=_make_ctx(spread_bps=2.0))
    ex._latest_universe_status_sync = MagicMock(return_value=UNIVERSE_STATUS_SHADOW)
    gate_run = _make_denied_gate_run(
        "negative_edge",
        "maker_attempt_adverse",
        "cost_aware_admission",
    )
    monkeypatch.setattr(ex_mod, "run_gates", lambda *_a, **_kw: gate_run)
    calls = []

    async def _record_maker_probe(**kwargs):
        calls.append(kwargs)

    async def _fail_reject(*_a, **_kw):
        raise AssertionError("shadow learned denial should route to maker probe")

    ex._process_alert_maker = _record_maker_probe
    ex._write_decision = _fail_reject

    asyncio.run(ex._process_alert(_make_alert()))

    assert len(calls) == 1
    assert calls[0]["gate_run"] is gate_run
    assert calls[0]["execution_mode"] == "maker_only"
    ex._latest_universe_status_sync.assert_called_once_with("BTC-USD")


def test_shadow_maker_probe_blocks_terminal_learned_denials(monkeypatch):
    """Shadow probes should not keep sampling lanes already proven toxic."""
    settings = replace(
        _make_settings(execution_mode="maker_only"),
        universe_rotation_enabled=True,
    )
    ex = _make_executor(settings)
    ex._build_context = MagicMock(return_value=_make_ctx(spread_bps=2.0))
    ex._latest_universe_status_sync = MagicMock(return_value=UNIVERSE_STATUS_SHADOW)
    gate_run = GateRunResult(
        allow=False,
        deny_reason="negative_edge:negative_edge",
        results=[
            GateResult(
                name="negative_edge",
                allow=False,
                reason="negative_edge",
                detail={"verdict": "negative_edge"},
            ),
        ],
    )
    monkeypatch.setattr(ex_mod, "run_gates", lambda *_a, **_kw: gate_run)
    writes = []

    async def _record_write(*_a, **kwargs):
        writes.append(kwargs)

    async def _fail_maker_probe(**_kwargs):
        raise AssertionError("terminal learned denial must not be re-probed")

    ex._write_decision = _record_write
    ex._process_alert_maker = _fail_maker_probe

    asyncio.run(ex._process_alert(_make_alert()))

    assert len(writes) == 1
    assert writes[0]["decision"] == "rejected"
    assert writes[0]["reject_reason"] == "negative_edge:negative_edge"
    ex._latest_universe_status_sync.assert_not_called()


def test_shadow_maker_probe_rechecks_terminal_denials_when_enabled(monkeypatch):
    settings = replace(
        _make_settings(execution_mode="maker_only"),
        universe_rotation_enabled=True,
        universe_shadow_terminal_reprobe_enabled=True,
    )
    ex = _make_executor(settings)
    ex._build_context = MagicMock(return_value=_make_ctx(spread_bps=2.0))
    ex._latest_universe_status_sync = MagicMock(return_value=UNIVERSE_STATUS_SHADOW)
    gate_run = GateRunResult(
        allow=False,
        deny_reason="negative_edge:negative_edge",
        results=[
            GateResult(
                name="negative_edge",
                allow=False,
                reason="negative_edge",
                detail={"verdict": "negative_edge"},
            ),
        ],
    )
    monkeypatch.setattr(ex_mod, "run_gates", lambda *_a, **_kw: gate_run)
    calls = []

    async def _record_maker_probe(**kwargs):
        calls.append(kwargs)

    async def _fail_reject(*_a, **_kw):
        raise AssertionError("terminal shadow reprobe should route to maker")

    ex._process_alert_maker = _record_maker_probe
    ex._write_decision = _fail_reject

    asyncio.run(ex._process_alert(_make_alert()))

    assert len(calls) == 1
    assert calls[0]["gate_run"] is gate_run
    ex._latest_universe_status_sync.assert_called_once_with("BTC-USD")


def test_shadow_maker_probe_capacity_block_is_observe_only(monkeypatch):
    settings = replace(
        _make_settings(execution_mode="maker_only"),
        universe_rotation_enabled=True,
        universe_shadow_capacity_probe_enabled=True,
    )
    ex = _make_executor(settings)
    ex._build_context = MagicMock(return_value=_make_ctx(spread_bps=2.0))
    ex._latest_universe_status_sync = MagicMock(return_value=UNIVERSE_STATUS_SHADOW)
    gate_run = GateRunResult(
        allow=False,
        deny_reason="capacity:pair_already_held",
        results=[
            GateResult(
                name="capacity",
                allow=False,
                reason="pair_already_held",
                detail={"open": 1, "max": 1},
            ),
        ],
    )
    monkeypatch.setattr(ex_mod, "run_gates", lambda *_a, **_kw: gate_run)
    calls = []

    async def _record_maker_probe(**kwargs):
        calls.append(kwargs)

    async def _fail_reject(*_a, **_kw):
        raise AssertionError("shadow capacity block should route to observe-only maker")

    ex._process_alert_maker = _record_maker_probe
    ex._write_decision = _fail_reject

    asyncio.run(ex._process_alert(_make_alert()))

    assert len(calls) == 1
    assert calls[0]["observe_only"] is True
    ex._latest_universe_status_sync.assert_called_once_with("BTC-USD")


def test_shadow_maker_probe_keeps_operational_denials_blocking(monkeypatch):
    settings = replace(
        _make_settings(execution_mode="maker_only"),
        universe_rotation_enabled=True,
    )
    ex = _make_executor(settings)
    ex._build_context = MagicMock(return_value=_make_ctx(spread_bps=20.0))
    ex._latest_universe_status_sync = MagicMock(return_value=UNIVERSE_STATUS_SHADOW)
    gate_run = _make_denied_gate_run("negative_edge", "spread_sanity")
    monkeypatch.setattr(ex_mod, "run_gates", lambda *_a, **_kw: gate_run)
    writes = []

    async def _record_write(*_a, **kwargs):
        writes.append(kwargs)

    async def _fail_maker_probe(**_kwargs):
        raise AssertionError("spread sanity denial must not be bypassed")

    ex._write_decision = _record_write
    ex._process_alert_maker = _fail_maker_probe

    asyncio.run(ex._process_alert(_make_alert()))

    assert len(writes) == 1
    assert writes[0]["decision"] == "rejected"
    assert writes[0]["reject_reason"] == "negative_edge:negative_edge"
    assert ex._latest_universe_status_sync.call_count == 0


def test_open_positions_refresh_uses_fast_exits_as_truth():
    """Capacity state must release after the exit manager writes fast_exits."""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE fast_executions (
                id INTEGER PRIMARY KEY,
                ticker TEXT,
                decision TEXT,
                mode TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE fast_exits (
                id INTEGER PRIMARY KEY,
                entry_execution_id INTEGER
            )
        """))
        conn.execute(text("""
            INSERT INTO fast_executions (id, ticker, decision, mode) VALUES
              (1, 'BTC-USD', 'paper_fill', 'paper'),
              (2, 'BTC-USD', 'paper_fill', 'paper'),
              (3, 'SOL-USD', 'live_placed', 'live'),
              (4, 'DOGE-USD', 'rejected', 'paper')
        """))
        conn.execute(text("""
            INSERT INTO fast_exits (id, entry_execution_id) VALUES
              (10, 2)
        """))

    ex = FastPathExecutor(_make_settings(), engine, SimpleNamespace(_books={}))
    ex._open_positions = {"BTC-USD": 99, "DOGE-USD": 7}

    ex._refresh_open_positions_sync()

    assert ex._open_positions == {"BTC-USD": 1, "SOL-USD": 1}


def test_paper_position_allowed_requires_latest_active_universe():
    settings = replace(
        _make_settings(),
        universe_rotation_enabled=True,
        universe_shadow_paper_fills_enabled=False,
    )

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE fast_path_universe (
                ticker TEXT,
                status TEXT,
                rotation_at TEXT
            )
        """))
        conn.execute(text("""
            INSERT INTO fast_path_universe (ticker, status, rotation_at) VALUES
              ('BTC-USD', :active_status, '2026-05-23 15:00:00'),
              ('BTC-USD', :shadow_status, '2026-05-23 15:05:00'),
              ('ETH-USD', :active_status, '2026-05-23 15:05:00')
        """), {
            "active_status": UNIVERSE_STATUS_ACTIVE,
            "shadow_status": UNIVERSE_STATUS_SHADOW,
        })

    ex = FastPathExecutor(settings, engine, SimpleNamespace(_books={}))

    assert ex._paper_position_allowed_for_universe("BTC-USD") == (
        False, UNIVERSE_STATUS_SHADOW,
    )
    assert ex._paper_position_allowed_for_universe("ETH-USD") == (
        True, UNIVERSE_STATUS_ACTIVE,
    )
    assert ex._paper_position_allowed_for_universe("SOL-USD") == (False, None)


# ---------------------------------------------------------------------------
# Outstanding-per-(ticker, side) cap
# ---------------------------------------------------------------------------

def test_one_outstanding_maker_cap_rejects_duplicate():
    settings = _make_settings(execution_mode="maker_only",
                              maker_cancel_on_timeout_s=120)
    ex = _make_executor(settings)
    ctx = _make_ctx()

    async def run():
        # Pre-occupy the slot to simulate a still-resting first order.
        ex._outstanding_maker[("BTC-USD", "buy")] = {"placeholder": True}
        # Stub _write_decision so the rejection-row write doesn't try
        # to hit the engine.
        async def _noop_write(*a, **kw):
            return None
        ex._write_decision = _noop_write

        await ex._process_alert_maker(
            alert=_make_alert(), ctx=ctx, gate_run=_make_gate_run(),
            side="buy", quantity=0.001, fill_price=100.10,
            notional_usd=10.0, decided_at=ctx.now_wall, latency_ms=1.0,
            execution_mode="maker_only",
        )

    asyncio.run(run())

    assert ex._metrics.maker_attempts_capped == 1
    assert ex._metrics.decisions_rejected == 1
    # Original placeholder still present; no INSERT attempted.
    assert ex._outstanding_maker[("BTC-USD", "buy")] == {"placeholder": True}
    assert ex._insert_maker_attempt_sync.call_count == 0


# ---------------------------------------------------------------------------
# _process_alert_maker happy path: places, inserts attempt, schedules timeout
# ---------------------------------------------------------------------------

def test_maker_only_paper_places_and_schedules_timeout():
    settings = _make_settings(execution_mode="maker_only",
                              maker_cancel_on_timeout_s=120)
    ex = _make_executor(settings)
    ctx = _make_ctx()
    ex._build_context = MagicMock(return_value=ctx)
    writes = []

    async def run():
        async def _record(*a, **kw):
            writes.append(kw)
            return None
        ex._write_decision = _record

        await ex._process_alert_maker(
            alert=_make_alert(), ctx=ctx, gate_run=_make_gate_run(),
            side="buy", quantity=0.001, fill_price=100.10,
            notional_usd=10.0, decided_at=ctx.now_wall, latency_ms=1.0,
            execution_mode="maker_only",
        )
        # Cancel the timeout task so the test loop closes cleanly.
        rec = ex._outstanding_maker.get(("BTC-USD", "buy"))
        if rec and rec.get("timeout_task"):
            rec["timeout_task"].cancel()
            try:
                await rec["timeout_task"]
            except (asyncio.CancelledError, Exception):
                pass

    asyncio.run(run())

    assert ex._metrics.maker_attempts_placed == 1
    assert ex._metrics.decisions_paper_fill == 0
    assert writes == []
    assert ex._insert_maker_attempt_sync.call_count == 1
    payload = ex._insert_maker_attempt_sync.call_args.args[0]
    assert payload["ticker"] == "BTC-USD"
    assert payload["side"] == "buy"
    assert payload["limit_price"] > 100.0  # bid + tick
    assert payload["limit_price"] < 100.10  # below ask
    assert payload["execution_mode"] == "maker_only"


def test_paper_maker_sweep_resolves_crossed_book_before_timeout():
    """Paper fills should resolve on the polling sweep, not at cancel timeout."""
    settings = _make_settings(
        execution_mode="maker_only",
        maker_cancel_on_timeout_s=120,
    )
    decay = MagicMock()
    ex = _make_executor(settings, decay_miner=decay)

    ctx_place = _make_ctx(best_bid=100.0, best_ask=100.10)
    ctx_after = _make_ctx(best_bid=100.005, best_ask=100.005)
    ex._build_context = MagicMock(return_value=ctx_after)
    writes = []

    async def _record_write(*a, **kw):
        writes.append(kw)
        return None
    ex._write_decision = _record_write

    timeout_task = MagicMock()
    attempt = {
        "attempt_id": 7,
        "alert_id": 1,
        "ticker": "BTC-USD",
        "side": "buy",
        "limit_price": 100.005,
        "broker_order_id": None,
        "execution_mode": "maker_only",
        "alert_type": "imbalance_long",
        "signal_score": 0.85,
        "fired_at": datetime.now(timezone.utc).replace(tzinfo=None),
        "placed_at": time.monotonic() - 0.25,
        "quantity": 0.001,
        "notional_usd": 10.0,
        "timeout_task": timeout_task,
        "_placement_ctx": ctx_place,
        "_alert": _make_alert(),
        "_gate_run": _make_gate_run(),
        "_unfilled_outcome": "cancelled",
    }
    ex._outstanding_maker[("BTC-USD", "buy")] = attempt

    asyncio.run(ex._sweep_paper_maker_fills())

    timeout_task.cancel.assert_called_once()
    payload = ex._update_maker_attempt_sync.call_args.args[0]
    assert payload["fill_outcome"] == "filled"
    assert payload["final_price"] == pytest.approx(100.005)
    assert 0 < payload["time_to_fill_ms"] < 2_000
    assert attempt["resolved"] is True
    assert ("BTC-USD", "buy") not in ex._outstanding_maker
    assert ex._metrics.maker_attempts_filled == 1
    assert ex._metrics.decisions_paper_fill == 1
    assert len(writes) == 1
    decay.record_maker_outcome.assert_called_once()


def test_maker_adverse_cancel_threshold_is_quote_distance():
    """The early-cancel threshold comes from the order's own quote distance."""
    ctx_place = _make_ctx(best_bid=100.0, best_ask=100.10)
    limit_price = 100.01
    near_but_not_at_quote = _make_ctx(best_bid=99.99, best_ask=100.05)
    at_quote_mid = _make_ctx(best_bid=99.98, best_ask=100.04)

    assert FastPathExecutor._maker_quote_distance_bps(
        "buy",
        limit_price,
        ctx_place,
    ) == pytest.approx(((100.05 - 100.01) / 100.05) * 10_000.0)
    assert not FastPathExecutor._maker_adverse_drift_reached_quote(
        "buy",
        limit_price,
        ctx_place,
        near_but_not_at_quote,
    )
    assert FastPathExecutor._maker_adverse_drift_reached_quote(
        "buy",
        limit_price,
        ctx_place,
        at_quote_mid,
    )


def test_paper_maker_sweep_cancels_adverse_drift_before_timeout():
    """Cancel when mid reaches our passive quote before the book crosses."""
    settings = _make_settings(
        execution_mode="maker_only",
        maker_cancel_on_timeout_s=120,
    )
    decay = MagicMock()
    ex = _make_executor(settings, decay_miner=decay)

    ctx_place = _make_ctx(best_bid=100.0, best_ask=100.10)
    # Mid is exactly the passive buy quote, but ask has not traded
    # through the quote yet, so this is an early cancel rather than a fill.
    ctx_after = _make_ctx(best_bid=99.98, best_ask=100.04)
    ex._build_context = MagicMock(return_value=ctx_after)

    timeout_task = MagicMock()
    attempt = {
        "attempt_id": 7,
        "alert_id": 1,
        "ticker": "BTC-USD",
        "side": "buy",
        "limit_price": 100.01,
        "broker_order_id": None,
        "execution_mode": "maker_only",
        "alert_type": "imbalance_long",
        "signal_score": 0.85,
        "fired_at": datetime.now(timezone.utc).replace(tzinfo=None),
        "placed_at": time.monotonic() - 0.25,
        "quantity": 0.001,
        "notional_usd": 10.0,
        "timeout_task": timeout_task,
        "_placement_ctx": ctx_place,
        "_alert": _make_alert(),
        "_gate_run": _make_gate_run(),
        "_unfilled_outcome": "cancelled",
    }
    ex._outstanding_maker[("BTC-USD", "buy")] = attempt

    asyncio.run(ex._sweep_paper_maker_fills())

    timeout_task.cancel.assert_called_once()
    payload = ex._update_maker_attempt_sync.call_args.args[0]
    assert payload["fill_outcome"] == "cancelled"
    assert payload["final_price"] is None
    assert payload["time_to_fill_ms"] is None
    assert payload["mid_drift_bps"] == pytest.approx(-3.998, abs=0.01)
    assert attempt["resolved"] is True
    assert attempt["_adverse_cancel"] is True
    assert ("BTC-USD", "buy") not in ex._outstanding_maker
    assert ex._metrics.maker_attempts_cancelled == 1
    assert ex._metrics.maker_attempts_adverse_cancelled == 1
    assert ex._metrics.maker_attempts_filled == 0
    decay.record_maker_outcome.assert_not_called()


# ---------------------------------------------------------------------------
# Cancel-on-timeout: paper book NOT crossed -> cancelled
# ---------------------------------------------------------------------------

def test_maker_timeout_paper_no_book_cross_cancels():
    """Paper mode with the book unchanged at timeout → cancelled."""
    settings = _make_settings(execution_mode="maker_only",
                              maker_cancel_on_timeout_s=1)
    decay = MagicMock()
    ex = _make_executor(settings, decay_miner=decay)

    ctx = _make_ctx()
    # Book unchanged at timeout — best_bid still 100.0, far below our
    # buy limit at ~100.0 + 0.01.
    ex._build_context = MagicMock(return_value=ctx)

    attempt = {
        "attempt_id": 7,
        "alert_id": 1,
        "ticker": "BTC-USD",
        "side": "buy",
        "limit_price": 100.01,
        "broker_order_id": None,
        "execution_mode": "maker_only",
        "alert_type": "imbalance_long",
        "signal_score": 0.85,
        "fired_at": datetime.now(timezone.utc).replace(tzinfo=None),
        "placed_at": 0.0,
        "quantity": 0.001,
        "notional_usd": 10.0,
    }

    async def run():
        # Avoid the full sleep — patch asyncio.sleep to no-op.
        orig_sleep = asyncio.sleep
        async def _instant(_): return None
        asyncio.sleep = _instant
        try:
            await ex._maker_timeout_handler(
                cap_key=("BTC-USD", "buy"), attempt=attempt,
                timeout_s=1, unfilled_outcome="cancelled",
                ctx=ctx, alert=_make_alert(), gate_run=_make_gate_run(),
            )
        finally:
            asyncio.sleep = orig_sleep

    asyncio.run(run())

    payload = ex._update_maker_attempt_sync.call_args.args[0]
    assert payload["fill_outcome"] == "cancelled"
    assert payload["final_price"] is None
    assert ex._metrics.maker_attempts_cancelled == 1
    decay.record_maker_outcome.assert_not_called()


# ---------------------------------------------------------------------------
# Cancel-on-timeout: paper book CROSSED -> filled + decay notify
# ---------------------------------------------------------------------------

def test_maker_timeout_paper_book_crossed_fills_and_notifies_decay():
    """Paper mode: bid moved up to/past our limit at timeout → filled."""
    settings = _make_settings(execution_mode="maker_only",
                              maker_cancel_on_timeout_s=1)
    decay = MagicMock()
    ex = _make_executor(settings, decay_miner=decay)

    # ctx-at-place at original quote.
    ctx_place = _make_ctx(best_bid=100.0, best_ask=100.10)

    # ctx-at-timeout has the BBO crossed our buy-limit at 100.005.
    # Book trades down past our limit: best_bid <= limit AND
    # best_ask <= limit (a strong cross).
    ctx_after = _make_ctx(best_bid=100.005, best_ask=100.005)
    ex._build_context = MagicMock(return_value=ctx_after)
    writes = []

    async def _record_write(*a, **kw):
        writes.append(kw)
        return None
    ex._write_decision = _record_write

    attempt = {
        "attempt_id": 7,
        "alert_id": 1,
        "ticker": "BTC-USD",
        "side": "buy",
        "limit_price": 100.005,
        "broker_order_id": None,
        "execution_mode": "maker_only",
        "alert_type": "imbalance_long",
        "signal_score": 0.85,
        "fired_at": datetime.now(timezone.utc).replace(tzinfo=None),
        "placed_at": 0.0,
        "quantity": 0.001,
        "notional_usd": 10.0,
    }

    async def run():
        orig_sleep = asyncio.sleep
        async def _instant(_): return None
        asyncio.sleep = _instant
        try:
            await ex._maker_timeout_handler(
                cap_key=("BTC-USD", "buy"), attempt=attempt,
                timeout_s=1, unfilled_outcome="cancelled",
                ctx=ctx_place, alert=_make_alert(), gate_run=_make_gate_run(),
            )
        finally:
            asyncio.sleep = orig_sleep

    asyncio.run(run())

    payload = ex._update_maker_attempt_sync.call_args.args[0]
    assert payload["fill_outcome"] == "filled"
    assert payload["final_price"] == pytest.approx(100.005)
    assert ex._metrics.maker_attempts_filled == 1
    assert ex._metrics.decisions_paper_fill == 1
    assert ex._open_positions["BTC-USD"] == 1
    assert ex._daily_notional_used_usd == pytest.approx(10.0)
    assert len(writes) == 1
    assert writes[0]["decision"] == "paper_fill"
    assert writes[0]["fill_price"] == pytest.approx(100.005)
    decay.record_maker_outcome.assert_called_once()
    kwargs = decay.record_maker_outcome.call_args.kwargs
    assert kwargs["fill_outcome"] == "filled"
    assert kwargs["ticker"] == "BTC-USD"
    assert kwargs["alert_type"] == "imbalance_long"


def test_maker_timeout_shadow_fill_records_decay_without_paper_position():
    """Shadow maker fills are learning samples, not dashboard positions."""
    settings = replace(
        _make_settings(execution_mode="maker_only", maker_cancel_on_timeout_s=1),
        universe_rotation_enabled=True,
        universe_shadow_paper_fills_enabled=False,
    )
    decay = MagicMock()
    ex = _make_executor(settings, decay_miner=decay)
    ex._paper_position_allowed_for_universe = MagicMock(
        return_value=(False, UNIVERSE_STATUS_SHADOW),
    )

    ctx_place = _make_ctx(best_bid=100.0, best_ask=100.10)
    ctx_after = _make_ctx(best_bid=100.005, best_ask=100.005)
    ex._build_context = MagicMock(return_value=ctx_after)
    writes = []

    async def _record_write(*a, **kw):
        writes.append(kw)
        return None
    ex._write_decision = _record_write

    attempt = {
        "attempt_id": 7,
        "alert_id": 1,
        "ticker": "BTC-USD",
        "side": "buy",
        "limit_price": 100.005,
        "broker_order_id": None,
        "execution_mode": "maker_only",
        "alert_type": "imbalance_long",
        "signal_score": 0.85,
        "fired_at": datetime.now(timezone.utc).replace(tzinfo=None),
        "placed_at": 0.0,
        "quantity": 0.001,
        "notional_usd": 10.0,
    }

    async def run():
        orig_sleep = asyncio.sleep
        async def _instant(_): return None
        asyncio.sleep = _instant
        try:
            await ex._maker_timeout_handler(
                cap_key=("BTC-USD", "buy"), attempt=attempt,
                timeout_s=1, unfilled_outcome="cancelled",
                ctx=ctx_place, alert=_make_alert(), gate_run=_make_gate_run(),
            )
        finally:
            asyncio.sleep = orig_sleep

    asyncio.run(run())

    payload = ex._update_maker_attempt_sync.call_args.args[0]
    assert payload["fill_outcome"] == "filled"
    assert ex._metrics.maker_attempts_filled == 1
    assert ex._metrics.maker_observe_only_fills == 1
    assert ex._metrics.decisions_paper_fill == 0
    assert writes == []
    assert ex._open_positions == {}
    assert ex._daily_notional_used_usd == pytest.approx(0.0)
    decay.record_maker_outcome.assert_called_once()


def test_maker_timeout_observe_only_fill_records_decay_without_position():
    settings = replace(
        _make_settings(execution_mode="maker_only", maker_cancel_on_timeout_s=1),
        universe_rotation_enabled=True,
        universe_shadow_paper_fills_enabled=True,
    )
    decay = MagicMock()
    ex = _make_executor(settings, decay_miner=decay)
    ex._paper_position_allowed_for_universe = MagicMock(return_value=(True, None))

    ctx_place = _make_ctx(best_bid=100.0, best_ask=100.10)
    ctx_after = _make_ctx(best_bid=100.005, best_ask=100.005)
    ex._build_context = MagicMock(return_value=ctx_after)
    writes = []

    async def _record_write(*a, **kw):
        writes.append(kw)
        return None
    ex._write_decision = _record_write

    attempt = {
        "attempt_id": 8,
        "alert_id": 1,
        "ticker": "BTC-USD",
        "side": "buy",
        "limit_price": 100.005,
        "broker_order_id": None,
        "execution_mode": "maker_only",
        "alert_type": "imbalance_long",
        "signal_score": 0.85,
        "fired_at": datetime.now(timezone.utc).replace(tzinfo=None),
        "placed_at": 0.0,
        "quantity": 0.001,
        "notional_usd": 10.0,
        "observe_only": True,
    }

    async def run():
        orig_sleep = asyncio.sleep
        async def _instant(_): return None
        asyncio.sleep = _instant
        try:
            await ex._maker_timeout_handler(
                cap_key=("BTC-USD", "buy"), attempt=attempt,
                timeout_s=1, unfilled_outcome="cancelled",
                ctx=ctx_place, alert=_make_alert(), gate_run=_make_gate_run(),
            )
        finally:
            asyncio.sleep = orig_sleep

    asyncio.run(run())

    payload = ex._update_maker_attempt_sync.call_args.args[0]
    assert payload["fill_outcome"] == "filled"
    assert ex._metrics.maker_attempts_filled == 1
    assert ex._metrics.maker_observe_only_fills == 1
    assert ex._metrics.decisions_paper_fill == 0
    assert writes == []
    assert ex._open_positions == {}
    assert ex._daily_notional_used_usd == pytest.approx(0.0)
    decay.record_maker_outcome.assert_called_once()


# ---------------------------------------------------------------------------
# Hybrid mode: replaced -> taker fallback fires
# ---------------------------------------------------------------------------

def test_hybrid_replaced_triggers_taker_fallback_in_paper():
    settings = _make_settings(execution_mode="maker_first_then_taker",
                              maker_first_taker_fallback_s=1)
    ex = _make_executor(settings)
    ctx = _make_ctx()  # paper, book NOT crossed
    ex._build_context = MagicMock(return_value=ctx)

    async def _noop_write(*a, **kw):
        return None
    ex._write_decision = _noop_write

    attempt = {
        "attempt_id": 9,
        "alert_id": 1,
        "ticker": "BTC-USD",
        "side": "buy",
        "limit_price": 100.01,
        "broker_order_id": None,
        "execution_mode": "maker_first_then_taker",
        "alert_type": "imbalance_long",
        "signal_score": 0.85,
        "fired_at": datetime.now(timezone.utc).replace(tzinfo=None),
        "placed_at": 0.0,
        "quantity": 0.001,
        "notional_usd": 10.0,
    }

    async def run():
        orig_sleep = asyncio.sleep
        async def _instant(_): return None
        asyncio.sleep = _instant
        try:
            await ex._maker_timeout_handler(
                cap_key=("BTC-USD", "buy"), attempt=attempt,
                timeout_s=1, unfilled_outcome="replaced",
                ctx=ctx, alert=_make_alert(), gate_run=_make_gate_run(),
            )
        finally:
            asyncio.sleep = orig_sleep

    asyncio.run(run())

    # Maker leg was 'replaced'.
    upd_payload = ex._update_maker_attempt_sync.call_args.args[0]
    assert upd_payload["fill_outcome"] == "replaced"
    assert ex._metrics.maker_attempts_replaced == 1
    # Sibling taker placed -> paper_fill in paper mode.
    assert ex._metrics.decisions_paper_fill == 1
    assert ex._open_positions["BTC-USD"] == 1
    assert ex._daily_notional_used_usd == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# coinbase_service.place_buy_order receives post_only=True via maker_only
# ---------------------------------------------------------------------------

def test_place_coinbase_maker_order_passes_post_only_true(monkeypatch):
    """The live maker placement helper must pass post_only=True."""
    captured = {}

    def fake_place_buy(ticker, qty, order_type=None, limit_price=None,
                       post_only=False):
        captured["call"] = {
            "ticker": ticker, "qty": qty, "order_type": order_type,
            "limit_price": limit_price, "post_only": post_only,
        }
        return {"ok": True, "order_id": "O-1", "state": "pending"}

    fake_cb = SimpleNamespace(
        is_connected=lambda: True,
        place_buy_order=fake_place_buy,
        place_sell_order=lambda *a, **kw: {"ok": False, "error": "shouldn't be called"},
        connect=lambda: None,
    )

    monkeypatch.setattr(
        "app.services.trading.fast_path.executor.is_live_authorized",
        lambda: True,
    )
    monkeypatch.setattr(
        "app.services.trading.fast_path.executor._live_notional_override",
        lambda: True,
    )

    import sys
    import app.services as services_pkg

    monkeypatch.setitem(sys.modules, "app.services.coinbase_service", fake_cb)
    monkeypatch.setattr(services_pkg, "coinbase_service", fake_cb, raising=False)

    order_id = ex_mod._place_coinbase_maker_order_live(
        "BTC-USD", "buy", 0.001, 100.01, 10.0,
    )
    assert order_id == "O-1"
    assert captured["call"]["order_type"] == "limit"
    assert captured["call"]["post_only"] is True
    assert captured["call"]["limit_price"] == 100.01

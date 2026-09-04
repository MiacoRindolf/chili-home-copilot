"""The replay mock's execution BBO must satisfy the runner's own submit-boundary validator.

MEASURED 2026-09-04 (SDOT 2026-06-26, alpaca_spot, stride 1), the fourth distinct gate on
this family: with the seed frozen (#1317), the certified id in the driver env (#1318) and
the mock binding the account (#1320), the FSM finally ran -- and every tick was held with
``execution_bbo_capability_missing``: 3x ``live_held_execution_bbo_blocked`` +
``live_blocked_by_risk``, then ``live_declined`` -> ``live_cancelled`` in 71 s, 0 fills.
``live_runner._final_entry_bbo`` requires ``adapter.get_execution_bbo`` and then validates
source, symbol, uncrossed market and provider-clocked age; ``MockBrokerAdapter`` had only
the ordinary ``get_best_bid_ask``.

These tests drive the REAL validator (``lr._final_entry_bbo``) against the mock, so a
future tightening of the validator surfaces here instead of as another silent decline.
DB-free.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural.replay_mock_broker import (
    MockBrokerAdapter,
    RecordedQuote,
)


T = datetime(2026, 6, 26, 13, 20, 0)  # naive-UTC sim instant, the codebase's convention
T_AWARE = T.replace(tzinfo=timezone.utc)


def _mock_at(t=T, *, quote=RecordedQuote(bid=10.00, ask=10.02, last=10.01)):
    mock = MockBrokerAdapter(resting_limit_fills=True, volume_cap_enabled=True, freshness_mode="wall")
    mock.set_clock(t)
    if quote is not None:
        mock.set_quote("SDOT", quote)
    return mock


def test_the_runner_accepts_the_mock_execution_bbo_at_the_sim_instant():
    mock = _mock_at()
    with lr.replay_clock(T):
        tick, snap = lr._final_entry_bbo(mock, "SDOT", max_age_seconds=2.0)
    assert tick is not None, snap
    assert snap["ok"] is True and snap["reason"] == "execution_bbo_ok"
    assert snap["source"] == MockBrokerAdapter.EXECUTION_BBO_SOURCE
    assert snap["symbol"] == "SDOT"
    assert (tick.bid, tick.ask) == (10.00, 10.02)


def test_the_entry_seam_shape_with_stand_in_and_locked_opt_ins_is_accepted():
    """live_entry_final_bbo passes allow_stand_in + resolve_locked; the mock must accept the
    signature and answer from the recorded tape (there is no second tier to stand in)."""
    mock = _mock_at()
    with lr.replay_clock(T):
        tick, snap = lr._final_entry_bbo(
            mock, "SDOT", max_age_seconds=2.0, allow_stand_in=True,
            stand_in_max_age_seconds=15.0, resolve_locked=True,
        )
    assert tick is not None and snap["ok"] is True


def test_provider_clock_is_the_sim_instant_not_the_wall_clock():
    mock = _mock_at()
    with lr.replay_clock(T):  # outside a frozen clock the read is 70 days old and refused
        tick, meta = mock.get_execution_bbo("SDOT", max_age_seconds=2.0)
    assert tick is not None
    assert meta.provider_time_utc == T_AWARE
    assert meta.retrieved_at_utc == T_AWARE
    assert tick.raw["provider_event_at_utc"] == T_AWARE.isoformat()
    assert tick.raw["timestamp_basis"] == MockBrokerAdapter.EXECUTION_BBO_TIMESTAMP_BASIS
    # even in the parity "wall" freshness mode, which reports the ordinary quote as age~0
    assert mock.get_best_bid_ask("SDOT")[1].provider_time_utc is None


def test_no_recorded_quote_is_named_unavailable_not_silent():
    mock = _mock_at(quote=None)
    with lr.replay_clock(T):
        tick, snap = lr._final_entry_bbo(mock, "SDOT", max_age_seconds=2.0)
    assert tick is None
    assert snap["reason"] == "execution_bbo_unavailable"


def test_a_quote_older_than_the_ceiling_is_refused_like_the_real_adapter():
    """Recorded at T, asked at T+5s with a 2 s ceiling: the mock returns (None, meta) exactly
    as AlpacaSpotAdapter._execution_bbo_from_direct does, and the runner attributes it."""
    mock = _mock_at()
    with lr.replay_clock(T + timedelta(seconds=5)):
        tick, snap = lr._final_entry_bbo(mock, "SDOT", max_age_seconds=2.0)
    assert tick is None
    assert snap["reason"] == "execution_bbo_unavailable"
    assert snap["unavailable_kind"] == "stale_beyond_ceiling"
    assert snap["age_seconds"] == 5.0


def test_a_crossed_recorded_quote_is_refused():
    mock = _mock_at(quote=RecordedQuote(bid=10.05, ask=10.00))
    with lr.replay_clock(T):
        tick, snap = lr._final_entry_bbo(mock, "SDOT", max_age_seconds=2.0)
    assert tick is None
    assert snap["reason"] == "execution_bbo_unavailable"


def test_the_ordinary_quote_path_is_untouched():
    """The Robinhood/Coinbase families keep reading get_best_bid_ask exactly as before."""
    mock = _mock_at()
    tick, meta = mock.get_best_bid_ask("SDOT")
    assert tick.raw == {"venue": "replay_mock"}
    assert "source" not in tick.raw

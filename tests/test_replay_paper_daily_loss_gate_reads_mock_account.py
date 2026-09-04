"""Gate #6: the paper daily-loss gate must read the sim account in a replay.

MEASURED 2026-09-04 (SDOT 2026-06-26, alpaca_spot, with the lane's dispatch mode): 26
entry attempts, 26 ``live_entry_blocked_by_breaker`` -- breaker=daily_loss_cap_broker,
daily_pnl_usd 0.0, cap 5000. governance._alpaca_account_daily_change_usd had read the REAL
Alpaca adapter on every tick of a replay, got nothing, and returned the transient
fail-closed block. These tests drive the REAL observation and the REAL gate against the
mock's snapshot through the provider seam. The governance parts are DB-free; the gate
test passes db=None because the paper family never touches the ledger.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.services.trading import governance as gov
from app.services.trading.momentum_neural.replay_mock_broker import (
    MockBrokerAdapter,
    RecordedQuote,
)
from app.services.trading.venue.protocol import NormalizedFill


T = datetime(2026, 6, 26, 13, 20, 0)


def _mock(start=100_000.0):
    m = MockBrokerAdapter(resting_limit_fills=True, volume_cap_enabled=True, freshness_mode="wall")
    m.set_clock(T)
    m.set_quote("SDOT", RecordedQuote(bid=10.00, ask=10.02))
    if start is not None:
        m.set_account_equity(start)
    return m


def _fill(fill_id, side, size, price):
    return NormalizedFill(fill_id=fill_id, order_id="o-" + fill_id, product_id="SDOT",
                          side=side, size=size, price=price)


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch):
    monkeypatch.setattr(gov, "_per_broker_daily_loss", {}, raising=False)
    monkeypatch.setattr(gov, "_alpaca_day_change_cache", {"ts": 0.0, "realized": None, "meta": {}})
    monkeypatch.setattr(gov.settings, "chili_alpaca_paper", True, raising=False)


def test_mock_snapshot_is_alpaca_shaped_and_on_the_sim_clock():
    snap = _mock().get_account_snapshot()
    assert snap["ok"] is True and snap["paper"] is True
    assert snap["equity"] == 100_000.0 and snap["last_equity"] == 100_000.0
    assert snap["retrieved_at_utc"].startswith("2026-06-26T13:20:00")


def test_without_start_equity_the_stub_is_unchanged():
    assert _mock(start=None).get_account_snapshot() == {
        "ok": True, "venue": "replay_mock", "data": {}, "raw": {}
    }


def test_observation_reads_the_installed_provider_not_the_adapter(monkeypatch):
    calls = {"adapter": 0}

    class _Boom:
        def get_account_snapshot(self):
            calls["adapter"] += 1
            raise RuntimeError("network")

    import app.services.trading.venue.alpaca_spot as ap
    monkeypatch.setattr(ap, "AlpacaSpotAdapter", lambda *a, **k: _Boom())
    m = _mock()
    with gov.alpaca_account_snapshot_provider(m.get_account_snapshot):
        realized, meta = gov._alpaca_account_daily_change_usd(force_refresh=True)
    assert realized == 0.0
    assert meta["data_source"] == "replay_mock_account_snapshot"
    assert calls["adapter"] == 0
    # outside the manager the real adapter path is taken again (and fails closed here)
    realized2, meta2 = gov._alpaca_account_daily_change_usd(force_refresh=True)
    assert realized2 is None and "snapshot_exception" in meta2["error"]
    assert calls["adapter"] == 1


def test_the_gate_does_not_breach_on_a_flat_sim_account():
    """The REAL gate end to end. The paper cap is 5% of account equity, which the cap reads
    through risk_policy._REPLAY_EQUITY -- the seam ReplayV3Driver installs from the bench's
    EQUITY (the run measured cap 5000 = 5% x 100k). Both seams are installed here exactly as
    the driver installs them."""
    from app.services.trading.momentum_neural import risk_policy as rp

    m = _mock()
    token = rp._REPLAY_EQUITY.set(lambda *a, **k: 100_000.0)
    try:
        with gov.alpaca_account_snapshot_provider(m.get_account_snapshot):
            breached, info = gov.broker_daily_loss_breached(None, "alpaca_spot", user_id=1, force_refresh=True)
    finally:
        rp._REPLAY_EQUITY.reset(token)
    assert breached is False, info
    assert info["realized"] == 0.0 and info["transient"] is False
    assert info["cap"] == 5000.0


def test_day_change_follows_the_book_marked_at_the_recorded_quote():
    m = _mock()
    # a long 10 @ 10.02 marked at the bid 10.00 -> -0.20 day change
    m._fills.append(_fill("f1", "buy", 10.0, 10.02))
    with gov.alpaca_account_snapshot_provider(m.get_account_snapshot):
        realized, _ = gov._alpaca_account_daily_change_usd(force_refresh=True)
    assert realized == pytest.approx(-0.20, abs=1e-9)


def test_cache_is_bypassed_while_a_provider_is_installed():
    m = _mock()
    with gov.alpaca_account_snapshot_provider(m.get_account_snapshot):
        r1, _ = gov._alpaca_account_daily_change_usd()
        m.set_quote("SDOT", RecordedQuote(bid=9.00, ask=9.02))
        m._fills.append(_fill("f2", "buy", 1.0, 10.0))   # long 1 @ 10 marked at 9 -> -1.00
        r2, _ = gov._alpaca_account_daily_change_usd()
    assert r1 == 0.0 and r2 == pytest.approx(-1.0, abs=1e-9)

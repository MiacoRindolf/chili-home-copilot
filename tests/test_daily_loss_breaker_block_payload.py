"""The daily-loss breaker block must carry the observation's attribution, not a bare $0.

MEASURED 2026-09-04 (SDOT 2026-06-26 Ross bench, alpaca_spot): 26 ``live_entry_blocked_by_
breaker`` rows reading ``daily_pnl_usd: 0.0, max_daily_loss_usd: 5000.0``. The paper-family
observation had returned ``realized=None, transient=True,
reason=alpaca_account_daily_change_unavailable`` -- a fail-closed read, not a breach --
and the payload coerced None to 0.0 and dropped the reason. DB-free.
"""
from __future__ import annotations

from app.services.trading.momentum_neural import live_runner as lr


def test_a_transient_fail_closed_read_is_not_a_zero_dollar_breach():
    info = {"family": "alpaca_spot", "realized": None, "cap": 5000.0, "sticky": False,
            "transient": True, "reason": "alpaca_account_daily_change_unavailable",
            "source": "settings"}
    p = lr._daily_loss_breaker_block_payload(info)
    assert p["breaker"] == "daily_loss_cap_broker"
    assert p["daily_pnl_usd"] is None
    assert p["max_daily_loss_usd"] == 5000.0
    assert p["transient"] is True and p["sticky"] is False
    assert p["reason"] == "alpaca_account_daily_change_unavailable"


def test_a_measured_breach_keeps_its_numbers():
    info = {"family": "robinhood_agentic_mcp", "realized": -812.345, "cap": 500.0,
            "sticky": True, "transient": False, "source": "db_ledger"}
    p = lr._daily_loss_breaker_block_payload(info)
    assert p["daily_pnl_usd"] == -812.35
    assert p["max_daily_loss_usd"] == 500.0
    assert p["transient"] is False and p["sticky"] is True
    assert p["reason"] is None


def test_malformed_info_never_raises():
    p = lr._daily_loss_breaker_block_payload({"realized": "x", "cap": "y"})
    assert p["daily_pnl_usd"] is None and p["max_daily_loss_usd"] == 0.0
    assert lr._daily_loss_breaker_block_payload(None)["breaker"] == "daily_loss_cap_broker"

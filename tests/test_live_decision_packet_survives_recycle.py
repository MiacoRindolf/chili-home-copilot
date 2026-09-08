"""A recycled live trade must not lose the decision packet that authorised it.

`entry_decision_packet_id` is a per-trade key and the recycle clears it — it is
one of the 163 in `_RECYCLE_ENTRY_STATE_KEYS`, correctly, because the NEXT trade
must not inherit it.  But the outcome is extracted at SESSION end, after the
recycle, so a session that stopped out, recycled and went back to watching
reported `missing_entry_decision_packet` for a trade that had one — and lost its
evolution credit.

Measured in the live book: clean until 2026-06-29 (0 rows missing a packet), 10
of 16 that day, and essentially all of them afterwards. July credited 0 of 77
outcomes and August 0 of 4.

DB-free.
"""
from __future__ import annotations

from app.services.trading.momentum_neural.live_runner import _RECYCLE_ENTRY_STATE_KEYS
from app.services.trading.momentum_neural.outcome_extract import (
    outcome_evolution_credit_from_extracted,
)


def test_the_per_trade_key_is_still_cleared_on_recycle():
    """The clear is correct — the next trade must not inherit a stale packet."""
    assert "entry_decision_packet_id" in _RECYCLE_ENTRY_STATE_KEYS


def test_the_durable_copy_is_never_cleared():
    """...which is exactly why the durable copy must survive it."""
    assert "last_entry_decision_packet_id" not in _RECYCLE_ENTRY_STATE_KEYS


def _credit(**over):
    extracted = {
        "entry_occurred": True,
        "entry_decision_packet_id": 48459,
        "outcome_class": "stop_loss",
        "mode": "live",
        "quote_source_at_entry": "iqfeed_l1",
        "return_bps": -120.0,
        "realized_pnl_usd": -38.40,
    }
    extracted.update(over)
    return outcome_evolution_credit_from_extracted(extracted)


def test_a_packet_backed_outcome_earns_credit():
    out = _credit()
    assert out["contributes_to_evolution"] is True
    assert out["entry_decision_packet_id"] == 48459


def test_the_regression_shape_loses_credit():
    """What the book actually recorded for 71 days."""
    out = _credit(entry_decision_packet_id=None)
    assert out["contributes_to_evolution"] is False
    assert "missing_entry_decision_packet" in out["reason_codes"]


def test_recycled_live_state_still_resolves_the_packet():
    """The extraction contract: post-recycle `le` keeps only the durable copy."""
    from app.services.trading.momentum_neural import outcome_extract as oe

    le_after_recycle = {"last_entry_decision_packet_id": 48459}
    for key in _RECYCLE_ENTRY_STATE_KEYS:
        assert key not in le_after_recycle, f"{key} should have been cleared"

    raw = le_after_recycle.get("entry_decision_packet_id") or le_after_recycle.get(
        "last_entry_decision_packet_id"
    )
    assert int(raw) == 48459
    assert oe.outcome_evolution_credit_from_extracted({
        "entry_occurred": True,
        "entry_decision_packet_id": int(raw),
        "outcome_class": "stop_loss",
        "mode": "live",
        "return_bps": -120.0,
        "realized_pnl_usd": -38.40,
    })["contributes_to_evolution"] is True


def test_a_session_that_never_traded_still_has_no_packet():
    """The fallback must not invent credit for a session with no entry."""
    out = _credit(entry_occurred=False, entry_decision_packet_id=None)
    assert out["contributes_to_evolution"] is False
    assert "no_entry" in out["reason_codes"]
    assert "missing_entry_decision_packet" in out["reason_codes"]

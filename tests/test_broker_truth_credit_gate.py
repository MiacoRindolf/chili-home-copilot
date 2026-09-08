"""Evolution credit must not be taught from a P&L the account could not produce.

Measured 2026-09-08 over the 57 live arms carrying an entry-strength receipt:
37 (65%) had no broker confirmation, and six of those booked a magnitude past the
per-trade loss cap — together -$4,382 of a -$4,816 population loss.  The worst,
VTAK session 11685, books -$3,390.70 against a $13,000 account capped at $390.

Sweeping a strength threshold against that book produced a clean monotone
"improvement" that was one unconfirmed row.  The same sweep over broker-confirmed
rows pointed somewhere else entirely.  Hence the gate.

DB-free.
"""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural import feedback_emit as fe


@pytest.fixture()
def cap_390(monkeypatch):
    """The operator's canon: $13,000 equity x 3% = $390 per trade."""
    monkeypatch.setattr(fe.settings, "chili_momentum_risk_max_loss_per_trade_usd", 390.0,
                        raising=False)
    return 390.0


def _gate(pnl, *, status=None, broker_pnl=None, credit=None):
    return fe._apply_broker_truth_credit_gate(
        dict(credit or {"contributes_to_evolution": True}),
        realized_pnl_usd=pnl,
        broker_recon_status=status,
        broker_realized_pnl_usd=broker_pnl,
    )


def test_broker_confirmed_row_is_untouched(cap_390):
    out = _gate(-3390.70, status="reconciled", broker_pnl=-3390.70)
    assert out["contributes_to_evolution"] is True
    assert "broker_unconfirmed_pnl" not in (out.get("reason_codes") or [])
    assert "broker_truth_verification" not in out


def test_unconfirmed_but_plausible_keeps_credit_and_is_labelled(cap_390):
    """Unverified is not the same as impossible — label it, do not discard it."""
    out = _gate(-120.00)
    assert out["contributes_to_evolution"] is True
    assert "broker_unconfirmed_pnl" in out["reason_codes"]
    assert "broker_unconfirmed_impossible_magnitude" not in out["reason_codes"]
    assert out["broker_truth_verification"]["reason"] == "broker_unconfirmed"


def test_the_vtak_row_loses_credit(cap_390):
    """The row that bent the whole derivation."""
    out = _gate(-3390.70)
    assert out["contributes_to_evolution"] is False
    assert "broker_unconfirmed_impossible_magnitude" in out["reason_codes"]
    v = out["broker_truth_verification"]
    assert v["reason"] == "impossible_magnitude"
    assert v["over_cap_multiple"] == pytest.approx(8.69, abs=0.01)
    assert v["single_trade_loss_cap_usd"] == 390.0


@pytest.mark.parametrize("pnl,expect_credit", [
    (-389.0, True),      # inside the cap
    (-1169.0, True),     # inside the headroom multiple
    (-1171.0, False),    # past it
    (1171.0, False),     # a WIN can be impossible too
])
def test_the_line_is_drawn_at_the_cap_multiple(cap_390, pnl, expect_credit):
    assert _gate(pnl)["contributes_to_evolution"] is expect_credit


def test_no_configured_cap_means_the_gate_stays_silent(monkeypatch):
    """Never guess a cap; an unknowable policy is not evidence of a defect."""
    monkeypatch.setattr(fe.settings, "chili_momentum_risk_max_loss_per_trade_usd", 0.0,
                        raising=False)
    out = _gate(-99999.0)
    assert out["contributes_to_evolution"] is True
    assert "broker_unconfirmed_impossible_magnitude" not in out["reason_codes"]


def test_zero_and_missing_pnl_are_not_judged(cap_390):
    for pnl in (0, 0.0, None, "not a number"):
        out = _gate(pnl)
        assert out["contributes_to_evolution"] is True
        assert "broker_truth_verification" not in out


def test_a_gate_can_only_ever_refuse_credit(cap_390):
    """The chain's invariant: no gate hands credit back."""
    out = _gate(-120.00, credit={"contributes_to_evolution": False})
    assert out["contributes_to_evolution"] is False


def test_reason_codes_accumulate_rather_than_replace(cap_390):
    out = _gate(-3390.70, credit={"contributes_to_evolution": True,
                                  "reason_codes": ["economic_ledger_parity_mismatch"]})
    assert "economic_ledger_parity_mismatch" in out["reason_codes"]
    assert "broker_unconfirmed_impossible_magnitude" in out["reason_codes"]
    assert out["reason_codes"] == sorted(set(out["reason_codes"]))


def test_a_recon_status_that_is_not_reconciled_does_not_count_as_confirmation(cap_390):
    for status in ("fee_unconfirmed", "pending", "", None, "RECONCILED_LATER"):
        out = _gate(-3390.70, status=status, broker_pnl=-3390.70)
        assert out["contributes_to_evolution"] is False, status


def test_reconciled_status_without_a_broker_number_is_not_confirmation(cap_390):
    out = _gate(-3390.70, status="reconciled", broker_pnl=None)
    assert out["contributes_to_evolution"] is False

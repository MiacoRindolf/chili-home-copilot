"""[E] review (2026-09-11): the counterfactual replay sizes the canon account under the SAME
broker multiplier the Ross bench and the live lane freeze.

THE DEFECT. ``risk_policy.replay_seam_multiplier`` answers 1.0 for a provider without a
``replay_multiplier`` (the opt-in contract), and counterfactual_replay installed a bare
``lambda`` -- so this instrument froze min(13,000 x 1.0, 390 / 0.003) = 13,000 (crossover
3.0%) while the Ross bench, which pins the live Alpaca multiplier, froze 52,000 (crossover
0.0075, the live value). Two replay instruments 4x apart on one account.

THE FIX. The counterfactual opts in with the newest LIVE admission receipt whose multiplier
is broker truth (``risk_policy.live_broker_multiplier_receipt``) and sizes under that
receipt's family; with no receipt it keeps the seam's named 1.0 and SAYS so.

DB-free: the tape loaders and the receipt read are monkeypatched.
Runnable: pytest tests/test_counterfactual_replay_broker_multiplier.py -v
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.config import settings
from app.services.trading.momentum_neural import counterfactual_replay as cf
from app.services.trading.momentum_neural import risk_policy as rp

T0 = datetime(2026, 7, 13, 12, 37)
T1 = datetime(2026, 7, 13, 13, 37)


def _run(monkeypatch, receipt):
    monkeypatch.setattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.0)
    monkeypatch.setattr(settings, "chili_momentum_risk_loss_fraction_of_equity", 0.03)
    monkeypatch.setattr(rp, "_alpaca_account_cached", lambda: pytest.fail("broker read in replay"))
    monkeypatch.setattr(cf, "load_nbbo_tape", lambda *a, **k: [])
    monkeypatch.setattr(cf, "load_trade_tape", lambda *a, **k: [])
    seen = {}

    def _receipt(db, execution_family=None):
        seen["family_arg"] = execution_family
        return receipt

    monkeypatch.setattr(cf, "live_broker_multiplier_receipt", _receipt)
    res = cf.run_counterfactual_symbol_replay(
        None, "VEEE", since=T0, until=T1, risk_usd=390.0, account_equity_usd=13_000.0,
    )
    return res, seen


def _reason(res, prefix):
    return [r for r in res.confidence_reasons if r.startswith(prefix)]


def test_the_counterfactual_sizes_under_the_live_broker_multiplier(monkeypatch):
    res, seen = _run(monkeypatch, {"multiplier": 4.0, "source": "broker_multiplier",
                                   "execution_family": "alpaca_spot", "session_id": 22311,
                                   "symbol": "PMI", "updated_at": "2026-09-11T18:56:33"})
    assert seen == {"family_arg": None}            # the account the lane is live on now
    assert _reason(res, "live_sizing_broker_multiplier:") == [
        "live_sizing_broker_multiplier:4.0_source:broker_multiplier_family:alpaca_spot"
        "_session:22311"]
    cap = _reason(res, "live_sizing_equity_usd:")
    assert cap and "_equity_cap:52000.0_" in cap[0], cap      # the bench's canon ceiling


def test_no_live_receipt_keeps_the_seams_named_1_0_and_says_so(monkeypatch):
    res, _ = _run(monkeypatch, None)
    assert _reason(res, "live_sizing_broker_multiplier_unavailable:") == [
        "live_sizing_broker_multiplier_unavailable:no_live_admission_receipt_seam_1.0"]
    cap = _reason(res, "live_sizing_equity_usd:")
    assert cap and "_equity_cap:13000.0_" in cap[0], cap


def test_the_app_side_provider_is_the_seams_opt_in(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.0)
    monkeypatch.setattr(settings, "chili_momentum_risk_loss_fraction_of_equity", 0.03)
    seam = rp.ReplayEquitySeam(13_000.0, 4.0, "broker_multiplier", "alpaca_spot")
    assert seam() == 13_000.0
    with rp.replay_account_equity(seam):
        usd, meta = rp.equity_relative_notional_cap_with_meta(
            100_000.0, "alpaca_spot", loss_fixed_fallback_usd=390.0)
    assert usd == pytest.approx(52_000.0)
    assert meta["source"] == "replay_equity_seam:broker_multiplier_pinned"
    assert meta["crossover_stop_pct"] == pytest.approx(0.0075)

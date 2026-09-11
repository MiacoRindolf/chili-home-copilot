"""Per-trade notional ceiling: DERIVED from broker truth by default ([27]), an explicit
fraction is a NAMED operator override, and the fixed cap is the fallback when equity is
unavailable. Every path carries a receipt naming its source.

    derived  = min(equity x broker multiplier, loss_budget / RISK_FIRST_STOP_FLOOR_PCT)
    override = equity x chili_momentum_risk_notional_fraction_of_equity   (> 0 only)
    fallback = the documented fixed cap                                  (no equity)

Broker truth used as the fixture (read-only Alpaca paper get_account_snapshot, 2026-09-11
00:55Z): equity 10,320.34 / buying_power 41,281.36 / multiplier 4.0.
[[feedback_adaptive_no_magic]] [[feedback_report_binding_not_defaults]]
"""
from __future__ import annotations

import time

import pytest

from app.config import settings
from app.services.trading.momentum_neural import risk_policy as rp
from app.services.trading.venue import alpaca_spot as alpaca_spot_mod

EQUITY = 10_320.34
BUYING_POWER = 41_281.36
MULTIPLIER = 4.0


def _alpaca_paper(monkeypatch, *, equity=EQUITY, bp=BUYING_POWER, multiplier=MULTIPLIER,
                  loss_fraction=0.03, notional_fraction=0.0) -> None:
    """Certified paper account in the cache, no network: the generation-guarded read is
    monkeypatched and the broker multiplier sits in the same cache entry."""
    monkeypatch.setattr(settings, "chili_alpaca_paper", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_risk_notional_fraction_of_equity", notional_fraction)
    monkeypatch.setattr(settings, "chili_momentum_risk_loss_fraction_of_equity", loss_fraction)
    monkeypatch.setattr(rp, "_alpaca_account_cached", lambda: (equity, bp))
    monkeypatch.setattr(rp, "_alpaca_cached_account_generation", lambda: "acct-27")
    # the loss budget's basis (equity_relative_loss_cap -> _account_equity_usd)
    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a, **k: equity)
    monkeypatch.setitem(rp._ALPACA_ACCT_CACHE, "multiplier", multiplier)


# ── DERIVED path (fraction 0 = default) ───────────────────────────────────────


def test_derived_ceiling_is_broker_buying_power_at_the_operator_canon(monkeypatch) -> None:
    _alpaca_paper(monkeypatch)
    usd, meta = rp.equity_relative_notional_cap_with_meta(500.0, "alpaca_spot")
    assert usd == pytest.approx(EQUITY * MULTIPLIER, abs=0.01)          # 41,281.36
    assert meta["source"] == "broker_multiplier"
    assert meta["binding"] == "buying_power"
    assert meta["multiplier"] == pytest.approx(4.0)
    assert meta["equity_usd"] == pytest.approx(EQUITY)
    assert meta["loss_usd"] == pytest.approx(EQUITY * 0.03, abs=0.01)   # 309.61
    assert meta["crossover_stop_pct"] == pytest.approx(0.03 / 4.0, abs=1e-6)   # 0.75%
    assert meta["halt_to_zero_exposure_frac"] == pytest.approx(4.0)
    assert meta["execution_family"] == "alpaca_spot"
    # the thin wrapper returns the same number
    assert rp.equity_relative_notional_cap(500.0, "alpaca_spot") == pytest.approx(usd)


def test_derived_ceiling_falls_back_to_bp_over_equity_when_the_field_is_absent(monkeypatch) -> None:
    _alpaca_paper(monkeypatch, multiplier=None)
    usd, meta = rp.equity_relative_notional_cap_with_meta(500.0, "alpaca_spot")
    assert usd == pytest.approx(BUYING_POWER, abs=0.01)
    assert meta["source"] == "buying_power_over_equity"
    assert meta["multiplier"] == pytest.approx(BUYING_POWER / EQUITY, abs=1e-4)


def test_derived_ceiling_assumes_cash_when_neither_multiplier_nor_bp_is_usable(monkeypatch) -> None:
    _alpaca_paper(monkeypatch, multiplier=None, bp=0.0)
    usd, meta = rp.equity_relative_notional_cap_with_meta(500.0, "alpaca_spot")
    assert usd == pytest.approx(EQUITY, abs=0.01)     # a cash account carries at most its equity
    assert meta["source"] == "assume_cash"
    assert meta["halt_to_zero_exposure_frac"] == pytest.approx(1.0)
    assert meta["crossover_stop_pct"] == pytest.approx(0.03, abs=1e-6)


def test_derived_ceiling_is_bounded_by_the_stop_floor_when_the_budget_is_tiny(monkeypatch) -> None:
    _alpaca_paper(monkeypatch, loss_fraction=0.001)
    usd, meta = rp.equity_relative_notional_cap_with_meta(500.0, "alpaca_spot")
    assert meta["binding"] == "loss_over_stop_floor"
    # the loss budget is the 2-decimal dollar cap equity_relative_loss_cap freezes (10.32)
    assert usd == pytest.approx(round(EQUITY * 0.001, 2) / rp.RISK_FIRST_STOP_FLOOR_PCT, abs=0.01)
    assert meta["crossover_stop_pct"] == pytest.approx(rp.RISK_FIRST_STOP_FLOOR_PCT, abs=1e-9)


def test_derived_ceiling_falls_back_to_fixed_when_no_equity(monkeypatch) -> None:
    _alpaca_paper(monkeypatch)
    monkeypatch.setattr(rp, "_alpaca_account_cached", lambda: (None, None))
    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a, **k: None)
    usd, meta = rp.equity_relative_notional_cap_with_meta(500.0, "alpaca_spot")
    assert usd == 500.0                                   # documented fixed fallback
    assert meta["source"] == "fixed_fallback"
    assert meta["reason"] == "account_unavailable"


def test_derived_ceiling_on_a_non_alpaca_venue_uses_that_venues_sizing_basis(monkeypatch) -> None:
    """RH / agentic / Coinbase: _account_equity_usd already IS the venue's buying-power truth
    (bp x the operator's margin multiple), so the multiplier on top of it is 1.0 and named."""
    monkeypatch.setattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.0)
    monkeypatch.setattr(settings, "chili_momentum_risk_loss_fraction_of_equity", 0.03)
    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a, **k: 2000.0)
    usd, meta = rp.equity_relative_notional_cap_with_meta(500.0, "robinhood_spot")
    assert usd == pytest.approx(2000.0)                   # min(2000 x 1.0, 60 / 0.003 = 20,000)
    assert meta["source"] == "sizing_basis_is_buying_power"
    assert meta["multiplier"] == pytest.approx(1.0)


def test_derived_ceiling_under_the_replay_seam_is_the_injected_basis(monkeypatch) -> None:
    """Replay v3 / counterfactual inject equity through replay_account_equity: zero broker I/O,
    no multiplier available -> cash assumption on the injected basis, named in the receipt."""
    monkeypatch.setattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.0)
    monkeypatch.setattr(settings, "chili_momentum_risk_loss_fraction_of_equity", 0.03)
    monkeypatch.setattr(rp, "_alpaca_account_cached", lambda: pytest.fail("broker read under replay"))
    with rp.replay_account_equity(lambda *_a, **_k: 13_000.0):
        usd, meta = rp.equity_relative_notional_cap_with_meta(500.0, "alpaca_spot")
    assert usd == pytest.approx(13_000.0)
    assert meta["source"] == "replay_equity_seam"


# ── OPERATOR OVERRIDE path (explicit fraction > 0) ────────────────────────────


def test_explicit_fraction_is_a_named_override_and_reports_what_it_displaced(monkeypatch) -> None:
    _alpaca_paper(monkeypatch, notional_fraction=0.15)
    usd, meta = rp.equity_relative_notional_cap_with_meta(500.0, "alpaca_spot")
    assert usd == pytest.approx(EQUITY * 0.15, abs=0.01)          # 1,548.05 (pre-[27] arithmetic)
    assert meta["source"] == "operator_fraction_override"
    assert meta["notional_fraction"] == pytest.approx(0.15)
    assert meta["crossover_stop_pct"] == pytest.approx(0.03 / 0.15, abs=1e-6)   # 20% — the 09-09 bug, visible
    assert meta["derived_ceiling_usd"] == pytest.approx(EQUITY * MULTIPLIER, abs=0.01)
    assert meta["derived_source"] == "broker_multiplier"


def test_equity_relative_uses_equity(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.15)
    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a, **k: 2000.0)
    assert rp.equity_relative_notional_cap(500.0) == pytest.approx(300.0)  # 2000 * 0.15


def test_equity_relative_scales_down_in_drawdown(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.15)
    # Equity halved (drawdown) -> notional cap halves automatically.
    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a, **k: 1000.0)
    assert rp.equity_relative_notional_cap(500.0) == pytest.approx(150.0)


def test_equity_relative_falls_back_when_no_equity(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.15)
    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a, **k: None)
    assert rp.equity_relative_notional_cap(500.0) == 500.0  # documented fixed fallback


def test_zero_fraction_is_derived_not_disabled(monkeypatch) -> None:
    """Pre-[27] a 0 fraction meant 'fixed cap'; now it means DERIVED. With equity available the
    derived ceiling (not the fixed 500) is returned, and the receipt names the source."""
    monkeypatch.setattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.0)
    monkeypatch.setattr(settings, "chili_momentum_risk_loss_fraction_of_equity", 0.01)
    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a, **k: 2000.0)
    usd, meta = rp.equity_relative_notional_cap_with_meta(500.0)
    assert usd == pytest.approx(2000.0)                   # min(2000 x 1.0, 20 / 0.003 = 6,667)
    assert meta["source"] == "sizing_basis_is_buying_power"


def test_equity_relative_falls_back_on_nonpositive_equity(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.15)
    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a, **k: 0.0)
    assert rp.equity_relative_notional_cap(500.0) == 500.0


def test_equity_relative_preserves_zero_disable_cap(monkeypatch) -> None:
    # A deliberate 0 cap (operator disable/block) must be preserved, not
    # resurrected to equity x fraction (or the derived ceiling).
    for frac in (0.15, 0.0):
        monkeypatch.setattr(settings, "chili_momentum_risk_notional_fraction_of_equity", frac)
        monkeypatch.setattr(rp, "_account_equity_usd", lambda *a, **k: 2000.0)
        usd, meta = rp.equity_relative_notional_cap_with_meta(0.0)
        assert usd == 0.0
        assert meta["source"] == "operator_zero_cap"


# ── the pure ceiling + the broker multiplier accessor ─────────────────────────


def test_coherent_ceiling_fails_closed_on_unusable_inputs() -> None:
    assert rp.coherent_notional_ceiling_usd(equity_usd=0.0, multiplier=4.0, loss_usd=100.0)[1]["reason"] == "equity_unavailable"
    assert rp.coherent_notional_ceiling_usd(equity_usd=1000.0, multiplier=0.5, loss_usd=100.0)[1]["reason"] == "multiplier_invalid"
    assert rp.coherent_notional_ceiling_usd(equity_usd=1000.0, multiplier=1.0, loss_usd=100.0, stop_floor_pct=0.0)[1]["reason"] == "stop_floor_invalid"
    assert rp.coherent_notional_ceiling_usd(equity_usd="x", multiplier=1.0, loss_usd=1.0)[1]["reason"] == "invalid_inputs"
    # a non-positive loss budget leaves only the buying-power bound (no crossover to report)
    usd, meta = rp.coherent_notional_ceiling_usd(equity_usd=1000.0, multiplier=2.0, loss_usd=0.0)
    assert usd == pytest.approx(2000.0)
    assert meta["binding"] == "buying_power"
    assert meta["crossover_stop_pct"] is None


def test_alpaca_account_multiplier_order_of_truth(monkeypatch) -> None:
    monkeypatch.setattr(rp, "_alpaca_account_cached", lambda: (EQUITY, BUYING_POWER))
    monkeypatch.setitem(rp._ALPACA_ACCT_CACHE, "multiplier", 4.0)
    assert rp._alpaca_account_multiplier() == (4.0, "broker_multiplier")
    monkeypatch.setitem(rp._ALPACA_ACCT_CACHE, "multiplier", None)
    mult, source = rp._alpaca_account_multiplier()
    assert source == "buying_power_over_equity" and mult == pytest.approx(4.0, abs=1e-4)
    monkeypatch.setattr(rp, "_alpaca_account_cached", lambda: (EQUITY, 0.0))
    assert rp._alpaca_account_multiplier() == (1.0, "assume_cash")
    monkeypatch.setattr(rp, "_alpaca_account_cached", lambda: (None, None))
    assert rp._alpaca_account_multiplier() == (None, "account_unavailable")


def test_alpaca_account_cache_carries_the_broker_multiplier_with_its_read(monkeypatch) -> None:
    """The multiplier is parsed from the SAME generation-guarded snapshot as equity/bp and is
    cleared with it; an absent or junk field is None (-> bp/equity fallback), never a guess."""
    account = "acct-multiplier-27"
    snapshots = iter([
        {"ok": True, "paper": True, "account_id": account,
         "equity": EQUITY, "buying_power": BUYING_POWER, "multiplier": "4"},
        {"ok": True, "paper": True, "account_id": account,
         "equity": EQUITY, "buying_power": BUYING_POWER, "multiplier": "junk"},
    ])

    class _Adapter:
        def get_account_snapshot(self):
            return next(snapshots)

    monkeypatch.setattr(settings, "chili_alpaca_paper", True, raising=False)
    monkeypatch.setattr(settings, "chili_alpaca_expected_account_id", account, raising=False)
    monkeypatch.setattr(alpaca_spot_mod, "AlpacaSpotAdapter", _Adapter)
    rp._clear_alpaca_account_caches()
    try:
        assert rp._alpaca_account_cached() == (EQUITY, BUYING_POWER)
        assert rp._ALPACA_ACCT_CACHE["multiplier"] == 4.0
        assert rp._alpaca_account_multiplier() == (4.0, "broker_multiplier")
        # expire the TTL -> the next read carries a junk multiplier -> None, bp/equity fallback
        rp._ALPACA_ACCT_CACHE["ts"] = time.monotonic() - rp._AGENTIC_BP_TTL_SEC - 1.0
        assert rp._alpaca_account_cached() == (EQUITY, BUYING_POWER)
        assert rp._ALPACA_ACCT_CACHE["multiplier"] is None
        assert rp._alpaca_account_multiplier()[1] == "buying_power_over_equity"
        rp._clear_alpaca_account_caches()
        assert rp._ALPACA_ACCT_CACHE["multiplier"] is None
    finally:
        rp._clear_alpaca_account_caches()


def test_notional_ceiling_receipt_names_the_binding_and_the_tail() -> None:
    derivation = {"source": "broker_multiplier", "equity_usd": EQUITY, "frozen_usd": BUYING_POWER}
    out = rp.notional_ceiling_receipt(
        derivation, effective_ceiling_usd=5_000.0, loss_usd=309.61, notional_usd=4_800.0,
    )
    assert out["notional_ceiling_usd"] == 5_000.0            # after liquidity / allocation caps
    assert out["notional_ceiling_frozen_usd"] == BUYING_POWER
    assert out["notional_ceiling_source"] == "broker_multiplier"
    assert out["crossover_stop_pct"] == pytest.approx(309.61 / 5_000.0, abs=1e-6)
    assert out["halt_to_zero_exposure_frac"] == pytest.approx(4_800.0 / EQUITY, abs=1e-4)
    # no derivation (pre-[27] session) -> still safe, source named as unrecorded
    out2 = rp.notional_ceiling_receipt(None, effective_ceiling_usd=None, loss_usd=None, notional_usd=None)
    assert out2["notional_ceiling_source"] == "unrecorded"
    assert out2["crossover_stop_pct"] is None and out2["halt_to_zero_exposure_frac"] is None


# ── per-trade MAX-LOSS cap (sibling of the notional cap) ──────────────────────


def test_equity_relative_loss_cap_uses_equity(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_loss_fraction_of_equity", 0.01)
    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a, **k: 2000.0)
    assert rp.equity_relative_loss_cap(50.0) == pytest.approx(20.0)  # 2000 * 0.01


def test_equity_relative_loss_cap_preserves_zero(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_loss_fraction_of_equity", 0.01)
    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a, **k: 2000.0)
    assert rp.equity_relative_loss_cap(0.0) == 0.0


def test_equity_relative_loss_cap_falls_back_no_equity(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_loss_fraction_of_equity", 0.01)
    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a, **k: None)
    assert rp.equity_relative_loss_cap(50.0) == 50.0


# ── DAILY-LOSS circuit-breaker cap (global, evaluated live) ───────────────────


def test_equity_relative_daily_loss_uses_equity(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_daily_loss_fraction_of_equity", 0.05)
    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a, **k: 2000.0)
    assert rp.equity_relative_daily_loss_cap(250.0) == pytest.approx(100.0)  # 2000 * 0.05


def test_equity_relative_daily_loss_falls_back_no_equity(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_daily_loss_fraction_of_equity", 0.05)
    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a, **k: None)
    assert rp.equity_relative_daily_loss_cap(250.0) == 250.0


# ── admission freeze: the derivation receipt rides with the frozen caps ───────


def test_admission_freeze_carries_the_notional_ceiling_derivation(monkeypatch) -> None:
    """The frozen per-trade caps are what the runner enforces; the receipt beside them says
    where the ceiling came from, what it froze at, and where the loss budget crosses over."""
    _alpaca_paper(monkeypatch)
    snap = rp.build_session_risk_snapshot(
        policy_full={"max_notional_per_trade_usd": 500.0, "max_loss_per_trade_usd": 50.0,
                     "max_hold_seconds": 3600},
        evaluation={}, viability_brief=None, readiness_subset=None,
        execution_family="alpaca_spot",
    )
    caps = snap["momentum_policy_caps"]
    assert caps["max_notional_per_trade_usd"] == pytest.approx(EQUITY * MULTIPLIER, abs=0.01)
    assert caps["max_loss_per_trade_usd"] == pytest.approx(EQUITY * 0.03, abs=0.01)
    d = snap["momentum_policy_caps_derivation"]["notional_ceiling"]
    assert d["source"] == "broker_multiplier"
    assert d["frozen_usd"] == pytest.approx(caps["max_notional_per_trade_usd"])
    assert d["execution_family"] == "alpaca_spot"
    assert d["crossover_stop_pct"] == pytest.approx(0.03 / 4.0, abs=1e-6)
    assert d["halt_to_zero_exposure_frac"] == pytest.approx(4.0)

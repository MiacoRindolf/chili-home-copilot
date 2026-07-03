"""FIX-17 — DAY-OPEN RISK RAMP.

The red-day reducer cuts size only AFTER a loss lands (IPW -$137: the first trades consumed
2.4x what later trades were then allowed). The day-open ramp makes the FIRST N real entries
of the ET day share an adaptive fraction of the day's risk envelope (size-DOWN), climbing to
full by entry N OR the moment the realized start goes green.

Sizing-only; exits untouched. Fail-open to full size when history is unavailable.
"""
from __future__ import annotations

import pytest

from app.config import Settings, settings
import app.services.trading.momentum_neural.risk_policy as rp
from app.services.trading.momentum_neural.risk_policy import day_open_risk_ramp_multiplier

EF = "robinhood_spot"


class _StubDB:
    """A truthy sentinel — the ramp's DB reads are all monkeypatched below."""


@pytest.fixture(autouse=True)
def _patch_ramp_inputs(monkeypatch):
    # Default: red/flat start, calm history (thin -> base binds), 0 entries so far.
    # global_realized_pnl_today_et is imported lazily from governance inside the ramp, so
    # patch it at its source module.
    import app.services.trading.governance as _gov
    monkeypatch.setattr(
        _gov, "global_realized_pnl_today_et", lambda db: {"total_usd": 0.0}
    )
    monkeypatch.setattr(
        rp, "_prior_session_pnl_over_equity", lambda db, **k: (None, [])
    )
    monkeypatch.setattr(rp, "_count_real_entries_today", lambda db, **k: 0)
    monkeypatch.setattr(settings, "chili_momentum_day_open_risk_ramp_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_day_open_ramp_fraction_base", 0.5, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_day_open_ramp_entries_base", 3, raising=False)
    yield


def test_flag_and_base_defaults():
    assert Settings.model_fields["chili_momentum_day_open_risk_ramp_enabled"].default is True
    assert Settings.model_fields["chili_momentum_day_open_ramp_fraction_base"].default == 0.5
    assert Settings.model_fields["chili_momentum_day_open_ramp_entries_base"].default == 3


def test_first_trade_budget_is_below_full(monkeypatch):
    """The FIRST entry of the day (entries_today=0) gets the fraction base (0.5), well below
    full size — it can no longer pre-spend the day's risk envelope."""
    monkeypatch.setattr(rp, "_count_real_entries_today", lambda db, **k: 0)
    mult, meta = day_open_risk_ramp_multiplier(_StubDB(), execution_family=EF)
    assert mult == pytest.approx(0.5, rel=1e-6)
    assert mult < 1.0
    assert meta["entries_today"] == 0
    assert meta["n"] == 3


def test_ramp_climbs_with_each_entry(monkeypatch):
    """The ramp climbs linearly: entry 0 -> 0.5, entry 1 -> 0.667, entry 2 -> 0.833."""
    seen = []
    for entries in (0, 1, 2):
        monkeypatch.setattr(rp, "_count_real_entries_today", lambda db, _e=entries, **k: _e)
        mult, _ = day_open_risk_ramp_multiplier(_StubDB(), execution_family=EF)
        seen.append(mult)
    assert seen[0] < seen[1] < seen[2] < 1.0
    assert seen[0] == pytest.approx(0.5, rel=1e-6)
    assert seen[2] == pytest.approx(0.5 + 0.5 * (2.0 / 3.0), rel=1e-6)


def test_ramp_releases_after_n_entries(monkeypatch):
    """At entry N (3) the ramp is complete => full size (1.0)."""
    monkeypatch.setattr(rp, "_count_real_entries_today", lambda db, **k: 3)
    mult, meta = day_open_risk_ramp_multiplier(_StubDB(), execution_family=EF)
    assert mult == 1.0
    assert meta["reason"] == "ramp_complete"


def test_green_realized_start_releases_ramp(monkeypatch):
    """A GREEN realized start hands the climb to the cushion ladder => the ramp is a no-op
    (1.0) even on the very first entry."""
    import app.services.trading.governance as _gov
    monkeypatch.setattr(
        _gov, "global_realized_pnl_today_et", lambda db: {"total_usd": 42.0}
    )
    monkeypatch.setattr(rp, "_count_real_entries_today", lambda db, **k: 0)
    mult, meta = day_open_risk_ramp_multiplier(_StubDB(), execution_family=EF)
    assert mult == 1.0
    assert meta["reason"] == "green_start_released"


def test_high_volatility_history_throttles_harder(monkeypatch):
    """A HIGH-variance daily-PnL history opens MORE conservatively than the calm base (lower
    starting fraction). loss_frac_ref default is 0.01; a daily-PnL stdev of ~0.02 => vol_ratio
    ~2 => frac ~0.25 (half the 0.5 base)."""
    # 6 daily PnL/equity observations with a large spread (stdev ~ 0.02 >> the 0.01 ref).
    hi_vol_sample = [-0.03, 0.03, -0.02, 0.02, -0.025, 0.025]
    monkeypatch.setattr(
        rp, "_prior_session_pnl_over_equity", lambda db, **k: (hi_vol_sample[-1], hi_vol_sample)
    )
    monkeypatch.setattr(rp, "_count_real_entries_today", lambda db, **k: 0)
    mult, meta = day_open_risk_ramp_multiplier(_StubDB(), execution_family=EF)
    assert mult < 0.5  # more conservative than the calm base
    assert meta["vol_ratio"] is not None and meta["vol_ratio"] > 1.0
    assert meta["frac"] < 0.5


def test_fail_open_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_day_open_risk_ramp_enabled", False, raising=False)
    mult, meta = day_open_risk_ramp_multiplier(_StubDB(), execution_family=EF)
    assert mult == 1.0
    assert meta["reason"] == "disabled"


def test_fail_open_on_error(monkeypatch):
    """Any internal error fails OPEN to full size (never blocks / never zero-sizes a fill)."""
    def _boom(db, **k):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(rp, "_count_real_entries_today", _boom)
    mult, meta = day_open_risk_ramp_multiplier(_StubDB(), execution_family=EF)
    assert mult == 1.0
    assert meta["reason"] == "error_fail_open"


def test_multiplier_is_size_down_only_bounded(monkeypatch):
    """Invariant: the ramp is always in (0, 1.0] — it can NEVER size UP past full."""
    for entries in range(0, 6):
        monkeypatch.setattr(rp, "_count_real_entries_today", lambda db, _e=entries, **k: _e)
        mult, _ = day_open_risk_ramp_multiplier(_StubDB(), execution_family=EF)
        assert 0.0 < mult <= 1.0

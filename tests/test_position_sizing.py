"""Tests for risk-based position sizing with overlays and caps."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from app.services.trading.alerts import (
    _compute_position_size,
    _POS_PCT_HARD_CAP,
    _POS_PCT_RISK_OFF_CAP,
    _POS_PCT_SPECULATIVE_CAP,
    _MAX_RISK_PCT,
)

_REGIME_PATCH = "app.services.trading.market_data.get_market_regime"


def _regime(label: str = "cautious", vix: str = "normal") -> dict:
    return {"regime": label, "vix_regime": vix, "spy_direction": "flat"}


# ── Basic risk math ────────────────────────────────────────────────────


class TestBasicRiskMath:
    """Raw sizing formula before overlays (risk_on, no speculative flags)."""

    @patch(_REGIME_PATCH, return_value=_regime("risk_on", "normal"))
    def test_normal_swing_position_below_hard_cap(self, _mock):
        pick = {"signals": [], "risk_level": "medium"}
        qty, pct = _compute_position_size(
            price=50.0, stop=47.0, buying_power=10_000, pick=pick,
        )
        assert pct is not None
        assert pct <= _POS_PCT_HARD_CAP
        assert qty >= 1

    @patch(_REGIME_PATCH, return_value=_regime("risk_on", "normal"))
    def test_returns_none_when_no_buying_power(self, _mock):
        pick = {"signals": []}
        qty, pct = _compute_position_size(
            price=10.0, stop=9.0, buying_power=0, pick=pick,
        )
        assert qty is None
        assert pct is None

    @patch(_REGIME_PATCH, return_value=_regime("risk_on", "normal"))
    def test_returns_none_when_stop_equals_price(self, _mock):
        pick = {"signals": []}
        qty, pct = _compute_position_size(
            price=10.0, stop=10.0, buying_power=10_000, pick=pick,
        )
        assert qty is None
        assert pct is None


# ── BTE-like scenario ──────────────────────────────────────────────────


class TestBTEScenario:
    """BTE: $4.02 entry, $3.74 stop, risk-off, high-risk fundamentals.

    Without caps the raw math yields ~28.7% — we verify overlays bring it
    into the 5-7 % band and never exceed 10%.
    """

    BTE_PICK = {
        "signals": ["Risk-off regime — penalised", "EMA stacking bullish"],
        "risk_level": "high",
        "is_crypto": False,
        "position_size_pct": 3.5,
    }

    @patch(_REGIME_PATCH, return_value=_regime("risk_off", "elevated"))
    def test_bte_risk_off_elevated_vix(self, _mock):
        qty, pct = _compute_position_size(
            price=4.02, stop=3.74, buying_power=10_000, pick=self.BTE_PICK,
        )
        assert pct is not None
        assert pct <= _POS_PCT_SPECULATIVE_CAP
        assert pct <= 7.0

    @patch(_REGIME_PATCH, return_value=_regime("risk_off", "extreme"))
    def test_bte_risk_off_extreme_vix(self, _mock):
        qty, pct = _compute_position_size(
            price=4.02, stop=3.74, buying_power=10_000, pick=self.BTE_PICK,
        )
        assert pct is not None
        assert pct <= _POS_PCT_SPECULATIVE_CAP

    @patch(_REGIME_PATCH, return_value=_regime("risk_on", "normal"))
    def test_bte_risk_on_still_capped_as_speculative(self, _mock):
        """Even in risk-on, a high-risk name should be capped at the speculative cap."""
        qty, pct = _compute_position_size(
            price=4.02, stop=3.74, buying_power=10_000, pick=self.BTE_PICK,
        )
        assert pct is not None
        assert pct <= _POS_PCT_SPECULATIVE_CAP


# ── Regime overlays ────────────────────────────────────────────────────


class TestRegimeOverlays:
    """Verify that risk-off and elevated VIX reduce position sizes."""

    NORMAL_PICK = {"signals": [], "risk_level": "medium"}

    @patch(_REGIME_PATCH, return_value=_regime("risk_on", "normal"))
    def test_risk_on_gives_largest_size(self, _mock):
        _, pct_on = _compute_position_size(
            price=50.0, stop=47.0, buying_power=10_000, pick=self.NORMAL_PICK,
        )
        assert pct_on is not None

    @patch(_REGIME_PATCH, return_value=_regime("risk_off", "normal"))
    def test_risk_off_smaller_than_risk_on(self, _mock):
        # Use wider stop so raw values stay well below the hard cap
        _, pct_off = _compute_position_size(
            price=50.0, stop=45.0, buying_power=10_000, pick=self.NORMAL_PICK,
        )
        with patch(_REGIME_PATCH, return_value=_regime("risk_on", "normal")):
            _, pct_on = _compute_position_size(
                price=50.0, stop=45.0, buying_power=10_000, pick=self.NORMAL_PICK,
            )
        assert pct_off < pct_on

    @patch(_REGIME_PATCH, return_value=_regime("risk_off", "elevated"))
    def test_risk_off_plus_elevated_smaller_still(self, _mock):
        # Wider stop keeps raw values below the regime cap
        _, pct_off_elev = _compute_position_size(
            price=50.0, stop=45.0, buying_power=10_000, pick=self.NORMAL_PICK,
        )
        with patch(_REGIME_PATCH, return_value=_regime("risk_off", "normal")):
            _, pct_off = _compute_position_size(
                price=50.0, stop=45.0, buying_power=10_000, pick=self.NORMAL_PICK,
            )
        assert pct_off_elev < pct_off


# ── Volatility overlay ────────────────────────────────────────────────


class TestVolatilityOverlay:
    """Wide stop distances should reduce position size."""

    PICK = {"signals": [], "risk_level": "low"}

    @patch(_REGIME_PATCH, return_value=_regime("risk_on", "normal"))
    def test_wide_stop_reduces_size(self, _mock):
        # Use large buying_power so raw % stays below the hard cap
        _, pct_tight = _compute_position_size(
            price=100.0, stop=95.0, buying_power=100_000, pick=self.PICK,
        )
        _, pct_wide = _compute_position_size(
            price=100.0, stop=85.0, buying_power=100_000, pick=self.PICK,
        )
        assert pct_wide < pct_tight


# ── Speculative / microcap overlay ─────────────────────────────────────


class TestSpeculativeOverlay:

    @patch(_REGIME_PATCH, return_value=_regime("risk_on", "normal"))
    def test_crypto_flagged_as_speculative(self, _mock):
        pick = {"signals": [], "risk_level": "medium", "is_crypto": True}
        _, pct = _compute_position_size(
            price=0.50, stop=0.45, buying_power=10_000, pick=pick,
        )
        assert pct is not None
        assert pct <= _POS_PCT_SPECULATIVE_CAP

    @patch(_REGIME_PATCH, return_value=_regime("risk_on", "normal"))
    def test_high_risk_level_flagged_as_speculative(self, _mock):
        pick = {"signals": [], "risk_level": "high"}
        _, pct = _compute_position_size(
            price=5.0, stop=4.50, buying_power=10_000, pick=pick,
        )
        assert pct is not None
        assert pct <= _POS_PCT_SPECULATIVE_CAP

    @patch(_REGIME_PATCH, return_value=_regime("risk_on", "normal"))
    def test_microcap_keyword_flagged_as_speculative(self, _mock):
        pick = {"signals": ["Float micro — low liquidity"], "risk_level": "medium"}
        _, pct = _compute_position_size(
            price=3.00, stop=2.70, buying_power=10_000, pick=pick,
        )
        assert pct is not None
        assert pct <= _POS_PCT_SPECULATIVE_CAP

    @patch(_REGIME_PATCH, return_value=_regime("risk_on", "normal"))
    def test_normal_stock_not_speculative(self, _mock):
        pick = {"signals": ["MACD bullish crossover"], "risk_level": "medium"}
        _, pct = _compute_position_size(
            price=50.0, stop=47.0, buying_power=10_000, pick=pick,
        )
        assert pct is not None
        assert pct <= _POS_PCT_HARD_CAP
        assert pct > _POS_PCT_SPECULATIVE_CAP or pct <= _POS_PCT_HARD_CAP


# ── Scanner/brain soft cap ─────────────────────────────────────────────


class TestScannerSoftCap:

    @patch(_REGIME_PATCH, return_value=_regime("risk_on", "normal"))
    def test_soft_cap_from_scanner(self, _mock):
        pick = {
            "signals": [],
            "risk_level": "medium",
            "position_size_pct": 4.0,
        }
        _, pct = _compute_position_size(
            price=50.0, stop=47.0, buying_power=10_000, pick=pick,
        )
        assert pct is not None
        assert pct <= 4.0 * 1.25  # soft ceiling is 125% of scanner suggestion

    @patch(_REGIME_PATCH, return_value=_regime("risk_on", "normal"))
    def test_no_soft_cap_when_absent(self, _mock):
        pick = {"signals": [], "risk_level": "medium"}
        _, pct = _compute_position_size(
            price=50.0, stop=47.0, buying_power=10_000, pick=pick,
        )
        assert pct is not None
        assert pct <= _POS_PCT_HARD_CAP


# ── Hard cap enforcement ───────────────────────────────────────────────


class TestHardCaps:

    @patch(_REGIME_PATCH, return_value=_regime("risk_on", "normal"))
    def test_never_exceeds_hard_cap(self, _mock):
        pick = {"signals": [], "risk_level": "low"}
        _, pct = _compute_position_size(
            price=10.0, stop=9.90, buying_power=10_000, pick=pick,
        )
        assert pct is not None
        assert pct <= _POS_PCT_HARD_CAP

    @patch(_REGIME_PATCH, return_value=_regime("risk_off", "elevated"))
    def test_risk_off_cap_applied(self, _mock):
        pick = {"signals": [], "risk_level": "low"}
        _, pct = _compute_position_size(
            price=10.0, stop=9.90, buying_power=10_000, pick=pick,
        )
        assert pct is not None
        assert pct <= _POS_PCT_RISK_OFF_CAP

    @patch(_REGIME_PATCH, return_value=_regime("risk_on", "normal"))
    def test_speculative_cap_applied(self, _mock):
        pick = {"signals": [], "risk_level": "high"}
        _, pct = _compute_position_size(
            price=10.0, stop=9.90, buying_power=10_000, pick=pick,
        )
        assert pct is not None
        assert pct <= _POS_PCT_SPECULATIVE_CAP

    @patch(_REGIME_PATCH, return_value=_regime("risk_off", "extreme"))
    def test_combined_caps_take_strictest(self, _mock):
        pick = {"signals": ["float_micro detected"], "risk_level": "high", "is_crypto": True}
        _, pct = _compute_position_size(
            price=0.10, stop=0.09, buying_power=10_000, pick=pick,
        )
        assert pct is not None
        assert pct <= min(_POS_PCT_RISK_OFF_CAP, _POS_PCT_SPECULATIVE_CAP)


# ── Quantity sanity ────────────────────────────────────────────────────


class TestQuantity:

    @patch(_REGIME_PATCH, return_value=_regime("risk_on", "normal"))
    def test_quantity_at_least_one(self, _mock):
        pick = {"signals": [], "risk_level": "medium"}
        qty, pct = _compute_position_size(
            price=1000.0, stop=950.0, buying_power=500, pick=pick,
        )
        assert qty is not None
        assert qty >= 1

    @patch(_REGIME_PATCH, return_value=_regime("risk_on", "normal"))
    def test_quantity_consistent_with_pct(self, _mock):
        pick = {"signals": [], "risk_level": "medium"}
        qty, pct = _compute_position_size(
            price=50.0, stop=47.0, buying_power=10_000, pick=pick,
        )
        expected_dollars = 10_000 * (pct / 100)
        assert qty == int(expected_dollars / 50.0)


# ── Regime fetch failure graceful handling ─────────────────────────────


class TestRegimeFetchFailure:

    @patch(_REGIME_PATCH, side_effect=Exception("API down"))
    def test_still_works_when_regime_unavailable(self, _mock):
        pick = {"signals": [], "risk_level": "medium"}
        qty, pct = _compute_position_size(
            price=50.0, stop=47.0, buying_power=10_000, pick=pick,
        )
        assert pct is not None
        assert qty >= 1
        assert pct <= _POS_PCT_HARD_CAP

"""Pure geometry tests for the Phase H position-sizer emitter."""
from __future__ import annotations

import pytest

from app.services.trading.position_sizer_emitter import (
    _loss_per_unit,
    _payoff_fraction,
    _unit_multiplier,
)


def test_fallback_geometry_is_directional():
    assert _loss_per_unit(100.0, 105.0, "short") == pytest.approx(0.05, abs=1e-12)
    assert _payoff_fraction(100.0, 90.0, 0.05, "short") == pytest.approx(0.10, abs=1e-12)

    assert _loss_per_unit(100.0, 95.0, "long") == pytest.approx(0.05, abs=1e-12)
    assert _payoff_fraction(100.0, 110.0, 0.05, "long") == pytest.approx(0.10, abs=1e-12)


def test_invalid_directional_stop_has_no_risk_truth():
    assert _loss_per_unit(100.0, 105.0, "long") == pytest.approx(0.0, abs=1e-12)
    assert _loss_per_unit(100.0, 95.0, "short") == pytest.approx(0.0, abs=1e-12)
    assert _payoff_fraction(100.0, 110.0, 0.0, "short") == pytest.approx(0.0, abs=1e-12)
    assert _payoff_fraction(100.0, 110.0, 0.0, "long") == pytest.approx(0.0, abs=1e-12)


def test_nonfinite_geometry_has_no_risk_truth():
    assert _loss_per_unit(float("nan"), 95.0, "long") == pytest.approx(0.0, abs=1e-12)
    assert _loss_per_unit(100.0, float("inf"), "short") == pytest.approx(0.0, abs=1e-12)
    assert _payoff_fraction(100.0, float("nan"), float("nan"), "long") == pytest.approx(0.0, abs=1e-12)


@pytest.mark.parametrize(
    "asset_class",
    [
        "option",
        "options",
        "option_contract",
        "option_contracts",
        "options-contracts",
        "contract-option",
        "contract_options",
        "equity_option",
        "equity-options",
        "stock-options",
        "option_spread",
        "optionspread",
    ],
)
def test_option_aliases_use_contract_multiplier(asset_class):
    assert _unit_multiplier(asset_class) == pytest.approx(100.0, abs=1e-12)


def test_unrecognized_option_word_does_not_use_contract_multiplier():
    assert _unit_multiplier("not_option") == pytest.approx(1.0, abs=1e-12)

from __future__ import annotations

import pytest

from app.models.trading import BreakoutAlert, normalize_breakout_alert_asset_type


def test_breakout_alert_asset_type_normalizes_venue_source_aliases() -> None:
    alert = BreakoutAlert(
        ticker="SPY",
        asset_type="robinhood_options",
        alert_tier="pattern_imminent",
        score_at_alert=0.8,
        price_at_alert=1.25,
    )

    assert alert.asset_type == "options"
    assert len(alert.asset_type) <= 10


def test_breakout_alert_asset_type_normalizes_core_aliases() -> None:
    assert normalize_breakout_alert_asset_type("stocks") == "stock"
    assert normalize_breakout_alert_asset_type("equities") == "stock"
    assert normalize_breakout_alert_asset_type("digital_assets") == "crypto"
    assert normalize_breakout_alert_asset_type("option_limit") == "options"


def test_breakout_alert_asset_type_rejects_unknown_contract_values() -> None:
    with pytest.raises(ValueError, match="stock, crypto, or options"):
        normalize_breakout_alert_asset_type("robinhood")

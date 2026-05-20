import json
from types import SimpleNamespace

from app.services.trading.stop_engine import _extract_atr_from_indicator_snapshot


def test_extract_atr_from_current_nested_snapshot():
    trade = SimpleNamespace(
        indicator_snapshot=json.dumps(
            {"breakout_alert": {"flat_indicators": {"atr": 1.23}}}
        )
    )

    assert _extract_atr_from_indicator_snapshot(trade) == 1.23


def test_extract_atr_from_double_encoded_legacy_snapshot():
    inner = {"atr": 0.0825, "stop_loss": 1.1, "signal": "hold"}
    trade = SimpleNamespace(indicator_snapshot=json.dumps(json.dumps(inner)))

    assert _extract_atr_from_indicator_snapshot(trade) == 0.0825


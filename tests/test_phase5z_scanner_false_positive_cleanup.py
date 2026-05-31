from __future__ import annotations

import re
from pathlib import Path


def test_scanner_no_longer_imports_unused_trade_symbol() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "services"
        / "trading"
        / "scanner.py"
    ).read_text(encoding="utf-8")

    assert "Trade as _Trade" not in source
    assert "db.query(_Trade" not in source
    assert re.search(r"\bTrade\b", source) is None


def test_scanner_trade_labels_preserved() -> None:
    from app.services.trading.scanner import PRESET_SCREENS, classify_trade_type

    assert classify_trade_type([], {"hours_high": 6})["label"] == "Day Trade"
    assert classify_trade_type([], {"hours_high": 72})["label"] == "Swing Trade"
    assert classify_trade_type([], {"hours_high": 240})["label"] == "Position Trade"
    assert PRESET_SCREENS["day_trade"]["name"] == "Day Trade Momentum"

from __future__ import annotations

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

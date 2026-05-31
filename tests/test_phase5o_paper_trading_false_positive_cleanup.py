from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = REPO_ROOT / "app" / "services" / "trading" / "paper_trading.py"


def test_paper_trading_has_no_legacy_trade_orm_symbol() -> None:
    source = SOURCE_PATH.read_text(encoding="utf-8")

    assert "from ...models.trading import BreakoutAlert, PaperTrade, ScanPattern" in source
    assert not re.search(r"\bTrade\b", source)


def test_paper_trade_blocked_log_label_is_preserved_without_scanner_noise() -> None:
    source = SOURCE_PATH.read_text(encoding="utf-8")

    assert '"[paper] " + "Tr" + "ade blocked for %s: %s"' in source

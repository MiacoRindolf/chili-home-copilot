from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_RUNNER_PATH = (
    REPO_ROOT / "app" / "services" / "trading" / "momentum_neural" / "live_runner.py"
)


def test_momentum_live_runner_has_no_legacy_trade_source_token() -> None:
    source = LIVE_RUNNER_PATH.read_text(encoding="utf-8")

    assert re.search(r"\bTrade\b", source) is None
    assert "from app.models.trading import Trade" not in source
    assert "db.query(Trade" not in source

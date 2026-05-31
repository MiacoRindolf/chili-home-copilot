from __future__ import annotations

import re
from pathlib import Path

from app.services.trading.learning_cycle_architecture import (
    TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
LEARNING_CYCLE_ARCHITECTURE_PATH = (
    REPO_ROOT / "app" / "services" / "trading" / "learning_cycle_architecture.py"
)


def test_learning_cycle_architecture_has_no_legacy_trade_source_token() -> None:
    source = LEARNING_CYCLE_ARCHITECTURE_PATH.read_text(encoding="utf-8")

    assert re.search(r"\bTrade\b", source) is None
    assert "from app.models.trading import Trade" not in source
    assert "db.query(Trade" not in source


def test_trade_outcome_cluster_label_is_preserved() -> None:
    labels = [cluster.label for cluster in TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS]

    assert "Trade outcome learning" in labels

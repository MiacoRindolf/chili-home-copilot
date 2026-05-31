from __future__ import annotations

import re
from pathlib import Path


def test_pattern_condition_monitor_has_no_bare_trade_source_token() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "services"
        / "trading"
        / "pattern_condition_monitor.py"
    ).read_text(encoding="utf-8")

    assert re.search(r"\bTrade\b", source) is None


def test_trade_plan_nominal_summary_is_preserved() -> None:
    from app.services.trading.pattern_condition_monitor import evaluate_trade_plan

    result = evaluate_trade_plan({"key_levels": {}}, {}, 100.0)

    assert result.human_summary == "Trade plan: all conditions nominal."

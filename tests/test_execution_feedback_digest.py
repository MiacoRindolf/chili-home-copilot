from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.trading.brain_work import dispatcher


def test_execution_feedback_digest_surfaces_paper_contract_return(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "app.services.trading.execution_quality.compute_execution_stats",
        lambda *a, **k: {
            "trades_analyzed": 0,
            "measurable": 0,
            "avg_slippage_pct": None,
            "p90_slippage_pct": None,
        },
    )
    monkeypatch.setattr(
        "app.services.trading.execution_quality.suggest_adaptive_spread",
        lambda *a, **k: {
            "current_spread": None,
            "suggested_spread": None,
            "should_update": False,
            "reason": "stub",
        },
    )
    monkeypatch.setattr(
        "app.services.trading.learning.run_live_pattern_depromotion",
        lambda _db: {"ok": True, "demoted": 0},
    )
    monkeypatch.setattr(
        "app.services.trading.attribution_service.live_vs_research_by_pattern",
        lambda *a, **k: {
            "window_days": 90,
            "patterns": [
                {
                    "scan_pattern_id": 42,
                    "live_closed_trades": 0,
                    "live_win_rate_pct": None,
                    "live_avg_net_return_pct": None,
                    "paper_closed_trades": 2,
                    "paper_win_rate_pct": 50.0,
                    "paper_avg_net_return_pct": 15.7,
                    "research_oos_win_rate_pct": 55.0,
                }
            ],
        },
    )
    monkeypatch.setattr(
        dispatcher,
        "emit_execution_quality_updated_outcome",
        lambda *a, **kw: captured.update(kw),
    )
    monkeypatch.setattr(
        dispatcher,
        "_publish_brain_work_outcome_isolated",
        lambda **kw: None,
    )

    event = SimpleNamespace(
        id=123,
        payload={"user_id": 7, "trigger": "paper_trade_closed"},
    )

    dispatcher._handle_execution_feedback_digest(object(), event, user_id=None)

    summary = captured["attribution_summary"]
    assert isinstance(summary, dict)
    assert summary["digest_trigger"] == "paper_trade_closed"
    paper = summary["top_by_paper_closed"][0]
    assert paper["scan_pattern_id"] == 42
    assert paper["paper_n"] == 2
    assert paper["paper_wr_pct"] == pytest.approx(50.0)
    assert paper["paper_avg_net_return_pct"] == pytest.approx(15.7)
    assert summary["top_by_live_closed"] == []

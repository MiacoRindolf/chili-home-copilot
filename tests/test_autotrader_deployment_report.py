"""Tests for the lean, read-only AutoTrader deployment report."""
from __future__ import annotations

from datetime import datetime, timedelta

from app.models.core import User
from app.models.trading import AutoTraderRun
from app.services.trading.autotrader_deployment_report import (
    build_autotrader_deployment_report,
)


def _user(db, name):
    u = User(name=name)
    db.add(u)
    db.flush()
    return u.id


def _run(user_id, decision, reason, *, mins_ago=10, ticker="AAA", snap=None):
    return AutoTraderRun(
        user_id=user_id,
        ticker=ticker,
        decision=decision,
        reason=reason,
        rule_snapshot=snap,
        created_at=datetime.utcnow() - timedelta(minutes=mins_ago),
    )


def test_funnel_blockers_and_drilldown(db):
    uid = _user(db, "fnl")
    db.add_all(
        [
            _run(uid, "placed", None, ticker="AAA", mins_ago=8),
            _run(uid, "blocked", "drawdown_breached", ticker="BBB", mins_ago=5, snap={"dd_pct": -0.18}),
            _run(uid, "blocked", "drawdown_breached", ticker="CCC", mins_ago=10),
            _run(uid, "blocked", "drawdown_breached", ticker="DDD", mins_ago=15),
            _run(uid, "skipped", "non_positive_expected_edge", ticker="EEE", mins_ago=12),
            _run(uid, "skipped", "non_positive_expected_edge", ticker="FFF", mins_ago=14),
            _run(uid, "error", "exception", ticker="GGG", mins_ago=20),
        ]
    )
    db.commit()

    rep = build_autotrader_deployment_report(db, user_id=uid, hours=24)
    f = rep["decision_funnel"]
    assert f["total_runs"] == 7
    assert f["by_decision"]["blocked"]["count"] == 3
    assert f["by_decision"]["placed"]["count"] == 1
    assert f["placement_rate_pct"] == round(1 / 7 * 100, 1)

    top = f["top_blockers"][0]
    assert top["reason"] == "drawdown_breached"
    assert top["count"] == 3
    # rule_snapshot drill-down: most-recent sample for the top blocker
    assert top.get("sample", {}).get("rule_snapshot") == {"dd_pct": -0.18}
    assert top["sample"]["ticker"] == "BBB"

    assert any("drawdown_breached" in a for a in rep["recommended_actions"])
    assert set(rep["gates"].keys()) == {"autotrader", "kill_switch", "circuit_breaker"}


def test_empty_window_recommends_supply_check(db):
    rep = build_autotrader_deployment_report(db, user_id=None, hours=24)
    assert rep["decision_funnel"]["total_runs"] == 0
    assert any("No AutoTrader runs" in a for a in rep["recommended_actions"])


def test_window_excludes_old_runs(db):
    uid = _user(db, "win")
    db.add(_run(uid, "placed", None, mins_ago=10))
    db.add(_run(uid, "blocked", "old_reason", mins_ago=60 * 48))  # 48h ago
    db.commit()
    rep = build_autotrader_deployment_report(db, user_id=uid, hours=24)
    assert rep["decision_funnel"]["total_runs"] == 1


def test_user_scoping(db):
    u1 = _user(db, "su1")
    u2 = _user(db, "su2")
    db.add(_run(u1, "placed", None))
    db.add(_run(u2, "blocked", "x"))
    db.commit()
    rep = build_autotrader_deployment_report(db, user_id=u1, hours=24)
    assert rep["decision_funnel"]["total_runs"] == 1
    assert rep["decision_funnel"]["by_decision"].get("placed", {}).get("count") == 1

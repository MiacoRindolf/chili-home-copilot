"""Q3b: low-novelty + budget remaining must route PREMIUM, not dead-end.

Live failure (task 36 retry, 2026-06-11): after one failed attempt the task
scored low novelty, fell past Q3, and ESCALATED with $0.50 budget unspent.
With no template and no promoted local model, premium is the only executor;
escalate is reserved for exhausted budget (per the router's own docstring).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.code_brain import decision_router as dr
from app.services.code_brain.decision_router import Decision, TaskContext, route


@pytest.fixture
def ctx():
    return TaskContext(
        task_id=36,
        title="Update stale tier-4 line",
        brief_body="fix the docstring",
        sub_path="",
        repo_id=8,
        repo_name="chili-home-copilot",
        intended_files=[],
        prior_failure_count=1,
        is_high_stakes=False,
        estimated_diff_loc=5,
    )


def _force_low_novelty_no_template(monkeypatch):
    monkeypatch.setattr(dr, "_novelty_score", lambda db, c: Decimal("0.10"))
    monkeypatch.setattr(dr, "_match_template", lambda db, c: None, raising=False)


def test_low_novelty_with_budget_routes_premium(db, ctx, monkeypatch):
    _force_low_novelty_no_template(monkeypatch)
    decision = route(db, ctx)
    assert decision.decision == Decision.PREMIUM
    assert "no cheaper tier" in decision.reason


def test_low_novelty_without_budget_escalates(db, ctx, monkeypatch):
    _force_low_novelty_no_template(monkeypatch)
    from sqlalchemy import text

    db.execute(text(
        "UPDATE code_brain_runtime_state SET spent_today_usd = daily_premium_usd_cap"
    ))
    db.commit()
    decision = route(db, ctx)
    assert decision.decision == Decision.ESCALATE

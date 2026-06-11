"""Paper shadow mass: probed eligibles that lose the live rank race become
PAPER sessions — free outcome data instead of n=6 anecdotes."""

from __future__ import annotations

from types import SimpleNamespace

from app import models
from app.models.trading import MomentumStrategyVariant
from app.services.trading.momentum_neural.auto_arm import _paper_shadow_arm


def _candidates(v, *symbols):
    return [SimpleNamespace(symbol=s, variant_id=v.id) for s in symbols]


def _setup(db):
    u = models.User(name="paper-mass")
    db.add(u)
    db.flush()
    v = MomentumStrategyVariant(family="pm", variant_key="pm_v", label="pm", params_json={})
    db.add(v)
    db.flush()
    return u, v


def test_shadow_arms_rank_losers_not_the_live_winner(db, monkeypatch) -> None:
    from app.config import settings
    import app.services.trading.momentum_neural.auto_arm as aa

    u, v = _setup(db)
    monkeypatch.setattr(settings, "chili_momentum_paper_runner_enabled", True, raising=False)
    created: list[str] = []

    def _fake_create(db_, *, user_id, symbol, variant_id, execution_family):
        created.append(symbol)
        return {"ok": True, "session_id": len(created), "deduped": False}

    import app.services.trading.momentum_neural.operator_actions as oa

    monkeypatch.setattr(oa, "create_paper_draft_session", _fake_create)
    n = _paper_shadow_arm(
        db, uid=u.id, candidates=_candidates(v, "AAA", "BBB", "CCC"), exclude_symbol="BBB",
    )
    assert n == 2
    assert created == ["AAA", "CCC"]  # the live winner is excluded


def test_shadow_respects_concurrent_cap(db, monkeypatch) -> None:
    from app.config import settings
    import app.services.trading.momentum_neural.operator_actions as oa

    u, v = _setup(db)
    monkeypatch.setattr(settings, "chili_momentum_paper_runner_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_paper_shadow_max_sessions", 2, raising=False)
    created: list[str] = []

    def _fake_create(db_, **kw):
        created.append(kw["symbol"])
        return {"ok": True, "deduped": False}

    monkeypatch.setattr(oa, "create_paper_draft_session", _fake_create)
    n = _paper_shadow_arm(db, uid=u.id, candidates=_candidates(v, "AAA", "BBB", "CCC", "DDD"))
    assert n == 2 and created == ["AAA", "BBB"]


def test_shadow_noop_when_runner_off(db, monkeypatch) -> None:
    from app.config import settings

    u, v = _setup(db)
    monkeypatch.setattr(settings, "chili_momentum_paper_runner_enabled", False, raising=False)
    assert _paper_shadow_arm(db, uid=u.id, candidates=_candidates(v, "AAA")) == 0

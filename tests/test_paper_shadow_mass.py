"""Paper shadow mass: probed eligibles that lose the live rank race become
PAPER sessions — free outcome data instead of n=6 anecdotes."""

from __future__ import annotations

from types import SimpleNamespace

from app import models
from app.models.trading import MomentumStrategyVariant, TradingAutomationSession
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


def test_candidate_list_drops_closed_markets_before_limit(db, monkeypatch) -> None:
    # 2026-06-12 night-lane fix: overnight, stale CLOSED equities outscored
    # crypto and consumed the whole candidate limit -> empty lane all night
    from datetime import datetime

    from app.models.trading import MomentumSymbolViability
    import app.services.trading.momentum_neural.auto_arm as aa

    _u, v = _setup(db)
    now = datetime.utcnow()
    for sym, score in (("STALEEQ", 0.9), ("KAIO-USD", 0.6)):
        db.add(MomentumSymbolViability(
            symbol=sym, variant_id=v.id, scope="symbol", viability_score=score,
            live_eligible=True, freshness_ts=now,
        ))
    # the fresh-tape gate (#669) requires a live tape row — seed one for the
    # crypto candidate (the closed equity stays tape-less, doubly dead)
    from sqlalchemy import text as _text

    db.execute(_text(
        "INSERT INTO momentum_nbbo_spread_tape (symbol, observed_at, source) "
        "VALUES ('KAIO-USD', now() at time zone 'utc', 'test')"
    ))
    db.commit()
    # night: equities closed, crypto open
    monkeypatch.setattr(aa, "_symbol_market_open", lambda s: s.endswith("-USD"))
    out = aa._fresh_live_eligible_candidates(db, limit=1)
    assert [c.symbol for c in out] == ["KAIO-USD"]  # the closed equity no longer crowds the limit


def test_paper_equities_route_to_alpaca_when_configured(monkeypatch) -> None:
    from app.config import settings
    from app.services.trading.execution_family_registry import resolve_execution_family_for_symbol

    monkeypatch.setattr(settings, "chili_alpaca_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_alpaca_paper", True, raising=False)
    monkeypatch.setattr(settings, "chili_alpaca_api_key", "pk_test", raising=False)
    assert resolve_execution_family_for_symbol("AAPL", mode="paper") == "alpaca_spot"
    assert resolve_execution_family_for_symbol("AAPL", mode="live") == "robinhood_spot"
    assert resolve_execution_family_for_symbol("KAIO-USD", mode="paper") == "coinbase_spot"
    monkeypatch.setattr(settings, "chili_alpaca_enabled", False, raising=False)
    assert resolve_execution_family_for_symbol("AAPL", mode="paper") == "robinhood_spot"


def test_alpaca_paper_outcomes_excluded_from_real_daily_pnl(db) -> None:
    # fake-money twin outcomes must never move the REAL daily-loss math
    from datetime import datetime

    from app.models.trading import MomentumAutomationOutcome
    import app.services.trading.governance as gov

    u, v = _setup(db)
    rh = TradingAutomationSession(
        user_id=u.id, symbol="AAPL", mode="live", variant_id=v.id,
        state="live_exited", execution_family="robinhood_spot",
    )
    al = TradingAutomationSession(
        user_id=u.id, symbol="AAPL", mode="live", variant_id=v.id,
        state="live_exited", execution_family="alpaca_spot",
    )
    db.add_all([rh, al])
    db.flush()
    now = datetime.utcnow()
    db.add_all([
        MomentumAutomationOutcome(
            session_id=rh.id, user_id=u.id, variant_id=v.id, symbol="AAPL",
            mode="live", realized_pnl_usd=-100.0, terminal_at=now,
            terminal_state="live_exited", outcome_class="loss",
        ),
        MomentumAutomationOutcome(
            session_id=al.id, user_id=u.id, variant_id=v.id, symbol="AAPL",
            mode="live", realized_pnl_usd=-5000.0, terminal_at=now,  # fake-money disaster
            terminal_state="live_exited", outcome_class="loss",
        ),
    ])
    db.commit()
    out = gov.global_realized_pnl_today_et(db, u.id)
    assert out["momentum_usd"] == -100.0  # the fake -5000 is invisible to real risk


def test_alpaca_sessions_excluded_from_aggregate_risk(db) -> None:
    from app.services.trading.momentum_neural.risk_evaluator import aggregate_open_risk_usd

    u, v = _setup(db)
    for fam, sym in (("robinhood_spot", "AAA"), ("alpaca_spot", "BBB")):
        db.add(TradingAutomationSession(
            user_id=u.id, symbol=sym, mode="live", variant_id=v.id,
            state="live_entered", execution_family=fam,
            risk_snapshot_json={"momentum_live_execution": {"position": {
                "quantity": 100, "avg_entry_price": 10.0, "stop_price": 9.0,
            }}},
        ))
    db.commit()
    total, rows = aggregate_open_risk_usd(db, user_id=u.id)
    assert abs(total - 100.0) < 1e-9  # only the RH position counts
    assert [r["symbol"] for r in rows] == ["AAA"]

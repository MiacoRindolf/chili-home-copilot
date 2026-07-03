"""A2 (Ross CLRO-lesson 2026-07-02) — QUALITY-RANKED RISK-ENVELOPE DISPLACEMENT.

On 07-02 two dying IPW losers pinned the whole 3%-of-equity aggregate risk envelope (726
live_blocked_by_risk) through CLRO's curl. A2: when the aggregate-open-risk cap blocks a
TOP-RANKED candidate, enqueue a stop-TIGHTEN on the LARGEST at-risk open position to its OWN
already-computed most-defensive trail candidate (INVARIANT-A max(candidate,current)). THIS
tick still blocks; the position's next tick applies the tighten and the freed envelope admits
the next candidate.

FAIL-CLOSED everywhere: non-top-ranked / no candidate level / frees < planned => plain block,
byte-identical. Locks:
  * _defensive_trail_candidate_for_session computes a NEVER-INVENTED trail candidate (or None),
  * the enqueue only fires for the #1 top-ranked name and only when the freed risk >= planned,
  * the enqueue writes the request onto the TARGET position (not the candidate),
  * non-top-ranked / thin data => nothing enqueued.

[[project_momentum_engine]] [[feedback_evolve_not_devolve]] [[feedback_adaptive_no_magic]]
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from app.config import settings
from app.models.core import User
from app.models.trading import (
    MomentumStrategyVariant,
    MomentumSymbolViability,
    TradingAutomationSession,
)
from app.services.trading.momentum_neural.persistence import ensure_momentum_strategy_variants
from app.services.trading.momentum_neural.risk_evaluator import (
    _defensive_trail_candidate_for_session,
    _enqueue_risk_envelope_displacement,
)

_EF = "robinhood_agentic"


def _require_tables(db: Session) -> None:
    names = set(sa_inspect(db.bind).get_table_names())
    for t in ("trading_automation_sessions", "momentum_symbol_viability"):
        if t not in names:
            pytest.skip(f"{t} table not present")


def _setup(db: Session) -> tuple[User, list[MomentumStrategyVariant]]:
    _require_tables(db)
    ensure_momentum_strategy_variants(db)
    db.commit()
    variants = db.query(MomentumStrategyVariant).all()
    assert variants
    u = User(name="RiskDisplA2")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u, variants


def _held(
    db: Session,
    u: User,
    v: MomentumStrategyVariant,
    *,
    symbol: str,
    qty: float,
    entry: float,
    stop: float,
    hwm: float,
    atr_pct: float = 0.04,
) -> TradingAutomationSession:
    """A HELD live position with a full momentum_live_execution.position snapshot."""
    s = TradingAutomationSession(
        user_id=u.id,
        mode="live",
        symbol=symbol,
        variant_id=v.id,
        execution_family=_EF,
        state="live_entered",
        risk_snapshot_json={
            "momentum_live_execution": {
                "entry_stop_atr_pct": atr_pct,
                "position": {
                    "side": "long",
                    "quantity": qty,
                    "avg_entry_price": entry,
                    "stop_price": stop,
                    "high_water_mark": hwm,
                    "target_price": entry * 1.2,
                },
            }
        },
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _add_viability(db: Session, *, symbol: str, variant_id: int, score: float) -> None:
    db.add(
        MomentumSymbolViability(
            symbol=symbol,
            scope="symbol",
            variant_id=variant_id,
            viability_score=score,
            paper_eligible=True,
            live_eligible=True,
            freshness_ts=datetime.utcnow(),
        )
    )
    db.commit()


def _enable(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_envelope_displacement_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_trade_budget_top_rank_exempt_enabled", True, raising=False)


# ── _defensive_trail_candidate_for_session ──────────────────────────────────────


def test_defensive_candidate_tightens_a_winner_with_room(db: Session, monkeypatch) -> None:
    # A position well in profit (hwm >> entry) has a defensive trail candidate ABOVE its
    # current (loose) stop => frees risk.
    u, variants = _setup(db)
    s = _held(db, u, variants[0], symbol="IPW", qty=100.0, entry=10.0, stop=9.0, hwm=12.0)
    cand, cur, freed, meta = _defensive_trail_candidate_for_session(s)
    assert cand is not None, meta
    assert cand > cur  # strictly tighter (INVARIANT-A)
    assert freed > 0.0
    assert meta["freed_usd"] == pytest.approx(freed, rel=1e-3)


def test_defensive_candidate_none_when_no_room(db: Session, monkeypatch) -> None:
    # A position whose current stop is already at/above its trail candidate (hwm == entry, no
    # profit) yields NO tighter candidate => fail-closed None.
    u, variants = _setup(db)
    s = _held(db, u, variants[0], symbol="FLAT", qty=100.0, entry=10.0, stop=9.95, hwm=10.0)
    cand, cur, freed, meta = _defensive_trail_candidate_for_session(s)
    assert cand is None
    assert freed == 0.0


def test_defensive_candidate_fail_closed_on_missing_atr(db: Session, monkeypatch) -> None:
    # Strip the entry ATR -> cannot compute the existing trail -> fail-closed None (no invention).
    u, variants = _setup(db)
    s = _held(db, u, variants[0], symbol="NOATR", qty=100.0, entry=10.0, stop=9.0, hwm=12.0)
    snap = dict(s.risk_snapshot_json)
    snap["momentum_live_execution"].pop("entry_stop_atr_pct", None)
    s.risk_snapshot_json = snap
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(s, "risk_snapshot_json")
    db.commit()
    cand, cur, freed, meta = _defensive_trail_candidate_for_session(s)
    assert cand is None
    assert meta["reason"] == "no_entry_atr"


# ── _enqueue_risk_envelope_displacement ─────────────────────────────────────────


def test_displacement_enqueued_for_top_ranked_candidate(db: Session, monkeypatch) -> None:
    u, variants = _setup(db)
    _enable(monkeypatch)
    # two losers pin the envelope; the LARGER one is the displacement target.
    big = _held(db, u, variants[0], symbol="IPW", qty=200.0, entry=10.0, stop=9.0, hwm=12.5)
    small = _held(db, u, variants[0], symbol="ARCT", qty=50.0, entry=8.0, stop=7.5, hwm=8.2)
    # CLRO is the #1 freshness-valid live-eligible mover (top-percentile score).
    _add_viability(db, symbol="CLRO", variant_id=variants[0].id, score=0.96)
    for i, sc in enumerate([0.55, 0.5, 0.52, 0.58]):
        _add_viability(db, symbol=f"J{i}", variant_id=variants[i % len(variants)].id, score=sc)
    open_rows = [
        {"symbol": "IPW", "session_id": big.id, "at_risk_usd": (10.0 - 9.0) * 200.0},
        {"symbol": "ARCT", "session_id": small.id, "at_risk_usd": (8.0 - 7.5) * 50.0},
    ]
    meta = _enqueue_risk_envelope_displacement(
        db, user_id=u.id, candidate_symbol="CLRO", execution_family=_EF,
        planned_risk_usd=20.0, open_rows=open_rows,
    )
    assert meta["enqueued"] is True, meta
    assert meta["target_session_id"] == big.id  # the LARGEST at-risk position
    # the request landed on the TARGET position's snapshot (not the candidate).
    db.refresh(big)
    req = big.risk_snapshot_json["momentum_live_execution"]["pending_risk_displacement_tighten"]
    assert req["for_candidate"] == "CLRO"
    assert float(req["candidate_stop"]) > 9.0  # a tighter stop than IPW's current


def test_displacement_not_enqueued_for_non_top_ranked(db: Session, monkeypatch) -> None:
    u, variants = _setup(db)
    _enable(monkeypatch)
    big = _held(db, u, variants[0], symbol="IPW", qty=200.0, entry=10.0, stop=9.0, hwm=12.5)
    _add_viability(db, symbol="CLRO", variant_id=variants[0].id, score=0.96)  # #1 is CLRO
    open_rows = [{"symbol": "IPW", "session_id": big.id, "at_risk_usd": 200.0}]
    # a NON-#1 candidate (ZZZZ) earns no displacement.
    meta = _enqueue_risk_envelope_displacement(
        db, user_id=u.id, candidate_symbol="ZZZZ", execution_family=_EF,
        planned_risk_usd=20.0, open_rows=open_rows,
    )
    assert meta["enqueued"] is False
    assert meta["reason"] == "not_top_ranked"
    db.refresh(big)
    assert "pending_risk_displacement_tighten" not in big.risk_snapshot_json["momentum_live_execution"]


def test_displacement_fail_closed_when_frees_less_than_planned(db: Session, monkeypatch) -> None:
    u, variants = _setup(db)
    _enable(monkeypatch)
    # a position with only a TINY defensive gap (hwm barely above entry) can't free the planned risk.
    tiny = _held(db, u, variants[0], symbol="IPW", qty=10.0, entry=10.0, stop=9.9, hwm=10.05)
    _add_viability(db, symbol="CLRO", variant_id=variants[0].id, score=0.96)
    for i, sc in enumerate([0.55, 0.5, 0.52]):
        _add_viability(db, symbol=f"K{i}", variant_id=variants[i % len(variants)].id, score=sc)
    open_rows = [{"symbol": "IPW", "session_id": tiny.id, "at_risk_usd": (10.0 - 9.9) * 10.0}]
    # planned risk is far larger than anything this tiny position could free.
    meta = _enqueue_risk_envelope_displacement(
        db, user_id=u.id, candidate_symbol="CLRO", execution_family=_EF,
        planned_risk_usd=500.0, open_rows=open_rows,
    )
    assert meta["enqueued"] is False, meta
    assert meta["reason"] in ("no_position_frees_enough", "no_at_risk_position")
    db.refresh(tiny)
    assert "pending_risk_displacement_tighten" not in tiny.risk_snapshot_json["momentum_live_execution"]


def test_displacement_rank_unreadable_fail_closed(db: Session, monkeypatch) -> None:
    u, variants = _setup(db)
    _enable(monkeypatch)
    big = _held(db, u, variants[0], symbol="IPW", qty=200.0, entry=10.0, stop=9.0, hwm=12.5)
    # NO viability rows -> rank unreadable -> fail-closed (no enqueue).
    open_rows = [{"symbol": "IPW", "session_id": big.id, "at_risk_usd": 200.0}]
    meta = _enqueue_risk_envelope_displacement(
        db, user_id=u.id, candidate_symbol="CLRO", execution_family=_EF,
        planned_risk_usd=20.0, open_rows=open_rows,
    )
    assert meta["enqueued"] is False
    assert meta["reason"] == "rank_unreadable"


def test_displacement_flag_off_no_enqueue(db: Session, monkeypatch) -> None:
    u, variants = _setup(db)
    monkeypatch.setattr(settings, "chili_momentum_risk_envelope_displacement_enabled", False, raising=False)
    big = _held(db, u, variants[0], symbol="IPW", qty=200.0, entry=10.0, stop=9.0, hwm=12.5)
    _add_viability(db, symbol="CLRO", variant_id=variants[0].id, score=0.96)
    open_rows = [{"symbol": "IPW", "session_id": big.id, "at_risk_usd": 200.0}]
    meta = _enqueue_risk_envelope_displacement(
        db, user_id=u.id, candidate_symbol="CLRO", execution_family=_EF,
        planned_risk_usd=20.0, open_rows=open_rows,
    )
    assert meta["enqueued"] is False
    assert meta["reason"] == "disabled"


def test_displacement_no_at_risk_position(db: Session, monkeypatch) -> None:
    u, variants = _setup(db)
    _enable(monkeypatch)
    _add_viability(db, symbol="CLRO", variant_id=variants[0].id, score=0.96)
    for i, sc in enumerate([0.55, 0.5]):
        _add_viability(db, symbol=f"Q{i}", variant_id=variants[i % len(variants)].id, score=sc)
    # empty open_rows -> nothing to displace.
    meta = _enqueue_risk_envelope_displacement(
        db, user_id=u.id, candidate_symbol="CLRO", execution_family=_EF,
        planned_risk_usd=20.0, open_rows=[],
    )
    assert meta["enqueued"] is False
    assert meta["reason"] == "no_at_risk_position"

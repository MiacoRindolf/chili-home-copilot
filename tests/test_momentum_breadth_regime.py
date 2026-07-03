"""A3 (Ross CLRO-lesson 2026-07-02) — scanner-breadth WILDCARD regime.

Ross's wildcard thesis: "one stock squeezes for lack of anything else." 07-02 was the labeled
example — a DEAD scanner (junk) except CLRO (+200%). This module detects that regime from the
live scanner snapshot (momentum_symbol_viability) + a trailing same-time-of-day baseline
(momentum_viability_history, mig311), so the lane can CONCENTRATE slots/size on the lone leader.

Proves the contract:
  1. a lone dominant mover among junk on a bottom-decile-breadth day => WILDCARD ON, the leader
     is named the dominant symbol, and the B-grade size-tilt is DOWN;
  2. a BROAD day (many eligible movers, no lone leader) => WILDCARD OFF (neutral effects);
  3. an EMPTY viability table => NEUTRAL (fail-closed, zero effects);
  4. the pure pre-holiday helper flags a day-before-holiday;
  5. flag OFF => neutral (byte-identical).

Self-contained: seeds its own variants + viability + history in ``chili_test``; no prod ``chili``
data. One pytest at a time (DB-truncate rule).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.models.trading import MomentumSymbolViability, MomentumViabilityHistory
from app.services.trading.momentum_neural.breadth_regime import (
    compute_breadth_regime,
    is_pre_holiday,
)
from app.services.trading.momentum_neural.persistence import (
    ensure_momentum_strategy_variants,
)


_NOW = datetime(2026, 7, 2, 14, 0, 0, tzinfo=timezone.utc).replace(tzinfo=None)


def _variant_id(db: Session) -> int:
    ensure_momentum_strategy_variants(db)
    db.commit()
    vid = db.execute(text("SELECT id FROM momentum_strategy_variants ORDER BY id ASC LIMIT 1")).scalar()
    assert vid is not None, "no momentum_strategy_variants seeded"
    return int(vid)


def _seed_viability(db: Session, vid: int, rows: list[tuple[str, float, bool]], *, now: datetime) -> None:
    """rows = [(symbol, viability_score, live_eligible)]. Fresh (freshness_ts = now)."""
    for sym, score, elig in rows:
        db.add(MomentumSymbolViability(
            symbol=sym.upper(), scope="symbol", variant_id=vid,
            viability_score=float(score), paper_eligible=True, live_eligible=bool(elig),
            freshness_ts=now,
        ))
    db.commit()


def _seed_history_baseline(db: Session, *, now: datetime, n_sessions: int, breadth_per_session: int) -> None:
    """Seed ``n_sessions`` prior sessions each with ``breadth_per_session`` distinct eligible
    equity symbols, at the SAME time-of-day (hour) as ``now`` — the trailing breadth baseline."""
    for d in range(1, n_sessions + 1):
        day_at = now - timedelta(days=d)
        for k in range(breadth_per_session):
            db.add(MomentumViabilityHistory(
                symbol=f"HIST{d}_{k}", variant_id=1, scope="symbol",
                observed_at=day_at, live_eligible=True, viability_score=0.6,
            ))
    db.commit()


def test_pre_holiday_pure_helper() -> None:
    """The day BEFORE a US market holiday is pre-holiday; a normal day is not."""
    # 2026-07-03 is an observed holiday (July 4 observed Fri) -> 2026-07-02 is pre-holiday.
    assert is_pre_holiday(date(2026, 7, 2)) is True
    assert is_pre_holiday(date(2026, 7, 1)) is False


def test_empty_table_is_neutral(db: Session) -> None:
    """No viability rows at all => NEUTRAL (fail-closed): not wildcard, no dominant symbol."""
    settings.chili_momentum_wildcard_breadth_regime_enabled = True
    reg = compute_breadth_regime(db, now=_NOW)
    assert reg.is_wildcard is False
    assert reg.dominant_symbol is None
    assert reg.b_grade_size_tilt() == 1.0 or reg.is_pre_holiday  # neutral (or pre-holiday deweight only)


def test_dominant_plus_junk_is_wildcard_on(db: Session) -> None:
    """A lone dominant mover (CLRO-class, high score) among JUNK (a couple of low-score eligibles)
    on a bottom-decile-breadth day => WILDCARD ON, the leader is the dominant symbol, B-grade
    size-tilt DOWN."""
    settings.chili_momentum_wildcard_breadth_regime_enabled = True
    vid = _variant_id(db)
    # trailing baseline: prior sessions carried MANY movers (breadth ~10) -> today's breadth of 3
    # sits far below the p20 floor.
    _seed_history_baseline(db, now=_NOW, n_sessions=20, breadth_per_session=10)
    # today: ONE dominant (0.95) + two junk (0.40, 0.42) eligible -> breadth 3, high dominance.
    _seed_viability(
        db, vid,
        [("CLRO", 0.95, True), ("JUNKA", 0.40, True), ("JUNKB", 0.42, True)],
        now=_NOW,
    )
    reg = compute_breadth_regime(db, now=_NOW)
    assert reg.is_wildcard is True, f"expected wildcard, got {reg}"
    assert reg.dominant_symbol == "CLRO"
    assert reg.dominance > 0.0
    assert reg.b_grade_size_tilt() < 1.0  # B-grade admissions size DOWN


def test_broad_day_is_wildcard_off(db: Session) -> None:
    """A BROAD day (many eligible movers, no lone leader) => WILDCARD OFF (neutral)."""
    settings.chili_momentum_wildcard_breadth_regime_enabled = True
    vid = _variant_id(db)
    # trailing baseline: prior sessions carried FEW movers (breadth ~2) -> today's breadth of 12
    # is NOT bottom-decile.
    _seed_history_baseline(db, now=_NOW, n_sessions=20, breadth_per_session=2)
    # today: 12 eligible movers, all clustered around the same score (no lone leader).
    rows = [(f"MOV{i}", 0.60 + (i % 3) * 0.01, True) for i in range(12)]
    _seed_viability(db, vid, rows, now=_NOW)
    reg = compute_breadth_regime(db, now=_NOW)
    assert reg.is_wildcard is False, f"broad day must not be wildcard, got {reg}"
    assert reg.dominant_symbol is None


def test_flag_off_is_neutral(db: Session) -> None:
    """Kill-switch OFF => NEUTRAL even for a textbook dominant+junk day (byte-identical)."""
    vid = _variant_id(db)
    _seed_history_baseline(db, now=_NOW, n_sessions=20, breadth_per_session=10)
    _seed_viability(db, vid, [("CLRO", 0.95, True), ("JUNKA", 0.40, True), ("JUNKB", 0.42, True)], now=_NOW)
    settings.chili_momentum_wildcard_breadth_regime_enabled = False
    try:
        reg = compute_breadth_regime(db, now=_NOW)
        assert reg.is_wildcard is False
        assert reg.dominant_symbol is None
    finally:
        settings.chili_momentum_wildcard_breadth_regime_enabled = True


def test_thin_baseline_fails_closed(db: Session) -> None:
    """Too few prior sessions to establish a baseline => fail-closed to NOT wildcard (never call a
    day 'thin-breadth' without a real baseline to compare against)."""
    settings.chili_momentum_wildcard_breadth_regime_enabled = True
    vid = _variant_id(db)
    _seed_history_baseline(db, now=_NOW, n_sessions=2, breadth_per_session=10)  # only 2 sessions (<5)
    _seed_viability(db, vid, [("CLRO", 0.95, True), ("JUNKA", 0.40, True)], now=_NOW)
    reg = compute_breadth_regime(db, now=_NOW)
    assert reg.is_wildcard is False
    assert reg.reason == "thin_baseline"

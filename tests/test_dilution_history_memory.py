"""A10 (Ross CLRO-lesson 2026-07-02) — own-headline dilution-history memory (mig312).

Ross has "written off" serial diluters (WHLR-class: "many secondary offerings, many reverse
splits"). No corp-actions vendor exists — but ``catalyst.weak_catalyst_symbols`` flags dilution
symbols DAILY. This feature PERSISTS those daily observations (momentum_dilution_history) and a
symbol flagged on >= an adaptive-K distinct days in the trailing window earns a DECAYING
selection derate — NEVER a hard ban (the fresh reverse-split-squeeze carve-out must still win).

Proves the contract:
  1. the migration is idempotent (runs twice, no error) + the table is queryable;
  2. persist writes one idempotent row per (symbol, observed_day) (a same-day re-write no-ops);
  3. a serial diluter (>= K distinct flagged days) earns a POSITIVE derate that DECAYS with
     recency; a symbol flagged on only 1 day earns NONE (not serial);
  4. a FRESH reverse-split squeeze (in today's strong-catalyst set) is EXEMPT — still boosts;
  5. empty history / flag OFF => neutral (0.0 derate).

Self-contained: seeds its own rows in ``chili_test``; no prod ``chili`` data. One pytest at a
time (DB-truncate rule).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.migrations import _migration_312_momentum_dilution_history
from app.models.trading import MomentumDilutionHistory
from app.services.trading.momentum_neural.dilution_history import (
    dilution_history_derate,
    persist_dilution_flags,
)


def _now() -> datetime:
    return datetime(2026, 7, 2, 14, 0, 0, tzinfo=timezone.utc).replace(tzinfo=None)


def _seed_flag_days(db: Session, symbol: str, days_back: list[int], *, now: datetime) -> None:
    """Insert dilution-flag rows for ``symbol`` on each ``now - d`` day (distinct days)."""
    for d in days_back:
        day = (now - timedelta(days=d))
        db.execute(
            text(
                "INSERT INTO momentum_dilution_history "
                "(symbol, observed_day, observed_at, flag_reason) "
                "VALUES (:s, :day, :at, 'weak_catalyst') "
                "ON CONFLICT (symbol, observed_day) DO NOTHING"
            ),
            {"s": symbol.upper(), "day": day.date(), "at": day},
        )
    db.commit()


def test_migration_312_is_idempotent() -> None:
    """The A10 migration creates the table + indexes and re-runs cleanly (CREATE IF NOT
    EXISTS throughout) — running it twice must not raise; the table is then queryable."""
    with engine.begin() as conn:
        _migration_312_momentum_dilution_history(conn)
    with engine.begin() as conn:
        _migration_312_momentum_dilution_history(conn)
    with engine.connect() as conn:
        cnt = conn.execute(text("SELECT COUNT(*) FROM momentum_dilution_history")).scalar()
    assert cnt is not None


def test_persist_is_idempotent_per_day(db: Session) -> None:
    """persist_dilution_flags writes ONE row per (symbol, observed_day); a same-day re-write
    no-ops (idempotent via the UNIQUE(symbol, observed_day) ON CONFLICT DO NOTHING)."""
    settings.chili_momentum_dilution_history_derate_enabled = True
    now = _now()
    n1 = persist_dilution_flags(db, {"WHLR", "USDE"}, now_utc=now, correlation_id="a10")
    assert n1 == 2
    # same day, same symbols -> zero new rows (idempotent).
    n2 = persist_dilution_flags(db, {"WHLR", "USDE"}, now_utc=now, correlation_id="a10")
    assert n2 == 0
    rows = db.query(MomentumDilutionHistory).filter(MomentumDilutionHistory.symbol == "WHLR").all()
    assert len(rows) == 1
    assert rows[0].flag_reason == "weak_catalyst"
    assert rows[0].observed_day == now.date()


def test_serial_diluter_earns_decaying_derate(db: Session) -> None:
    """A symbol flagged on 3 distinct recent days (>= adaptive K) earns a POSITIVE derate; a
    symbol flagged on only 1 day is NOT serial (no derate). The recent diluter derates MORE than
    a same-count diluter whose last flag is older (recency decay)."""
    settings.chili_momentum_dilution_history_derate_enabled = True
    settings.chili_momentum_dilution_history_window_days = 90
    now = _now()
    # WHLR: a serial diluter flagged on 3 distinct recent days (most recent = today).
    _seed_flag_days(db, "WHLR", [0, 5, 12], now=now)
    # ONEOFF: a single-day flag (not serial) — must earn NO derate.
    _seed_flag_days(db, "ONEOFF", [3], now=now)
    # STALE: same 3-day count as WHLR but the last flag is far back in the window (recency decay).
    _seed_flag_days(db, "STALE", [60, 70, 85], now=now)

    d_whlr = dilution_history_derate(db, "WHLR", now_utc=now)
    d_oneoff = dilution_history_derate(db, "ONEOFF", now_utc=now)
    d_stale = dilution_history_derate(db, "STALE", now_utc=now)

    assert d_whlr > 0.0, "a 3-distinct-day serial diluter must earn a derate"
    assert d_oneoff == 0.0, "a single-day flag is not a serial diluter"
    assert d_stale >= 0.0
    assert d_whlr > d_stale, "a recently-flagged diluter derates more than a stale one (recency)"
    assert d_whlr <= 0.12, "the derate is bounded (never a hard ban)"


def test_empty_history_is_neutral(db: Session) -> None:
    """No history for the symbol (or an empty table) => neutral: 0.0 derate (fail-open)."""
    settings.chili_momentum_dilution_history_derate_enabled = True
    now = _now()
    assert dilution_history_derate(db, "FRESHNAME", now_utc=now) == 0.0
    # crypto is exempt entirely.
    assert dilution_history_derate(db, "BTC-USD", now_utc=now) == 0.0


def test_flag_off_is_neutral(db: Session) -> None:
    """Kill-switch OFF: no derate is computed (0.0), even for a seeded serial diluter."""
    now = _now()
    _seed_flag_days(db, "WHLR", [0, 5, 12], now=now)
    settings.chili_momentum_dilution_history_derate_enabled = False
    try:
        assert dilution_history_derate(db, "WHLR", now_utc=now) == 0.0
        # persist is also a no-op when the flag is off.
        assert persist_dilution_flags(db, {"NEWSYM"}, now_utc=now) == 0
    finally:
        settings.chili_momentum_dilution_history_derate_enabled = True


def test_fresh_squeeze_carveout_still_boosts(db: Session) -> None:
    """THE CARVE-OUT: a symbol that IS a serial diluter by history but is in TODAY's strong-
    catalyst set (a fresh reverse-split squeeze) must still SCORE HIGHER than the same name
    WITHOUT the fresh catalyst — the live squeeze overrides the stale-diluter memory. Proven at
    the score_viability integration level (the derate is skipped for a fresh-squeeze symbol)."""
    from app.services.trading.momentum_neural.context import build_momentum_regime_context
    from app.services.trading.momentum_neural.features import ExecutionReadinessFeatures
    from app.services.trading.momentum_neural.variants import get_family
    from app.services.trading.momentum_neural.viability import score_viability

    settings.chili_momentum_dilution_history_derate_enabled = True
    settings.chili_momentum_dilution_history_window_days = 90
    now = _now()
    # SQZ is a heavy serial diluter by our own headline memory.
    _seed_flag_days(db, "SQZ", [0, 4, 9, 15, 22], now=now)

    fam = get_family("impulse_breakout")
    assert fam is not None
    feats = ExecutionReadinessFeatures(
        spread_bps=40.0, slippage_estimate_bps=4.0, fee_to_target_ratio=0.08,
        meta={"ross_signals": {"SQZ": {"rvol": 8.0, "daily_change_pct": 22.0}}},
    )

    def _ctx(strong: set[str] | None):
        meta = {"spread_regime": "normal"}
        if strong:
            meta["strong_catalyst_symbols"] = sorted(strong)
        return build_momentum_regime_context(
            now=datetime(2026, 7, 2, 14, 0, 0, tzinfo=timezone.utc), atr_pct=0.05, meta=meta,
        )

    # WITHOUT the fresh catalyst -> the serial-diluter derate applies (lower score).
    vr_derated = score_viability("SQZ", fam, _ctx(strong=None), feats, db=db)
    # WITH the fresh squeeze (SQZ in today's strong set) -> derate is SKIPPED (carve-out wins).
    vr_squeeze = score_viability("SQZ", fam, _ctx(strong={"SQZ"}), feats, db=db)

    assert vr_squeeze.viability > vr_derated.viability, (
        "a fresh reverse-split squeeze must outrank the same name's stale-diluter memory "
        f"(squeeze={vr_squeeze.viability} vs derated={vr_derated.viability})"
    )

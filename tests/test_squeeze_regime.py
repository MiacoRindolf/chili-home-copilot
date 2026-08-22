"""S6 suppression/squeeze regime index (Ross "Forcing a Crash" 08-21).

Pinapatunayan ang kontrata:
  1. per-day mover stats: +50%/+100% na bilang, HELD vs FADED vs UNRESOLVED
     (ang mover na binitawan ng scanner bago ang huling 1/4 ng araw ay hindi
     hinuhulaan), crypto excluded;
  2. refresh: idempotent upsert + FTD aggregate (market total + mover subset);
  3. PURE label logic: squeeze_hold / squeeze_emergent / suppression_fade /
     suppression_vanish / mixed / thin_baseline / no_daily_rows;
  4. ang label ay nakalakip sa BreadthRegime (observability-only) at ang
     kill-switch ay neutral;
  5. mig369 ay nakarehistro nang isang beses (366/367 nakalaan sa ibang branch).

Self-contained sa ``chili_test``; isang pytest sa isang pagkakataon (DB-truncate
rule). Ang momentum_squeeze_regime_daily at sec_fails_to_deliver ay WALANG ORM
model (hindi tinu-truncate ng conftest) — nililinis dito nang tahasan.

Runnable: pytest tests/test_squeeze_regime.py -v
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.migrations import MIGRATIONS
from app.models.trading import MomentumViabilityHistory
from app.services.trading.momentum_neural import squeeze_regime as sq
from app.services.trading.momentum_neural.breadth_regime import compute_breadth_regime

_NOW = datetime(2026, 8, 22, 14, 0, 0)  # UTC-naive; "kahapon" = 2026-08-21
_DAY = date(2026, 8, 21)


def _clean_non_orm_tables(db: Session) -> None:
    db.execute(text("DELETE FROM momentum_squeeze_regime_daily"))
    db.execute(text("DELETE FROM sec_fails_to_deliver"))
    db.commit()


def _hist(db: Session, sym: str, at: datetime, chg: float) -> None:
    db.add(MomentumViabilityHistory(
        symbol=sym, variant_id=1, scope="symbol", observed_at=at,
        live_eligible=True, change_pct=chg,
    ))


def _seed_mover_day(db: Session) -> None:
    """Isang araw (2026-08-21 UTC): span anchors 08:00-23:30 => EOD cutoff sa
    08:00 + 0.75*15.5h = 19:37:30.
      HOLDER: max +80, last +70 @23:00  -> resolved, HELD (70 >= 40)
      FADER : max +60, last +12 @22:00  -> resolved, FADED (12 < 30)
      GONE  : max +120, last @10:45     -> UNRESOLVED (bago ang cutoff); movers_100
      SMALL : max +30                    -> hindi mover
      AAA-USD: +200                      -> crypto, excluded
    """
    d = datetime(2026, 8, 21, 0, 0, 0)
    _hist(db, "SPANLO", d + timedelta(hours=8), 1.0)
    _hist(db, "SPANHI", d + timedelta(hours=23, minutes=30), 2.0)
    _hist(db, "HOLDER", d + timedelta(hours=10), 55.0)
    _hist(db, "HOLDER", d + timedelta(hours=12), 80.0)
    _hist(db, "HOLDER", d + timedelta(hours=23), 70.0)
    _hist(db, "FADER", d + timedelta(hours=10, minutes=30), 60.0)
    _hist(db, "FADER", d + timedelta(hours=22), 12.0)
    _hist(db, "GONE", d + timedelta(hours=9), 120.0)
    _hist(db, "GONE", d + timedelta(hours=10, minutes=45), 115.0)
    _hist(db, "SMALL", d + timedelta(hours=11), 30.0)
    _hist(db, "AAA-USD", d + timedelta(hours=12), 200.0)
    db.commit()


def test_migration_registered_exactly_once():
    ids = [m[0] for m in MIGRATIONS]
    assert ids.count("369_squeeze_regime_daily") == 1
    # 366/367 ay nakalaan sa captured-paper branch — hindi dapat naririto.
    assert not any(i.startswith(("366_", "367_")) for i in ids)


def test_daily_mover_stats_hold_fade_unresolved(db: Session) -> None:
    _clean_non_orm_tables(db)
    _seed_mover_day(db)
    stats = sq.compute_daily_mover_stats(db, _DAY)
    assert stats is not None
    assert stats["movers_50"] == 3, stats
    assert stats["movers_100"] == 1, stats
    assert stats["held_50"] == 1, stats
    assert stats["faded_50"] == 1, stats
    assert stats["unresolved_50"] == 1, stats
    assert set(stats["mover_symbols"]) == {"HOLDER", "FADER", "GONE"}
    # Walang history rows sa araw na ito => None (walang phantom zero row).
    assert sq.compute_daily_mover_stats(db, date(2026, 8, 15)) is None


def test_refresh_upserts_ftd_and_is_idempotent(db: Session) -> None:
    _clean_non_orm_tables(db)
    _seed_mover_day(db)
    db.execute(
        text(
            "INSERT INTO sec_fails_to_deliver (settlement_date, symbol, cusip, fail_qty, price) "
            "VALUES (:d, :s, :c, :q, :p)"
        ),
        [
            {"d": date(2026, 8, 10), "s": "GONE", "c": "C1", "q": 500_000, "p": 2.5},
            {"d": date(2026, 8, 10), "s": "OTHER", "c": "C2", "q": 250_000, "p": 9.0},
        ],
    )
    db.commit()

    for _ in range(2):  # idempotent: pangalawang pass ay pareho ang resulta
        report = sq.refresh_squeeze_regime_daily(db, now=_NOW)
        assert _DAY.isoformat() in report, report

    rows = db.execute(
        text(
            "SELECT day, movers_50, movers_100, held_50, faded_50, unresolved_50, "
            "ftd_settlement_date, ftd_total_qty, ftd_symbols, ftd_mover_qty "
            "FROM momentum_squeeze_regime_daily"
        )
    ).fetchall()
    assert len(rows) == 1, rows
    r = rows[0]
    assert r[0] == _DAY
    assert (r[1], r[2], r[3], r[4], r[5]) == (3, 1, 1, 1, 1)
    assert r[6] == date(2026, 8, 10)
    assert r[7] == 750_000       # market total sa pinakabagong settlement date
    assert r[8] == 2             # distinct symbols
    assert r[9] == 500_000       # subset sa mga mover ng araw (GONE)


def _daily_rows(specs: list[tuple[int, int, int, int]]) -> list[dict]:
    """specs = [(movers_50, movers_100, held_50, faded_50)] BAGONG-UNA."""
    return [
        {
            "day": _DAY - timedelta(days=i), "movers_50": m50,
            "movers_100": m100, "held_50": h, "faded_50": f,
        }
        for i, (m50, m100, h, f) in enumerate(specs)
    ]


def test_label_squeeze_hold() -> None:
    rows = _daily_rows([(6, 2, 5, 1)] * 5 + [(5, 0, 1, 4)] * 20)
    reg = sq.label_from_daily_rows(rows)
    assert (reg.label, reg.reason) == ("squeeze", "squeeze_hold"), reg
    assert reg.window_movers_50 == 30 and reg.window_movers_100 == 10
    assert reg.as_of_day == _DAY.isoformat()


def test_label_squeeze_emergent_from_dead_baseline() -> None:
    # H1-2026 na hugis: patay na baseline (walang mover) -> biglang may mover na nagho-hold.
    rows = _daily_rows([(3, 1, 2, 1)] * 5 + [(0, 0, 0, 0)] * 20)
    reg = sq.label_from_daily_rows(rows)
    assert (reg.label, reg.reason) == ("squeeze", "squeeze_emergent"), reg


def test_label_suppression_fade() -> None:
    baseline = [(5, 0, 3, 2)] * 10 + [(5, 0, 2, 3)] * 10  # ratios .6 / .4
    rows = _daily_rows([(5, 0, 0, 5)] * 5 + baseline)     # window ratio 0
    reg = sq.label_from_daily_rows(rows)
    assert (reg.label, reg.reason) == ("suppression", "suppression_fade"), reg


def test_label_suppression_vanish() -> None:
    rows = _daily_rows([(0, 0, 0, 0)] * 5 + [(4, 1, 2, 2)] * 20)
    reg = sq.label_from_daily_rows(rows)
    assert (reg.label, reg.reason) == ("suppression", "suppression_vanish"), reg


def test_label_neutral_shapes() -> None:
    assert sq.label_from_daily_rows([]).reason == "no_daily_rows"
    thin = sq.label_from_daily_rows(_daily_rows([(5, 0, 3, 2)] * 5 + [(5, 0, 3, 2)] * 3))
    assert (thin.label, thin.reason) == ("neutral", "thin_baseline"), thin
    baseline = [(5, 0, 3, 2)] * 10 + [(5, 0, 2, 3)] * 10  # p20 .4 / p80 .6
    mixed = sq.label_from_daily_rows(_daily_rows([(5, 0, 3, 3)] * 5 + baseline))
    assert (mixed.label, mixed.reason) == ("neutral", "mixed"), mixed


def _seed_daily_table(db: Session, specs: list[tuple[int, int, int, int]]) -> None:
    db.execute(
        text(
            "INSERT INTO momentum_squeeze_regime_daily "
            "(day, movers_50, movers_100, held_50, faded_50) "
            "VALUES (:day, :m50, :m100, :h, :f)"
        ),
        [
            {"day": r["day"], "m50": r["movers_50"], "m100": r["movers_100"],
             "h": r["held_50"], "f": r["faded_50"]}
            for r in _daily_rows(specs)
        ],
    )
    db.commit()


def test_reader_and_breadth_regime_attach(db: Session) -> None:
    _clean_non_orm_tables(db)
    _seed_daily_table(db, [(6, 2, 5, 1)] * 5 + [(5, 0, 1, 4)] * 20)
    reg = sq.read_squeeze_regime(db)
    assert reg.label == "squeeze" and reg.as_of_day == _DAY.isoformat()
    # Nakalakip sa breadth-regime context (observability): ang wildcard axis ay
    # neutral (walang viability rows) pero dala nito ang squeeze axis.
    breg = compute_breadth_regime(db, now=_NOW)
    assert breg.squeeze_label == "squeeze"
    assert breg.squeeze_reason == "squeeze_hold"
    assert breg.squeeze_window_movers_50 == 30
    assert breg.is_wildcard is False  # hindi ginagalaw ang umiiral na axis


def test_kill_switch_is_neutral(db: Session) -> None:
    _clean_non_orm_tables(db)
    _seed_daily_table(db, [(6, 2, 5, 1)] * 5 + [(5, 0, 1, 4)] * 20)
    settings.chili_momentum_squeeze_regime_enabled = False
    try:
        assert sq.read_squeeze_regime(db).label == "neutral"
        assert sq.refresh_squeeze_regime_daily(db, now=_NOW) == {"skipped": "flag_off"}
        breg = compute_breadth_regime(db, now=_NOW)
        assert breg.squeeze_label == "neutral"
    finally:
        settings.chili_momentum_squeeze_regime_enabled = True


def test_empty_daily_table_is_neutral(db: Session) -> None:
    _clean_non_orm_tables(db)
    reg = sq.read_squeeze_regime(db)
    assert (reg.label, reg.reason) == ("neutral", "no_daily_rows")

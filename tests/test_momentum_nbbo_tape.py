"""NBBO spread tape — persist the clean consolidated bid/ask (Massive snapshot
lastQuote) for the Ross universe so the spread-sensitive replay uses REAL spreads
(project_momentum_zero_fills_root_cause)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

import app.services.massive_client as massive_client
from app.services.trading.momentum_neural.nbbo_tape import (
    _is_rth,
    _ross_row,
    prune_nbbo_tape,
    read_spread_profile,
    sample_universe_nbbo_spreads,
)


def _snap_entry(ticker, c, o, v, bid, ask):
    return {
        "ticker": ticker,
        "day": {"c": c, "o": o, "v": v},
        "lastQuote": {"p": bid, "P": ask},
    }


# ── pure: the Ross-universe + clean-NBBO row filter ──────────────────────────
def test_ross_row_valid_mover_with_clean_nbbo() -> None:
    r = _ross_row(_snap_entry("PAVS", 5.0, 4.5, 1_000_000, 4.95, 5.05))  # +11%, $5M, 200bps
    assert r is not None
    assert r["symbol"] == "PAVS"
    assert round(r["spread_bps"]) == 200
    assert r["bid"] == 4.95 and r["ask"] == 5.05


def test_ross_row_rejects_out_of_band_price() -> None:
    assert _ross_row(_snap_entry("BIGCO", 25.0, 22.0, 5_000_000, 24.9, 25.1)) is None  # >$20
    assert _ross_row(_snap_entry("SUBPENNY", 0.5, 0.4, 50_000_000, 0.49, 0.51)) is None  # <$1


def test_ross_row_rejects_thin_dollar_volume() -> None:
    assert _ross_row(_snap_entry("THIN", 5.0, 4.5, 100, 4.95, 5.05)) is None  # $500 << $1M


def test_ross_row_rejects_non_mover() -> None:
    assert _ross_row(_snap_entry("FLAT", 5.0, 4.95, 5_000_000, 4.98, 5.02)) is None  # +1% < 5%


def test_ross_row_rejects_crossed_or_invalid_nbbo() -> None:
    assert _ross_row(_snap_entry("CROSS", 5.0, 4.5, 5_000_000, 5.05, 4.95)) is None  # ask<bid
    assert _ross_row(_snap_entry("ZEROBID", 5.0, 4.5, 5_000_000, 0.0, 5.05)) is None


def test_ross_row_rejects_crypto_and_stale_wide() -> None:
    assert _ross_row(_snap_entry("BTC-USD", 5.0, 4.5, 9e9, 4.95, 5.05)) is None  # -USD
    # stale overnight quote: 1.0/2.0 on a 1.5 mid = 6667bps > 5000 sanity cap
    assert _ross_row(_snap_entry("STALE", 1.5, 1.3, 50_000_000, 1.0, 2.0)) is None


def test_ross_row_falls_back_to_prevday_when_current_zero() -> None:
    s = {"ticker": "PRE", "day": {"c": 0, "o": 0, "v": 0},
         "prevDay": {"c": 6.0, "o": 5.0, "v": 2_000_000}, "lastQuote": {"p": 5.95, "P": 6.05}}
    r = _ross_row(s)
    assert r is not None and r["symbol"] == "PRE"


# ── pure: RTH gating ─────────────────────────────────────────────────────────
def test_is_rth_weekday_window() -> None:
    assert _is_rth(datetime(2026, 6, 9, 14, 0, tzinfo=timezone.utc)) is True       # Tue 14:00 UTC
    assert _is_rth(datetime(2026, 6, 9, 13, 30, tzinfo=timezone.utc)) is True      # open edge
    assert _is_rth(datetime(2026, 6, 9, 21, 0, tzinfo=timezone.utc)) is False      # after close
    assert _is_rth(datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)) is False      # pre-open
    assert _is_rth(datetime(2026, 6, 13, 14, 0, tzinfo=timezone.utc)) is False     # Saturday


# ── integration: sample -> read -> prune (real test DB) ──────────────────────
def _ensure_table(db: Session) -> None:
    db.execute(text(
        "CREATE TABLE IF NOT EXISTS momentum_nbbo_spread_tape ("
        " id BIGSERIAL PRIMARY KEY, symbol VARCHAR(32) NOT NULL,"
        " observed_at TIMESTAMPTZ NOT NULL DEFAULT now(), bid DOUBLE PRECISION,"
        " ask DOUBLE PRECISION, mid DOUBLE PRECISION, spread_bps DOUBLE PRECISION,"
        " day_volume DOUBLE PRECISION, source VARCHAR(24) NOT NULL DEFAULT 'massive_snapshot')"
    ))
    db.execute(text("DELETE FROM momentum_nbbo_spread_tape"))
    db.commit()


def test_sample_inserts_only_ross_movers_and_reader_reads_back(monkeypatch, db: Session) -> None:
    _ensure_table(db)
    snap = [
        _snap_entry("PAVS", 5.0, 4.5, 1_000_000, 4.95, 5.05),   # valid mover -> inserted
        _snap_entry("BIGCO", 25.0, 22.0, 5_000_000, 24.9, 25.1),  # out of band -> skipped
        _snap_entry("FLAT", 5.0, 4.95, 5_000_000, 4.98, 5.02),    # non-mover -> skipped
    ]
    monkeypatch.setattr(massive_client, "get_full_market_snapshot", lambda **k: snap)
    out = sample_universe_nbbo_spreads(db, now_utc=datetime(2026, 6, 9, 14, 0, tzinfo=timezone.utc))
    assert out["ok"] and out["inserted"] == 1, out

    prof = read_spread_profile(db, "PAVS")
    assert len(prof) == 1
    assert round(prof[0]["spread_bps"]) == 200
    assert read_spread_profile(db, "BIGCO") == []  # never inserted


def test_sample_skipped_outside_rth(monkeypatch, db: Session) -> None:
    _ensure_table(db)
    monkeypatch.setattr(massive_client, "get_full_market_snapshot",
                        lambda **k: [_snap_entry("PAVS", 5.0, 4.5, 1_000_000, 4.95, 5.05)])
    out = sample_universe_nbbo_spreads(db, now_utc=datetime(2026, 6, 9, 22, 0, tzinfo=timezone.utc))
    assert out.get("skipped") == "outside_rth" and out["inserted"] == 0


def test_prune_removes_old_rows(db: Session) -> None:
    _ensure_table(db)
    db.execute(text(
        "INSERT INTO momentum_nbbo_spread_tape (symbol, observed_at, spread_bps) "
        "VALUES ('OLD', now() - interval '40 days', 100), ('NEW', now(), 100)"
    ))
    db.commit()
    res = prune_nbbo_tape(db, retention_days=30)
    assert res["ok"] and res["pruned"] == 1
    remaining = db.execute(text("SELECT symbol FROM momentum_nbbo_spread_tape ORDER BY symbol")).scalars().all()
    assert remaining == ["NEW"]

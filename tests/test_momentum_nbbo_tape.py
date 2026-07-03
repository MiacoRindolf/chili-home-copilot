"""NBBO spread tape — persist the clean consolidated bid/ask (Massive snapshot
lastQuote) for the Ross universe so the spread-sensitive replay uses REAL spreads
(project_momentum_zero_fills_root_cause)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

import app.services.massive_client as massive_client
from app.services.trading.momentum_neural.nbbo_tape import (
    _in_sampling_window,
    _ross_row,
    prune_nbbo_tape,
    read_spread_profile,
    sample_universe_nbbo_spreads,
    tape_running_up_symbols,
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


def test_ross_row_premarket_uses_live_tick_not_prevday() -> None:
    # #595: with the 'day' aggregate zeroed (premarket), the row must come from the
    # LIVE tick (lastTrade) + accumulated minute volume (min.av) — vs prevDay.c as
    # the change base. A zeroed day with NO live tick yields no row (the old
    # prevDay-price fallback graded premarket names by yesterday's move).
    s = {"ticker": "PRE", "day": {"c": 0, "o": 0, "v": 0},
         "lastTrade": {"p": 6.0}, "min": {"av": 2_000_000},
         "prevDay": {"c": 5.0}, "lastQuote": {"p": 5.95, "P": 6.05}}
    r = _ross_row(s)
    assert r is not None and r["symbol"] == "PRE"  # +20% vs prev close, $12M live vol
    dead = {"ticker": "GHOST", "day": {"c": 0, "o": 0, "v": 0},
            "prevDay": {"c": 6.0, "o": 5.0, "v": 2_000_000}, "lastQuote": {"p": 5.95, "P": 6.05}}
    assert _ross_row(dead) is None


# ── pure: RTH gating ─────────────────────────────────────────────────────────
def test_sampling_window_covers_data_session() -> None:
    # #595: the sampler covers the full US DATA session (04:00-20:00 ET), not RTH —
    # premarket movers (Ross gap-and-go) must already be on tape by the 7:00 entries.
    assert _in_sampling_window(datetime(2026, 6, 9, 14, 0, tzinfo=timezone.utc)) is True   # Tue 10:00 ET
    assert _in_sampling_window(datetime(2026, 6, 9, 9, 0, tzinfo=timezone.utc)) is True    # 05:00 ET premarket
    assert _in_sampling_window(datetime(2026, 6, 9, 22, 0, tzinfo=timezone.utc)) is True   # 18:00 ET afterhours
    assert _in_sampling_window(datetime(2026, 6, 9, 7, 0, tzinfo=timezone.utc)) is False   # 03:00 ET overnight
    assert _in_sampling_window(datetime(2026, 6, 13, 14, 0, tzinfo=timezone.utc)) is False  # Saturday


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


def test_sample_skipped_outside_data_session(monkeypatch, db: Session) -> None:
    _ensure_table(db)
    monkeypatch.setattr(massive_client, "get_full_market_snapshot",
                        lambda **k: [_snap_entry("PAVS", 5.0, 4.5, 1_000_000, 4.95, 5.05)])
    out = sample_universe_nbbo_spreads(db, now_utc=datetime(2026, 6, 13, 14, 0, tzinfo=timezone.utc))
    assert out.get("skipped") == "outside_session" and out["inserted"] == 0


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


# ── running-up feeder (the SKYQ gap): burst detection off the tape ───────────
def _seed_path(db: Session, sym: str, mids: list[float], minutes_ago_start: float = 4.0) -> None:
    """Insert a mid path for sym, evenly spaced from minutes_ago_start to now."""
    n = len(mids)
    for i, m in enumerate(mids):
        ago = minutes_ago_start * (1 - i / max(n - 1, 1))
        db.execute(text(
            "INSERT INTO momentum_nbbo_spread_tape (symbol, observed_at, mid, spread_bps) "
            "VALUES (:s, now() at time zone 'utc' - make_interval(secs => :ago), :m, 100)"
        ), {"s": sym, "ago": ago * 60.0, "m": m})
    db.commit()


def test_running_up_detects_burst_and_ignores_flat(db: Session) -> None:
    _ensure_table(db)
    _seed_path(db, "SKYQ", [1.80, 1.85, 1.90, 1.95])   # +8.3% over the window
    _seed_path(db, "FLAT", [5.00, 5.00, 5.01, 5.01])   # +0.2%
    assert tape_running_up_symbols(db) == ["SKYQ"]


def test_running_up_requires_min_samples(db: Session) -> None:
    _ensure_table(db)
    _seed_path(db, "ONEPRINT", [1.00, 2.00])  # 2 rows < 3-sample floor
    assert tape_running_up_symbols(db) == []


def test_running_up_orders_by_burst_and_caps(db: Session, monkeypatch) -> None:
    _ensure_table(db)
    from app.config import settings as _settings
    monkeypatch.setattr(_settings, "chili_momentum_running_up_max_symbols", 2, raising=False)
    _seed_path(db, "FAST", [1.00, 1.05, 1.10, 1.20])   # +20%
    _seed_path(db, "MED", [2.00, 2.05, 2.10, 2.16])    # +8%
    _seed_path(db, "SLOW", [3.00, 3.05, 3.08, 3.12])   # +4%
    assert tape_running_up_symbols(db) == ["FAST", "MED"]


def test_running_up_ignores_rows_outside_lookback(db: Session) -> None:
    _ensure_table(db)
    # burst happened 30+ minutes ago; only 2 fresh flat rows inside the window
    _seed_path(db, "OLDPOP", [1.00, 1.40, 1.50], minutes_ago_start=40.0)
    assert tape_running_up_symbols(db) == []


# ── data window LEADS the entry window (operator 2026-06-11, twice) ──────────
def test_data_session_open_is_derived_from_entry_window(monkeypatch) -> None:
    from app.config import settings as _settings
    from app.services.trading.momentum_neural.market_profile import _data_session_open_min

    # entries at 07:00 -> data keeps the historical 04:00 exchange open
    monkeypatch.setattr(_settings, "chili_momentum_premarket_start_et", "07:00", raising=False)
    assert _data_session_open_min() == 4 * 60
    # entries at 04:00 -> data PULLS FORWARD to 03:00 (entry − 60min lead)
    monkeypatch.setattr(_settings, "chili_momentum_premarket_start_et", "04:00", raising=False)
    assert _data_session_open_min() == 3 * 60
    # lead knob respected; never below midnight
    monkeypatch.setattr(_settings, "chili_momentum_selection_prep_lead_min", 300, raising=False)
    assert _data_session_open_min() == 0


# ── STEP-C #13: early-unlock recalibration (gap-OR-velocity + adaptive N) ─────

class _FakeRows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    """Stand-in SessionLocal() whose execute() returns pre-baked rows (no real DB)."""

    def __init__(self, rows):
        self._rows = rows

    def execute(self, *_a, **_k):
        return _FakeRows(self._rows)

    def rollback(self):
        pass

    def close(self):
        pass


def _patch_first_mover_db(monkeypatch, rows):
    import app.db as _db

    monkeypatch.setattr(_db, "SessionLocal", lambda: _FakeSession(rows))


def _patch_prior_close(monkeypatch, prior_close_by_sym):
    snap = [
        {"ticker": sym, "prevDay": {"c": pc}}
        for sym, pc in prior_close_by_sym.items()
    ]
    monkeypatch.setattr(massive_client, "get_full_market_snapshot", lambda **k: snap)


def test_early_unlock_overnight_gapper_with_flat_tape_qualifies(monkeypatch) -> None:
    """A +40% overnight gapper prints a FLAT 5-min tape (first_mid ~= last_mid) — the
    velocity-only test structurally never passed it. With the GAP axis it qualifies off
    gap-vs-prior-close (5.60 vs a 4.00 prior close = +40%)."""
    from app.services.trading.momentum_neural.nbbo_tape import early_premarket_first_mover

    now = datetime(2026, 6, 9, 9, 0, tzinfo=timezone.utc)  # inside the window
    fresh_row = now.replace(tzinfo=None)  # freshest row is "now" (well within freshness_sec)
    # first_mid == last_mid => 0% trailing velocity; but mid 5.60 vs prior close 4.00 = +40%.
    rows = [("GAPR", 5.60, 5.60, fresh_row, fresh_row)]
    _patch_first_mover_db(monkeypatch, rows)
    _patch_prior_close(monkeypatch, {"GAPR": 4.00})

    first_at, n, lead, pct = early_premarket_first_mover(
        now_utc=now, window_min=5.0, min_move_pct=5.0, freshness_sec=30.0, gap_floor_pct=5.0,
    )
    assert n == 1
    assert lead == "GAPR"
    assert pct == 40.0  # ranked by the stronger (gap) axis


def test_early_unlock_flat_non_gapper_does_not_qualify(monkeypatch) -> None:
    """A flat name (tiny velocity AND a tiny gap vs prior close) must NOT qualify."""
    from app.services.trading.momentum_neural.nbbo_tape import early_premarket_first_mover

    now = datetime(2026, 6, 9, 9, 0, tzinfo=timezone.utc)
    fresh_row = now.replace(tzinfo=None)
    rows = [("FLAT", 5.00, 5.01, fresh_row, fresh_row)]  # +0.2% velocity
    _patch_first_mover_db(monkeypatch, rows)
    _patch_prior_close(monkeypatch, {"FLAT": 4.98})  # 5.01 vs 4.98 = +0.6% gap

    first_at, n, lead, pct = early_premarket_first_mover(
        now_utc=now, window_min=5.0, min_move_pct=5.0, freshness_sec=30.0, gap_floor_pct=5.0,
    )
    assert first_at is None
    assert n == 0
    assert lead is None


def test_early_unlock_velocity_axis_still_qualifies_without_prior_close(monkeypatch) -> None:
    """Velocity axis unchanged: a live runner (+10% over the window) qualifies even when the
    prior-close snapshot is unavailable (gap axis degrades to velocity-only, fail-open)."""
    from app.services.trading.momentum_neural.nbbo_tape import early_premarket_first_mover

    now = datetime(2026, 6, 9, 9, 0, tzinfo=timezone.utc)
    fresh_row = now.replace(tzinfo=None)
    rows = [("RUNR", 5.00, 5.50, fresh_row, fresh_row)]  # +10% trailing velocity
    _patch_first_mover_db(monkeypatch, rows)
    monkeypatch.setattr(massive_client, "get_full_market_snapshot", lambda **k: [])  # no prior close

    first_at, n, lead, pct = early_premarket_first_mover(
        now_utc=now, window_min=5.0, min_move_pct=5.0, freshness_sec=30.0, gap_floor_pct=5.0,
    )
    assert n == 1
    assert lead == "RUNR"
    assert pct == 10.0


def test_early_unlock_adaptive_n_clamps_configured_min_movers_to_coverage(monkeypatch) -> None:
    """ADAPTIVE N: the configured min-movers is a CEILING. With only 2 concurrent movers on
    the tape (coverage p75 = 2) a configured 3-bar clamps to 2, so a 2-mover day unlocks —
    the exact premarket-zero fix (max concurrent was 2, the 3-bar was structurally dead)."""
    import app.services.trading.momentum_neural.market_profile as mp
    import app.services.trading.momentum_neural.nbbo_tape as nt
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "chili_momentum_premarket_start_et", "07:00", raising=False)
    monkeypatch.setattr(_settings, "chili_momentum_early_premarket_enabled", True, raising=False)
    monkeypatch.setattr(_settings, "chili_momentum_early_premarket_min_movers", 3, raising=False)

    # Two qualifying movers surfaced at 04:20 ET; coverage p75 = 2.
    monkeypatch.setattr(
        nt, "early_premarket_first_mover",
        lambda **k: (datetime(2026, 6, 9, 8, 20, tzinfo=timezone.utc), 2, "AAA", 22.0),
    )
    monkeypatch.setattr(nt, "early_premarket_mover_coverage_p75", lambda **k: 2)

    # 05:00 ET, before the 07:00 fixed start — only unlocks if the 2 movers clear the bar.
    ok, first, detail = mp.early_premarket_unlocked(now=datetime(2026, 6, 9, 9, 0, tzinfo=timezone.utc))
    assert ok is True
    assert detail["min_movers"] == 2  # clamped from configured 3
    assert detail["configured_min_movers"] == 3
    assert detail["coverage_p75"] == 2

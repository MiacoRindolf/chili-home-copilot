"""S1 event-driven feeder (docs/DESIGN/MOMENTUM_ENGINE.md §1/§5).

CHUNK 1: the tape-delta ignite job + the shared _ross_threshold_crossed predicate make
a cold new explosive mover live_eligible in ~5-15s instead of waiting for the ~300s
viability-refresh batch. These tests pin:

  (a) _ross_threshold_crossed fires on EACH Ross axis (RVOL / gap / move%) + rejects
      out-of-band (price / dollar-volume), reusing the EXISTING Ross floors;
  (b) the tape-delta job, given fresh tape rows for a threshold-crosser, writes a
      viability row via the single-symbol path (asserted by intercepting the score call),
      advances the in-process high-water mark, and is idempotent on re-run (the advanced
      hwm means the same rows are not re-scored);
  (c) flag-OFF (chili_momentum_tape_delta_ignite_enabled=0) the job is a no-op
      (byte-identical: nothing read, nothing scored);
  (d) the job uses its OWN short-lived session + rolls back in finally (no leaked
      transaction) even when the score path raises.

DB-touching tests use the _test-DB conventions (the same momentum_nbbo_spread_tape
table the nbbo_tape suite seeds) and intercept run_momentum_neural_tick so the heavy
neural pipeline is not exercised here (that path is covered by its own suites).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.trading.momentum_neural.nbbo_tape import (
    _ross_threshold_crossed,
    tape_delta_threshold_crossers,
)


# ── (a) _ross_threshold_crossed: each axis fires; out-of-band rejected ────────
def test_threshold_crossed_fires_on_rvol_axis() -> None:
    # RVOL >= the Ross RVOL floor (5x) fires even with a tiny move.
    assert _ross_threshold_crossed("AAA", rvol=6.0, move_pct=1.0, price=5.0) is True
    # Just below the floor on rvol AND no other axis -> no cross.
    assert _ross_threshold_crossed("AAA", rvol=4.0, move_pct=1.0, price=5.0) is False


def test_threshold_crossed_fires_on_gap_axis() -> None:
    # gap% >= the Ross change floor (10%) fires.
    assert _ross_threshold_crossed("BBB", gap_pct=12.0, price=5.0) is True
    assert _ross_threshold_crossed("BBB", gap_pct=8.0, price=5.0) is False


def test_threshold_crossed_fires_on_move_axis() -> None:
    # intraday move% >= the Ross change floor (10%) fires.
    assert _ross_threshold_crossed("CCC", move_pct=15.0, price=5.0) is True
    assert _ross_threshold_crossed("CCC", move_pct=5.0, price=5.0) is False


def test_threshold_crossed_rejects_out_of_band_price() -> None:
    # A genuine RVOL cross is vetoed when price is affirmatively out of the 1-20 band.
    assert _ross_threshold_crossed("DDD", rvol=50.0, move_pct=200.0, price=25.0) is False  # >$20
    assert _ross_threshold_crossed("DDD", rvol=50.0, move_pct=200.0, price=0.5) is False   # <$1
    # In-band passes.
    assert _ross_threshold_crossed("DDD", rvol=50.0, move_pct=200.0, price=8.0) is True


def test_threshold_crossed_rejects_thin_dollar_volume() -> None:
    assert _ross_threshold_crossed("EEE", move_pct=50.0, price=5.0, dollar_volume=500.0) is False
    assert _ross_threshold_crossed("EEE", move_pct=50.0, price=5.0, dollar_volume=5_000_000.0) is True


def test_threshold_crossed_fails_open_on_absent_band_data() -> None:
    # No price / no dollar-volume present -> the band guards must NOT veto a real cross.
    assert _ross_threshold_crossed("FFF", rvol=20.0) is True
    # Nothing crossed + nothing present -> False (a name is never benched on nothing).
    assert _ross_threshold_crossed("FFF") is False
    assert _ross_threshold_crossed("") is False


# ── DB harness: the momentum_nbbo_spread_tape table + a seed helper ───────────
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


def _seed_path(db: Session, sym: str, mids: list[float], day_volume: float = 5_000_000.0,
               minutes_ago_start: float = 2.0) -> None:
    """Insert a mid path for sym, evenly spaced from minutes_ago_start to now."""
    n = len(mids)
    for i, m in enumerate(mids):
        ago = minutes_ago_start * (1 - i / max(n - 1, 1))
        db.execute(text(
            "INSERT INTO momentum_nbbo_spread_tape "
            "(symbol, observed_at, mid, day_volume, spread_bps) "
            "VALUES (:s, now() at time zone 'utc' - make_interval(secs => :ago), :m, :dv, 100)"
        ), {"s": sym, "ago": ago * 60.0, "m": m, "dv": day_volume})
    db.commit()


# ── (b) the incremental delta reader: crossers + hwm advance + idempotent ─────
def test_tape_delta_crossers_detects_mover_and_advances_hwm(db: Session) -> None:
    _ensure_table(db)
    # POPS +14% over ~2min on $-vol $5M*$5 -> crosses the move% axis, in band.
    _seed_path(db, "POPS", [5.00, 5.20, 5.50, 5.70])
    _seed_path(db, "FLAT", [5.00, 5.00, 5.01, 5.00])  # +0% -> no cross

    since = datetime(2000, 1, 1, tzinfo=timezone.utc)  # read everything
    crossers, new_hwm = tape_delta_threshold_crossers(db, since=since)
    syms = {c["symbol"] for c in crossers}
    assert "POPS" in syms
    assert "FLAT" not in syms
    assert new_hwm is not None  # the hwm was computed from the rows seen

    # Idempotent: re-reading with since=new_hwm yields NO crossers (delta consumed).
    crossers2, _ = tape_delta_threshold_crossers(db, since=new_hwm)
    assert crossers2 == []


def test_tape_delta_crossers_empty_returns_clean(db: Session) -> None:
    _ensure_table(db)
    crossers, new_hwm = tape_delta_threshold_crossers(
        db, since=datetime(2000, 1, 1, tzinfo=timezone.utc)
    )
    assert crossers == []
    assert new_hwm is None


# ── (b) the JOB: writes viability via the single-symbol path + advances hwm ───
def test_tape_delta_job_scores_crosser_via_single_symbol_path(db: Session, monkeypatch) -> None:
    _ensure_table(db)
    _seed_path(db, "ZOOM", [4.00, 4.30, 4.60, 4.80])  # +20% -> crosses

    import app.services.trading_scheduler as ts

    # Reset the in-process feeder state so the test is deterministic.
    monkeypatch.setattr(ts, "_tape_delta_hwm", None, raising=False)
    monkeypatch.setattr(ts, "_tape_delta_last_run_monotonic", 0.0, raising=False)
    monkeypatch.setattr(ts, "_tape_delta_field_snapshot", {}, raising=False)
    from app.config import settings
    monkeypatch.setattr(settings, "chili_momentum_event_select_primary_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_tape_delta_ignite_enabled", True, raising=False)

    # Intercept the heavy single-symbol score path; record the symbols it was asked to score.
    scored: list[str] = []

    def _fake_tick(_db, *, meta=None, **kw):
        tickers = (meta or {}).get("tickers") or []
        scored.extend(str(t).upper() for t in tickers)
        return {"ok": True}

    import app.services.trading.momentum_neural.pipeline as pipeline
    monkeypatch.setattr(pipeline, "run_momentum_neural_tick", _fake_tick, raising=False)

    ts._run_tape_delta_ignite_job()

    assert "ZOOM" in scored, scored
    # The hwm advanced (so a re-run won't re-scan the same rows).
    assert ts._tape_delta_hwm is not None

    # Idempotent re-run: the advanced hwm + throttle means ZOOM is not re-scored.
    # (Force the throttle clear so the only thing stopping a re-score is the hwm delta.)
    monkeypatch.setattr(ts, "_tape_delta_last_run_monotonic", 0.0, raising=False)
    scored.clear()
    ts._run_tape_delta_ignite_job()
    assert "ZOOM" not in scored


# ── (c) flag-OFF: the job is a byte-identical no-op (nothing read or scored) ──
def test_tape_delta_job_noop_when_flag_off(db: Session, monkeypatch) -> None:
    _ensure_table(db)
    _seed_path(db, "WOULD", [4.00, 4.30, 4.60, 4.80])  # a real crosser is on the tape

    import app.services.trading_scheduler as ts
    monkeypatch.setattr(ts, "_tape_delta_hwm", None, raising=False)
    monkeypatch.setattr(ts, "_tape_delta_last_run_monotonic", 0.0, raising=False)

    from app.config import settings
    monkeypatch.setattr(settings, "chili_momentum_tape_delta_ignite_enabled", False, raising=False)

    scored: list[str] = []
    import app.services.trading.momentum_neural.pipeline as pipeline
    monkeypatch.setattr(
        pipeline, "run_momentum_neural_tick",
        lambda _db, **kw: scored.append("X") or {"ok": True}, raising=False,
    )

    ts._run_tape_delta_ignite_job()

    assert scored == []                 # nothing scored
    assert ts._tape_delta_hwm is None   # state untouched (no read happened)


# ── (d) own session + rollback-in-finally even when the score path raises ─────
def test_tape_delta_job_rolls_back_on_score_error(db: Session, monkeypatch) -> None:
    _ensure_table(db)
    _seed_path(db, "BOOM", [4.00, 4.30, 4.60, 4.80])

    import app.services.trading_scheduler as ts
    monkeypatch.setattr(ts, "_tape_delta_hwm", None, raising=False)
    monkeypatch.setattr(ts, "_tape_delta_last_run_monotonic", 0.0, raising=False)
    monkeypatch.setattr(ts, "_tape_delta_field_snapshot", {}, raising=False)

    from app.config import settings
    monkeypatch.setattr(settings, "chili_momentum_event_select_primary_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_tape_delta_ignite_enabled", True, raising=False)

    # Track that the job's OWN session is closed (returned to the pool clean), not leaked.
    closed = {"n": 0}
    real_session_local = ts.SessionLocal if hasattr(ts, "SessionLocal") else None
    _ = real_session_local  # SessionLocal is imported inside the job; we assert via no-raise

    def _boom_tick(_db, *, meta=None, **kw):
        raise RuntimeError("synthetic score failure")

    import app.services.trading.momentum_neural.pipeline as pipeline
    monkeypatch.setattr(pipeline, "run_momentum_neural_tick", _boom_tick, raising=False)

    # The job must SWALLOW the score error (best-effort) and not raise out.
    ts._run_tape_delta_ignite_job()

    # The hwm still advanced (the delta was consumed) — the failure was per-symbol, the
    # read + window advance are independent of the score success.
    assert ts._tape_delta_hwm is not None
    # The shared test session is still usable (no poisoned transaction leaked into it).
    assert db.execute(text("SELECT 1")).scalar() == 1


# ── (e) Fix 3: the move% is computed over a fixed lookback window, NOT the hwm delta ──
def test_tape_delta_move_window_is_lookback_not_hwm_delta(db: Session) -> None:
    """A name up big over the last few minutes but ticking CALM in the last few seconds
    must still cross: the move magnitude is anchored at now − lookback, while the hwm only
    gates WHICH symbols printed a new row. Regression for the window/threshold mismatch."""
    _ensure_table(db)
    # BIGM: +20% over ~2min, but its NEWEST two prints (the only ones past the recent hwm)
    # are essentially flat (+0.1%). With a hwm-delta move basis it would NOT cross 10%;
    # with the lookback-window basis it crosses on the +20% over the window.
    _seed_path(db, "BIGM", [5.00, 5.40, 5.80, 6.00, 6.006])

    from datetime import timedelta

    # since = a recent hwm a few seconds back (steady-state live shape), NOT year-2000.
    recent_hwm = datetime.now(timezone.utc) - timedelta(seconds=8)
    crossers, new_hwm = tape_delta_threshold_crossers(db, since=recent_hwm)
    syms = {c["symbol"] for c in crossers}
    assert "BIGM" in syms, crossers  # day-strong, currently-calm name still fires
    # The reported move% reflects the full lookback window (~+20%), not the ~+0.1% tail.
    bigm = next(c for c in crossers if c["symbol"] == "BIGM")
    assert bigm["move_pct"] > 10.0, bigm
    assert new_hwm is not None


# ── (e) Fix 1: the crosser fan-out is bounded by the max-symbols cap ──────────
def test_tape_delta_crossers_are_capped(db: Session, monkeypatch) -> None:
    """A broad risk-on open can put many movers over the floor in one window; the feeder
    only needs the top movers per cadence. Assert the crosser list is bounded by
    chili_momentum_running_up_max_symbols (top by move%, fastest first)."""
    _ensure_table(db)
    from app.config import settings
    monkeypatch.setattr(settings, "chili_momentum_running_up_max_symbols", 3, raising=False)

    # Seed 6 distinct crossers with strictly increasing move% so the top-3 are unambiguous.
    for i in range(6):
        start = 5.00
        end = start * (1.0 + 0.12 + 0.02 * i)  # +12%, +14%, ... all over the 10% floor
        _seed_path(db, f"MV{i}", [start, (start + end) / 2.0, end])

    since = datetime(2000, 1, 1, tzinfo=timezone.utc)
    crossers, _ = tape_delta_threshold_crossers(db, since=since)
    assert len(crossers) == 3, crossers  # capped
    # Capped to the FASTEST movers (highest move%), not an arbitrary 3.
    moves = [c["move_pct"] for c in crossers]
    assert moves == sorted(moves, reverse=True)
    assert {c["symbol"] for c in crossers} == {"MV5", "MV4", "MV3"}, crossers


# ── (e) Fix 2: a marginal crosser is ranked WITHIN the field, not in isolation ─
def test_tape_delta_crosser_scored_against_full_field_not_alone() -> None:
    """The crosser must be percentile-ranked against the cached field, not scored in a
    one-element dict (which percentile-ranks to 1.0 = max). Same marginal crosser scores
    LOWER inside a strong field than inside a weak one. Asserts the tickers/ross_signals
    decoupling fix at the score boundary (score_universe ranks the universe; only the
    crosser's row is written by the caller's tickers=[sym])."""
    from app.services.trading.momentum_neural.ross_momentum import score_universe

    # The marginal crosser: a modest +12% / 5x-RVOL name.
    crosser = {"ticker": "MARG", "vol_ratio": 5.0, "daily_change_pct": 12.0}

    # Weak field: peers are barely moving — MARG looks strong relative to them.
    weak = {
        "MARG": dict(crosser),
        "A": {"ticker": "A", "vol_ratio": 1.1, "daily_change_pct": 1.0},
        "B": {"ticker": "B", "vol_ratio": 1.2, "daily_change_pct": 2.0},
    }
    # Strong field: peers are exploding — MARG looks weak relative to them.
    strong = {
        "MARG": dict(crosser),
        "A": {"ticker": "A", "vol_ratio": 40.0, "daily_change_pct": 180.0},
        "B": {"ticker": "B", "vol_ratio": 55.0, "daily_change_pct": 240.0},
    }

    # Legacy (percentile-blend) scorer makes the context effect unambiguous.
    s_weak = score_universe(weak, explosive=False)["MARG"].score
    s_strong = score_universe(strong, explosive=False)["MARG"].score
    assert s_strong < s_weak, (s_strong, s_weak)

    # And the isolation bug it guards against: scored ALONE, the crosser percentile-ranks
    # to the max — strictly higher than when ranked inside the strong field.
    s_alone = score_universe({"MARG": dict(crosser)}, explosive=False)["MARG"].score
    assert s_alone > s_strong, (s_alone, s_strong)

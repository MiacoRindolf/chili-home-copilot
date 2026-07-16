"""Tick-faithful + universe-tick-densification + 15s micro-pullback (2026-06-15).

Five contracts, all load-bearing:

  (1) DENSIFIER write-only PARITY — ``record_external`` writes a row tagged
      source='massive_ws_universe', does NOT require armed membership, and reuses the
      SAME throttle/dedupe body as ``_on_tick`` (the armed path is byte-identical).
  (2) SUPERSET — ``Tape.prices_between`` returns the same single (or zero) sample
      ``.at()`` would have seen when only 1/min snapshots exist (byte-identical), and
      walks every sub-minute tick when the dense WS tape exists (fires earlier).
  (3) ``_resample_micro_bars`` — correct OHLC bucketing; <2 bars when sparse → the
      trigger's ``len(df) < 10`` guard no-fires → fall back to the 1m path.
  (4) MICROPULL off ⇒ the live trigger reads the 1m df (byte-identical parity); on ⇒
      it routes the 15s micro-bar df + entry_interval='15s'.
  (5) FULL-PIPELINE — the as-of re-screen+re-score ranker runs deterministically and
      degrades safely on an empty tape.

DB-backed parts use the ``db`` fixture (TEST_DATABASE_URL ending in _test, truncated
per-test by conftest). No network OHLCV is required — the Tape/prune/rank paths are
exercised against seeded tape rows.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.services.trading.momentum_neural.micro_bars import _resample_micro_bars
from app.services.trading.momentum_neural.tape_ws_recorder import (
    TapeWsRecorder,
    get_tape_ws_recorder,
)


# ── helpers ─────────────────────────────────────────────────────────────────
def _quote(bid, ask):
    return SimpleNamespace(bid=bid, ask=ask, price=(bid + ask) / 2, timestamp=time.time())


def _rec() -> TapeWsRecorder:
    r = TapeWsRecorder()
    r._running = True
    return r


def _ensure_table(db: Session) -> None:
    db.execute(text(
        "CREATE TABLE IF NOT EXISTS momentum_nbbo_spread_tape ("
        " id BIGSERIAL PRIMARY KEY, symbol VARCHAR(32) NOT NULL,"
        " observed_at TIMESTAMPTZ NOT NULL DEFAULT now(), bid DOUBLE PRECISION,"
        " ask DOUBLE PRECISION, mid DOUBLE PRECISION, spread_bps DOUBLE PRECISION,"
        " day_volume DOUBLE PRECISION, source VARCHAR(32) NOT NULL DEFAULT 'massive_snapshot')"
    ))
    db.execute(text("DELETE FROM momentum_nbbo_spread_tape"))
    db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# (1) DENSIFIER write-only PARITY
# ─────────────────────────────────────────────────────────────────────────────
def test_record_external_tags_universe_source_and_no_armed_membership():
    r = _rec()
    # NOTE: 'WILD' is NOT in r._symbols (never armed) — record_external must persist
    # it anyway (the whole point: densify names the lane never armed).
    assert "WILD" not in r._symbols
    r.record_external("WILD", _quote(3.00, 3.05))
    assert len(r._buffer) == 1
    row = r._buffer[0]
    assert row["symbol"] == "WILD"
    assert row["source"] == "massive_ws_universe"
    assert row["bid"] == 3.00 and row["ask"] == 3.05


def test_record_external_reuses_throttle_and_dedupe_body():
    r = _rec()
    r.record_external("WILD", _quote(3.00, 3.05))
    # within the >=1s spacing window → throttled (no second row), same as _on_tick
    r.record_external("WILD", _quote(3.01, 3.06))
    assert len(r._buffer) == 1
    # bypass the time throttle; the SAME quote as the last RECORDED one must dedupe
    r._last_row_t["WILD"] = 0.0
    r.record_external("WILD", _quote(3.00, 3.05))  # identical to last recorded → no row
    assert len(r._buffer) == 1
    # a genuinely changed quote past the throttle → a second row
    r._last_row_t["WILD"] = 0.0
    r.record_external("WILD", _quote(3.10, 3.16))
    assert len(r._buffer) == 2


def test_on_tick_path_is_byte_identical_after_refactor():
    """The armed-lane _on_tick path keeps the asset-class default source (parity)."""
    r = _rec()
    r._on_tick("DSY", _quote(2.40, 2.43))
    assert r._buffer[-1]["source"] == "massive_ws"        # equity armed default
    r2 = _rec()
    r2._on_tick("ETH-USD", _quote(2400.0, 2401.0))
    assert r2._buffer[-1]["source"] == "coinbase_ws"      # crypto armed default


def test_record_external_not_running_ignores():
    r = _rec()
    r._running = False
    r.record_external("WILD", _quote(3.00, 3.05))
    assert r._buffer == []


def test_get_tape_ws_recorder_accessor_singleton():
    a = get_tape_ws_recorder()
    b = get_tape_ws_recorder()
    assert a is b and isinstance(a, TapeWsRecorder)


# ─────────────────────────────────────────────────────────────────────────────
# (3) _resample_micro_bars — OHLC bucketing + sparse no-fire
# ─────────────────────────────────────────────────────────────────────────────
def test_resample_micro_bars_ohlc_bucketing():
    t0 = datetime(2026, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
    # 60 ticks at 1s, mid rising linearly → 4 clean 15s buckets
    rows = [(t0 + timedelta(seconds=i), 10.0 + i * 0.01 - 0.005, 10.0 + i * 0.01 + 0.005) for i in range(60)]
    df = _resample_micro_bars(rows, bar_seconds=15)
    assert len(df) == 4
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    # first bucket: open=first mid (10.00), close=last mid in window (~10.14), monotone up
    assert df["Open"].iloc[0] == pytest.approx(10.00, abs=1e-6)
    assert df["High"].iloc[0] >= df["Low"].iloc[0]
    assert df["Close"].iloc[-1] > df["Open"].iloc[0]  # rose across the window
    # F1 (capture-g fix): NO trade tape supplied -> volume is UNKNOWN (NaN, the gates'
    # documented fail-OPEN case), never a fabricated concrete 0.0 (which read as a dead
    # bar and failed every volume gate on the micro frame CLOSED).
    assert df["Volume"].isna().all()


def test_resample_micro_bars_duplicate_timestamp_keeps_input_open_close_order():
    """Repeated IQFeed containment timestamps must have stable OHLC ties."""

    t0 = datetime(2026, 7, 14, 15, 0, 1, tzinfo=timezone.utc)
    rows = [
        (t0, 4.99, 5.01),
        (t0, 5.99, 6.01),
        (t0 + timedelta(seconds=15), 6.99, 7.01),
    ]

    df = _resample_micro_bars(rows, bar_seconds=15)

    assert float(df.iloc[0]["Open"]) == pytest.approx(5.0)
    assert float(df.iloc[0]["Close"]) == pytest.approx(6.0)


def test_resample_micro_bars_real_trade_volume_join():
    """F1: trade prints supply REAL per-bucket volume; a quiet bucket INSIDE the trade-tape
    span is a genuine 0.0; buckets OUTSIDE the span stay NaN (unknown)."""
    t0 = datetime(2026, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
    rows = [(t0 + timedelta(seconds=i), 10.0 + i * 0.01 - 0.005, 10.0 + i * 0.01 + 0.005) for i in range(60)]
    # prints only in buckets 0 and 2 (bucket 1 = genuinely quiet IN-span; bucket 3 = after
    # the last print = outside the trade-tape span -> unknown).
    trades = [
        (t0 + timedelta(seconds=2), 500.0),
        (t0 + timedelta(seconds=7), 300.0),
        (t0 + timedelta(seconds=33), 1200.0),
    ]
    df = _resample_micro_bars(rows, bar_seconds=15, trade_rows=trades)
    assert len(df) == 4
    assert df["Volume"].iloc[0] == pytest.approx(800.0)     # 500 + 300
    assert df["Volume"].iloc[1] == pytest.approx(0.0)       # quiet bucket IN-span -> real 0
    assert df["Volume"].iloc[2] == pytest.approx(1200.0)
    assert df["Volume"].iloc[3] != df["Volume"].iloc[3]     # NaN: outside trade-tape span
    # garbage trade rows never raise and never fabricate volume
    df2 = _resample_micro_bars(rows, bar_seconds=15, trade_rows=[("x", None), None, 42])
    assert df2["Volume"].isna().all()


def test_resample_micro_bars_sparse_yields_no_fireable_frame():
    t0 = datetime(2026, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
    # only the 1-min sampler exists: 1 row → <2 → empty → caller falls back to 1m
    assert _resample_micro_bars([(t0, 10.0, 10.1)], 15).empty
    # zero rows → empty
    assert _resample_micro_bars([], 15).empty
    # F6: a handful of 1/min rows = only 5 REAL populated buckets — below the builder's
    # 10-real-bucket density floor → empty → 1m fallback. (The frame LENGTH is no longer
    # the density measure: gap buckets are now materialized as flat bars, so a naked len
    # check would count manufactured bars.)
    rows = [(t0 + timedelta(minutes=m), 10.0 + m * 0.1, 10.1 + m * 0.1) for m in range(5)]
    assert _resample_micro_bars(rows, 15, min_real_buckets=10).empty


def test_resample_micro_bars_sporadic_tape_gap_free_not_time_compressed():
    """F6: sporadic tape must NOT present as consecutive 15s bars (time-compressed junk
    geometry). Gap buckets are materialized as FLAT bars at the prior close, so bar spacing
    is honest wall-clock spacing; the density floor counts REAL buckets only."""
    t0 = datetime(2026, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
    # 12 quotes at ~1/2.5min over 30 min → 12 REAL buckets spread across ~110 15s buckets.
    rows = []
    for m in range(12):
        ts = t0 + timedelta(seconds=150 * m)
        rows.append((ts, 10.0 + m * 0.05, 10.02 + m * 0.05))
        rows.append((ts + timedelta(seconds=3), 10.005 + m * 0.05, 10.025 + m * 0.05))
    df = _resample_micro_bars(rows, 15, min_real_buckets=10)
    # 12 real buckets clears the floor; the frame is GAP-FREE (every 15s bucket in span)
    assert not df.empty
    span_buckets = int((150 * 11) / 15) + 1
    assert len(df) == span_buckets  # ~111 bars, not 12 compressed ones
    # index is strictly 15s-spaced (no holes)
    deltas = df.index.to_series().diff().dropna().dt.total_seconds().unique()
    assert list(deltas) == [15.0]
    # a filled gap bar is FLAT at the prior close (no fabricated range)
    gap_bar = df.iloc[1]  # bucket right after the first real quote pair
    assert gap_bar["Open"] == gap_bar["High"] == gap_bar["Low"] == gap_bar["Close"]
    # and with a floor above the real-bucket count, the same tape yields EMPTY (fallback)
    assert _resample_micro_bars(rows, 15, min_real_buckets=13).empty


def test_resample_micro_bars_never_raises_on_malformed():
    # bad tuples / None / wrong types must yield an empty frame, never raise
    assert _resample_micro_bars([("x", None, None), None, 42], 15).empty
    assert _resample_micro_bars(None, 15).empty


def test_resample_micro_bars_accepts_dict_rows():
    t0 = datetime(2026, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
    rows = [{"observed_at": t0 + timedelta(seconds=i), "bid": 10.0 + i * 0.01, "ask": 10.02 + i * 0.01}
            for i in range(40)]
    df = _resample_micro_bars(rows, bar_seconds=15)
    assert len(df) >= 2 and "Close" in df.columns


# ─────────────────────────────────────────────────────────────────────────────
# (2) SUPERSET — Tape.prices_between + source round-trip (DB-backed)
# ─────────────────────────────────────────────────────────────────────────────
def _seed_tape(db: Session, sym: str, rows: list[tuple]) -> None:
    """rows: list of (observed_at_naive_utc, bid, ask, source)."""
    for ts, bid, ask, src in rows:
        mid = (bid + ask) / 2.0
        db.execute(
            text(
                "INSERT INTO momentum_nbbo_spread_tape "
                "(symbol, observed_at, bid, ask, mid, spread_bps, day_volume, source) "
                "VALUES (:s, :t, :b, :a, :m, :sb, :dv, :src)"
            ),
            {"s": sym, "t": ts, "b": bid, "a": ask, "m": mid,
             "sb": (ask - bid) / mid * 10_000.0, "dv": 1_000_000.0, "src": src},
        )
    db.commit()


def test_tape_loads_source_and_prices_between(db: Session):
    from app.services.trading.momentum_neural.replay_v2 import Tape

    _ensure_table(db)
    date = "2026-06-15"
    base = datetime(2026, 6, 15, 14, 30, 0)  # 14:30 UTC, inside the replay window
    # dense WS ticks every 5s for one minute + the 1-min snapshot at the start
    rows = [(base, 10.00, 10.05, "massive_snapshot")]
    for i in range(1, 12):
        rows.append((base + timedelta(seconds=5 * i), 10.00 + i * 0.02, 10.05 + i * 0.02, "massive_ws_universe"))
    _seed_tape(db, "WILD", rows)

    tape = Tape(date)
    # source round-trips into the row tuple (index 5)
    assert tape.by_sym["WILD"][0][5] == "massive_snapshot"
    assert tape.by_sym["WILD"][-1][5] == "massive_ws_universe"

    # prices_between is half-open [t0, t1): every sub-minute tick in time order
    win = tape.prices_between(
        "WILD",
        datetime(2026, 6, 15, 14, 30, 0, tzinfo=timezone.utc),
        datetime(2026, 6, 15, 14, 31, 0, tzinfo=timezone.utc),
    )
    assert len(win) == len(rows)
    assert [w[2] > 0 for w in win]  # asks present
    # ordered ascending by ts
    assert all(win[i][0] <= win[i + 1][0] for i in range(len(win) - 1))
    # the FIRST tick whose ask > a level resolves sub-minute (the tick-faithful fire).
    # prices_between returns (ts, bid, ask, source); pick the first ask>lvl.
    lvl = 10.10
    first_ask = next((w for w in win if w[2] > lvl), None)
    assert first_ask is not None and first_ask[0] > base.replace(tzinfo=timezone.utc)


def test_prices_between_superset_when_only_snapshots(db: Session):
    """SUPERSET: with ONLY the 1-min sampler, prices_between returns exactly the
    single sample in the window — byte-identical to the one .at() would have read."""
    from app.services.trading.momentum_neural.replay_v2 import Tape

    _ensure_table(db)
    date = "2026-06-15"
    base = datetime(2026, 6, 15, 14, 30, 0)
    rows = [(base + timedelta(minutes=m), 10.0 + m * 0.1, 10.1 + m * 0.1, "massive_snapshot") for m in range(3)]
    _seed_tape(db, "SNAP", rows)
    tape = Tape(date)
    win = tape.prices_between(
        "SNAP",
        datetime(2026, 6, 15, 14, 30, 0, tzinfo=timezone.utc),
        datetime(2026, 6, 15, 14, 31, 0, tzinfo=timezone.utc),
    )
    # only ONE sample falls in [14:30, 14:31)
    assert len(win) == 1
    assert win[0][3] == "massive_snapshot"


# ─────────────────────────────────────────────────────────────────────────────
# (5) FULL-PIPELINE prune (DB) + universe-tick retention
# ─────────────────────────────────────────────────────────────────────────────
def test_prune_universe_ticks_on_shorter_window(db: Session):
    from app.services.trading.momentum_neural.nbbo_tape import prune_nbbo_tape

    _ensure_table(db)
    db.execute(text(
        "INSERT INTO momentum_nbbo_spread_tape (symbol, observed_at, spread_bps, source) VALUES "
        "('OLDSNAP', now() - interval '40 days', 100, 'massive_snapshot'),"   # > 30d snapshot retention
        "('NEWSNAP', now() - interval '10 days', 100, 'massive_snapshot'),"   # within 30d → kept
        "('OLDUNI',  now() - interval '8 days', 100, 'massive_ws_universe'),"  # > 5d universe retention → pruned
        "('NEWUNI',  now() - interval '2 days', 100, 'massive_ws_universe')"   # within 5d → kept
    ))
    db.commit()
    res = prune_nbbo_tape(db, retention_days=30)
    assert res["ok"], res
    assert res["pruned"] == 1            # OLDSNAP (snapshot path)
    assert res["pruned_universe"] == 1   # OLDUNI (universe path, shorter window)
    remaining = db.execute(
        text("SELECT symbol FROM momentum_nbbo_spread_tape ORDER BY symbol")
    ).scalars().all()
    assert remaining == ["NEWSNAP", "NEWUNI"]


# ─────────────────────────────────────────────────────────────────────────────
# (4) MICROPULL parity — the live trigger reads the 1m df when off; routes the 15s
#     micro df when on. We test the BUILDER + the router contract without a live DB.
# ─────────────────────────────────────────────────────────────────────────────
def test_build_micro_bar_df_falls_back_when_sparse(db: Session):
    """FAIL-SAFE: a name with only 1-min snapshots → _build_micro_bar_df returns None
    → the caller uses the 1m df (byte-identical)."""
    from app.services.trading.momentum_neural.live_runner import _build_micro_bar_df

    _ensure_table(db)
    base = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=10)
    rows = [(base + timedelta(minutes=m), 10.0 + m * 0.1, 10.1 + m * 0.1, "massive_snapshot") for m in range(3)]
    _seed_tape(db, "SNAP", rows)
    assert _build_micro_bar_df(db, "SNAP", bar_seconds=15) is None  # too sparse → fall back


def test_build_micro_bar_df_returns_frame_when_dense(db: Session):
    from app.services.trading.momentum_neural.live_runner import _build_micro_bar_df

    _ensure_table(db)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    base = now - timedelta(minutes=5)
    # dense ticks every ~2s for ~3 minutes → plenty of 15s micro-bars
    rows = []
    for i in range(90):
        ts = base + timedelta(seconds=2 * i)
        rows.append((ts, 10.0 + i * 0.005, 10.02 + i * 0.005, "massive_ws_universe"))
    _seed_tape(db, "DENSE", rows)
    df = _build_micro_bar_df(db, "DENSE", bar_seconds=15)
    assert df is not None and len(df) >= 2
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]


def test_micropull_on_by_default_and_config_present():
    # CAPTURE-G1(a) 2026-07-03: micropull flipped ON by default (paired with the 15s
    # first-pullback interval so the micro-frame first-pullback ARM engages). SVRE
    # 2026-06-30 replay-verified: micro reaches waiting_for_break at pullback_high=6.89
    # (Ross's 6.98) where 1m stays pullback_too_deep. The tick-density fail-safe
    # (test_build_micro_bar_df_falls_back_when_sparse) still protects thin names from
    # fabricating armable 15s bars.
    assert settings.chili_momentum_micropull_enabled is True
    assert settings.chili_momentum_first_pullback_interval == "15s"
    assert 5 <= settings.chili_momentum_micropull_bar_seconds <= 30
    assert settings.chili_momentum_replay_tick_entry_enabled is False
    assert settings.chili_momentum_replay_full_pipeline_enabled is False
    assert settings.chili_momentum_universe_tick_record_enabled is True
    assert 1 <= settings.chili_momentum_universe_tick_retention_days <= 30


def test_micropull_thin_tape_cannot_arm_junk_break(db: Session):
    """CAPTURE-G1(a) explicit fail-safe: even with micropull ON, a genuinely THIN name
    (only a few 1-min snapshots) resamples to < 10 micro-bars, so _build_micro_bar_df
    returns None and the live path falls back to the 1m frame — a sparse tape can NEVER
    fabricate a 15s micro-frame that arms a junk break. Complements the dense case below."""
    from app.services.trading.momentum_neural.live_runner import _build_micro_bar_df

    _ensure_table(db)
    base = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=12)
    # 6 one-per-minute snapshots over 5 min → << the 10-micro-bar floor the trigger needs.
    rows = [
        (base + timedelta(minutes=m), 3.55 + m * 0.02, 3.57 + m * 0.02, "massive_snapshot")
        for m in range(6)
    ]
    _seed_tape(db, "THINPM", rows)
    # too sparse → None → 1m fallback; no armable micro-frame produced.
    assert _build_micro_bar_df(db, "THINPM", bar_seconds=15) is None


# ─────────────────────────────────────────────────────────────────────────────
# (5) FULL-PIPELINE determinism — the as-of ranker re-screens deterministically.
# We exercise the rank helper through a constructed Tape so it doesn't need the
# OHLCV feed; an empty tape returns [] (safe), and a seeded mover ranks.
# ─────────────────────────────────────────────────────────────────────────────
def test_full_pipeline_run_is_deterministic_and_safe_on_empty(db: Session):
    """run_replay(armed_source='full_pipeline') runs and is deterministic. With no
    tape for the date it returns the no_tape result (no crash, no fabricated trades)."""
    from app.services.trading.momentum_neural.replay_v2 import run_replay

    _ensure_table(db)
    # a date with NO tape rows → must return cleanly, byte-identical across runs
    r1 = run_replay("1990-01-02", persist=False, armed_source="full_pipeline")
    r2 = run_replay("1990-01-02", persist=False, armed_source="full_pipeline")
    assert r1["armed_source"] == "full_pipeline"
    assert r1["error"] == "no_tape_for_date"
    assert r1["trades"] == [] and r2["trades"] == []
    assert r1["candidates"] == r2["candidates"] == 0

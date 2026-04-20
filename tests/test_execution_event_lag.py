"""Tests for the P0.6 execution-event lag gauge."""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import text

from app.services.trading import execution_event_lag as eel


def _insert_event(db, *, venue, event_at, recorded_at=None):
    db.execute(
        text(
            """
            INSERT INTO trading_execution_events
                (event_type, status, venue, broker_source, ticker,
                 event_at, recorded_at, payload_json)
            VALUES
                ('ack', 'accepted', :venue, :venue, 'ZZLAG',
                 :event_at, COALESCE(:rec_at, NOW()),
                 CAST('{}' AS JSONB))
            """
        ),
        {"venue": venue, "event_at": event_at, "rec_at": recorded_at},
    )


def test_measure_with_no_events_returns_empty_summary(db):
    db.execute(text("DELETE FROM trading_execution_events WHERE ticker = 'ZZLAG'"))
    db.commit()
    summary = eel.measure_execution_event_lag(db, lookback_seconds=300)
    assert summary.sample_size == 0
    assert summary.p95_ms is None
    assert summary.breach == "ok"


def test_measure_computes_percentiles_across_venues(db):
    db.execute(text("DELETE FROM trading_execution_events WHERE ticker = 'ZZLAG'"))
    db.commit()

    now = datetime.utcnow()
    # 5 robinhood events with ~500ms lag, 5 coinbase events with ~2000ms lag.
    for i in range(5):
        _insert_event(
            db,
            venue="robinhood",
            event_at=now - timedelta(seconds=1, milliseconds=500 + i),
            recorded_at=now - timedelta(seconds=1) + timedelta(seconds=i * 0),
        )
    for i in range(5):
        _insert_event(
            db,
            venue="coinbase",
            event_at=now - timedelta(seconds=3),
            recorded_at=now - timedelta(seconds=1),
        )
    db.commit()

    summary = eel.measure_execution_event_lag(db, lookback_seconds=300)
    assert summary.sample_size == 10
    assert summary.p50_ms is not None
    assert summary.p95_ms is not None
    assert set(summary.per_venue.keys()) == {"robinhood", "coinbase"}
    assert summary.per_venue["coinbase"]["sample_size"] == 5
    assert summary.per_venue["robinhood"]["sample_size"] == 5

    db.execute(text("DELETE FROM trading_execution_events WHERE ticker = 'ZZLAG'"))
    db.commit()


def test_p95_breach_warn_flips_breach_field(db, monkeypatch):
    db.execute(text("DELETE FROM trading_execution_events WHERE ticker = 'ZZLAG'"))
    db.commit()

    now = datetime.utcnow()
    # 20 events with 20-second lag — well above a 5s warn threshold.
    for _ in range(20):
        _insert_event(
            db,
            venue="robinhood",
            event_at=now - timedelta(seconds=30),
            recorded_at=now - timedelta(seconds=10),  # 20s lag
        )
    db.commit()

    cfg = SimpleNamespace(
        chili_execution_event_lag_warn_p95_ms=5_000.0,
        chili_execution_event_lag_error_p95_ms=60_000.0,
    )
    monkeypatch.setattr(eel, "settings", cfg)

    summary = eel.measure_execution_event_lag(db, lookback_seconds=300)
    assert summary.sample_size == 20
    assert summary.breach == "warn"
    assert summary.p95_ms and summary.p95_ms >= 5_000.0

    db.execute(text("DELETE FROM trading_execution_events WHERE ticker = 'ZZLAG'"))
    db.commit()


def test_p95_breach_error_escalates(db, monkeypatch):
    db.execute(text("DELETE FROM trading_execution_events WHERE ticker = 'ZZLAG'"))
    db.commit()

    now = datetime.utcnow()
    for _ in range(20):
        _insert_event(
            db,
            venue="robinhood",
            event_at=now - timedelta(seconds=120),
            recorded_at=now - timedelta(seconds=10),  # 110s lag
        )
    db.commit()

    cfg = SimpleNamespace(
        chili_execution_event_lag_warn_p95_ms=5_000.0,
        chili_execution_event_lag_error_p95_ms=60_000.0,
    )
    monkeypatch.setattr(eel, "settings", cfg)

    summary = eel.measure_execution_event_lag(db, lookback_seconds=300)
    assert summary.breach == "error"

    db.execute(text("DELETE FROM trading_execution_events WHERE ticker = 'ZZLAG'"))
    db.commit()


def test_tick_disabled_short_circuits(db, monkeypatch):
    cfg = SimpleNamespace(chili_execution_event_lag_enabled=False)
    monkeypatch.setattr(eel, "settings", cfg)
    out = eel.run_execution_event_lag_tick(db)
    assert out["skipped"] is True


def test_negative_lag_excluded(db):
    """Clock skew can produce event_at > recorded_at; those rows must be
    excluded, not counted as zero/negative lag."""
    db.execute(text("DELETE FROM trading_execution_events WHERE ticker = 'ZZLAG'"))
    db.commit()

    now = datetime.utcnow()
    # One bad event (skewed), one good event.
    _insert_event(
        db, venue="robinhood",
        event_at=now + timedelta(seconds=5),  # in the future
        recorded_at=now,
    )
    _insert_event(
        db, venue="robinhood",
        event_at=now - timedelta(seconds=2),
        recorded_at=now,
    )
    db.commit()

    summary = eel.measure_execution_event_lag(db, lookback_seconds=300)
    assert summary.sample_size == 1  # only the non-skewed row counts

    db.execute(text("DELETE FROM trading_execution_events WHERE ticker = 'ZZLAG'"))
    db.commit()

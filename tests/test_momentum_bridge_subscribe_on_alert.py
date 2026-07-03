"""CAPTURE-G3 — event-driven IQFeed-bridge subscribe-on-first-alert.

Covers:
  * the PURE fast-path trigger (select_fresh_subscribe_symbols) — freshness window, dedup,
    already-watched exclusion, cap, tz-aware/naive parity, garbage-safe;
  * request_bridge_subscription — writes a hint when enabled, no-ops when the kill-switch is
    OFF or on a crypto/invalid symbol, never raises on a DB error;
  * migration 313 creates the coordination table idempotently.

Pure-logic + a light DB fixture; the end-to-end first-alert->subscribed latency is verifiable
only live (the host bridge polls the table) — noted in the report.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.services.trading.momentum_neural.bridge_subscribe import (
    request_bridge_subscription,
    select_fresh_subscribe_symbols,
)


NOW = datetime(2026, 7, 3, 13, 0, 0, tzinfo=timezone.utc)


# ── PURE fast-path trigger ──────────────────────────────────────────────────

def test_select_fresh_only_within_window():
    rows = [
        ("VWAV", NOW - timedelta(seconds=10)),   # fresh
        ("OLD", NOW - timedelta(seconds=600)),   # stale (outside 180s)
    ]
    out = select_fresh_subscribe_symbols(rows, now_utc=NOW, fresh_window_s=180.0)
    assert out == ["VWAV"]


def test_select_excludes_already_watched_and_dedups():
    rows = [
        ("VWAV", NOW - timedelta(seconds=5)),
        ("VWAV", NOW - timedelta(seconds=8)),    # dup
        ("HELD", NOW - timedelta(seconds=3)),    # already watched
        ("NEWM", NOW - timedelta(seconds=2)),
    ]
    out = select_fresh_subscribe_symbols(
        rows, now_utc=NOW, fresh_window_s=180.0, already_watched={"HELD"}
    )
    assert out == ["NEWM", "VWAV"]  # newest-first, HELD excluded, VWAV de-duped


def test_select_respects_cap_newest_first():
    rows = [
        ("A", NOW - timedelta(seconds=30)),
        ("B", NOW - timedelta(seconds=20)),
        ("C", NOW - timedelta(seconds=10)),
    ]
    out = select_fresh_subscribe_symbols(rows, now_utc=NOW, fresh_window_s=180.0, max_new=2)
    assert out == ["C", "B"]  # freshest two


def test_select_naive_and_aware_timestamps_parity():
    naive = [("VWAV", (NOW - timedelta(seconds=5)).replace(tzinfo=None))]
    aware = [("VWAV", NOW - timedelta(seconds=5))]
    assert select_fresh_subscribe_symbols(naive, now_utc=NOW, fresh_window_s=180.0) == ["VWAV"]
    assert select_fresh_subscribe_symbols(aware, now_utc=NOW, fresh_window_s=180.0) == ["VWAV"]


def test_select_skips_garbage_and_crypto():
    rows = [
        ("BTC-USD", NOW - timedelta(seconds=5)),  # crypto -> skipped (equity bridge)
        ("", NOW - timedelta(seconds=5)),          # empty
        ("GOODNAME", None),                        # unreadable ts -> skipped
        ("REALMOVER", NOW - timedelta(seconds=5)),
    ]
    out = select_fresh_subscribe_symbols(rows, now_utc=NOW, fresh_window_s=180.0)
    assert out == ["REALMOVER"]


# ── request_bridge_subscription (write side) ────────────────────────────────

class _CaptureDB:
    def __init__(self):
        self.calls = []
        self.savepoints_opened = 0
        self.savepoints_rolled_back = 0

    def begin_nested(self):  # F3: Session-shaped SAVEPOINT context manager
        outer = self

        class _SP:
            def __enter__(self_sp):
                outer.savepoints_opened += 1
                return self_sp

            def __exit__(self_sp, exc_type, exc, tb):
                if exc_type is not None:
                    outer.savepoints_rolled_back += 1
                return False  # propagate — request_bridge_subscription swallows

        return _SP()

    def execute(self, stmt, params=None):
        self.calls.append((str(stmt), params))


class _RaisingDB(_CaptureDB):
    """F3: the INSERT itself fails (missing table / db down) — the savepoint must roll
    back and the wrapper must swallow (outer transaction untouched)."""

    def execute(self, *a, **k):
        raise RuntimeError("db down")


def _S(**over):
    ns = SimpleNamespace()
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def test_request_writes_hint_when_enabled(monkeypatch):
    import app.services.trading.momentum_neural.bridge_subscribe as bs

    monkeypatch.setattr(bs, "settings", _S(chili_momentum_bridge_subscribe_on_alert_enabled=True))
    db = _CaptureDB()
    ok = request_bridge_subscription(db, "vwav", reason="ws_ignition", now_utc=NOW)
    assert ok is True
    assert len(db.calls) == 1
    _, params = db.calls[0]
    assert params["s"] == "VWAV" and params["r"] == "ws_ignition"
    assert params["at"].tzinfo is None  # naive-UTC (table basis)


def test_request_noop_when_disabled(monkeypatch):
    import app.services.trading.momentum_neural.bridge_subscribe as bs

    monkeypatch.setattr(bs, "settings", _S(chili_momentum_bridge_subscribe_on_alert_enabled=False))
    db = _CaptureDB()
    assert request_bridge_subscription(db, "VWAV") is False
    assert db.calls == []  # no write when the kill-switch is OFF (byte-identical to poll-only)


def test_request_skips_crypto_and_empty(monkeypatch):
    import app.services.trading.momentum_neural.bridge_subscribe as bs

    monkeypatch.setattr(bs, "settings", _S(chili_momentum_bridge_subscribe_on_alert_enabled=True))
    db = _CaptureDB()
    assert request_bridge_subscription(db, "BTC-USD") is False
    assert request_bridge_subscription(db, "") is False
    assert db.calls == []


def test_request_never_raises_on_db_error(monkeypatch):
    import app.services.trading.momentum_neural.bridge_subscribe as bs

    monkeypatch.setattr(bs, "settings", _S(chili_momentum_bridge_subscribe_on_alert_enabled=True))
    # a failed hint must NEVER break the ignition path that called it.
    assert request_bridge_subscription(_RaisingDB(), "VWAV") is False


def test_request_uses_savepoint_success_and_rollback_on_failure(monkeypatch):
    """F3: the INSERT runs inside a SAVEPOINT so a failed hint can never abort the caller's
    shared ignition transaction (pre-mig-313 missing table = every score lost)."""
    import app.services.trading.momentum_neural.bridge_subscribe as bs

    monkeypatch.setattr(bs, "settings", _S(chili_momentum_bridge_subscribe_on_alert_enabled=True))
    ok_db = _CaptureDB()
    assert request_bridge_subscription(ok_db, "VWAV", now_utc=NOW) is True
    assert ok_db.savepoints_opened == 1 and ok_db.savepoints_rolled_back == 0
    bad_db = _RaisingDB()
    assert request_bridge_subscription(bad_db, "VWAV", now_utc=NOW) is False
    assert bad_db.savepoints_opened == 1 and bad_db.savepoints_rolled_back == 1


def test_request_truncates_symbol_to_varchar16(monkeypatch):
    """F3: the column is VARCHAR(16); an over-long symbol must be truncated, not error."""
    import app.services.trading.momentum_neural.bridge_subscribe as bs

    monkeypatch.setattr(bs, "settings", _S(chili_momentum_bridge_subscribe_on_alert_enabled=True))
    db = _CaptureDB()
    assert request_bridge_subscription(db, "A" * 40, now_utc=NOW) is True
    assert db.calls[0][1]["s"] == "A" * 16


def test_failed_hint_does_not_poison_outer_transaction(db, monkeypatch):
    """F3 end-to-end (real Session): drop the table so the hint INSERT fails, then prove the
    OUTER transaction still commits cleanly — the exact pre-mig-313 poisoning scenario."""
    import sqlalchemy as _sa

    import app.services.trading.momentum_neural.bridge_subscribe as bs
    from app.migrations import _migration_313_momentum_bridge_subscribe_requests

    monkeypatch.setattr(bs, "settings", _S(chili_momentum_bridge_subscribe_on_alert_enabled=True))
    db.execute(_sa.text("DROP TABLE IF EXISTS momentum_bridge_subscribe_requests"))
    db.commit()
    # an unrelated write in the SAME transaction (stands in for the ignition viability write)
    db.execute(_sa.text(
        "CREATE TABLE IF NOT EXISTS _f3_probe (id INT); INSERT INTO _f3_probe VALUES (1)"
    ))
    assert request_bridge_subscription(db, "VWAV") is False  # table missing -> hint fails
    # the outer transaction MUST still be committable (savepoint confined the abort)
    db.commit()
    n = db.execute(_sa.text("SELECT count(*) FROM _f3_probe")).scalar()
    assert n == 1
    db.execute(_sa.text("DROP TABLE IF EXISTS _f3_probe"))
    db.commit()
    # restore the coordination table for any later test — on a SEPARATE connection (the
    # migration helper commits its own Connection; mixing that with Session.commit on the
    # same connection leaves the session's root transaction inactive).
    with db.get_bind().connect() as _c:
        _migration_313_momentum_bridge_subscribe_requests(_c)


# ── migration 313 (coordination table) ──────────────────────────────────────

def test_migration_313_creates_table_idempotently(db: Session):
    from app.migrations import _migration_313_momentum_bridge_subscribe_requests

    # the conftest DB may already have it (migrations run at setup); assert it exists + re-run.
    _migration_313_momentum_bridge_subscribe_requests(db.connection())
    cols = db.execute(sa.text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='momentum_bridge_subscribe_requests'"
    )).fetchall()
    names = {r[0] for r in cols}
    assert {"symbol", "requested_at", "reason"}.issubset(names)
    # idempotent re-run (IF NOT EXISTS) must not raise.
    _migration_313_momentum_bridge_subscribe_requests(db.connection())


def test_request_and_read_roundtrip_through_db(db: Session, monkeypatch):
    """End-to-end (DB): a hint written by the app side is readable within the fresh window by
    the bridge's fast-path query shape."""
    import app.services.trading.momentum_neural.bridge_subscribe as bs
    from app.migrations import _migration_313_momentum_bridge_subscribe_requests

    _migration_313_momentum_bridge_subscribe_requests(db.connection())
    monkeypatch.setattr(bs, "settings", _S(chili_momentum_bridge_subscribe_on_alert_enabled=True))
    assert request_bridge_subscription(db, "VWAV", reason="ws_ignition") is True
    db.flush()
    # the bridge's fast-path read shape (recent trailing window).
    rows = db.execute(sa.text(
        "SELECT symbol, requested_at FROM momentum_bridge_subscribe_requests "
        "WHERE requested_at > (now() at time zone 'utc') - make_interval(secs => 180)"
    )).fetchall()
    syms = {str(r[0]).upper() for r in rows}
    assert "VWAV" in syms

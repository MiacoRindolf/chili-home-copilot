"""position-identity-phase-1 (2026-05-04) regression tests.

Per docs/DESIGN/POSITION_IDENTITY.md § 8.1 + the brief's 11 scenarios
(A-K). Tests cover:
    A. migration applies cleanly + creates expected schema
    B. opened event on first broker observation
    C. qty_change event on quantity diff
    D. closed event on broker-drop
    E. re_opened event when broker reports previously-closed position
    F. sync_gap event when prior event is stale (> 2x cron threshold)
    G. direction in natural key (long + short = 2 distinct rows)
    H. account_type='paper' for paper-mode positions
    I. backfill covers Trade AND PaperTrade
    J. shadow-mode no-readers (static grep canary)
    K. idempotent migration

Run with ``-p no:asyncio``.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text


def _seed_trade(db, *, trade_id: int, ticker: str, broker_source: str = "robinhood",
                direction: str = "long", status: str = "open", qty: float = 10.0,
                entry: float = 5.0) -> None:
    db.execute(text("""
        INSERT INTO trading_trades (
            id, ticker, status, broker_source, direction, quantity,
            entry_price, entry_date
        ) VALUES (
            :id, :ticker, :status, :bs, :dir, :qty,
            :entry, NOW()
        ) ON CONFLICT (id) DO NOTHING
    """), {
        "id": trade_id, "ticker": ticker, "status": status, "bs": broker_source,
        "dir": direction, "qty": qty, "entry": entry,
    })
    db.commit()


def _seed_paper_trade(db, *, trade_id: int, ticker: str, direction: str = "long",
                      status: str = "open", qty: int = 10,
                      entry: float = 5.0) -> None:
    db.execute(text("""
        INSERT INTO trading_paper_trades (
            id, ticker, direction, quantity, entry_price, status, entry_date
        ) VALUES (
            :id, :ticker, :dir, :qty, :entry, :status, NOW()
        ) ON CONFLICT (id) DO NOTHING
    """), {
        "id": trade_id, "ticker": ticker, "dir": direction, "qty": qty,
        "entry": entry, "status": status,
    })
    db.commit()


# ── A: migration applies + tables exist with constraints ──────────────


def test_a_migration_creates_expected_schema(db):
    """The conftest db fixture runs migrations through head, including
    mig 224. Verify both new tables exist with the expected columns
    and constraints."""
    cols = db.execute(text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name='trading_positions'
        ORDER BY ordinal_position
    """)).scalars().all()
    expected_cols = {
        "id", "user_id", "broker_source", "account_type", "ticker",
        "direction", "asset_kind", "current_quantity", "current_avg_price",
        "state", "current_envelope_id", "last_observed_at",
        "last_state_transition_at", "created_at", "updated_at",
    }
    assert expected_cols.issubset(set(cols)), (
        f"missing expected columns: {expected_cols - set(cols)}"
    )

    cols_evt = db.execute(text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name='trading_position_events'
    """)).scalars().all()
    expected_evt_cols = {
        "id", "position_id", "event_type", "transition_reason",
        "quantity", "avg_price", "broker_payload", "envelope_id",
        "observed_at", "recorded_at",
    }
    assert expected_evt_cols.issubset(set(cols_evt))

    # mig 223 orphan column dropped
    bracket_cols = db.execute(text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name='trading_bracket_intents'
    """)).scalars().all()
    assert "phantom_close_consecutive_zero_qty_sweeps" not in bracket_cols, (
        "mig 223 orphan column should be dropped by mig 224"
    )


# ── B: opened event on first observation ──────────────────────────────


def test_b_opened_event_on_first_observation(db):
    from app.services import broker_service

    broker_service._phase1_record_position_observation(
        db,
        user_id=None,
        broker_source="robinhood",
        account_type="cash",
        ticker="ALPHA",
        direction="long",
        asset_kind="equity",
        broker_qty=100.0,
        broker_avg=5.0,
        broker_payload={"raw": "test"},
    )

    row = db.execute(text("""
        SELECT id, state, current_quantity, current_avg_price
        FROM trading_positions WHERE ticker='ALPHA' AND direction='long'
    """)).first()
    assert row is not None
    assert row[1] == "open"
    assert float(row[2]) == 100.0

    events = db.execute(text("""
        SELECT event_type, transition_reason
        FROM trading_position_events WHERE position_id=:pid
        ORDER BY id
    """), {"pid": int(row[0])}).fetchall()
    assert len(events) == 1
    assert events[0][0] == "opened"
    assert events[0][1] == "broker_sync_first_observation"


# ── C: qty_change event ───────────────────────────────────────────────


def test_c_qty_change_event(db):
    from app.services import broker_service

    # First observation: qty=100
    broker_service._phase1_record_position_observation(
        db, user_id=None, broker_source="robinhood", account_type="cash",
        ticker="BETA", direction="long", asset_kind="equity",
        broker_qty=100.0, broker_avg=5.0, broker_payload=None,
    )
    # Second observation: qty=80 (partial fill of cover)
    broker_service._phase1_record_position_observation(
        db, user_id=None, broker_source="robinhood", account_type="cash",
        ticker="BETA", direction="long", asset_kind="equity",
        broker_qty=80.0, broker_avg=5.0, broker_payload=None,
    )

    pid = db.execute(text(
        "SELECT id FROM trading_positions WHERE ticker='BETA'"
    )).scalar()
    events = db.execute(text("""
        SELECT event_type, quantity FROM trading_position_events
        WHERE position_id=:pid ORDER BY id
    """), {"pid": int(pid)}).fetchall()
    assert len(events) == 2
    assert events[0][0] == "opened"
    assert events[1][0] == "qty_change"
    assert float(events[1][1]) == 80.0

    qty = db.execute(text(
        "SELECT current_quantity FROM trading_positions WHERE id=:pid"
    ), {"pid": int(pid)}).scalar()
    assert float(qty) == 80.0


# ── D: closed event when broker drops ─────────────────────────────────


def test_d_closed_event_on_broker_drop(db):
    from app.services import broker_service

    broker_service._phase1_record_position_observation(
        db, user_id=None, broker_source="robinhood", account_type="cash",
        ticker="GAMMA", direction="long", asset_kind="equity",
        broker_qty=50.0, broker_avg=2.0, broker_payload=None,
    )
    # Cycle 2: GAMMA dropped from broker response.
    broker_service._phase1_close_dropped_positions(
        db, user_id=None, broker_source="robinhood",
        observed_tickers={"OTHER_TICKER"},
    )

    row = db.execute(text(
        "SELECT id, state FROM trading_positions WHERE ticker='GAMMA'"
    )).first()
    assert row[1] == "closed"

    events = db.execute(text("""
        SELECT event_type FROM trading_position_events
        WHERE position_id=:pid ORDER BY id
    """), {"pid": int(row[0])}).fetchall()
    types = [e[0] for e in events]
    assert "opened" in types
    assert "closed" in types


# ── E: re_opened event ────────────────────────────────────────────────


def test_e_re_opened_event(db):
    from app.services import broker_service

    # Open + drop sequence.
    broker_service._phase1_record_position_observation(
        db, user_id=None, broker_source="robinhood", account_type="cash",
        ticker="DELTA", direction="long", asset_kind="equity",
        broker_qty=10.0, broker_avg=3.0, broker_payload=None,
    )
    broker_service._phase1_close_dropped_positions(
        db, user_id=None, broker_source="robinhood",
        observed_tickers={"OTHER"},
    )
    # Now broker reports DELTA again.
    broker_service._phase1_record_position_observation(
        db, user_id=None, broker_source="robinhood", account_type="cash",
        ticker="DELTA", direction="long", asset_kind="equity",
        broker_qty=15.0, broker_avg=3.5, broker_payload=None,
    )

    pid = db.execute(text(
        "SELECT id FROM trading_positions WHERE ticker='DELTA'"
    )).scalar()
    events = db.execute(text("""
        SELECT event_type FROM trading_position_events
        WHERE position_id=:pid ORDER BY id
    """), {"pid": int(pid)}).scalars().all()
    assert events == ["opened", "closed", "re_opened"]

    state = db.execute(text(
        "SELECT state FROM trading_positions WHERE id=:pid"
    ), {"pid": int(pid)}).scalar()
    assert state == "open"


# ── F: sync_gap event ─────────────────────────────────────────────────


def test_f_sync_gap_event(db):
    from app.services import broker_service

    broker_service._phase1_record_position_observation(
        db, user_id=None, broker_source="robinhood", account_type="cash",
        ticker="EPSI", direction="long", asset_kind="equity",
        broker_qty=10.0, broker_avg=2.0, broker_payload=None,
    )
    pid = db.execute(text(
        "SELECT id FROM trading_positions WHERE ticker='EPSI'"
    )).scalar()
    # Force last_observed_at into the distant past.
    stale_ts = datetime.utcnow() - timedelta(
        seconds=broker_service._SYNC_GAP_THRESHOLD_SECONDS + 60
    )
    db.execute(text(
        "UPDATE trading_positions SET last_observed_at=:ts WHERE id=:pid"
    ), {"ts": stale_ts, "pid": int(pid)})
    db.commit()

    # Next observation should detect the gap and emit a sync_gap event.
    broker_service._phase1_record_position_observation(
        db, user_id=None, broker_source="robinhood", account_type="cash",
        ticker="EPSI", direction="long", asset_kind="equity",
        broker_qty=10.0, broker_avg=2.0, broker_payload=None,
    )

    events = db.execute(text("""
        SELECT event_type FROM trading_position_events
        WHERE position_id=:pid ORDER BY id
    """), {"pid": int(pid)}).scalars().all()
    assert "sync_gap" in events


# ── G: direction in natural key ───────────────────────────────────────


def test_g_direction_in_natural_key(db):
    from app.services import broker_service

    broker_service._phase1_record_position_observation(
        db, user_id=None, broker_source="robinhood", account_type="cash",
        ticker="ZETA", direction="long", asset_kind="equity",
        broker_qty=100.0, broker_avg=5.0, broker_payload=None,
    )
    broker_service._phase1_record_position_observation(
        db, user_id=None, broker_source="robinhood", account_type="cash",
        ticker="ZETA", direction="short", asset_kind="equity",
        broker_qty=50.0, broker_avg=5.5, broker_payload=None,
    )

    rows = db.execute(text("""
        SELECT direction, current_quantity FROM trading_positions
        WHERE ticker='ZETA' ORDER BY direction
    """)).fetchall()
    assert len(rows) == 2
    dirs = {r[0] for r in rows}
    assert dirs == {"long", "short"}


# ── H: account_type='paper' ───────────────────────────────────────────


def test_h_account_type_paper(db):
    from app.services import broker_service

    broker_service._phase1_record_position_observation(
        db, user_id=None, broker_source="paper", account_type="paper",
        ticker="ETA", direction="long", asset_kind="equity",
        broker_qty=10.0, broker_avg=2.5, broker_payload=None,
    )

    row = db.execute(text(
        "SELECT account_type, broker_source FROM trading_positions WHERE ticker='ETA'"
    )).first()
    assert row[0] == "paper"
    assert row[1] == "paper"


# ── I: backfill covers Trade AND PaperTrade ──────────────────────────


def test_i_backfill_covers_both_kinds(db):
    _seed_trade(db, trade_id=8001, ticker="LIVE", broker_source="robinhood")
    _seed_paper_trade(db, trade_id=9001, ticker="PAPER")

    # Import + run the backfill module's main.
    import importlib.util as _ilu
    repo_root = Path(__file__).resolve().parent.parent
    mod_path = repo_root / "scripts" / "backfill_position_rows.py"
    spec = _ilu.spec_from_file_location("backfill_position_rows", str(mod_path))
    mod = _ilu.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    # The script's SessionLocal opens its own session; run via the
    # internal helpers using the test db session for visibility.
    mod._backfill_open_from_trades(db)
    mod._backfill_open_from_paper_trades(db)

    live_row = db.execute(text(
        "SELECT account_type FROM trading_positions WHERE ticker='LIVE'"
    )).scalar()
    paper_row = db.execute(text(
        "SELECT account_type FROM trading_positions WHERE ticker='PAPER'"
    )).scalar()
    assert live_row == "cash"
    assert paper_row == "paper"


# ── J: static-grep no-readers canary ─────────────────────────────────


def test_j_no_readers_in_decision_paths():
    """Phase 1 ships in shadow mode. The bracket reconciler, stop
    engine, bracket writer, inverse-reconcile, and emergency_close_all
    must NOT read from trading_positions / trading_position_events for
    any decision-making purpose. Static check.

    The new helpers in broker_service.py (the writers) DO touch these
    tables -- those are explicitly the writers, not readers.
    """
    repo_root = Path(__file__).resolve().parent.parent
    pattern = re.compile(r"\btrading_positions\b|\btrading_position_events\b")
    forbidden_files = [
        "app/services/trading/bracket_reconciliation_service.py",
        "app/services/trading/bracket_writer_g2.py",
        "app/services/trading/stop_engine.py",
        "app/services/trading/emergency_liquidation.py",
    ]
    offenders = []
    for relpath in forbidden_files:
        body = (repo_root / relpath).read_text(encoding="utf-8")
        for line in body.splitlines():
            if pattern.search(line):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                offenders.append(f"{relpath}: {line}")
    assert not offenders, (
        "Phase 1 ships in shadow mode -- decision-making code must not "
        "read trading_positions / trading_position_events. Offenders:\n  "
        + "\n  ".join(offenders)
    )


# ── K: idempotent migration ──────────────────────────────────────────


def test_k_migration_idempotent(db):
    """Re-run mig 224 inline; assert no errors, table count unchanged."""
    from app.migrations import _migration_224_position_identity_phase_1

    before_pos = db.execute(text(
        "SELECT COUNT(*) FROM trading_positions"
    )).scalar() or 0
    before_evt = db.execute(text(
        "SELECT COUNT(*) FROM trading_position_events"
    )).scalar() or 0

    # Re-run via raw connection (mirrors how migrations apply).
    conn = db.connection()
    _migration_224_position_identity_phase_1(conn)

    after_pos = db.execute(text(
        "SELECT COUNT(*) FROM trading_positions"
    )).scalar() or 0
    after_evt = db.execute(text(
        "SELECT COUNT(*) FROM trading_position_events"
    )).scalar() or 0

    assert after_pos == before_pos
    assert after_evt == before_evt

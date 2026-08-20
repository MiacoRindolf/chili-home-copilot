"""MIG 362 — ang lason ng risk-ledger scan (2026-08-20).

Ang pending-session scan ay bumabasa ng BAWAT live alpaca row anuman ang estado
at nagre-raise sa position marker na walang certification fields. 14 na row mula
06-12..07-13 (kasama ang crypto-sa-alpaca_spot na TRUMP-USD sid 1198, isinulat
BAGO idagdag ang mga certification field) = `risk_ledger_unreadable` nang
PASULPOT-SULPOT (walang ORDER BY ⇒ heap order) = pinatay ang XRPZ ×2 at BTCT.

Ang migration ay NAGLILIPAT (hindi nagbubura) ng marker papunta sa
`position_quarantined_uncertified` — buo ang audit trail, wala na sa mata ng
scan, at idempotent.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.migrations import (
    MIGRATIONS,
    _migration_362_quarantine_uncertified_position_markers,
)


def test_migration_registered_exactly_once():
    ids = [m[0] for m in MIGRATIONS]
    assert ids.count("362_quarantine_uncertified_position_markers") == 1
    assert (
        MIGRATIONS[-1][1]
        is _migration_362_quarantine_uncertified_position_markers
    )


def _ensure_user(db):
    db.execute(text(
        "INSERT INTO users (id, name) VALUES (990001, 'mig362-test') "
        "ON CONFLICT (id) DO NOTHING"
    ))
    db.execute(text(
        "INSERT INTO momentum_strategy_variants "
        "(id, family, variant_key, version, label, params_json, is_active, "
        " execution_family, created_at, updated_at) "
        "VALUES (990001, 'momentum', 'mig362-test', 1, 'mig362', '{}', false, "
        "        'alpaca_spot', now(), now()) "
        "ON CONFLICT (id) DO NOTHING"
    ))


def _mk_session(db, *, sid, state, days_old, scope, with_position=True):
    snap = {"momentum_live_execution": {}}
    if with_position:
        snap["momentum_live_execution"]["position"] = {
            "side": "long", "quantity": 4.549487, "product_id": "TRUMP-USD",
        }
    if scope is not None:
        snap["alpaca_account_scope"] = scope
    db.execute(text(
        "INSERT INTO trading_automation_sessions "
        "(id, user_id, venue, symbol, mode, execution_family, state, "
        " variant_id, started_at, created_at, updated_at, risk_snapshot_json) "
        "VALUES (:id, 990001, 'alpaca', 'TRUMP-USD', 'live', 'alpaca_spot', :state, "
        "        990001, :started, :started, :started, :snap)"
    ), {
        "id": sid, "state": state,
        "started": datetime.now(timezone.utc) - timedelta(days=days_old),
        "snap": json.dumps(snap),
    })


def _position_of(db, sid):
    row = db.execute(text(
        "SELECT risk_snapshot_json::jsonb #> "
        "'{momentum_live_execution,position}', "
        "risk_snapshot_json::jsonb #> "
        "'{momentum_live_execution,position_quarantined_uncertified}' "
        "FROM trading_automation_sessions WHERE id = :id"
    ), {"id": sid}).fetchone()
    return row[0], row[1]


def test_quarantines_only_the_poison_shape(db):
    conn = db.connection()
    _ensure_user(db)
    # Ang tunay na hugis ng lason: terminal, luma, walang scope, may position.
    _mk_session(db, sid=901198, state="live_error", days_old=60, scope=None)
    # Mga hindi dapat magalaw:
    _mk_session(db, sid=901199, state="live_error", days_old=60,
                scope="alpaca:paper")          # certified na — hindi lason
    _mk_session(db, sid=901200, state="watching_live", days_old=60,
                scope=None)                    # HINDI terminal — fresh exposure
    _mk_session(db, sid=901201, state="live_error", days_old=1,
                scope=None)                    # bago pa — baka tunay
    _mk_session(db, sid=901202, state="live_error", days_old=60,
                scope=None, with_position=False)  # walang position key

    _migration_362_quarantine_uncertified_position_markers(conn)

    pos, quar = _position_of(db, 901198)
    assert pos is None, "ang lason ay dapat na-quarantine"
    assert quar is not None and quar["quantity"] == 4.549487, (
        "buo dapat ang audit trail"
    )
    for sid in (901199, 901200, 901201):
        pos, quar = _position_of(db, sid)
        assert pos is not None, f"sid {sid}: hindi dapat ginalaw"
        assert quar is None, sid

    # IDEMPOTENT: ang pangalawang takbo ay walang binabago.
    _migration_362_quarantine_uncertified_position_markers(conn)
    pos, quar = _position_of(db, 901198)
    assert pos is None and quar is not None

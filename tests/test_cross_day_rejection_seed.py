"""Cross-day rejection seed (#1252) — doktrina mula sa stream ni Ross 08-31.

"This is the one on Friday that I tried to trade and it popped up and then
rejected, so I don't really trust it" — ang pangalang pumalpak sa nakaraang
ET trading day (pulang stop/bailout exit) ay nagsisimula sa g4 escalation
level 1 ngayon: structural trigger + positibong tape ang hinihingi, hindi
lockout. Fail-open sa 0.

Runnable: pytest tests/test_cross_day_rejection_seed.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.services.trading.momentum_neural.risk_policy import (
    prior_day_rejection_seed,
)


def _prev_trading_day_noon_utc():
    et = ZoneInfo("America/New_York")
    today = datetime.now(et).date()
    prev = today - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    noon_et = datetime.combine(prev, datetime.min.time(), et) + timedelta(hours=12)
    return noon_et.astimezone(timezone.utc).replace(tzinfo=None)


def _variant_id(db):
    import uuid as _uuid
    from app.models.trading import MomentumStrategyVariant

    v = MomentumStrategyVariant(
        family="momentum_pullback",
        variant_key=f"xday-{_uuid.uuid4().hex[:8]}",
        params_json={},
        label="xday-test",
        execution_family="alpaca_spot",
    )
    db.add(v)
    db.flush()
    return int(v.id)


def _user_id(db):
    from app.models.core import User

    u = User(name="xday-test-user")
    db.add(u)
    db.flush()
    return int(u.id)


def _seed_session_with_exit(db, sym, *, pnl, reason, ts):
    uid = _user_id(db)
    sid = db.execute(text(
        "INSERT INTO trading_automation_sessions "
        "(user_id, symbol, mode, state, execution_family, venue, variant_id, "
        " risk_snapshot_json, allocation_decision_json, started_at, created_at, updated_at) "
        "VALUES (:u, :s, 'live', 'live_cancelled', 'alpaca_spot', 'alpaca', :v, "
        " '{}'::jsonb, '{}'::jsonb, :t, :t, :t) "
        "RETURNING id"
    ), dict(s=sym, t=ts, v=_variant_id(db), u=uid)).scalar()
    db.execute(text(
        "INSERT INTO trading_automation_events (session_id, ts, event_type, payload_json) "
        "VALUES (:sid, :t, 'live_exit_filled', "
        "CAST(:p AS jsonb))"
    ), dict(sid=sid, t=ts, p=f'{{"reason": "{reason}", "pnl_usd": {pnl}}}'))
    db.commit()


def test_red_bailout_yesterday_seeds_level_1(db):
    _seed_session_with_exit(
        db, "LGPS", pnl=-12.5, reason="bailout", ts=_prev_trading_day_noon_utc(),
    )
    assert prior_day_rejection_seed(db, "LGPS") == 1


def test_red_stop_yesterday_seeds_level_1(db):
    _seed_session_with_exit(
        db, "BRNX", pnl=-8.0, reason="trail_stop", ts=_prev_trading_day_noon_utc(),
    )
    assert prior_day_rejection_seed(db, "BRNX") == 1


def test_green_exit_yesterday_does_not_seed(db):
    _seed_session_with_exit(
        db, "AEHL", pnl=+20.0, reason="trail_stop", ts=_prev_trading_day_noon_utc(),
    )
    assert prior_day_rejection_seed(db, "AEHL") == 0


def test_red_non_stop_reason_does_not_seed(db):
    _seed_session_with_exit(
        db, "MIMI", pnl=-5.0, reason="max_hold", ts=_prev_trading_day_noon_utc(),
    )
    assert prior_day_rejection_seed(db, "MIMI") == 0


def test_clean_symbol_is_zero(db):
    assert prior_day_rejection_seed(db, "WALA") == 0


def test_crypto_and_bad_inputs_are_zero(db):
    assert prior_day_rejection_seed(db, "BTC-USD") == 0
    assert prior_day_rejection_seed(db, "") == 0
    assert prior_day_rejection_seed(None, "LGPS") == 0

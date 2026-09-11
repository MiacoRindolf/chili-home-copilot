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

import json
import pytest

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
    import uuid as _uuid
    from app.models.core import User

    # [23] review fix: unique per call — a test may seed two exits for one symbol-day.
    u = User(name=f"xday-test-user-{_uuid.uuid4().hex[:8]}")
    db.add(u)
    db.flush()
    return int(u.id)


def _seed_session_with_exit(db, sym, *, pnl, reason, ts):
    uid = _user_id(db)
    sid = db.execute(text(
        "INSERT INTO trading_automation_sessions "
        "(user_id, symbol, mode, state, execution_family, venue, variant_id, "
        " risk_snapshot_json, started_at, created_at, updated_at) "
        "VALUES (:u, :s, 'live', 'live_cancelled', 'alpaca_spot', 'alpaca', :v, "
        " '{}'::jsonb, :t, :t, :t) "
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


# ── [23] review fix: the seed keys on the CLASS, not the name ────────────────
#
# The #1252 query was a list of what counts (``LIKE '%stop%' OR '%bailout%'``) — the same
# blindness [23] inverted in the cap. The #1385 verdict exits that replaced the bailout
# (``tape_accel_rollover``, ``tape_sellers_took_it``) match neither pattern. LBGJ on
# 2026-09-11 had exactly one red exit, ``tape_accel_rollover`` −$40.00 (session 22135,
# 09:45:41Z) ⇒ on Monday 09-14 the seed returned 0. 30 d of red live exits (40 symbol-days):
# old rule 35 seeded, new rule 36 — exactly one change (LBGJ 09-11), none lost.

from app.services.trading.momentum_neural import live_runner as LR  # noqa: E402
from app.services.trading.momentum_neural import risk_policy as RP  # noqa: E402
from app.services.trading.momentum_neural.risk_policy import (  # noqa: E402
    prior_day_rejection_seed_detail,
)

#: LBGJ 22135, 2026-09-11 09:45:41Z (read-only, live ``chili``), and the decision instant.
LBGJ_EXIT_TS = datetime(2026, 9, 11, 9, 45, 41)
MONDAY_0914_OPEN = datetime(2026, 9, 14, 13, 30, 0)


def test_lbgj_0911_verdict_exit_seeds_monday(db):
    _seed_session_with_exit(db, "LBGJ", pnl=-40.00, reason="tape_accel_rollover", ts=LBGJ_EXIT_TS)
    assert prior_day_rejection_seed(db, "LBGJ", as_of_utc=MONDAY_0914_OPEN) == 1
    d = prior_day_rejection_seed_detail(db, "LBGJ", as_of_utc=MONDAY_0914_OPEN)
    assert d["prev_trading_day"] == "2026-09-11"
    assert d["strike_reasons"] == ["tape_accel_rollover"]
    assert d["strike_classes"] == ["exit_verdict"]
    assert d["seed_basis"] == "strike_class"
    # the #1252 substring rule (the revert path) was blind to it
    assert prior_day_rejection_seed(
        db, "LBGJ", as_of_utc=MONDAY_0914_OPEN, counts_every_loss=False) == 0
    d_old = prior_day_rejection_seed_detail(
        db, "LBGJ", as_of_utc=MONDAY_0914_OPEN, counts_every_loss=False)
    assert d_old["seed_basis"] == "revert_stop_or_bailout_substring"
    assert d_old["non_strike_red_reasons"] == ["tape_accel_rollover"]


def test_the_sellers_verdict_seeds_too(db):
    _seed_session_with_exit(db, "SXTC", pnl=-7.82, reason="tape_sellers_took_it", ts=LBGJ_EXIT_TS)
    assert prior_day_rejection_seed(db, "SXTC", as_of_utc=MONDAY_0914_OPEN) == 1


def test_a_red_commanded_flatten_does_not_seed(db):
    _seed_session_with_exit(db, "BRNX", pnl=-26.0, reason="operator_flatten", ts=LBGJ_EXIT_TS)
    _seed_session_with_exit(db, "BRNX", pnl=-3.0,
                            reason="alpaca_fractional_remainder_day_close", ts=LBGJ_EXIT_TS)
    d = prior_day_rejection_seed_detail(db, "BRNX", as_of_utc=MONDAY_0914_OPEN)
    assert d["level"] == 0
    assert d["non_strike_red_reasons"] == [
        "alpaca_fractional_remainder_day_close", "operator_flatten"]


def test_the_prior_day_is_prior_to_the_decision_instant(db):
    """The seed anchors on the decision instant (the sim clock in replay), not on the
    wall clock: Thursday's red exit is NOT Monday's prior day (Friday is)."""
    _seed_session_with_exit(db, "DPU", pnl=-9.10, reason="stop", ts=datetime(2026, 9, 10, 15, 0))
    assert prior_day_rejection_seed(db, "DPU", as_of_utc=MONDAY_0914_OPEN) == 0
    assert prior_day_rejection_seed(db, "DPU", as_of_utc=datetime(2026, 9, 11, 13, 30)) == 1


def test_the_runner_seeds_from_the_class_and_writes_the_reasons(monkeypatch, db):
    """The SHIPPED seam: ``_g4_reentry_escalation_check`` on a fresh LBGJ session on
    Monday 09-14 (decision clock = ``_utcnow``) reads the real query, starts at level 1,
    and the receipt names the reason, the class, the day and the basis."""
    from types import SimpleNamespace

    from app.services.trading.momentum_neural import entry_gates as EG

    _seed_session_with_exit(db, "LBGJ", pnl=-40.00, reason="tape_accel_rollover", ts=LBGJ_EXIT_TS)
    monkeypatch.setattr(LR, "_utcnow", lambda: MONDAY_0914_OPEN)
    monkeypatch.setattr(LR, "_replay_l2_as_of_or_none", lambda: None)
    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(LR, "_emit", lambda db_, sess, et, payload: emitted.append((et, payload)))
    monkeypatch.setattr(LR, "_commit_le", lambda sess, le: None)
    monkeypatch.setattr(LR, "same_day_escalation_seed", lambda *a, **k: {
        "level": 0, "stopout_cycles": 0, "source_session_id": None, "prior_trade": None,
        "prior_trade_session_id": None, "sessions_seen": 0})
    monkeypatch.setattr(EG, "signed_tape_accel_features", lambda *a, **k: None)
    monkeypatch.setattr(LR, "_own_tape_noise_floor_pct", lambda db_, s, entry_price: (None, 0))
    le: dict = {"g4_leader_min": MONDAY_0914_OPEN.strftime("%Y%m%d%H%M"), "g4_leader_is": False}
    sess = SimpleNamespace(id=987654, symbol="LBGJ", execution_family="alpaca_spot")
    via = SimpleNamespace(viability_score=0.5)
    ok, dbg, lvl = LR._g4_reentry_escalation_check(
        db, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=1.0)
    assert lvl == 1 and le["g4_reentry_escalation"] == 1, dbg
    seeds = [p for et, p in emitted if et == "g4_cross_day_rejection_seed"]
    assert len(seeds) == 1
    s = seeds[0]
    assert s["seed_level"] == 1 and s["prev_trading_day"] == "2026-09-11"
    assert s["strike_reasons"] == ["tape_accel_rollover"]
    assert s["strike_classes"] == ["exit_verdict"]
    assert s["seed_basis"] == "strike_class"
    assert RP.reentry_ramp_strike_class("tape_accel_rollover") == "exit_verdict"


@pytest.mark.parametrize("every_loss", [True, False], ids=["classifier", "legacy"])
def test_prior_day_seed_calendar_and_decision_boundaries_actual_sql(db, every_loss):
    """Execute the actual helper SQL against transaction-local temporary rows.

    Weekday edges, the weekend, and both DST offsets are checked. US clock
    changes fall on Sunday, which the existing previous-trading-day selector
    skips; the Monday/Tuesday cases check both sides without inventing a Sunday
    trading day. No persisted/live event or session is read by this helper.
    """
    db.execute(text("CREATE TEMP TABLE trading_automation_sessions "
                    "(id bigint, symbol text) ON COMMIT DROP"))
    db.execute(text("CREATE TEMP TABLE trading_automation_events "
                    "(session_id bigint, event_type text, ts timestamp, payload_json jsonb) "
                    "ON COMMIT DROP"))
    db.execute(text("INSERT INTO pg_temp.trading_automation_sessions VALUES (1, 'BOUND')"))

    class Recorder:
        def __init__(self):
            self.calls = []

        def execute(self, statement, parameters):
            self.calls.append(dict(parameters))
            return db.execute(statement, parameters)

    # as-of, expected previous-day inclusive/exclusive UTC boundaries.
    periods = [
        (datetime(2026, 9, 15, 8, 30, tzinfo=timezone.utc),
         datetime(2026, 9, 14, 4), datetime(2026, 9, 15, 4)),
        (datetime(2026, 9, 14, 13, 30),
         datetime(2026, 9, 11, 4), datetime(2026, 9, 12, 4)),
        (datetime(2026, 3, 9, 5, 30, tzinfo=ZoneInfo("America/Los_Angeles")),
         datetime(2026, 3, 6, 5), datetime(2026, 3, 7, 5)),
        (datetime(2026, 3, 10, 8, 30, tzinfo=timezone.utc),
         datetime(2026, 3, 9, 4), datetime(2026, 3, 10, 4)),
        (datetime(2026, 11, 2, 5, 30, tzinfo=ZoneInfo("America/Los_Angeles")),
         datetime(2026, 10, 30, 4), datetime(2026, 10, 31, 4)),
        (datetime(2026, 11, 3, 9, 30, tzinfo=timezone.utc),
         datetime(2026, 11, 2, 5), datetime(2026, 11, 3, 5)),
    ]
    for asof, start, end in periods:
        frontier = (asof.replace(tzinfo=timezone.utc) if asof.tzinfo is None
                    else asof.astimezone(timezone.utc)).replace(tzinfo=None)
        cases = [(start - timedelta(microseconds=1), 0), (start, 1),
                 (end - timedelta(microseconds=1), 1), (end, 0),
                 (frontier - timedelta(minutes=30), 0),
                 (frontier + timedelta(hours=1), 0)]
        for event_at, expected in cases:
            db.execute(text("DELETE FROM pg_temp.trading_automation_events"))
            reason = "tape_accel_rollover" if every_loss else "tick_deadman_stop"
            db.execute(text("INSERT INTO pg_temp.trading_automation_events "
                            "VALUES (1, 'live_exit_filled', :t, CAST(:p AS jsonb))"),
                       {"t": event_at, "p": json.dumps({"reason": reason, "pnl_usd": -10})})
            recorder = Recorder()
            detail = prior_day_rejection_seed_detail(
                recorder, "BOUND", as_of_utc=asof, counts_every_loss=every_loss,
            )
            assert detail["level"] == expected, (asof, event_at, detail)
            assert len(recorder.calls) == 1
            assert recorder.calls[0]["a"] == start
            assert recorder.calls[0]["b"] == end <= frontier
            assert detail["prev_trading_day"] == start.replace(
                tzinfo=timezone.utc).astimezone(ZoneInfo("America/New_York")).date().isoformat()

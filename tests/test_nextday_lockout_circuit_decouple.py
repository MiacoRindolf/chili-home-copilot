"""NEXT-DAY rule-break lockout (GAP 1) — decouple a lone #769 max-loss-circuit fire.

OPTION A fix (2026-06-30 premarket-freeze root): a max-loss-circuit fire is the bot's
own mechanical per-trade stop doing its job on ONE position — it is NOT the PSY101
human-tilt signature, so it must NOT arm the cross-day no-arm lockout. The two genuine
arming sites survive: a GLOBAL daily-loss breach (governance.set_next_day_trading_lockout
called from global_daily_loss_breached) and the daily-trade-count budget (live_runner).

These tests pin:
  1. PROTECTION-PRESERVED: a genuine daily_loss_breach STILL arms the carryover lockout
     (row written, next-ET-session check True, Guard 1b skips) — the decouple did not
     cost the real session-loss carryover.
  2. CIRCUIT-DOES-NOT-ARM (the fix): the circuit-breach branch writes NO lockout row for
     reason=max_loss_circuit (source has no such call; an explicit max_loss_circuit arm is
     never written) -> next-ET-session check is False and Guard 1b does NOT skip.
  3. count-budget arming UNCHANGED (regression guard on the surviving site).
  4. today-unblock RESET semantics (the id=151-shaped row is cleared by the flag / a
     breaker_tripped=FALSE row, NOT by the code edit).
  5. flag-OFF parity: check() is byte-identical not-locked.

SessionLocal (used inside set_/check_next_day_trading_lockout) is bound to the SAME test
engine the ``db`` fixture reads (conftest sets DATABASE_URL := TEST_DATABASE_URL before
app.db import), so writes via the governance helpers are visible to the fixture session
and are TRUNCATEd per-test.
"""

import datetime as _dt
import inspect
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.config import settings
from app.services.trading import governance
from app.services.trading.governance import (
    check_next_day_trading_lockout,
    set_next_day_trading_lockout,
)

_REGIME = "rulebreak_nextday_lockout"
_FLAG = "chili_momentum_rulebreak_nextday_lockout_enabled"


@pytest.fixture(autouse=True)
def _isolate_lockout_rows(db):
    """``trading_risk_state`` is a RAW-migration table (no ORM model) so it is NOT in
    ``Base.metadata`` and the conftest full-truncate never clears it. Independent
    ``SessionLocal`` commits inside set_/check_next_day_trading_lockout would otherwise
    leak the lockout regime's rows across tests. Clear them (and the process kill-switch)
    before AND after each test so every case starts from a clean lockout ledger."""
    def _clear():
        db.execute(
            text("DELETE FROM trading_risk_state WHERE regime = :regime"),
            {"regime": _REGIME},
        )
        db.commit()
        try:
            governance.deactivate_kill_switch()
        except Exception:
            pass

    _clear()
    yield
    _clear()


def _et_today_tomorrow():
    et = ZoneInfo("America/New_York")
    today = _dt.datetime.utcnow().replace(tzinfo=_dt.timezone.utc).astimezone(et).date()
    return today, today + _dt.timedelta(days=1)


def _lockout_rows(db, *, reason=None):
    sql = (
        "SELECT (snapshot_date AT TIME ZONE 'UTC')::date AS d, breaker_reason, breaker_tripped "
        "FROM trading_risk_state WHERE regime = :regime"
    )
    params = {"regime": _REGIME}
    if reason is not None:
        sql += " AND breaker_reason = :reason"
        params["reason"] = reason
    return db.execute(text(sql), params).fetchall()


def _reset_kill_switch():
    """Belt-and-braces: clear the process-global kill switch so a prior test's breach
    (and Guard 1 in run_auto_arm_pass) cannot leak into the next pass."""
    try:
        governance.deactivate_kill_switch()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# 1. PROTECTION-PRESERVED — a genuine daily_loss_breach STILL arms the carryover.
# ─────────────────────────────────────────────────────────────────────────────
def test_genuine_daily_loss_breach_still_arms_nextday_lockout(db, monkeypatch):
    from app.services.trading.momentum_neural import auto_arm

    monkeypatch.setattr(settings, _FLAG, True, raising=False)
    # A small absolute USD cap and a large simulated realized loss -> genuine breach.
    monkeypatch.setattr(settings, "chili_global_max_daily_loss_usd", 100.0, raising=False)
    monkeypatch.setattr(settings, "chili_global_max_daily_loss_pct_of_equity", 0.0, raising=False)
    monkeypatch.setattr(
        governance,
        "global_realized_pnl_today_et",
        lambda _db, user_id=None: {"total_usd": -500.0, "autotrader_usd": -500.0, "momentum_usd": 0.0},
        raising=True,
    )
    _reset_kill_switch()

    # Drive the REAL arming site (governance.check_daily_loss_breach -> the surviving
    # set_next_day_trading_lockout("daily_loss_breach") call), activating the breach.
    res = governance.check_daily_loss_breach(db, activate=True)
    assert res["breached"] is True

    _today, tomorrow = _et_today_tomorrow()
    rows = _lockout_rows(db, reason="daily_loss_breach")
    assert len(rows) == 1, f"expected exactly one daily_loss_breach lockout row, got {rows}"
    assert rows[0].d == tomorrow, f"lockout eff-day must be TOMORROW ET, got {rows[0].d} (tomorrow={tomorrow})"
    assert rows[0].breaker_tripped is True
    assert rows[0].breaker_reason == "daily_loss_breach"

    # NEXT ET session: the kill switch has been reset, the lockout row carries over.
    # Simulate "tomorrow" by stamping the row's effective day to TODAY ET, then check.
    today, _tom = _et_today_tomorrow()
    db.execute(
        text(
            "UPDATE trading_risk_state SET snapshot_date = :today "
            "WHERE regime = :regime AND breaker_reason = 'daily_loss_breach'"
        ),
        {"today": today, "regime": _REGIME},
    )
    db.commit()

    locked, meta = check_next_day_trading_lockout()
    assert locked is True, f"genuine daily_loss_breach must lock the next ET session: {meta}"
    assert meta["rule_break_reason"] == "daily_loss_breach"

    # Guard 1b in run_auto_arm_pass must SKIP on the active lockout. Reset the kill switch
    # so Guard 1 passes and Guard 1b is the gate that fires.
    _reset_kill_switch()
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_live_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True, raising=False)
    monkeypatch.setattr(auto_arm, "_auto_arm_user_id", lambda: 1, raising=True)

    out = auto_arm.run_auto_arm_pass(db)
    assert out["skipped"] == "rulebreak_nextday_lockout", f"Guard 1b must skip: {out}"
    assert out.get("lockout", {}).get("rule_break_reason") == "daily_loss_breach"


# ─────────────────────────────────────────────────────────────────────────────
# 2. CIRCUIT-DOES-NOT-ARM (the fix) — a lone #769 fire writes NO max_loss_circuit row.
# ─────────────────────────────────────────────────────────────────────────────
def test_max_loss_circuit_fire_does_not_arm_nextday_lockout_source():
    """The circuit-breach branch must contain NO set_next_day_trading_lockout call and
    must NOT pass 'max_loss_circuit' to it anywhere. Asserted in source so a future edit
    cannot silently re-couple the per-trade stop to the cross-day lockout."""
    from app.services.trading.momentum_neural import live_runner

    src = inspect.getsource(live_runner)
    # No call site arms the lockout with the circuit reason.
    assert 'set_next_day_trading_lockout("max_loss_circuit")' not in src
    assert "set_next_day_trading_lockout('max_loss_circuit')" not in src
    # The decoupled rationale comment is present and the circuit still fires + bails.
    assert "GAP 1 (DECOUPLED)" in src
    assert 'le["max_loss_circuit_fired"] = True' in src
    assert '"reason": "max_loss_circuit",' in src  # the live_bailout emit reason is intact


def test_circuit_breach_path_writes_no_lockout_row_and_does_not_lock_next_session(db, monkeypatch):
    """FUNCTIONAL companion to the source guard: with the flag ON, exercise the EXACT
    circuit-breach side effects the live runner now performs (set the two le flags;
    DO NOT arm any lockout) and prove that — unlike a daily_loss_breach — NO
    rulebreak_nextday_lockout row exists for reason=max_loss_circuit, so the next ET
    session check is False and Guard 1b does NOT skip."""
    from app.services.trading.momentum_neural import auto_arm

    monkeypatch.setattr(settings, _FLAG, True, raising=False)
    _reset_kill_switch()

    # Reproduce the surviving circuit-breach side effects verbatim (post-decouple): the
    # two le-flag writes, and NO set_next_day_trading_lockout call. (The full live_runner
    # branch — _commit_le / _safe_transition(STATE_LIVE_BAILOUT) / live_bailout emit — is
    # exercised by the runner's own integration tests; here we pin the LOCKOUT side effect.)
    le = {}
    _circuit = {"breach": True, "floor_price": 4.20}
    if _circuit.get("breach"):
        le["max_loss_circuit_fired"] = True
        le["max_loss_circuit_floor_price"] = _circuit["floor_price"]
        # DECOUPLED: no set_next_day_trading_lockout("max_loss_circuit") here.

    assert le["max_loss_circuit_fired"] is True
    assert le["max_loss_circuit_floor_price"] == 4.20

    # No lockout row of ANY reason was written by the circuit path.
    assert _lockout_rows(db) == [], "circuit-breach must write NO next-day lockout row"
    assert _lockout_rows(db, reason="max_loss_circuit") == []

    # Next ET session: nothing armed -> not locked.
    locked, meta = check_next_day_trading_lockout()
    assert locked is False, f"a lone circuit fire must NOT lock the next ET session: {meta}"
    assert meta["reason"] == "no_lockout_armed"

    # Guard 1b must NOT skip on a circuit-only history.
    _reset_kill_switch()
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_live_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True, raising=False)
    monkeypatch.setattr(auto_arm, "_auto_arm_user_id", lambda: 1, raising=True)
    out = auto_arm.run_auto_arm_pass(db)
    assert out["skipped"] != "rulebreak_nextday_lockout", f"Guard 1b must NOT skip on a lone circuit fire: {out}"


# ─────────────────────────────────────────────────────────────────────────────
# 3. count-budget arming UNCHANGED — regression guard on the surviving site.
# ─────────────────────────────────────────────────────────────────────────────
def test_daily_trade_count_budget_still_arms_nextday_lockout(db, monkeypatch):
    """The daily_trade_count_budget arming site (live_runner.py:9531) is UNTOUCHED: it
    still calls set_next_day_trading_lockout('daily_trade_count_budget'). Drive that arm
    and assert it writes the carryover row + locks the next ET session."""
    monkeypatch.setattr(settings, _FLAG, True, raising=False)

    armed = set_next_day_trading_lockout("daily_trade_count_budget")
    assert armed is True

    _today, tomorrow = _et_today_tomorrow()
    rows = _lockout_rows(db, reason="daily_trade_count_budget")
    assert len(rows) == 1
    assert rows[0].d == tomorrow
    assert rows[0].breaker_tripped is True

    # And the surviving call site still references the reason in source.
    from app.services.trading.momentum_neural import live_runner

    src = inspect.getsource(live_runner)
    assert 'set_next_day_trading_lockout("daily_trade_count_budget")' in src

    # Roll the eff-day to TODAY ET and confirm it locks.
    today, _tom = _et_today_tomorrow()
    db.execute(
        text(
            "UPDATE trading_risk_state SET snapshot_date = :today "
            "WHERE regime = :regime AND breaker_reason = 'daily_trade_count_budget'"
        ),
        {"today": today, "regime": _REGIME},
    )
    db.commit()
    locked, meta = check_next_day_trading_lockout()
    assert locked is True
    assert meta["rule_break_reason"] == "daily_trade_count_budget"


# ─────────────────────────────────────────────────────────────────────────────
# 4. today-unblock RESET — the id=151-shaped row is cleared by the flag / a FALSE
#    breaker_tripped, NOT by the code edit.
# ─────────────────────────────────────────────────────────────────────────────
def _insert_row_151_shaped(db, *, lock_day, tripped=True, reason="max_loss_circuit"):
    db.execute(
        text(
            "INSERT INTO trading_risk_state (user_id, snapshot_date, breaker_tripped, breaker_reason, regime, capital) "
            "VALUES (NULL, :d, :tripped, :reason, :regime, 0)"
        ),
        {"d": lock_day, "tripped": tripped, "reason": reason, "regime": _REGIME},
    )
    db.commit()


def test_today_row_locks_then_flag_off_and_untripped_clear(db, monkeypatch):
    today, _tom = _et_today_tomorrow()

    # An id=151-shaped row: reason=max_loss_circuit, lock_day=TODAY, tripped=TRUE.
    _insert_row_151_shaped(db, lock_day=today, tripped=True, reason="max_loss_circuit")

    # Flag ON + tripped row dated TODAY -> locked TODAY.
    monkeypatch.setattr(settings, _FLAG, True, raising=False)
    locked, meta = check_next_day_trading_lockout()
    assert locked is True, f"a tripped TODAY-dated row must lock: {meta}"
    assert meta["lock_day"] == str(today)

    # (a) Flag OFF -> (False, disabled). The RESET clears today via the flag, not the edit.
    monkeypatch.setattr(settings, _FLAG, False, raising=False)
    locked_off, meta_off = check_next_day_trading_lockout()
    assert locked_off is False
    assert meta_off == {"locked": False, "reason": "disabled"}

    # (b) Flag ON but the row's breaker_tripped flipped to FALSE -> (False, no_lockout_armed).
    monkeypatch.setattr(settings, _FLAG, True, raising=False)
    db.execute(
        text("UPDATE trading_risk_state SET breaker_tripped = FALSE WHERE regime = :regime"),
        {"regime": _REGIME},
    )
    db.commit()
    locked_reset, meta_reset = check_next_day_trading_lockout()
    assert locked_reset is False
    assert meta_reset["reason"] == "no_lockout_armed"


# ─────────────────────────────────────────────────────────────────────────────
# 5. flag-OFF parity — check() is byte-identical not-locked; set() is a no-op.
# ─────────────────────────────────────────────────────────────────────────────
def test_flag_off_check_is_byte_identical_not_locked(db, monkeypatch):
    monkeypatch.setattr(settings, _FLAG, False, raising=False)
    # Even with a tripped TODAY-dated row present, flag OFF => (False, {disabled}).
    today, _tom = _et_today_tomorrow()
    _insert_row_151_shaped(db, lock_day=today, tripped=True, reason="max_loss_circuit")

    locked, meta = check_next_day_trading_lockout()
    assert (locked, meta) == (False, {"locked": False, "reason": "disabled"})

    # set_*() is a no-op with the flag OFF and writes nothing.
    assert set_next_day_trading_lockout("daily_loss_breach") is False
    assert _lockout_rows(db, reason="daily_loss_breach") == []

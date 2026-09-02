"""Bug B1 ng 09-02 CANF 19471: ni-reap ng auto-arm reaper ang session na
nagsu-submit — sa PAREHONG SEGUNDO ng fill.

ANG INSIDENTE. 11:19:10.127Z order f3ed508d FILLED 165 CANF @ 4.62. Parehong
segundo: `[auto_arm] reaped stale pre-entry session=19471 CANF
state=watching_live (watched > 300s, never entered)`. Ang session ay armed
11:03:23, nag-trade nang isang beses, na-recycle 11:11:10 (entry_submitted
reset sa False), at nasa live_pending_entry mula 11:16:34 na may
pending_place. Tatlong depekto:
  (1) ang broker-order guard ay nagbasa ng snapshot BAGO ang SELECT FOR UPDATE
      ng cancel_automation_session — lumang premise;
  (2) ang age basis ay started_at (orihinal na arm), hindi ang huling recycle;
  (3) ang DEFERRED na cancel (durable claim na may CID) ay binilang bilang
      reaped at ang writes nito (pause + pointers) ay nakadepende sa
      `if reaped: db.commit()` — kung hindi nabilang, nire-rollback ng
      run_auto_arm_pass bago ang probe phase.
Resulta: watching_live + entry_submitted=true + filled order, WALANG position,
WALANG deadman — 15 minutong hubad, manu-manong flatten sa 3.96 = −108.85.

Runnable: pytest tests/test_naked_position_0902_reaper.py -v
"""
from __future__ import annotations

import ast
import inspect
import pathlib
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import auto_arm as AA
from app.services.trading.momentum_neural import live_runner as LR

_SRC = pathlib.Path(AA.__file__)


def _reap_fn_src() -> str:
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_reap_stale_watching_sessions"
    )
    return ast.unparse(fn)


# ── fakes ────────────────────────────────────────────────────────────────────


class _Nested:
    def __init__(self, db):
        self._db = db
        self.is_active = True

    def commit(self):
        self._db.nested_commits += 1
        self.is_active = False

    def rollback(self):
        self._db.nested_rollbacks += 1
        self.is_active = False


class _LockedQuery:
    def __init__(self, db):
        self._db = db

    def filter(self, *_a, **_k):
        return self

    def with_for_update(self, *, nowait=False, **_k):
        self._db.nowait_seen = bool(nowait)
        return self

    def populate_existing(self):
        return self

    def one_or_none(self):
        if self._db.lock_error is not None:
            raise self._db.lock_error
        return self._db.locked_row

    def all(self):
        return list(self._db.rows)


class _LockingDb:
    """Row-list db with a savepoint + FOR UPDATE NOWAIT probe."""

    def __init__(self, rows, *, locked_row=None, lock_error=None):
        self.rows = rows
        self.locked_row = locked_row if locked_row is not None else (rows[0] if rows else None)
        self.lock_error = lock_error
        self.commits = 0
        self.nested_commits = 0
        self.nested_rollbacks = 0
        self.nowait_seen = None

    def query(self, *_a, **_k):
        return _LockedQuery(self)

    def begin_nested(self):
        return _Nested(self)

    def commit(self):
        self.commits += 1

    def flush(self):
        pass

    def rollback(self):
        pass


class _PlainDb(_LockingDb):
    """No begin_nested => the probe is skipped (legacy doubles)."""
    begin_nested = None


def _row(sid=19471, *, state="watching_live", le=None, started_age_sec=900, symbol="CANF"):
    return SimpleNamespace(
        id=sid, symbol=symbol, state=state,
        started_at=datetime.utcnow() - timedelta(seconds=started_age_sec),
        risk_snapshot_json={"momentum_live_execution": dict(le or {})},
    )


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(AA, "_max_watch_seconds", lambda: 300)
    monkeypatch.setattr(AA, "_watch_extend_seconds", lambda: 600)
    monkeypatch.setattr(AA, "_symbol_of_day_focus_enabled", lambda: False)
    monkeypatch.setattr(AA, "_event_based_abandonment_enabled", lambda: False)
    monkeypatch.setattr(AA.settings, "chili_momentum_adaptive_watch_enabled", False, raising=False)
    cooldowns: list[str] = []
    monkeypatch.setattr(AA, "_write_reap_cooldown", lambda sym, now: cooldowns.append(sym))
    return cooldowns


def _patch_cancel(monkeypatch, result, calls=None):
    def _cancel(db, *, user_id, session_id):
        if calls is not None:
            calls.append(session_id)
        return result
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.automation_query.cancel_automation_session",
        _cancel,
    )


# ── T1: the pure in-flight predicate ─────────────────────────────────────────


@pytest.mark.parametrize("state,le,expected", [
    ("watching_live", {}, None),
    ("queued_live", {}, None),
    ("armed_pending_runner", {}, None),
    ("live_pending_entry", {}, "state_not_reapable:live_pending_entry"),
    ("live_entry_candidate", {}, "state_not_reapable:live_entry_candidate"),
    ("live_entered", {}, "state_not_reapable:live_entered"),
    ("watching_live", {"entry_order_id": "f3ed508d"}, "entry_order_id"),
    ("watching_live", {"entry_submitted": True}, "entry_submitted"),
    ("watching_live", {"entry_order_ids_all": ["a"], "entry_orders_resolved": {}},
     "unresolved_entry_order"),
    ("watching_live", {"entry_order_ids_all": ["a"], "entry_orders_resolved": {"a": "void"}},
     None),
    ("watching_live", {"entry_place_count": 1}, "entry_place_count"),
    ("watching_live", {"entry_place_count": "x"}, "entry_place_count_unreadable"),
    ("watching_live", {"entry_client_order_id": "chili-1"}, "entry_client_order_id"),
    ("watching_live", {"entry_reconcile_pending_client_order_id": "chili-1"},
     "entry_client_order_id"),
    ("watching_live", {"entry_order_request": {"x": 1}}, "entry_order_request"),
    ("watching_live", "garbage", None),
])
def test_reap_in_flight_reason_table(state, le, expected):
    assert AA._reap_in_flight_reason(state, le) == expected


# ── T2: the pure age anchor ──────────────────────────────────────────────────


def test_reap_age_anchor_prefers_recycle_then_exit_then_started_at():
    started = datetime(2026, 9, 2, 11, 3, 23)
    row = SimpleNamespace(started_at=started)
    assert AA._reap_age_anchor(row, {}) == started
    assert AA._reap_age_anchor(row, "garbage") == started
    assert AA._reap_age_anchor(row, {"last_exit_at_utc": "2026-09-02T11:11:05"}) == \
        datetime(2026, 9, 2, 11, 11, 5)
    assert AA._reap_age_anchor(
        row, {"last_exit_at_utc": "2026-09-02T11:11:05",
              "last_recycled_at_utc": "2026-09-02T11:11:10"}
    ) == datetime(2026, 9, 2, 11, 11, 10)
    # tz-aware stamp is normalised to naive UTC
    assert AA._reap_age_anchor(
        row, {"last_recycled_at_utc": "2026-09-02T11:11:10+00:00"}
    ) == datetime(2026, 9, 2, 11, 11, 10)
    # garbage stamp falls through to started_at
    assert AA._reap_age_anchor(row, {"last_recycled_at_utc": "nope"}) == started
    assert AA._reap_age_anchor(SimpleNamespace(), {}) is None


# ── T3: the CANF replay — locked row is pending => NOT reaped ────────────────


def test_canf_replay_locked_row_is_pending_so_cancel_is_never_called(quiet, monkeypatch):
    """ANG PANGUNAHING KASO. Ang unlocked snapshot ay watching_live at malinis;
    ang LOCKED row ay live_pending_entry na may entry_place_count=1 (ang runner
    ay nagsu-submit). Walang cancel, 0 reaped, isang savepoint rollback."""
    calls: list[int] = []
    _patch_cancel(monkeypatch, {"ok": True}, calls)
    stale = _row(state="watching_live", le={})
    fresh = _row(state="live_pending_entry", le={"entry_place_count": 1})
    db = _LockingDb([stale], locked_row=fresh)
    n = AA._reap_stale_watching_sessions(db, user_id=1, now=datetime.utcnow())
    assert n == 0
    assert calls == []
    assert db.nested_rollbacks == 1
    assert db.nested_commits == 0
    assert db.nowait_seen is True
    assert quiet == []


def test_locked_row_with_submitted_order_aborts(quiet, monkeypatch):
    calls: list[int] = []
    _patch_cancel(monkeypatch, {"ok": True}, calls)
    stale = _row(state="watching_live", le={})
    fresh = _row(state="watching_live", le={"entry_submitted": True, "entry_order_id": "f3ed508d"})
    db = _LockingDb([stale], locked_row=fresh)
    assert AA._reap_stale_watching_sessions(db, user_id=1, now=datetime.utcnow()) == 0
    assert calls == []


# ── T4: lock busy => skip, never wait ────────────────────────────────────────


def test_lock_busy_skips_without_cancel(quiet, monkeypatch):
    calls: list[int] = []
    _patch_cancel(monkeypatch, {"ok": True}, calls)
    db = _LockingDb([_row()], lock_error=RuntimeError("LockNotAvailable"))
    assert AA._reap_stale_watching_sessions(db, user_id=1, now=datetime.utcnow()) == 0
    assert calls == []
    assert db.nested_rollbacks == 1
    assert db.commits == 0


# ── T5: fresh since recycle => KEEP ─────────────────────────────────────────


def test_fresh_since_recycle_is_kept_even_if_started_at_is_old(quiet, monkeypatch):
    calls: list[int] = []
    _patch_cancel(monkeypatch, {"ok": True}, calls)
    now = datetime.utcnow()
    le = {"last_recycled_at_utc": (now - timedelta(seconds=120)).isoformat()}
    row = _row(state="watching_live", le=le, started_age_sec=950)
    db = _LockingDb([row])
    assert AA._reap_stale_watching_sessions(db, user_id=1, now=now) == 0
    assert calls == []


def test_stale_since_recycle_is_reaped(quiet, monkeypatch):
    calls: list[int] = []
    _patch_cancel(monkeypatch, {"ok": True, "state": "live_cancelled"}, calls)
    now = datetime.utcnow()
    le = {"last_recycled_at_utc": (now - timedelta(seconds=400)).isoformat()}
    row = _row(state="watching_live", le=le, started_age_sec=950)
    db = _LockingDb([row])
    assert AA._reap_stale_watching_sessions(db, user_id=1, now=now) == 1
    assert calls == [19471]
    assert db.nested_commits == 1
    assert quiet == ["CANF"]


def test_fresh_since_recycle_under_lock_aborts(quiet, monkeypatch):
    """Ang recycle ay nag-commit sa pagitan ng unlocked read at ng lock."""
    calls: list[int] = []
    _patch_cancel(monkeypatch, {"ok": True}, calls)
    now = datetime.utcnow()
    stale = _row(state="watching_live", le={})
    fresh = _row(state="watching_live",
                 le={"last_recycled_at_utc": (now - timedelta(seconds=30)).isoformat()})
    db = _LockingDb([stale], locked_row=fresh)
    assert AA._reap_stale_watching_sessions(db, user_id=1, now=now) == 0
    assert calls == []
    assert db.nested_rollbacks == 1


# ── T6: deferred cancel => committed, not counted, no cooldown ──────────────


@pytest.mark.parametrize("result", [
    {"ok": True, "pending": "durable_alpaca_entry_claim_reconcile", "state": "watching_live"},
    {"ok": True, "pending": "broker_terminal_truth_reconcile",
     "terminalization_deferred": True, "state": "watching_live"},
    {"ok": True, "state": "watching_live"},
])
def test_deferred_cancel_is_committed_once_and_not_counted(quiet, monkeypatch, result):
    calls: list[int] = []
    _patch_cancel(monkeypatch, result, calls)
    db = _LockingDb([_row()])
    n = AA._reap_stale_watching_sessions(db, user_id=1, now=datetime.utcnow())
    assert n == 0
    assert calls == [19471]
    assert db.commits == 1, "ang pause/pointer writes ay dapat mag-persist"
    assert quiet == [], "walang cooldown sa deferred cancel"


@pytest.mark.parametrize("result", [
    {"ok": True},
    {"ok": True, "state": "live_cancelled"},
    {"ok": True, "session_id": 19471, "state": "live_cancelled"},
])
def test_terminal_cancel_counts_and_writes_cooldown(quiet, monkeypatch, result):
    _patch_cancel(monkeypatch, result)
    db = _LockingDb([_row()])
    assert AA._reap_stale_watching_sessions(db, user_id=1, now=datetime.utcnow()) == 1
    assert quiet == ["CANF"]
    assert db.commits == 0, "ang terminal reap ay kino-commit ng caller (reaped>0)"


def test_failed_cancel_is_neither_committed_nor_counted(quiet, monkeypatch):
    _patch_cancel(monkeypatch, {"ok": False, "error": "not_cancellable"})
    db = _LockingDb([_row()])
    assert AA._reap_stale_watching_sessions(db, user_id=1, now=datetime.utcnow()) == 0
    assert db.commits == 0
    assert quiet == []


def test_cancel_returning_none_is_deferred_not_reaped(quiet, monkeypatch):
    _patch_cancel(monkeypatch, None)
    db = _LockingDb([_row()])
    assert AA._reap_stale_watching_sessions(db, user_id=1, now=datetime.utcnow()) == 0
    assert quiet == []


def test_db_without_begin_nested_skips_the_probe(quiet, monkeypatch):
    """Legacy doubles (test_momentum_auto_arm._DBWithRows) have no savepoint."""
    _patch_cancel(monkeypatch, {"ok": True})
    db = _PlainDb([_row()])
    assert AA._reap_stale_watching_sessions(db, user_id=1, now=datetime.utcnow()) == 1


# ── T7: AST — ordering is the fix ────────────────────────────────────────────


def test_lock_probe_precedes_the_cancel():
    src = _reap_fn_src()
    i_cancel = src.index("cancel_automation_session(")
    assert src.index("with_for_update(nowait=True)") < i_cancel
    assert src.index("begin_nested") < i_cancel
    assert "_reap_in_flight_reason(" in src
    assert "_reap_age_anchor(" in src


def test_deferred_commit_sits_between_cancel_and_cooldown():
    src = _reap_fn_src()
    i_cancel = src.index("cancel_automation_session(")
    i_cool = src.index("_write_reap_cooldown(")
    i_commit = src.index("db.commit()")
    assert i_cancel < i_commit < i_cool
    i_pending = src.index("'pending'")
    assert i_pending < i_cool, "ang cooldown ay dapat pagkatapos ng pending check"
    assert "clear_operator_pause" not in src


def test_started_at_comparisons_only_live_in_the_sql_prefilter():
    src = _reap_fn_src()
    assert "s.started_at >=" not in src
    assert "s.started_at <" not in src
    assert "TradingAutomationSession.started_at < cutoff" in src


# ── T8: the recycle stamp in live_runner ─────────────────────────────────────


def test_recycle_stamp_survives_the_recycle_reset():
    assert "last_recycled_at_utc" not in LR._RECYCLE_ENTRY_STATE_KEYS
    le = {"last_recycled_at_utc": "x", "last_exit_at_utc": "y", "entry_order_id": "z"}
    cleared = LR._reset_entry_state_on_recycle(le)
    assert "entry_order_id" in cleared
    assert le["last_recycled_at_utc"] == "x"
    assert le["last_exit_at_utc"] == "y"


def test_recycle_branch_stamps_before_transition_to_watching():
    src = inspect.getsource(LR.tick_live_session)
    i_reset = src.index("_reset_entry_state_on_recycle(le)")
    tail = src[i_reset:i_reset + 2500]
    i_stamp = tail.index('le["last_recycled_at_utc"]')
    i_trans = tail.index("_safe_transition(db, sess, STATE_WATCHING_LIVE)")
    assert i_stamp < i_trans


# ── T9: the call site is byte-identical ──────────────────────────────────────


def test_run_auto_arm_pass_call_site_unchanged():
    src = inspect.getsource(AA.run_auto_arm_pass)
    assert "reaped = _reap_stale_watching_sessions(db, user_id=uid, now=pass_as_of)" in src
    assert '_mark("watching_reaper")' in src

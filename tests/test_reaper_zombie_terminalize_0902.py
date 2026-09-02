"""Zombie watcher 09-02 (JLHL 19463): ang reaper ay HINDI kailanman
nakapag-terminalize ng na-recycle, broker-flat na Alpaca watcher — at
muling nagtanong kada 10s.

ANG INSIDENTE. 19463 JLHL (alpaca_spot, alpaca:paper): pumasok 10:47Z,
na-stop out 10:49:05Z, na-recycle 10:49:09Z → watching_live; claim
`resolved` 10:49:04; walang quarantine; broker FLAT. Mula 10:54:16Z hanggang
13:44:17Z (883 pass, kada 10s) ang reaper ay nag-log ng "reaped stale
pre-entry session=19463" — pero nanatili ang row sa watching_live na may
`operator_pause` {active, resume_state: watching_live, paused_at_utc
re-stamped kada pass}. Walang tick (paused), walang terminal, zombie slot.

ANG UGAT (origin/main 947b521):
  * `venue/factory.py` `_BUILDERS` — WALANG `alpaca_spot` / `alpaca_short`
    builder (robinhood, coinbase, robinhood_agentic_mcp lang). Ang
    `get_adapter` ay `_BUILDERS.get(src)` → None nang TAHIMIK (walang log:
    0 na `[venue_factory]` na linya sa lane log ng buong araw).
  * `automation_query._reaper_broker_position_truth` → `adapter is None` →
    `(None, {"reason": "broker_position_unknown"})` — in-process man o hindi.
  * `_flatten_live_session_for_stop` → `broker_flat_unconfirmed`,
    `terminalization_deferred=True` → `cancel_automation_session` →
    `_pause_operator_terminalization` (re-stamp ng pause) → `ok=True,
    pending=broker_flat_confirmation`.
  * Ang bind (`_bind_persisted_alpaca_adapter`), ang generation quarantine,
    at ang 404-only-flat contract ng `get_position_quantity` ay HINDI naabot —
    lahat ay PAGKATAPOS ng adapter-None check. Kaya walang quarantine marker
    o event sa row (`execution_quarantine=null`, 0 quarantine event), at ang
    row JSON ay walang entry identity o position (ang recycle ay naglinis sa
    `_RECYCLE_ENTRY_STATE_KEYS`) — ang tanging sangay na natitira ay ang
    broker read, at iyon ang laging UNKNOWN.

ANG AYOS. (1) `factory._BUILDERS["alpaca_spot"|"alpaca_short"]` →
`AlpacaSpotAdapter()` (paper-only, account-pinned); walang binago sa bind +
generation fences na sumusunod; hindi mababasa ang broker ⇒ UNKNOWN pa rin
(fail-closed). (2) `cause` sa bawat UNKNOWN detail. (3) Reaper HOLD: habang
ang persisted `operator_pause` ay active na may parehong `resume_state` at
`paused_at_utc` na mas bata sa `chili_momentum_reap_deferred_retry_seconds`
(300s), walang lock probe at walang cancel — isang `reap HELD` na log kada
session — at muling tinatanong ang cancel kada cadence, hindi kada pass.

Runnable: pytest tests/test_reaper_zombie_terminalize_0902.py -v
"""
from __future__ import annotations

import ast
import inspect
import logging
import pathlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services.trading.momentum_neural import auto_arm as AA
from app.services.trading.momentum_neural import automation_query as aq
from app.services.trading.momentum_neural import session_lifecycle as SL
from app.services.trading.momentum_neural.live_fsm import (
    STATE_LIVE_CANCELLED,
    STATE_WATCHING_LIVE,
)
from app.services.trading.venue import factory
from app.services.trading.venue.alpaca_spot import AlpacaSpotAdapter

# Non-secret PAPER account UUID frozen on 19463's row (risk_snapshot_json).
_FROZEN = "c7d421e0-4fae-4219-9503-5ce051d4d923"


# ── fakes ────────────────────────────────────────────────────────────────────


class _FakeAlpaca:
    """Models the adapter's contract with no I/O: bind only to the configured
    pin (rebind to the SAME id is allowed, like the real one), reads refuse
    when unbound, 0.0 ONLY on an explicit 404, None on any other failure."""

    def __init__(self, *, qty=0.0, http_status=None, raise_exc=None):
        self.qty = qty
        self.http_status = http_status
        self.raise_exc = raise_exc
        self.bound = None
        self.binds: list = []
        self.reads: list = []

    @property
    def bound_account_id(self):
        return self.bound

    def bind_account_id(self, account_id):
        self.binds.append(account_id)
        expected = str(
            getattr(aq.settings, "chili_alpaca_expected_account_id", "") or ""
        ).strip()
        if not account_id or account_id != expected:
            return False
        if self.bound not in (None, account_id):
            return False
        self.bound = account_id
        return True

    def get_position_quantity(self, symbol):
        self.reads.append(symbol)
        if self.bound is None:
            return None
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.http_status == 404:
            return 0.0
        if self.http_status is not None:
            return None
        return self.qty


def _install_fake(monkeypatch, fake) -> None:
    """The factory imports AlpacaSpotAdapter at CALL time — patch the class."""
    import app.services.trading.venue.alpaca_spot as alpaca_spot

    monkeypatch.setattr(alpaca_spot, "AlpacaSpotAdapter", lambda: fake)


def _configure(monkeypatch, *, expected_account_id: str = _FROZEN) -> None:
    monkeypatch.setattr(aq.settings, "chili_alpaca_paper", True, raising=False)
    monkeypatch.setattr(
        aq.settings, "chili_alpaca_expected_account_id", expected_account_id, raising=False
    )


def _jlhl_19463(*, pause: dict | None = None, le_extra: dict | None = None):
    """The row as read from the live DB at 13:43Z: recycled watcher, no entry
    identity, no position, claim resolved elsewhere, pause re-stamped."""
    le = {
        "last_exit_at_utc": "2026-09-02T10:49:05.677374",
        "last_exit_reason": "stop",
        "trade_cycles": 1,
        "stopout_cycles": 1,
        "realized_pnl_usd": -18.830024,
        "entry_fill_event_id": 1459001,
        "deadman_generation": 1,
    }
    le.update(le_extra or {})
    snap = {
        "alpaca_account_id": _FROZEN,
        "alpaca_account_scope": "alpaca:paper",
        "alpaca_symbol_claim_token": "arm-3fc5050b-e3e8-4170-9c3b-3be84c69e3c2",
        "momentum_live_execution": le,
    }
    if pause is not None:
        snap["operator_pause"] = pause
    return SimpleNamespace(
        id=19463, user_id=1, mode="live", venue="alpaca",
        execution_family="alpaca_spot", symbol="JLHL", state=STATE_WATCHING_LIVE,
        correlation_id="corr-19463",
        started_at=datetime(2026, 9, 2, 10, 47, 2, 443622),
        updated_at=datetime(2026, 9, 2, 13, 43, 17, 31385),
        ended_at=None,
        risk_snapshot_json=snap,
    )


_PAUSE_19463 = {
    "active": True,
    "resume_state": "watching_live",
    "paused_at_utc": "2026-09-02T13:43:17.031385",
}


# ── T1: the factory serves the Alpaca families ──────────────────────────────


def test_factory_serves_the_alpaca_families_as_fresh_paper_adapters():
    a = factory.get_adapter("alpaca_spot")
    b = factory.get_adapter("alpaca_spot")
    assert isinstance(a, AlpacaSpotAdapter)
    assert isinstance(factory.get_adapter(" ALPACA_SHORT "), AlpacaSpotAdapter)
    assert a is not b, "fresh instance per call: a bind never leaks across sessions"
    assert a.bound_account_id is None
    assert factory.is_supported("alpaca_spot") is True
    assert factory.is_supported("alpaca_short") is True
    # unchanged surfaces
    assert factory.get_adapter("alpaca") is None
    assert factory.is_supported("alpaca") is False
    # Trade.broker_source filter set (robinhood / coinbase rows) — Alpaca sessions
    # never write those rows, so the families are deliberately NOT added here.
    assert {"robinhood", "coinbase"} <= factory.SUPPORTED_BROKER_SOURCES
    assert not {"alpaca_spot", "alpaca_short"} & factory.SUPPORTED_BROKER_SOURCES


def test_factory_alpaca_build_failure_returns_none_and_logs(monkeypatch, caplog):
    import app.services.trading.venue.alpaca_spot as alpaca_spot

    def _boom():
        raise RuntimeError("simulated adapter init failure")

    monkeypatch.setattr(alpaca_spot, "AlpacaSpotAdapter", _boom)
    caplog.set_level(logging.WARNING, logger="app.services.trading.venue.factory")
    assert factory.get_adapter("alpaca_spot") is None
    assert any("adapter build failed" in r.getMessage() for r in caplog.records)


def test_factory_imports_the_adapter_class_at_call_time():
    """So a test that forbids any Alpaca build by patching
    ``alpaca_spot.AlpacaSpotAdapter`` (test_automation_operator_exit_truth
    ``_forbid_legacy_recertification_io``) still fails loudly."""
    src = inspect.getsource(factory._build_alpaca_paper)
    assert "from .alpaca_spot import AlpacaSpotAdapter" in src
    assert factory._BUILDERS["alpaca_spot"] is factory._BUILDERS["alpaca_short"]


# ── T2: the real adapter's 404-only-flat contract (stub client, no I/O) ─────


class _Err(Exception):
    def __init__(self, status):
        super().__init__(f"http {status}")
        self.status_code = status


class _StubClient:
    def __init__(self, *, exc=None, qty=None):
        self.exc = exc
        self.qty = qty

    def get_open_position(self, symbol):
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(qty=self.qty)


def _sdk_404():
    """The exact alpaca-py shape the lane sees for an absent position."""
    from alpaca.common.exceptions import APIError

    http_error = SimpleNamespace(
        response=SimpleNamespace(status_code=404), request=None,
    )
    return APIError('{"code":40410000,"message":"position does not exist"}', http_error)


@pytest.mark.parametrize("client,expected", [
    (_StubClient(exc=_Err(404)), 0.0),
    (_StubClient(exc=_sdk_404()), 0.0),
    (_StubClient(exc=_Err(500)), None),
    (_StubClient(exc=_Err(403)), None),
    (_StubClient(exc=RuntimeError("Alpaca adapter account generation changed")), None),
    (_StubClient(qty="0"), 0.0),
    (_StubClient(qty="149"), 149.0),
    (_StubClient(qty=None), None),
])
def test_get_position_quantity_is_flat_only_on_an_explicit_404(monkeypatch, client, expected):
    adapter = AlpacaSpotAdapter()
    monkeypatch.setattr(adapter, "_account_client", lambda: client)
    assert adapter.get_position_quantity("JLHL") == expected


def test_get_position_quantity_refuses_when_the_pinned_client_generation_moved(monkeypatch):
    """``_account_client`` raises when the cached client's observed account no
    longer equals the bound id — the read must be UNKNOWN, never flat."""
    adapter = AlpacaSpotAdapter()
    monkeypatch.setattr(
        "app.services.trading.venue.alpaca_spot._expected_account_id", lambda: _FROZEN
    )
    assert adapter.bind_account_id(_FROZEN) is True
    assert adapter.bind_account_id(_FROZEN) is True, "same-id rebind is a no-op"
    assert adapter.bind_account_id("someone-else") is False
    monkeypatch.setattr(
        "app.services.trading.venue.alpaca_spot._trading_client",
        lambda: SimpleNamespace(),  # not the cached 'trading:paper' client
    )
    assert adapter.get_position_quantity("JLHL") is None


# ── T3: the 19463 replay through the real gate ──────────────────────────────


def test_19463_replay_before_the_fix_was_unknown_with_no_broker_call(monkeypatch):
    """The PRE-FIX world, reproduced: no builder => adapter None => UNKNOWN,
    before bind / generation / read — exactly what the row showed (no
    quarantine marker, no event, pause re-stamped)."""
    _configure(monkeypatch)
    fake = _FakeAlpaca(http_status=404)
    _install_fake(monkeypatch, fake)
    monkeypatch.delitem(factory._BUILDERS, "alpaca_spot")
    sess = _jlhl_19463(pause=_PAUSE_19463)
    assert aq._persisted_alpaca_execution_quarantine_reason(sess) is None
    flat, detail = aq._reaper_broker_position_truth(sess)
    assert flat is None
    assert detail == {"reason": "broker_position_unknown", "cause": "adapter_unavailable"}
    assert fake.binds == [] and fake.reads == []


def test_19463_replay_after_the_fix_confirms_flat_through_bind_and_404(monkeypatch):
    _configure(monkeypatch)
    fake = _FakeAlpaca(http_status=404)
    _install_fake(monkeypatch, fake)
    sess = _jlhl_19463(pause=_PAUSE_19463)
    flat, detail = aq._reaper_broker_position_truth(sess)
    assert flat is True
    assert detail == {"broker_quantity": 0.0, "expected_side": "long"}
    assert fake.binds == [_FROZEN], "bound to the FROZEN generation before the read"
    assert fake.reads == ["JLHL"]


@pytest.mark.parametrize("fake,expected_cause", [
    (_FakeAlpaca(http_status=500), "read_unreadable"),
    (_FakeAlpaca(qty=None), "read_unreadable"),
    (_FakeAlpaca(raise_exc=RuntimeError("boom")), "read_raised"),
])
def test_unreadable_broker_is_never_flat(monkeypatch, fake, expected_cause):
    """FAIL-CLOSED: any non-404 outcome stays UNKNOWN (the caller defers)."""
    _configure(monkeypatch)
    _install_fake(monkeypatch, fake)
    flat, detail = aq._reaper_broker_position_truth(_jlhl_19463())
    assert flat is None
    assert detail == {"reason": "broker_position_unknown", "cause": expected_cause}
    assert fake.binds == [_FROZEN]


def test_generation_mismatch_is_quarantined_before_any_adapter_build(monkeypatch):
    _configure(monkeypatch, expected_account_id="acct-rotated")
    fake = _FakeAlpaca(http_status=404)
    _install_fake(monkeypatch, fake)
    flat, detail = aq._reaper_broker_position_truth(_jlhl_19463())
    assert flat is None
    assert detail["reason"] == "alpaca_account_generation_mismatch"
    assert detail["broker_calls"] == 0
    assert fake.binds == [] and fake.reads == []


def test_bind_refusal_is_unknown_with_zero_reads(monkeypatch):
    _configure(monkeypatch)
    fake = _FakeAlpaca(http_status=404)
    fake.bound = "another-generation"  # adapter already frozen elsewhere
    _install_fake(monkeypatch, fake)
    flat, detail = aq._reaper_broker_position_truth(_jlhl_19463())
    assert flat is None
    assert detail["reason"] == "alpaca_adapter_account_generation_bind_failed"
    assert fake.reads == []


@pytest.mark.parametrize("qty,expected_flat,expected_reason", [
    (149.0, False, None),
    (-5.0, None, "broker_position_direction_mismatch"),
])
def test_real_or_wrong_way_exposure_is_never_flat(monkeypatch, qty, expected_flat, expected_reason):
    _configure(monkeypatch)
    _install_fake(monkeypatch, _FakeAlpaca(qty=qty))
    flat, detail = aq._reaper_broker_position_truth(_jlhl_19463())
    assert flat is expected_flat
    assert detail.get("reason") == expected_reason
    assert detail["broker_quantity"] == qty


# ── T4: the stop/cancel gate on the 19463 shape ─────────────────────────────


class _FlushDb:
    def flush(self):
        pass


def test_flatten_for_stop_confirms_flat_on_the_recycled_watcher(monkeypatch):
    _configure(monkeypatch)
    _install_fake(monkeypatch, _FakeAlpaca(http_status=404))
    res = aq._flatten_live_session_for_stop(_FlushDb(), _jlhl_19463(pause=_PAUSE_19463), request_kind="cancel")
    assert res == {"ok": True, "action": "no_live_orders", "broker_flat_confirmed": True}


def test_flatten_for_stop_defers_with_the_cause_when_unreadable(monkeypatch):
    _configure(monkeypatch)
    _install_fake(monkeypatch, _FakeAlpaca(http_status=503))
    res = aq._flatten_live_session_for_stop(_FlushDb(), _jlhl_19463(), request_kind="cancel")
    assert res["action"] == "broker_flat_unconfirmed"
    assert res["terminalization_deferred"] is True
    assert res["pending"] == "broker_flat_confirmation"
    assert res["broker_truth"] == {"reason": "broker_position_unknown", "cause": "read_unreadable"}


# ── T5: cancel_automation_session terminalizes the zombie end-to-end ────────


class _CancelQuery:
    def __init__(self, sess):
        self.sess = sess

    def filter(self, *_a, **_k):
        return self

    def with_for_update(self, *_a, **_k):
        return self

    def populate_existing(self, *_a, **_k):
        return self

    def one_or_none(self):
        return self.sess


class _CancelDb:
    def __init__(self, sess):
        self.sess = sess
        self.events: list[tuple] = []

    def query(self, *_a, **_k):
        return _CancelQuery(self.sess)

    def add(self, obj):
        self.events.append((getattr(obj, "event_type", None), getattr(obj, "payload_json", None)))

    def flush(self):
        pass

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))


def _configure_cancel(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(aq, "_tables_present", lambda _db: True)
    # 19463's claim is phase=resolved (10:49:04) — the durable seam is readable and clear.
    monkeypatch.setattr(aq, "_owned_unresolved_alpaca_entry_claim", lambda *_a, **_k: (True, None))
    retired: list = []
    monkeypatch.setattr(
        aq, "_retire_confirmed_pre_http_alpaca_claim_before_terminal",
        lambda db, sess, *, new_state: retired.append(new_state),
    )
    return retired


def test_cancel_terminalizes_the_recycled_flat_watcher(monkeypatch):
    retired = _configure_cancel(monkeypatch)
    fake = _FakeAlpaca(http_status=404)
    _install_fake(monkeypatch, fake)
    sess = _jlhl_19463(pause=_PAUSE_19463)
    db = _CancelDb(sess)

    res = aq.cancel_automation_session(db, user_id=1, session_id=19463)

    assert res["ok"] is True, res
    assert res.get("pending") is None and not res.get("terminalization_deferred"), res
    assert sess.state == STATE_LIVE_CANCELLED
    assert sess.ended_at is not None
    assert sess.risk_snapshot_json["operator_pause"]["active"] is False
    assert retired == [STATE_LIVE_CANCELLED]
    assert fake.reads == ["JLHL"]
    cancelled = [p for et, p in db.events if et == "session_cancelled"]
    assert cancelled and cancelled[0]["by"] == "automation_monitor"
    assert cancelled[0]["previous_state"] == STATE_WATCHING_LIVE
    assert cancelled[0]["order_cleanup"] == {"skipped": "alpaca_exact_flatten_completed"}


def test_cancel_stays_deferred_and_paused_when_the_broker_is_unreadable(monkeypatch):
    """FAIL-CLOSED end to end: the row is left NON-terminal, paused with
    resume_state == its state, and the result names the cause."""
    retired = _configure_cancel(monkeypatch)
    _install_fake(monkeypatch, _FakeAlpaca(http_status=500))
    sess = _jlhl_19463()
    db = _CancelDb(sess)

    res = aq.cancel_automation_session(db, user_id=1, session_id=19463)

    assert res["ok"] is True
    assert res["pending"] == "broker_flat_confirmation"
    assert res["cancel_reconcile"]["broker_truth"]["cause"] == "read_unreadable"
    assert sess.state == STATE_WATCHING_LIVE
    assert sess.ended_at is None
    pause = sess.risk_snapshot_json["operator_pause"]
    assert pause["active"] is True and pause["resume_state"] == STATE_WATCHING_LIVE
    assert retired == []
    assert not [et for et, _ in db.events if et == "session_cancelled"]


# ── T6: the pure hold predicate ─────────────────────────────────────────────


_NOW = datetime(2026, 9, 2, 13, 43, 27)


def _pause(age_s, *, active=True, resume_state="watching_live"):
    return {
        "active": active,
        "resume_state": resume_state,
        "paused_at_utc": (_NOW - timedelta(seconds=age_s)).isoformat(),
    }


@pytest.mark.parametrize("state,snap,expected", [
    ("watching_live", {}, None),
    ("watching_live", "garbage", None),
    ("watching_live", {"operator_pause": "garbage"}, None),
    ("watching_live", {"operator_pause": _pause(10)}, 290.0),
    ("watching_live", {"operator_pause": _pause(299)}, 1.0),
    ("watching_live", {"operator_pause": _pause(300)}, None),
    ("watching_live", {"operator_pause": _pause(400)}, None),
    ("watching_live", {"operator_pause": _pause(10, active=False)}, None),
    ("watching_live", {"operator_pause": _pause(10, resume_state="live_exited")}, None),
    ("queued_live", {"operator_pause": _pause(10, resume_state="watching_live")}, None),
    ("watching_live", {"operator_pause": {"active": True, "resume_state": "watching_live"}}, None),
    ("watching_live", {"operator_pause": {"active": True, "resume_state": "watching_live",
                                          "paused_at_utc": "nope"}}, None),
    # future stamp (clock skew) counts as age 0 -> full cadence
    ("watching_live", {"operator_pause": _pause(-30)}, 300.0),
])
def test_reap_deferred_hold_remaining_table(state, snap, expected):
    got = AA._reap_deferred_hold_remaining(state, snap, _NOW, 300.0)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


def test_hold_normalizes_a_tz_aware_stamp():
    snap = {"operator_pause": {
        "active": True, "resume_state": "watching_live",
        "paused_at_utc": (_NOW - timedelta(seconds=10)).replace(tzinfo=timezone.utc).isoformat(),
    }}
    assert AA._reap_deferred_hold_remaining("watching_live", snap, _NOW, 300.0) == pytest.approx(290.0)


def test_hold_reads_the_same_key_the_pause_writer_uses():
    assert AA._OPERATOR_PAUSE_KEY == SL.OPERATOR_PAUSE_KEY
    paused = SL.apply_operator_pause({}, state="watching_live")
    assert AA._reap_deferred_hold_remaining("watching_live", paused, datetime.utcnow(), 300.0) > 0


def test_retry_cadence_floor_and_default(monkeypatch):
    monkeypatch.setattr(AA.settings, "chili_momentum_reap_deferred_retry_seconds", 5, raising=False)
    assert AA._reap_deferred_retry_seconds() == 30.0
    monkeypatch.setattr(AA.settings, "chili_momentum_reap_deferred_retry_seconds", "x", raising=False)
    assert AA._reap_deferred_retry_seconds() == 300.0
    monkeypatch.setattr(AA.settings, "chili_momentum_reap_deferred_retry_seconds", None, raising=False)
    assert AA._reap_deferred_retry_seconds() == 300.0
    assert Settings(_env_file=None).chili_momentum_reap_deferred_retry_seconds == 300


# ── T7: the reaper loop holds a deferred row, then retries on the cadence ───


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
        return self._db.locked_row

    def all(self):
        return list(self._db.rows)


class _LockingDb:
    def __init__(self, rows, *, locked_row=None):
        self.rows = rows
        self.locked_row = locked_row if locked_row is not None else (rows[0] if rows else None)
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


def _row(sid=19463, *, state="watching_live", le=None, pause=None, symbol="JLHL"):
    snap = {"momentum_live_execution": dict(le or {})}
    if pause is not None:
        snap["operator_pause"] = pause
    return SimpleNamespace(
        id=sid, symbol=symbol, state=state,
        started_at=datetime(2026, 9, 2, 10, 47, 2),
        risk_snapshot_json=snap,
    )


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(AA, "_max_watch_seconds", lambda: 300)
    monkeypatch.setattr(AA, "_watch_extend_seconds", lambda: 600)
    monkeypatch.setattr(AA, "_symbol_of_day_focus_enabled", lambda: False)
    monkeypatch.setattr(AA, "_event_based_abandonment_enabled", lambda: False)
    monkeypatch.setattr(AA.settings, "chili_momentum_adaptive_watch_enabled", False, raising=False)
    monkeypatch.setattr(AA.settings, "chili_momentum_reap_deferred_retry_seconds", 300, raising=False)
    monkeypatch.setattr(AA, "_REAP_HOLD_LOGGED", {})
    cooldowns: list[str] = []
    monkeypatch.setattr(AA, "_write_reap_cooldown", lambda sym, now: cooldowns.append(sym))
    return cooldowns


_DEFERRED = {
    "ok": True, "pending": "broker_flat_confirmation", "session_id": 19463,
    "state": "watching_live",
    "cancel_reconcile": {
        "ok": True, "action": "broker_flat_unconfirmed", "pending": "broker_flat_confirmation",
        "terminalization_deferred": True,
        "broker_truth": {"reason": "broker_position_unknown", "cause": "adapter_unavailable"},
    },
}


def _patch_cancel(monkeypatch, result, calls):
    def _cancel(db, *, user_id, session_id):
        calls.append(session_id)
        return result
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.automation_query.cancel_automation_session",
        _cancel,
    )


def test_19463_pass_replay_holds_the_paused_row_and_logs_once(quiet, monkeypatch, caplog):
    """13:43:17Z: the row is paused (resume_state=watching_live) 10s ago. The
    next pass must NOT probe the lock nor re-run the cancel; one HELD line."""
    calls: list[int] = []
    _patch_cancel(monkeypatch, _DEFERRED, calls)
    now = datetime(2026, 9, 2, 13, 43, 27)
    row = _row(pause={"active": True, "resume_state": "watching_live",
                      "paused_at_utc": "2026-09-02T13:43:17.031385"})
    db = _LockingDb([row])
    with caplog.at_level(logging.WARNING, logger=AA.logger.name):
        assert AA._reap_stale_watching_sessions(db, user_id=1, now=now) == 0
        assert AA._reap_stale_watching_sessions(db, user_id=1, now=now + timedelta(seconds=10)) == 0
        assert AA._reap_stale_watching_sessions(db, user_id=1, now=now + timedelta(seconds=20)) == 0
    assert calls == [], "walang cancel habang HELD"
    assert db.nested_commits == 0 and db.nested_rollbacks == 0, "walang lock probe"
    assert db.commits == 0
    assert quiet == []
    held = [r.getMessage() for r in caplog.records if "reap HELD" in r.getMessage()]
    assert len(held) == 1, held
    assert "session=19463" in held[0] and "resume_state=watching_live" in held[0]
    assert not any("reap DEFERRED" in r.getMessage() for r in caplog.records)


def test_held_row_is_retried_once_the_cadence_elapses(quiet, monkeypatch, caplog):
    calls: list[int] = []
    _patch_cancel(monkeypatch, _DEFERRED, calls)
    now = datetime(2026, 9, 2, 13, 43, 27)
    row = _row(pause={"active": True, "resume_state": "watching_live",
                      "paused_at_utc": "2026-09-02T13:43:17.031385"})
    db = _LockingDb([row])
    with caplog.at_level(logging.WARNING, logger=AA.logger.name):
        assert AA._reap_stale_watching_sessions(db, user_id=1, now=now) == 0
        assert calls == []
        # 300s after the stamp: the hold expires, the cancel is re-asked (DEFERRED again,
        # committed, not counted, no cooldown) and the DEFERRED line names the cause.
        assert AA._reap_stale_watching_sessions(
            db, user_id=1, now=datetime(2026, 9, 2, 13, 48, 18)
        ) == 0
    assert calls == [19463]
    assert db.nested_commits == 1
    assert db.commits == 1
    assert quiet == []
    deferred = [r.getMessage() for r in caplog.records if "reap DEFERRED" in r.getMessage()]
    assert len(deferred) == 1 and "adapter_unavailable" in deferred[0], deferred


def test_cadence_is_bounded_by_the_setting(quiet, monkeypatch):
    monkeypatch.setattr(AA.settings, "chili_momentum_reap_deferred_retry_seconds", 60, raising=False)
    calls: list[int] = []
    _patch_cancel(monkeypatch, _DEFERRED, calls)
    now = datetime(2026, 9, 2, 13, 43, 27)
    row = _row(pause={"active": True, "resume_state": "watching_live",
                      "paused_at_utc": "2026-09-02T13:43:17.031385"})
    db = _LockingDb([row])
    assert AA._reap_stale_watching_sessions(db, user_id=1, now=now + timedelta(seconds=40)) == 0
    assert calls == []
    assert AA._reap_stale_watching_sessions(db, user_id=1, now=now + timedelta(seconds=55)) == 0
    assert calls == [19463]


@pytest.mark.parametrize("pause", [
    None,
    {"active": False, "resume_state": "watching_live", "paused_at_utc": "2026-09-02T13:43:17"},
    {"active": True, "resume_state": "live_exited", "paused_at_utc": "2026-09-02T13:43:17"},
    {"active": True, "resume_state": "watching_live", "paused_at_utc": "garbage"},
])
def test_only_a_fresh_same_state_pause_holds(quiet, monkeypatch, pause):
    """No pause / inactive / moved / unreadable stamp => the cancel runs (fail-closed
    direction is to ASK; the cancel itself never terminalizes without proof)."""
    calls: list[int] = []
    _patch_cancel(monkeypatch, _DEFERRED, calls)
    db = _LockingDb([_row(pause=pause)])
    assert AA._reap_stale_watching_sessions(db, user_id=1, now=datetime(2026, 9, 2, 13, 43, 27)) == 0
    assert calls == [19463]
    assert db.commits == 1


def test_terminal_cancel_after_the_hold_is_counted_and_cooled(quiet, monkeypatch):
    """The fix in the venue factory makes the retried cancel TERMINAL."""
    calls: list[int] = []
    _patch_cancel(monkeypatch, {"ok": True, "session_id": 19463, "state": "live_cancelled"}, calls)
    row = _row(pause={"active": True, "resume_state": "watching_live",
                      "paused_at_utc": "2026-09-02T13:43:17.031385"})
    db = _LockingDb([row])
    assert AA._reap_stale_watching_sessions(db, user_id=1, now=datetime(2026, 9, 2, 13, 43, 27)) == 0
    assert AA._reap_stale_watching_sessions(db, user_id=1, now=datetime(2026, 9, 2, 13, 48, 30)) == 1
    assert calls == [19463]
    assert quiet == ["JLHL"]


def test_hold_log_is_once_per_session_not_per_symbol(quiet, monkeypatch, caplog):
    calls: list[int] = []
    _patch_cancel(monkeypatch, _DEFERRED, calls)
    pause = {"active": True, "resume_state": "watching_live", "paused_at_utc": "2026-09-02T13:43:17"}
    rows = [_row(1, symbol="AAA", pause=pause), _row(2, symbol="BBB", pause=pause)]
    db = _LockingDb(rows)
    with caplog.at_level(logging.WARNING, logger=AA.logger.name):
        AA._reap_stale_watching_sessions(db, user_id=1, now=datetime(2026, 9, 2, 13, 43, 27))
        AA._reap_stale_watching_sessions(db, user_id=1, now=datetime(2026, 9, 2, 13, 43, 37))
    held = [r.getMessage() for r in caplog.records if "reap HELD" in r.getMessage()]
    assert len(held) == 2 and calls == []


def test_note_reap_hold_is_bounded():
    now = datetime(2026, 9, 2, 13, 43, 27)
    AA._REAP_HOLD_LOGGED.clear()
    try:
        for i in range(1, 502):
            assert AA._note_reap_hold(i, now - timedelta(days=2)) is True
        assert AA._note_reap_hold(1, now) is False
        assert AA._note_reap_hold(9999, now) is True  # triggers the prune
        assert len(AA._REAP_HOLD_LOGGED) == 1
    finally:
        AA._REAP_HOLD_LOGGED.clear()


# ── T8: AST guards ──────────────────────────────────────────────────────────


def _fn_src(mod, name: str) -> str:
    tree = ast.parse(pathlib.Path(mod.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.unparse(fn)


def test_hold_precedes_the_lock_probe_and_the_cancel():
    src = _fn_src(AA, "_reap_stale_watching_sessions")
    i_hold = src.index("_reap_deferred_hold_remaining(")
    assert i_hold < src.index("_reap_age_anchor(s")
    assert i_hold < src.index("begin_nested")
    assert i_hold < src.index("with_for_update(nowait=True)")
    assert i_hold < src.index("cancel_automation_session(")
    assert "_note_reap_hold(" in src
    assert "clear_operator_pause" not in src


def test_every_unknown_broker_truth_return_names_a_cause():
    tree = ast.parse(pathlib.Path(aq.__file__).read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_reaper_broker_position_truth"
    )
    unknown_returns = 0
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple)):
            continue
        detail = node.value.elts[1]
        if not isinstance(detail, ast.Dict):
            continue
        keys = {k.value: v for k, v in zip(detail.keys, detail.values) if isinstance(k, ast.Constant)}
        reason = keys.get("reason")
        if isinstance(reason, ast.Constant) and reason.value == "broker_position_unknown":
            unknown_returns += 1
            assert "cause" in keys, ast.unparse(node)
    # coinbase (2) + robinhood_spot (2) + generic adapter branch (4)
    assert unknown_returns == 8, unknown_returns


def test_reaper_truth_still_resolves_through_the_factory():
    """The seam existing tests patch (venue_factory.get_adapter) is unchanged."""
    src = _fn_src(aq, "_reaper_broker_position_truth")
    assert "get_adapter(sess.execution_family)" in src
    assert "_bind_persisted_alpaca_adapter(sess, adapter)" in src
    assert src.index("get_adapter(") < src.index("_bind_persisted_alpaca_adapter(")


def test_run_auto_arm_pass_call_site_unchanged():
    src = inspect.getsource(AA.run_auto_arm_pass)
    assert "reaped = _reap_stale_watching_sessions(db, user_id=uid, now=pass_as_of)" in src

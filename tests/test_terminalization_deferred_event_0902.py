"""Item B ng 09-02: ang DEFERRED na terminalization ay TAHIMIK.

ANG INSIDENTE (CANF 19471, Alpaca paper, 2026-09-02). 11:19:10.224Z
`live_entry_submitted`, order f3ed508d, place_rtt 0.109s. Sa PAREHONG segundo
tinawag ng auto-arm stale reaper ang `cancel_automation_session`. Hindi ito
makapag-terminalize dahil hawak ng session ang isang hindi-nalulutas na durable
Alpaca entry claim (arm-257cc0f5) na may dalang order id na iyon, kaya kumuha
ito ng DEFERRED na sanga -- at ang sangang iyon ay nagsusulat lang ng
`operator_pause` at WALANG event. Resulta: 12m49s na WALANG KAHIT ANONG ROW sa
`trading_automation_events`, habang may na-fill na 165-share na posisyon na
walang software stop at walang deadman stop.

SUKAT NG SAKLAW (live DB, 21 araw hanggang 2026-09-02 23:32Z):
  * 1,654 live session ang may `operator_pause`; 34 ang aktibo.
  * 31 sa 34 (91.2%) ang WALANG event sa loob ng +/-3s ng sarili nilang
    `paused_at_utc` na magpapaliwanag sa pause.
  * 5 sa 34 ang na-pause habang may hawak na `entry_order_id` AT
    `entry_submitted=true`: CANF 19471, INBS 19398, GYGY 19338, AEHL 19192,
    BRNX 17370 -- 5 sa 38 live entry submission sa 21 araw (13.2%).
  * `trading_automation_events` ALL TIME: 0 row na may pangalang
    reap-deferred / terminalization-deferred. Walang ganitong event kailanman.

DALAWANG DEPEKTO, ISANG COMMIT:
  B1  Walang durable event, at ang tanging bakas (`operator_pause.paused_at_utc`)
      ay isinusulat ULIT kada deferral -- kaya nawawala ang oras ng UNANG
      deferral. Ang naka-imbak na stamp ng CANF 19471 ngayon ay
      2026-09-02T13:21:06, dalawang oras at isang operator flatten matapos ang
      pause na pumatay sa tick.
  B2  Ang `_guarded_reap_for_displacement` ay sumusuri lang ng `res["ok"]`. Ang
      DEFERRED na cancel ay nagbabalik ng ok=True habang buhay pa ang session,
      kaya nagsusulat ito ng reap cooldown, nagko-commit, at nag-uulat ng
      displacement para sa row na hindi nito na-terminalize.

Runnable: pytest tests/test_terminalization_deferred_event_0902.py -v -p no:randomly
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import auto_arm as AA
from app.services.trading.momentum_neural import automation_query as AQ
from app.services.trading.momentum_neural import session_lifecycle as SL


# ── fakes ────────────────────────────────────────────────────────────────────


class _Sess:
    def __init__(self, *, sid=19471, symbol="CANF", state="watching_live", snap=None):
        self.id = sid
        self.symbol = symbol
        self.state = state
        self.execution_family = "alpaca_spot"
        self.correlation_id = f"corr-{sid}"
        self.risk_snapshot_json = snap if snap is not None else {}
        self.updated_at = None


class _EventDb:
    """Captures append_trading_automation_event calls; no real DB."""

    def __init__(self):
        self.events: list[tuple[int, str, dict]] = []

    def flush(self):
        pass

    def commit(self):
        pass

    def rollback(self):
        pass


@pytest.fixture
def captured(monkeypatch):
    db = _EventDb()

    def _append(_db, session_id, event_type, payload, **_kw):
        db.events.append((int(session_id), str(event_type), dict(payload)))
        return SimpleNamespace(id=len(db.events))

    monkeypatch.setattr(AQ, "append_trading_automation_event", _append)
    return db


# ── B1a: the pause carries first-deferral provenance across restamps ─────────


def test_apply_operator_pause_still_restamps_paused_at_utc():
    """LOAD-BEARING: auto_arm._reap_deferred_hold_remaining reads paused_at_utc as
    the 300s re-ask clock. Freezing it would collapse the hold to ask-once."""
    first = SL.apply_operator_pause({}, state="watching_live",
                                    deferral={"pending": "p", "initiator": "i"})
    second = SL.apply_operator_pause(first, state="watching_live",
                                     deferral={"pending": "p", "initiator": "i"})
    a = first[SL.OPERATOR_PAUSE_KEY]["paused_at_utc"]
    b = second[SL.OPERATOR_PAUSE_KEY]["paused_at_utc"]
    assert b >= a
    # ...and the hold still reads it.
    now = datetime.utcnow()
    held = AA._reap_deferred_hold_remaining("watching_live", second, now, 300.0)
    assert held is not None and held > 0


def test_first_deferral_time_survives_the_restamp():
    """Ang eksaktong bagay na nawala sa CANF 19471."""
    snap = SL.apply_operator_pause(
        {}, state="watching_live",
        deferral={"pending": "durable_alpaca_entry_claim_reconcile",
                  "initiator": "automation_monitor"},
    )
    first_at = snap[SL.OPERATOR_PAUSE_KEY]["deferral_first_at_utc"]
    assert snap[SL.OPERATOR_PAUSE_KEY]["deferral_count"] == 1
    for expected in (2, 3, 4):
        snap = SL.apply_operator_pause(
            snap, state="watching_live",
            deferral={"pending": "durable_alpaca_entry_claim_reconcile",
                      "initiator": "automation_monitor"},
        )
        assert snap[SL.OPERATOR_PAUSE_KEY]["deferral_count"] == expected
        assert snap[SL.OPERATOR_PAUSE_KEY]["deferral_first_at_utc"] == first_at


_BASE_DEFERRAL = {"pending": "durable_alpaca_entry_claim_reconcile",
                  "initiator": "automation_monitor",
                  "order_key": "f3ed508d-e441-47b3-b76b-2449b6f0a133"}


@pytest.mark.parametrize("changed", [
    {"state": "queued_live"},
    {"deferral": {**_BASE_DEFERRAL, "order_key": "552efe43-0000-0000-0000-0"}},
])
def test_a_new_run_restarts_the_count(changed):
    base = dict(state="watching_live", deferral=dict(_BASE_DEFERRAL))
    snap = SL.apply_operator_pause({}, **base)
    snap = SL.apply_operator_pause(snap, **base)
    assert snap[SL.OPERATOR_PAUSE_KEY]["deferral_count"] == 2
    snap = SL.apply_operator_pause(snap, **{**base, **changed})
    assert snap[SL.OPERATOR_PAUSE_KEY]["deferral_count"] == 1


def test_a_changing_pending_reason_is_the_SAME_run(captured):
    """Ang run ay kinikilala ng ORDER, hindi ng dahilan.

    Anim na `pending` na halaga ang naaabot para sa IISANG nakabinbing order
    (`broker_flat_confirmation`, `broker_terminal_truth_reconcile`,
    `entry_order_truth_reconcile`, `filled_entry_management`,
    `execution_quarantine`, `durable_alpaca_entry_claim_reconcile`). Ang pag-key
    sa dahilan ay nagre-reset ng `deferral_count` sa 1 sa bawat pagpapalit --
    at ang rate limit sa `_pause_operator_terminalization` ay may kondisyong
    `_count > 1`, kaya HINDI ITO KAILANMAN PUPUTOK. Isang event lang ang dapat
    sa loob ng window kahit magpalit-palit ang dahilan."""
    sess = _Sess(snap={"momentum_live_execution": {
        "entry_order_id": "f3ed508d-e441-47b3-b76b-2449b6f0a133",
    }})
    for pending in (
        "durable_alpaca_entry_claim_reconcile",
        "broker_flat_confirmation",
        "durable_alpaca_entry_claim_reconcile",
        "broker_terminal_truth_reconcile",
        "filled_entry_management",
        "execution_quarantine",
    ):
        AQ._pause_operator_terminalization(
            sess, db=captured, pending=pending, initiator="automation_monitor",
        )
    pause = sess.risk_snapshot_json[SL.OPERATOR_PAUSE_KEY]
    assert pause["deferral_count"] == 6, "isang run, anim na re-ask"
    assert pause["deferral_order_key"] == "f3ed508d-e441-47b3-b76b-2449b6f0a133"
    assert len(captured.events) == 1, [e[1] for e in captured.events]


def test_a_new_broker_order_does_start_a_new_run(captured):
    sess = _Sess(snap={"momentum_live_execution": {"entry_order_id": "order-A"}})
    AQ._pause_operator_terminalization(
        sess, db=captured, pending="broker_flat_confirmation", initiator="operator",
    )
    sess.risk_snapshot_json["momentum_live_execution"]["entry_order_id"] = "order-B"
    AQ._pause_operator_terminalization(
        sess, db=captured, pending="broker_flat_confirmation", initiator="operator",
    )
    assert sess.risk_snapshot_json[SL.OPERATOR_PAUSE_KEY]["deferral_count"] == 1
    assert len(captured.events) == 2


def test_pause_without_deferral_carries_no_provenance():
    """Ang lumang tawag (walang db) ay hindi nagbabago ng hugis."""
    snap = SL.apply_operator_pause({}, state="watching_live")
    pause = snap[SL.OPERATOR_PAUSE_KEY]
    assert set(pause) == {"active", "paused_at_utc", "resume_state"}


# ── B1b: the deferral writes a durable event ─────────────────────────────────


def test_deferred_pause_emits_one_durable_event_with_the_broker_order_id(captured):
    sess = _Sess(snap={"momentum_live_execution": {
        "entry_submitted": True,
        "entry_order_id": "f3ed508d-e441-47b3-b76b-2449b6f0a133",
    }})
    AQ._pause_operator_terminalization(
        sess,
        db=captured,
        pending="durable_alpaca_entry_claim_reconcile",
        initiator="automation_monitor",
        claim={"claim_token": "arm-257cc0f5",
               "broker_order_id": "f3ed508d-e441-47b3-b76b-2449b6f0a133"},
    )
    assert len(captured.events) == 1, "walang event = ang CANF 19471 na katahimikan"
    sid, etype, payload = captured.events[0]
    assert sid == 19471
    assert etype == "terminalization_deferred_operator_pause"
    assert payload["broker_order_id"] == "f3ed508d-e441-47b3-b76b-2449b6f0a133"
    assert payload["initiator"] == "automation_monitor"
    assert payload["pending"] == "durable_alpaca_entry_claim_reconcile"
    assert payload["previous_state"] == "watching_live"
    assert payload["claim_token"] == "arm-257cc0f5"
    assert payload["entry_submitted"] is True
    assert payload["deferral_count"] == 1
    assert payload["deferral_first_at_utc"]
    # the fence itself is still written
    assert SL.is_operator_paused(sess.risk_snapshot_json) is True


def test_the_log_line_says_it_was_not_terminalized(captured, caplog):
    sess = _Sess(snap={"momentum_live_execution": {"entry_order_id": "f3ed508d"}})
    with caplog.at_level("WARNING", logger=AQ.logger.name):
        AQ._pause_operator_terminalization(
            sess, db=captured, pending="broker_flat_confirmation", initiator="operator",
        )
    msgs = [r.getMessage() for r in caplog.records]
    assert any("terminalization DEFERRED" in m and "NOT terminalized" in m
               for m in msgs), msgs


def test_initiator_distinguishes_a_human_from_the_reaper(captured):
    for who in ("operator", "automation_monitor"):
        sess = _Sess(snap={})
        AQ._pause_operator_terminalization(
            sess, db=captured, pending="broker_flat_confirmation", initiator=who,
        )
    assert [e[2]["initiator"] for e in captured.events] == [
        "operator", "automation_monitor",
    ]


def test_a_re_asked_deferral_is_rate_limited_not_a_second_storm(captured, monkeypatch):
    """Ang deferral ay muling itatanong kada 300s; ang event ay sumusunod sa
    parehong kadensya, hindi kada pass (ang aral ng 3,373-event na bagyo)."""
    monkeypatch.setattr(AQ, "_deferral_event_min_interval_seconds", lambda: 300.0)
    sess = _Sess(snap={})
    for _ in range(25):
        AQ._pause_operator_terminalization(
            sess, db=captured,
            pending="durable_alpaca_entry_claim_reconcile",
            initiator="automation_monitor",
        )
    assert len(captured.events) == 1, "isang event lang sa loob ng cadence window"
    # ...and the pause counted every one of them
    assert sess.risk_snapshot_json[SL.OPERATOR_PAUSE_KEY]["deferral_count"] == 25
    # once the cadence has elapsed, it speaks again
    pause = sess.risk_snapshot_json[SL.OPERATOR_PAUSE_KEY]
    pause["deferral_event_at_utc"] = (
        datetime.utcnow() - timedelta(seconds=601)
    ).isoformat()
    AQ._pause_operator_terminalization(
        sess, db=captured,
        pending="durable_alpaca_entry_claim_reconcile",
        initiator="automation_monitor",
    )
    assert len(captured.events) == 2
    assert captured.events[1][2]["deferral_count"] == 26


def test_no_db_means_no_event_and_no_provenance(captured):
    """Fail-safe: ang mga tumatawag na walang db ay hindi nagbabago ng ugali."""
    sess = _Sess(snap={})
    AQ._pause_operator_terminalization(sess)
    assert captured.events == []
    assert "deferral_count" not in sess.risk_snapshot_json[SL.OPERATOR_PAUSE_KEY]


def test_event_write_failure_still_writes_the_pause_and_the_log(monkeypatch, caplog):
    def _boom(*_a, **_k):
        raise RuntimeError("events table gone")

    monkeypatch.setattr(AQ, "append_trading_automation_event", _boom)
    sess = _Sess(snap={})
    with caplog.at_level("WARNING", logger=AQ.logger.name):
        AQ._pause_operator_terminalization(
            sess, db=_EventDb(), pending="p", initiator="operator",
        )
    assert SL.is_operator_paused(sess.risk_snapshot_json) is True
    msgs = [r.getMessage() for r in caplog.records]
    assert any("terminalization DEFERRED" in m for m in msgs), msgs


# ── B2: a deferred cancel is not a displacement ──────────────────────────────


class _Savepoint:
    def __init__(self, db):
        self._db = db
        self.is_active = True

    def commit(self):
        self.is_active = False
        self._db.savepoint_commits += 1

    def rollback(self):
        self.is_active = False
        self._db.savepoint_rollbacks += 1


class _DisplaceQuery:
    """Models SQLAlchemy's identity map, which is the whole point of item A.

    Without ``populate_existing()`` a re-query returns the instance the caller
    already loaded, with its attributes UNREFRESHED — so ``one_or_none()`` hands
    back ``stale``.  With it, the row is re-read and ``fresh`` is returned.  This
    is not a convenience: measured against real Postgres + SQLAlchemy 2.0.47, the
    same ``FOR UPDATE NOWAIT`` re-query answers ``entry_submitted=None`` without
    the call and ``entry_submitted=True`` with it, while a concurrent transaction
    commits the submit mid-pass.
    """

    def __init__(self, db):
        self._db = db

    def filter(self, *_a, **_k):
        return self

    def with_for_update(self, *, nowait=False, **_k):
        self._db.nowait_seen = bool(nowait)
        return self

    def populate_existing(self):
        self._db.populate_existing_seen = True
        return self

    def one_or_none(self):
        if self._db.populate_existing_seen:
            return self._db.fresh if self._db.fresh is not None else self._db.locked
        return self._db.locked


class _DisplaceDb:
    def __init__(self, locked, fresh=None, *, savepoints=True):
        self.locked = locked
        self.fresh = fresh
        self.commits = 0
        self.rollbacks = 0
        self.savepoint_commits = 0
        self.savepoint_rollbacks = 0
        self.nowait_seen = None
        self.populate_existing_seen = False
        self._savepoints = savepoints

    def query(self, *_a, **_k):
        return _DisplaceQuery(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def __getattr__(self, name):
        # `begin_nested` exists only when the double opts in, so the no-savepoint
        # degradation path stays exercised too.
        if name == "begin_nested" and self.__dict__.get("_savepoints"):
            return lambda: _Savepoint(self)
        raise AttributeError(name)


def _victim(state="queued_live", le=None, execution_family="robinhood_spot"):
    """The REACHABLE displacement victim shape.

    ``_maybe_rank_displace`` filters ``execution_family != "alpaca_spot"``
    (auto_arm.py, "never reap a paper twin"), so an alpaca_spot victim cannot
    reach this function in production at all.  The live DB agrees: 0 sessions
    all-time match ``state in ('armed_pending_runner','queued_live') AND
    execution_family <> 'alpaca_spot'``, and every one of the 4,081 live sessions
    in the 21 days to 2026-09-02 is alpaca_spot.  Default to the family the
    displaceable population actually has, and let the alpaca-only tests say so
    explicitly.
    """
    return SimpleNamespace(
        id=19471, symbol="CANF", state=state,
        execution_family=execution_family, mode="live",
        started_at=datetime.utcnow() - timedelta(seconds=900),
        risk_snapshot_json={"momentum_live_execution": dict(le or {})},
    )


@pytest.fixture
def displace(monkeypatch):
    cooldowns: list[str] = []
    monkeypatch.setattr(AA, "_write_reap_cooldown", lambda sym, now: cooldowns.append(sym))
    # claim ledger readable and clear unless a test says otherwise
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.automation_query."
        "_owned_unresolved_alpaca_entry_claim",
        lambda db, sess: (True, None),
    )
    return cooldowns


def _patch_cancel(monkeypatch, result):
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.automation_query.cancel_automation_session",
        lambda db, *, user_id, session_id: result,
    )


@pytest.mark.parametrize("result", [
    # the CANF branch: ok=True, pending, NO terminalization_deferred key
    {"ok": True, "pending": "durable_alpaca_entry_claim_reconcile",
     "state": "watching_live"},
    {"ok": True, "pending": "broker_flat_confirmation",
     "terminalization_deferred": True, "state": "watching_live"},
    # ok=True but the row never reached a terminal state
    {"ok": True, "state": "watching_live"},
])
def test_deferred_cancel_is_not_a_displacement(displace, monkeypatch, result, caplog):
    _patch_cancel(monkeypatch, result)
    db = _DisplaceDb(_victim())
    with caplog.at_level("WARNING", logger=AA.logger.name):
        got = AA._guarded_reap_for_displacement(
            db, user_id=1, session_id=19471, expected_symbol="CANF",
        )
    assert got is False, "ang deferral ay iniulat bilang na-reap na slot"
    assert displace == [], "walang cooldown para sa row na hindi na-terminalize"
    assert db.commits == 1, "ang pause/pointer/event writes ay dapat mag-persist"
    msgs = [r.getMessage() for r in caplog.records]
    assert any("displacement reap DEFERRED" in m and "HINDI" in m for m in msgs), msgs


@pytest.mark.parametrize("result", [
    {"ok": True, "state": "live_cancelled"},
    {"ok": True},
])
def test_terminal_cancel_is_still_a_displacement(displace, monkeypatch, result):
    _patch_cancel(monkeypatch, result)
    db = _DisplaceDb(_victim())
    assert AA._guarded_reap_for_displacement(
        db, user_id=1, session_id=19471, expected_symbol="CANF",
    ) is True
    assert displace == ["CANF"]
    assert db.commits == 1


def test_failed_cancel_rolls_back_and_writes_no_cooldown(displace, monkeypatch):
    _patch_cancel(monkeypatch, {"ok": False, "error": "not_cancellable"})
    db = _DisplaceDb(_victim())
    assert AA._guarded_reap_for_displacement(
        db, user_id=1, session_id=19471, expected_symbol="CANF",
    ) is False
    assert displace == []
    assert db.commits == 0
    assert db.rollbacks == 1


def test_unresolved_durable_claim_blocks_the_displacement_before_the_cancel(
    displace, monkeypatch,
):
    """Ang crash-survivor: walang laman ang session JSON, pero may hawak na
    hindi-nalulutas na claim ang ledger.

    REACHABILITY, STATED HONESTLY. `_owned_unresolved_alpaca_entry_claim`
    (automation_query.py) returns ``(True, None)`` immediately for any family
    outside {alpaca_spot, alpaca_short}, and `_maybe_rank_displace` filters
    ``execution_family != 'alpaca_spot'`` — and no alpaca_short session has ever
    existed (0 rows, all-time). So this guard is UNREACHABLE on the displacement
    path as the caller stands today; it is correctness for the day that filter is
    lifted, and it is not what makes the deferral discriminator work. The victim
    below is therefore built alpaca_spot ON PURPOSE, which is a shape the real
    caller excludes."""
    called: list[int] = []

    def _cancel(db, *, user_id, session_id):
        called.append(session_id)
        return {"ok": True, "state": "live_cancelled"}

    monkeypatch.setattr(
        "app.services.trading.momentum_neural.automation_query.cancel_automation_session",
        _cancel,
    )
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.automation_query."
        "_owned_unresolved_alpaca_entry_claim",
        lambda db, sess: (True, {"claim_token": "arm-257cc0f5",
                                 "broker_order_id": "f3ed508d"}),
    )
    db = _DisplaceDb(_victim(execution_family="alpaca_spot"))
    assert AA._guarded_reap_for_displacement(
        db, user_id=1, session_id=19471, expected_symbol="CANF",
    ) is False
    assert called == [], "hindi dapat naabot ang cancel"
    assert displace == []


def test_unreadable_claim_view_fails_closed(displace, monkeypatch):
    called: list[int] = []
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.automation_query.cancel_automation_session",
        lambda db, *, user_id, session_id: called.append(session_id) or {"ok": True},
    )
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.automation_query."
        "_owned_unresolved_alpaca_entry_claim",
        lambda db, sess: (False, None),
    )
    db = _DisplaceDb(_victim(execution_family="alpaca_spot"))
    assert AA._guarded_reap_for_displacement(
        db, user_id=1, session_id=19471, expected_symbol="CANF",
    ) is False
    assert called == []


# ── A: the re-read under the lock must actually RE-READ ──────────────────────


def test_the_locked_read_sees_the_submit_the_pass_top_snapshot_missed(
    displace, monkeypatch,
):
    """THE TOCTOU. `_maybe_rank_displace` loads every victim with
    ``victims = q.all()`` at the top of the pass; `app/db.py` builds the
    sessionmaker with ``autoflush=False, autocommit=False``, so nothing expires
    those instances. Without ``populate_existing()`` the FOR UPDATE re-query
    returns the identity-mapped object with PASS-TOP attributes — the exact
    snapshot the lock exists to invalidate — so a runner tick that committed
    ``entry_submitted=True`` mid-pass is invisible and the row is cancelled with a
    live broker order outstanding.

    Exactly the FIRST victim of each pass is judged this way (later calls are
    preceded by this function's own commit/rollback, which expires the map), and
    the first victim is the worst-ranked one — the one that actually gets reaped.
    """
    called: list[int] = []
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.automation_query.cancel_automation_session",
        lambda db, *, user_id, session_id: (
            called.append(session_id) or {"ok": True, "state": "live_cancelled"}
        ),
    )
    stale = _victim()                                   # what the pass read
    fresh = _victim(le={                                # what the row says NOW
        "entry_submitted": True,
        "entry_order_id": "f3ed508d-e441-47b3-b76b-2449b6f0a133",
    })
    db = _DisplaceDb(stale, fresh)

    got = AA._guarded_reap_for_displacement(
        db, user_id=1, session_id=19471, expected_symbol="CANF",
    )

    assert db.populate_existing_seen is True, "ang lock ay hindi nagbasa muli"
    assert got is False, "isang buhay na entry order ang na-cancel"
    assert called == [], "hindi dapat naabot ang cancel"
    assert displace == [], "walang cooldown para sa row na may order"


def test_a_state_advance_between_the_pass_and_the_lock_aborts_the_reap(
    displace, monkeypatch,
):
    called: list[int] = []
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.automation_query.cancel_automation_session",
        lambda db, *, user_id, session_id: (
            called.append(session_id) or {"ok": True, "state": "live_cancelled"}
        ),
    )
    db = _DisplaceDb(_victim(), _victim(state="watching_live"))
    assert AA._guarded_reap_for_displacement(
        db, user_id=1, session_id=19471, expected_symbol="CANF",
    ) is False
    assert called == []


def test_the_locking_query_carries_populate_existing_in_source():
    """AST, not regex (the house rule): assert the CALL CHAIN on the locking
    query, so a refactor cannot quietly drop the re-read while the fake keeps
    passing."""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(AA._guarded_reap_for_displacement)
    ))
    # Only MAXIMAL chains: a Call that is itself the receiver of another Call is
    # an inner link, not a statement.
    inner = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Call):
                inner.add(id(node.func.value))
    chains = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or id(node) in inner:
            continue
        names, cur = [], node
        while isinstance(cur, ast.Call) and isinstance(cur.func, ast.Attribute):
            names.append(cur.func.attr)
            cur = cur.func.value
        if "with_for_update" in names:
            chains.append(names)
    assert chains, "walang with_for_update sa function"
    assert all("populate_existing" in c for c in chains), chains


def test_the_savepoint_is_released_before_the_cancel_and_rolled_back_on_abort(
    displace, monkeypatch,
):
    """The savepoint keeps the row lock into the cancel AND keeps an abort from
    rolling back the caller's transaction. Mirrors _reap_stale_watching_sessions."""
    _patch_cancel(monkeypatch, {"ok": True, "state": "live_cancelled"})
    db = _DisplaceDb(_victim())
    assert AA._guarded_reap_for_displacement(
        db, user_id=1, session_id=19471, expected_symbol="CANF",
    ) is True
    assert db.savepoint_commits == 1 and db.savepoint_rollbacks == 0

    # ...and an abort touches only the savepoint.
    db2 = _DisplaceDb(_victim(), _victim(state="watching_live"))
    assert AA._guarded_reap_for_displacement(
        db2, user_id=1, session_id=19471, expected_symbol="CANF",
    ) is False
    assert db2.savepoint_rollbacks == 1
    assert db2.rollbacks == 0, "ang abort ay hindi dapat bumawi sa txn ng caller"


def test_without_begin_nested_the_reread_still_happens(displace, monkeypatch):
    """Degradation is to the PRE-savepoint behaviour (whole-txn rollback on
    abort), never to a lock that decides on stale data."""
    called: list[int] = []
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.automation_query.cancel_automation_session",
        lambda db, *, user_id, session_id: (
            called.append(session_id) or {"ok": True, "state": "live_cancelled"}
        ),
    )
    db = _DisplaceDb(
        _victim(),
        _victim(le={"entry_submitted": True, "entry_order_id": "f3ed508d"}),
        savepoints=False,
    )
    assert AA._guarded_reap_for_displacement(
        db, user_id=1, session_id=19471, expected_symbol="CANF",
    ) is False
    assert db.populate_existing_seen is True
    assert called == []
    assert db.rollbacks == 1


# ── B3: the stale-live reaper counts a deferral as a deferral ───────────────


def test_stale_live_reaper_has_a_deferral_counter():
    """`skipped_unknown` conflated a committed deferral with an unknown; the
    deferral is the one that leaves a live broker order behind."""
    import inspect

    src = inspect.getsource(AQ.reap_stale_live_sessions)
    assert "skipped_terminalization_deferred" in src
    i_def = src.index('out["skipped_terminalization_deferred"] += 1')
    i_unk = src.rindex('out["skipped_unknown"] += 1')
    assert i_def < i_unk, "ang deferral ay dapat masuri bago ang unknown fallback"

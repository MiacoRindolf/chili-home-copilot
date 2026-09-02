"""Q1 ng 09-02 CANF 19471: 12m49s na HUBAD dahil ang session ay tumigil sa
pagiging dispatched — hindi dahil mabagal o maling poll, kundi dahil WALANG poll.

ANG INSIDENTE. 11:19:10.224Z `live_entry_submitted` (order f3ed508d, 165 sh).
Sa parehong segundo ay tinawag ng stale-watch reaper ang
`cancel_automation_session`; hindi ito makapag-terminalize (may sariling
unresolved na durable Alpaca entry claim ang session), kaya dumaan ito sa
DEFERRED branch — na nagsusulat ng `operator_pause` at WALANG inilalabas na
event.  Pagkatapos noon ay nahulog ang row sa WALANG kahit isang lane ng
`list_runnable_live_sessions`: paused (kaya hindi "normal"), `watching_live`
(kaya `_paused_session_has_exit_authority` ay agad na False sa unang linya),
at ang durable-claim priority read lang ang natitirang pinto — isang
Alpaca-only na lambat na nagiging WALANG LAMAN pa kapag nabigo ang claim read.
Walang tick ⇒ walang `_heal_unrecognized_entry_fill` ⇒ walang adoption ⇒
WALANG software stop at WALANG deadman stop, 12 minuto at 49 segundo.

ANG AYOS. Ang session na ang snapshot ay nagsasabing may naisumiteng entry
order na HINDI pa na-adopt ay LAGING dinidispatch: paused man o hindi, anumang
venue, mabasa man ang claim table o hindi.  DISPATCH lamang ang ibinibigay —
hindi exit authority — dahil ang heal at ang paused adopt-only branch ay
parehong tumatakbo sa IBABAW ng operator-pause gate.

BUMABAGSAK SA origin/main: wala pang `_session_has_unadopted_entry_order`
doon, at ang paused/watching_live/no-claim na row ay hindi ibinabalik ng
`list_runnable_live_sessions`.

Runnable: pytest tests/test_unadopted_entry_dispatch_0902.py -v
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import live_runner as LR
from app.models.trading import TradingAutomationSession


# ── fakes ────────────────────────────────────────────────────────────────────


class _Q:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def distinct(self):
        return self

    def all(self):
        return list(self._rows)


class _Db:
    """Serves session rows to the session query and claim rows to the claim query."""

    def __init__(self, sessions, claim_owner_ids=(), claim_raises=False):
        self.sessions = list(sessions)
        self.claim_owner_ids = list(claim_owner_ids)
        self.claim_raises = claim_raises
        self.rolled_back = False

    def query(self, entity, *rest):
        if entity is TradingAutomationSession:
            return _Q(self.sessions)
        if self.claim_raises:
            raise RuntimeError("claim table unreadable")
        return _Q([(int(i),) for i in self.claim_owner_ids])

    def rollback(self):
        self.rolled_back = True


def _sess(
    sid=19471,
    state="watching_live",
    *,
    paused=True,
    le=None,
    family="alpaca_spot",
    symbol="CANF",
):
    snap: dict = {"momentum_live_execution": dict(le or {})}
    if paused:
        snap["operator_pause"] = {
            "active": True,
            "paused_at_utc": "2026-09-02T11:19:10.224443",
            "resume_state": "watching_live",
        }
    return SimpleNamespace(
        id=sid,
        symbol=symbol,
        state=state,
        mode="live",
        execution_family=family,
        updated_at=None,
        risk_snapshot_json=snap,
    )


# The exact CANF 19471 execution snapshot at 11:19:10.224Z: submitted, order id
# known, position never written, order not in entry_orders_resolved.
_CANF_LE = {
    "entry_submitted": True,
    "entry_order_id": "f3ed508d-e441-47b3-b76b-2449b6f0a133",
    "entry_client_order_id": "chili_ml_e_19471_a7c3e32c_9738f4d973",
    "position": None,
    "entry_submit_utc": "2026-09-02T11:19:10.224443",
}


_MISSING = object()


def _set_settings(values: dict):
    """Save/restore settings tolerantly.

    ``monkeypatch.setattr(..., raising=False)`` deletes the attribute on
    teardown, which turns "the field does not exist yet" into a setup/teardown
    ERROR instead of the behavioural assertion these tests exist to make."""
    saved = {k: getattr(LR.settings, k, _MISSING) for k in values}
    for k, v in values.items():
        try:
            setattr(LR.settings, k, v)
        except Exception:  # pragma: no cover - defensive
            pass
    try:
        yield
    finally:
        for k, v in saved.items():
            try:
                if v is _MISSING:
                    delattr(LR.settings, k)
                else:
                    setattr(LR.settings, k, v)
            except Exception:  # pragma: no cover - defensive
                pass


@pytest.fixture(autouse=True)
def _enabled():
    yield from _set_settings(
        {"chili_momentum_unadopted_entry_dispatch_priority_enabled": True}
    )


# ── 1: THE INCIDENT — paused watching_live with an unadopted order dispatches ─


def test_paused_watching_live_with_unadopted_fill_is_dispatched():
    """Ito ang eksaktong hugis ng 19471 sa 11:19:10Z. Sa main ay [] ito."""
    row = _sess(le=_CANF_LE)
    out = LR.list_runnable_live_sessions(_Db([row]), limit=25)
    assert [s.id for s in out] == [19471], (
        "a paused session holding a submitted-but-unadopted broker entry order "
        "must still be ticked — pause must never mean unstopped"
    )


def test_claim_table_unreadable_does_not_hide_the_naked_row():
    """Ang durable-claim lane ay nagiging walang laman kapag nabigo ang read;
    ang unadopted-entry lane ang dapat pumigil sa pagtatago."""
    db = _Db([_sess(le=_CANF_LE)], claim_raises=True)
    out = LR.list_runnable_live_sessions(db, limit=25)
    assert [s.id for s in out] == [19471]
    assert db.rolled_back is True


def test_non_alpaca_venue_is_covered_too():
    """Ang claim table ay Alpaca-only; ang predicate ay snapshot-only."""
    row = _sess(family="robinhood_spot", le=_CANF_LE)
    out = LR.list_runnable_live_sessions(_Db([row]), limit=25)
    assert [s.id for s in out] == [19471]


def test_priority_lane_outranks_the_batch_cap():
    """Ang hubad na row ay hindi dapat magutom sa likod ng ``limit``."""
    rows = [
        _sess(sid=i, state="watching_live", paused=False, symbol=f"S{i}")
        for i in range(100, 103)
    ]
    rows.append(_sess(le=_CANF_LE))
    out = LR.list_runnable_live_sessions(_Db(rows), limit=1)
    assert 19471 in [s.id for s in out]
    assert out[0].id == 19471, "unadopted exposure leads the batch"


# ── 2: the predicate is exactly the heal's precondition ─────────────────────


@pytest.mark.parametrize("state", sorted(LR._HEALABLE_PRE_ENTRY_STATES))
def test_every_healable_state_is_dispatchable(state):
    assert LR._session_has_unadopted_entry_order(_sess(state=state, le=_CANF_LE))


@pytest.mark.parametrize("state", ["live_cooldown", "live_exited", "live_finished"])
def test_finished_trade_shape_is_not_dispatch_authority(state):
    """Ang tapos nang trade ay may entry_submitted + order id + position=None
    hanggang recycle. Hindi ito hubad na posisyon."""
    assert not LR._session_has_unadopted_entry_order(_sess(state=state, le=_CANF_LE))


def test_adopted_order_is_not_dispatch_authority():
    le = dict(_CANF_LE)
    le["entry_orders_resolved"] = {_CANF_LE["entry_order_id"]: "adopted"}
    assert not LR._session_has_unadopted_entry_order(_sess(le=le))


def test_position_present_is_not_dispatch_authority():
    le = dict(_CANF_LE)
    le["position"] = {"qty": 165.0, "entry_price": 4.62}
    assert not LR._session_has_unadopted_entry_order(_sess(le=le))


def test_never_submitted_is_not_dispatch_authority():
    assert not LR._session_has_unadopted_entry_order(
        _sess(le={"entry_submitted": False})
    )


def test_all_history_resolved_without_pointer_is_not_dispatch_authority():
    le = {
        "entry_submitted": True,
        "entry_client_order_id": "chili-19471",
        "entry_order_ids_all": ["a", "b"],
        "entry_orders_resolved": {"a": "void", "b": "adopted"},
    }
    assert not LR._session_has_unadopted_entry_order(_sess(le=le))


def test_unreadable_snapshot_fails_open():
    """Ang hindi mabasang snapshot ay hindi kailanman dahilan para tumigil sa
    pagtingin sa posibleng hubad na posisyon."""

    class _Raiser:
        id = 7
        symbol = "X"
        state = "watching_live"

        @property
        def risk_snapshot_json(self):
            raise RuntimeError("snapshot unreadable")

    assert LR._session_has_unadopted_entry_order(_Raiser()) is True


def test_disable_flag_reverts_to_prior_behaviour():
    gen = _set_settings(
        {"chili_momentum_unadopted_entry_dispatch_priority_enabled": False}
    )
    next(gen)
    try:
        assert not LR._session_has_unadopted_entry_order(_sess(le=_CANF_LE))
        assert LR.list_runnable_live_sessions(_Db([_sess(le=_CANF_LE)]), limit=25) == []
    finally:
        next(gen, None)


# ── 3: dispatch authority is NOT exit authority ─────────────────────────────


def test_unadopted_entry_does_not_grant_paused_exit_authority():
    """Kung ito ay nagbigay ng exit authority, ang paused na watching_live row
    ay babagsak sa BUONG entry FSM — makakapaglagay ng BAGONG order habang
    naka-pause. Ang heal at ang paused adopt-only branch ay nasa IBABAW ng
    pause gate, kaya hindi ito kailangan."""
    assert LR._paused_session_has_exit_authority(_sess(le=_CANF_LE)) is False


def test_claim_owner_still_dispatched_unchanged():
    """Walang regression sa umiiral na durable-claim priority lane."""
    row = _sess(sid=19471, le={"entry_submitted": False})
    out = LR.list_runnable_live_sessions(_Db([row], claim_owner_ids=[19471]), limit=25)
    assert [s.id for s in out] == [19471]


def test_ordinary_paused_row_still_inert():
    """Ang basta paused na row na walang hawak na order ay hindi authority."""
    row = _sess(sid=555, le={"entry_submitted": False})
    assert LR.list_runnable_live_sessions(_Db([row]), limit=25) == []

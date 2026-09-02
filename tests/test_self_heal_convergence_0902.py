"""Q2 ng 09-02 CANF 19471: `live_entry_fill_self_healed` ay pumutok 3,373 beses
sa loob ng 108 minuto at HINDI kailanman nag-converge.

ANG MEKANISMO. Ang heal ay FIXPOINT ng sariling trigger. Ang precondition nito
ay: entry_submitted + walang position + may order id + hindi terminal na state.
Ang ginagawa nito ay `_bind_recovered_entry_order`, na muling nagtatakda ng
`entry_submitted = True` at ng order id — at HINDI kailanman sumusulat ng
`position`.  Kaya ang postcondition ay SUPERSET ng precondition: garantisadong
mag-uulit.  Ang tunay na convergence ay pag-aari ng fill-adoption path na nasa
libu-libong linya sa IBABA, sa likod ng bawat gate ng tick; kapag may gate na
bumalik nang maaga, ang susunod na wake ay makakakita ng BYTE-IDENTICAL na
input.  Sukat: 3,373 event, mean gap 1.927s, ~33/min, 111 magkakasunod na
minuto, at 3,373 na broker `get_order` round-trip — hanggang may taong
nag-flatten.

BAKIT HINDI SAPAT ANG #1285. Idinagdag nito ang `entry_fill_self_heal_sig` at
pinipigil ang emit — PERO (a) ang broker probe ay nauuna pa rin sa dedupe kaya
tumatakbo pa rin ang 3,373 round-trip; (b) ang dedupe ay nakakabit sa
`state == live_pending_entry` gayong LEGAL ang `live_pending_entry ->
watching_live`, kaya ang umaandap na session ay muling naaarmasan; (c) walang
bilang ng tangka, walang hangganan sa oras, at walang escalation — kaya ang
hubad na posisyon ay uupo na ngayon nang TAHIMIK, na mas mahirap pang mapansin
kaysa sa storm na naglantad nito noong 09-02.

ANG AYOS: throttle BAGO ang broker call; bilangin ang tangka kada order;
isang `live_entry_fill_self_healed` kada order kahit umandap ang state; at
kapag umabot sa cap nang hindi na-adopt, ISANG malakas na
`live_entry_fill_self_heal_unconverged` (severity critical) at mabagal na
re-probe.

BUMABAGSAK SA origin/main: doon ay tumatawag pa rin sa broker kada tick, muling
nag-e-emit pagkatapos ng legal na state hop, at walang
`live_entry_fill_self_heal_unconverged` kahit kailan.

Runnable: pytest tests/test_self_heal_convergence_0902.py -v
"""
from __future__ import annotations

import ast
import inspect
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import live_runner as LR
from app.services.trading.momentum_neural.live_fsm import assert_transition_live


# ── fakes ────────────────────────────────────────────────────────────────────


class _Db:
    def flush(self):
        pass


class _Order:
    def __init__(self, oid="f3ed508d", filled=165.0, avg=4.62, status="filled"):
        self.order_id = oid
        self.client_order_id = "chili_ml_e_19471_a7c3e32c_9738f4d973"
        self.filled_size = filled
        self.average_filled_price = avg
        self.status = status


class _Adapter:
    def __init__(self, order):
        self._order = order
        self.lookups = 0

    def get_order(self, oid):
        self.lookups += 1
        return (self._order, None)


class _Clock:
    """Deterministic replacement for LR._utcnow()."""

    def __init__(self, start=datetime(2026, 9, 2, 11, 31, 59)):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds: float):
        self.now = self.now + timedelta(seconds=seconds)


def _sess(state="watching_live", le=None):
    return SimpleNamespace(
        id=19471,
        symbol="CANF",
        state=state,
        execution_family="alpaca_spot",
        mode="live",
        correlation_id="corr",
        updated_at=None,
        ended_at=None,
        risk_snapshot_json={
            "alpaca_account_scope": "alpaca:paper",
            "momentum_live_execution": dict(
                le or {"entry_submitted": True, "entry_order_id": "f3ed508d"}
            ),
        },
    )


_MISSING = object()

_CFG = {
    "chili_momentum_entry_fill_self_heal_enabled": True,
    "chili_momentum_entry_fill_self_heal_probe_interval_s": 5.0,
    "chili_momentum_entry_fill_self_heal_max_attempts": 6,
    "chili_momentum_entry_fill_self_heal_unconverged_reprobe_s": 60.0,
}


@pytest.fixture
def harness(monkeypatch):
    """Save/restore settings by hand: ``monkeypatch.setattr(raising=False)``
    deletes on teardown, which would turn "the bound does not exist yet" into a
    setup ERROR instead of the behavioural assertion these tests exist to make."""
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        LR, "_emit", lambda db, sess, et, payload: events.append((et, payload))
    )

    def _fake_transition(db, sess, new_state):
        if sess.state == new_state:
            return
        assert_transition_live(sess.state, new_state)
        sess.state = new_state

    monkeypatch.setattr(LR, "_safe_transition", _fake_transition)
    saved = {k: getattr(LR.settings, k, _MISSING) for k in _CFG}
    for k, v in _CFG.items():
        try:
            setattr(LR.settings, k, v)
        except Exception:  # pragma: no cover - defensive
            pass
    try:
        yield events
    finally:
        for k, v in saved.items():
            try:
                if v is _MISSING:
                    delattr(LR.settings, k)
                else:
                    setattr(LR.settings, k, v)
            except Exception:  # pragma: no cover - defensive
                pass


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(LR, "_utcnow", c)
    return c


def _heal(sess, adapter):
    le = sess.risk_snapshot_json["momentum_live_execution"]
    return LR._heal_unrecognized_entry_fill(
        _Db(), sess, adapter, le=le, product_id="CANF"
    ), le


def _tick_storm(sess, adapter, clock, *, wakes, gap_s=1.93):
    """Replay the measured CANF cadence: a wake every ~1.93s that never reaches
    the downstream adoption path (so ``position`` is never written)."""
    for _ in range(wakes):
        _heal(sess, adapter)
        clock.advance(gap_s)


# ── 1: THE INCIDENT — 3,373 wakes must not mean 3,373 events or round-trips ──


def test_the_canf_storm_is_bounded(harness, clock):
    """Ang eksaktong hugis ng insidente: 108 minuto ng wake na walang adoption."""
    sess = _sess()
    adapter = _Adapter(_Order())
    _tick_storm(sess, adapter, clock, wakes=3373)

    healed = [p for et, p in harness if et == "live_entry_fill_self_healed"]
    unconverged = [p for et, p in harness if et == "live_entry_fill_self_heal_unconverged"]
    assert len(healed) == 1, f"one heal event per order, got {len(healed)}"
    assert len(unconverged) == 1, "exactly one loud alarm, not zero and not a storm"

    # Broker round-trips: 6 inside the fast window, then <= 1 per minute for the
    # remaining ~108 minutes. The old path issued one per wake.
    assert adapter.lookups < 200, (
        f"{adapter.lookups} broker get_order calls for one unresolved condition"
    )
    assert adapter.lookups >= 6


def test_the_alarm_says_what_is_wrong(harness, clock):
    sess = _sess()
    _tick_storm(sess, _Adapter(_Order()), clock, wakes=60)
    (alarm,) = [p for et, p in harness if et == "live_entry_fill_self_heal_unconverged"]
    assert alarm["severity"] == "critical"
    assert alarm["order_id"] == "f3ed508d"
    assert alarm["attempts"] >= 6
    assert alarm["max_attempts"] == 6
    assert alarm["filled_size"] == 165.0
    assert alarm["elapsed_s"] is not None and alarm["elapsed_s"] > 0
    assert "stop" in alarm["note"].lower() or "hubad" in alarm["note"].lower()


# ── 2: the probe is throttled BEFORE the broker is asked ────────────────────


def test_repeat_wake_inside_the_interval_never_touches_the_broker(harness, clock):
    sess = _sess()
    adapter = _Adapter(_Order())
    out1, _ = _heal(sess, adapter)
    assert out1["healed"] is True and adapter.lookups == 1
    clock.advance(1.93)  # the measured storm cadence
    out2, _ = _heal(sess, adapter)
    assert out2["throttled"] is True
    assert out2["repeat"] is True
    assert adapter.lookups == 1, "a throttled wake must not cost a broker round-trip"


def test_after_the_interval_the_probe_runs_again(harness, clock):
    sess = _sess()
    adapter = _Adapter(_Order())
    _heal(sess, adapter)
    clock.advance(6.0)
    _heal(sess, adapter)
    assert adapter.lookups == 2
    assert len([et for et, _ in harness if et == "live_entry_fill_self_healed"]) == 1


def test_unconverged_backs_off_to_the_slow_cadence(harness, clock):
    sess = _sess()
    adapter = _Adapter(_Order())
    for _ in range(6):
        _heal(sess, adapter)
        clock.advance(5.0)
    assert adapter.lookups == 6
    le = sess.risk_snapshot_json["momentum_live_execution"]
    assert le["entry_fill_self_heal_unconverged_sig"] == "f3ed508d"
    # 30s of fast-cadence wakes now buy zero round-trips.
    for _ in range(15):
        _heal(sess, adapter)
        clock.advance(2.0)
    assert adapter.lookups == 6
    clock.advance(61.0)
    _heal(sess, adapter)
    assert adapter.lookups == 7, "the slow re-probe still lets a late adoption land"


# ── 3: a LEGAL state hop must not re-arm the emit ───────────────────────────


def test_legal_hop_back_to_watching_does_not_re_emit(harness, clock):
    """`live_pending_entry -> watching_live` ay LEGAL na edge. Ang dedupe ng
    #1285 ay nakakabit sa state, kaya muli itong naaarmasan dito."""
    sess = _sess()
    adapter = _Adapter(_Order())
    _heal(sess, adapter)
    assert sess.state == "live_pending_entry"
    sess.state = "watching_live"  # legal FSM edge, taken elsewhere in the tick
    clock.advance(30.0)
    out, _ = _heal(sess, adapter)
    assert out["healed"] is True
    assert out["repeat"] is True, "same order, second heal — no second page"
    assert sess.state == "live_pending_entry", "the chain is still re-walked"
    assert len([et for et, _ in harness if et == "live_entry_fill_self_healed"]) == 1


def test_the_hop_is_actually_legal():
    """Kung tumigil itong maging legal, ang test sa itaas ay nagiging vacuous."""
    from app.services.trading.momentum_neural.live_fsm import can_transition_live

    assert can_transition_live("live_pending_entry", "watching_live")


# ── 4: a genuinely NEW order gets a fresh budget and a fresh page ───────────


def test_a_second_order_pages_again_with_a_fresh_budget(harness, clock):
    sess = _sess()
    _tick_storm(sess, _Adapter(_Order()), clock, wakes=40)
    assert len([et for et, _ in harness if et == "live_entry_fill_self_healed"]) == 1

    # Recycle into a new entry generation: new order id, position still unwritten.
    le = sess.risk_snapshot_json["momentum_live_execution"]
    le["entry_order_id"] = "9a1c77b2"
    le["entry_client_order_id"] = ""
    sess.state = "watching_live"
    clock.advance(120.0)
    out, le = _heal(sess, _Adapter(_Order(oid="9a1c77b2", filled=221.0)))
    assert out["healed"] is True and out.get("repeat") is not True
    assert out["attempts"] == 1
    assert le["entry_fill_self_heal_sig"] == "9a1c77b2"
    assert "entry_fill_self_heal_unconverged_sig" not in le
    assert len([et for et, _ in harness if et == "live_entry_fill_self_healed"]) == 2
    assert (
        len([et for et, _ in harness if et == "live_entry_fill_self_heal_unconverged"])
        == 1
    )


# ── 5: the CONVERGING case (the 11:10:18 control) stays a single event ──────


def test_the_converging_control_case_pages_once_and_stops(harness, clock):
    """Order 552efe43: heal 11:10:19.268 -> adopted 11:10:20.456, 1.19s later.
    Kapag naisulat ang position, ang trigger ay nawawala nang tuluyan."""
    sess = _sess()
    adapter = _Adapter(_Order(oid="552efe43", filled=221.0))
    out, le = _heal(sess, adapter)
    assert out["healed"] is True and out.get("unconverged") is not True
    # the downstream adoption path lands
    le["position"] = {"qty": 221.0, "entry_price": 2.09}
    clock.advance(1.19)
    for _ in range(50):
        assert _heal(sess, adapter)[0] == {}
        clock.advance(1.93)
    assert adapter.lookups == 1
    assert len([et for et, _ in harness if et == "live_entry_fill_self_healed"]) == 1
    assert not [
        et for et, _ in harness if et == "live_entry_fill_self_heal_unconverged"
    ]


def test_adoption_after_escalation_still_silences_the_loop(harness, clock):
    sess = _sess()
    adapter = _Adapter(_Order())
    _tick_storm(sess, adapter, clock, wakes=100)
    before = adapter.lookups
    sess.risk_snapshot_json["momentum_live_execution"]["position"] = {"qty": 165.0}
    clock.advance(300.0)
    assert _heal(sess, adapter)[0] == {}
    assert adapter.lookups == before


# ── 6: structural guards ────────────────────────────────────────────────────


def _fn_body_src(fn) -> str:
    tree = ast.parse(inspect.getsource(fn))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(getattr(body[0], "value", None), ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return ast.unparse(ast.Module(body=body, type_ignores=[]))


def test_the_throttle_precedes_every_broker_call():
    src = _fn_body_src(LR._heal_unrecognized_entry_fill)
    i_throttle = src.index("entry_fill_self_heal_last_utc")
    assert i_throttle < src.index("_recover_entry_order_by_client_id")
    assert i_throttle < src.index("adapter.get_order")


def test_the_dedupe_is_not_conditioned_on_the_state():
    """Ang bug (b) ng #1285: ``sig == oid AND state == live_pending_entry``."""
    src = _fn_body_src(LR._heal_unrecognized_entry_fill)
    i_sig = src.index("entry_fill_self_heal_sig")
    window = src[i_sig : i_sig + 400]
    assert "STATE_LIVE_PENDING_ENTRY" not in window, (
        "the one-event-per-order dedupe must not depend on the current state — "
        "live_pending_entry -> watching_live is a legal edge"
    )


def test_escalation_is_emitted_exactly_once_per_signature():
    src = _fn_body_src(LR._heal_unrecognized_entry_fill)
    assert "live_entry_fill_self_heal_unconverged" in src
    assert "entry_fill_self_heal_unconverged_sig" in src
    assert "_log.error(" in src, "the alarm must be loud in the lane log too"


def test_the_bounds_are_configurable_and_sane():
    assert LR._heal_max_attempts() >= 1
    assert LR._heal_probe_interval_s() >= 0.0
    assert LR._heal_reprobe_interval_s() >= 1.0
    from app.config import Settings

    s = Settings()
    assert s.chili_momentum_entry_fill_self_heal_max_attempts == 6
    assert s.chili_momentum_entry_fill_self_heal_probe_interval_s == 5.0
    assert s.chili_momentum_entry_fill_self_heal_unconverged_reprobe_s == 60.0


def test_heal_ts_normalizes_aware_stamps():
    """Ang naive/aware na pagbabawas ay nag-raise — at ang exception ay
    magbabalik sa hindi-nabound na landas."""
    assert LR._heal_ts("2026-09-02T11:31:59.347844+00:00") == datetime(
        2026, 9, 2, 11, 31, 59, 347844
    )
    assert LR._heal_ts("2026-09-02T11:31:59.347844Z") == datetime(
        2026, 9, 2, 11, 31, 59, 347844
    )
    assert LR._heal_ts(None) is None
    assert LR._heal_ts("not-a-date") is None

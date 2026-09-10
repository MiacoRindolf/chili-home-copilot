"""EXIT VERDICT F -- the per-leg phase machine on fakes (2026-09-10, [21]/[44]/[47]).

DB-free. `_emit`, `_commit_le`, `_utcnow` and the three bounded tape readers are faked (the
`_fake_env` pattern of tests/test_opinion_exits_ask_the_tape.py); a scripted adapter answers
the shrink pulse. Every phase edge of docs/DESIGN/EXIT_VERDICT_F.md §4 is driven here:

    arm -> armed (receipt once) -> D fires -> partial intent WRITTEN BEFORE any adapter call
    -> shrink pulse (tranche first, deadman cancel, not-terminal stays, filled abandons)
    -> certification (R generation protected) -> partial_sell_pending
    -> the f fill routes to `_verdict_partial_to_runner` (no state change, no breakeven move)
    -> runner: tick deadman on a print <= level, ratchet receipt only on a move, D2 only
       after a print above the partial fill, stale tape disarms, -USD is named unavailable.

The chokepoint bypass, the deadman head guard recursion (I2) and the POST shape are proven
against the real claim tables in tests/test_exit_verdict_f_chokepoint_partial.py.

Runnable: pytest tests/test_exit_verdict_f_state_machine.py -v
"""
from __future__ import annotations

import bisect
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import settings
from app.services.trading.momentum_neural import entry_gates as EG
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.venue.protocol import NormalizedOrder

T_ENTRY = datetime(2026, 9, 10, 14, 0, 0)
E0 = 1_800_000_000.0
#: the machine's write-ahead flushes the session transaction (I3); nothing else is a DB here
_DB = SimpleNamespace(flush=lambda: None)


# ── a synthetic tape and the three readers over it ──────────────────────────────

class FakeTape:
    """Rows `(price, size, bid, ask, epoch, observed_at, id)`, oldest-first; the readers
    apply the SAME bounds the SQL does (symbol-agnostic here, as-of and tuple bounds)."""

    def __init__(self) -> None:
        self.rows: list[tuple] = []
        self.reads: list[tuple[str, dict]] = []
        self.fail_with: dict | None = None

    def add(self, seconds: float, px: float, size: float = 100.0, *, aggressor: int = 0) -> None:
        at = T_ENTRY + timedelta(seconds=seconds)
        if aggressor > 0:
            b, a = px - 0.02, px
        elif aggressor < 0:
            b, a = px, px + 0.02
        else:
            b, a = px - 0.01, px + 0.01
        self.rows.append((px, size, b, a, E0 + seconds, at, 10_000 + len(self.rows)))
        self.rows.sort(key=lambda r: (r[5], r[6]))

    def sellers_took_it(self, t0: float, px0: float) -> None:
        """Four prints after t0 that make D fire on top of a buyer-held window: two SMALL
        buys, then two sells at lower prices -- the back half of the since-high window
        (time split for the accel, count split for the share / swing lows) is weaker."""
        self.add(t0 + 0.0, px0, 50, aggressor=1)
        self.add(t0 + 0.5, px0, 50, aggressor=1)
        self.add(t0 + 1.0, px0 - 0.02, 100, aggressor=-1)
        self.add(t0 + 1.5, px0 - 0.03, 100, aggressor=-1)

    @staticmethod
    def _t(v: Any) -> datetime | None:
        if v is None:
            return None
        if isinstance(v, datetime):
            return v
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).replace(tzinfo=None)

    def _fail(self, err):
        if self.fail_with is not None and isinstance(err, dict):
            err.update(self.fail_with)
        return self.fail_with is not None

    def leg_high_print_first(self, symbol, *, db=None, entry_at=None, as_of=None, err=None, timeout_ms=2000):
        self.reads.append(("high", {"entry_at": entry_at, "as_of": as_of}))
        if self._fail(err):
            return None
        a, b = self._t(entry_at), self._t(as_of)
        leg = [r for r in self.rows if a < r[5] <= b]
        if not leg:
            return None
        hi = max(leg, key=lambda r: (r[0], -r[4], -r[6]))
        n_after = sum(1 for r in leg if (r[5], r[6]) > (hi[5], hi[6]))
        return {"price": hi[0], "observed_at": hi[5], "id": hi[6], "n_after": n_after,
                "tie_rule": "first_occurrence"}

    def leg_prints_between(self, symbol, *, db=None, after=None, as_of=None, after_id=None, err=None, timeout_ms=2000):
        self.reads.append(("between", {"after": after, "as_of": as_of}))
        if self._fail(err):
            return None
        a, b = self._t(after), self._t(as_of)
        return [r for r in self.rows if a < r[5] <= b]

    def leg_prints_since_high(self, symbol, *, db=None, hi_at=None, hi_id=None, as_of=None, err=None, timeout_ms=2000):
        self.reads.append(("since", {"hi_at": hi_at, "hi_id": hi_id, "as_of": as_of}))
        if self._fail(err):
            return None
        a, b = self._t(hi_at), self._t(as_of)
        return [r for r in self.rows if (r[5], r[6]) > (a, hi_id) and r[5] <= b]

    def signed_tape_accel_features(self, symbol, *, db=None, as_of=None, window_prints=None, **_):
        b = self._t(as_of)
        upto = [r for r in self.rows if r[5] <= b]
        n = int(window_prints or 255)
        return EG._signed_tape_features(upto[-n:], window_s=15.0, tick_rate_floor_pctile=0.0)


class Env:
    def __init__(self, monkeypatch, *, tape: FakeTape, now: datetime):
        self.emitted: list[tuple[str, dict]] = []
        self.commits: list[dict] = []
        self.now = now
        self.tape = tape
        self.calls: list[str] = []          # call ORDER across commits and adapter calls
        monkeypatch.setattr(lr, "_emit", self._emit)
        monkeypatch.setattr(lr, "_commit_le", self._commit)
        monkeypatch.setattr(lr, "_utcnow", lambda: self.now)
        monkeypatch.setattr(lr, "_schedule_exit_continuation", lambda sid: self.calls.append("wake") or True)
        monkeypatch.setattr(lr, "_record_live_partial_exit_ledger_safe", lambda *a, **k: None)
        monkeypatch.setattr(lr, "_record_fill_outcome_safe", lambda *a, **k: None)
        monkeypatch.setattr(lr, "_safe_transition", self._no_transition)
        monkeypatch.setattr(EG, "leg_high_print_first", tape.leg_high_print_first)
        monkeypatch.setattr(EG, "leg_prints_between", tape.leg_prints_between)
        monkeypatch.setattr(EG, "leg_prints_since_high", tape.leg_prints_since_high)
        monkeypatch.setattr(EG, "signed_tape_accel_features", tape.signed_tape_accel_features)
        monkeypatch.setattr(lr, "_strict_alpaca_account_identity", lambda adapter, sess: (True, {}))
        # the fixtures are built on Q = 31 -> f = 11, R = 20 (the in-memory 11/31); the SHIPPED
        # default is the tick-by-tick 24/32 (see the config description) -- pinned in
        # tests/test_exit_verdict_f_source_pins.py, not here.
        monkeypatch.setattr(settings, "chili_momentum_exit_verdict_sell_fraction", 11 / 31)

    def _emit(self, db, sess, ev, payload):
        self.emitted.append((ev, dict(payload)))

    def _commit(self, sess, le):
        self.commits.append(dict(le))
        self.calls.append("commit")

    def _no_transition(self, db, sess, new_state):
        raise AssertionError(f"state transition attempted: {new_state}")

    def events(self, name: str) -> list[dict]:
        return [p for e, p in self.emitted if e == name]


class ScriptedAdapter:
    """Answers the shrink pulse: cancel records; the CID truth is scripted per call."""

    def __init__(self) -> None:
        self.cancel_calls: list[str] = []
        self.truth: dict[str, list[Any]] = {}
        self.log: list[str] = []

    def cancel_order_by_id(self, order_id: str) -> bool:
        self.cancel_calls.append(order_id)
        self.log.append(f"cancel:{order_id}")
        return True

    def get_order_by_client_order_id_truth(self, cid: str) -> dict[str, Any]:
        self.log.append(f"truth:{cid}")
        seq = self.truth.get(cid)
        if not seq:
            return {"readable": True, "found": False, "order": None}
        order = seq.pop(0) if len(seq) > 1 else seq[0]
        return {"readable": True, "found": True, "order": order}


def _order(oid, cid, *, status, filled=0.0):
    return NormalizedOrder(order_id=oid, client_order_id=cid, product_id="SKYQ", side="sell",
                           status=status, order_type="stop", filled_size=filled,
                           average_filled_price=(9.5 if filled else None),
                           raw={"alpaca_status": status})


def _sess(symbol="SKYQ", state="live_entered"):
    return SimpleNamespace(id=21605, state=state, symbol=symbol)


def _le(qty=10.0, *, armed=True, deadman=True, stop=9.0):
    le: dict = {
        "position": {"quantity": qty, "original_quantity": qty, "avg_entry_price": 10.0,
                     "stop_price": stop, "high_water_mark": 10.3},
        "entry_filled_at_utc": T_ENTRY.isoformat(),
        "last_held_execution_bbo": {"source": "iqfeed_l1", "age_seconds": 0.12, "reason": None},
    }
    if armed:
        le["opinion_exit_armed"] = {"reason": "breakout_failed_fast_bail", "at_utc": (T_ENTRY + timedelta(seconds=40)).isoformat(),
                                    "reasons": ["breakout_failed_fast_bail"], "prior_event": "live_bailout", "inputs": {}}
    if deadman:
        le["deadman_stop"] = {"order_id": "dm-oid-1", "client_order_id": "chili_dm_21605_1_abc", "stop_price": 8.98,
                              "qty": qty, "phase": "submitted", "owner_transport": {"client_order_id": "chili_dm_21605_1_abc"}}
    return le


PROD = SimpleNamespace(base_increment=1.0, base_min_size=1.0)


def _tick(env: Env, le, *, seconds: float, bid=10.1, prod=PROD, sess=None):
    env.now = T_ENTRY + timedelta(seconds=seconds)
    sess = sess or _sess()
    pos = le["position"]
    return lr._exit_verdict_tick(_DB, sess, le, as_of=env.now, bid=bid, ask=bid + 0.01, mid=bid + 0.005,
                                 qty=pos["quantity"], avg=pos["avg_entry_price"], stop_px=pos["stop_price"], prod=prod)


def _quiet_tape() -> FakeTape:
    """A leg that rises to 10.5 at +30 s and then prints a few buyer-held ticks."""
    t = FakeTape()
    # the base the tick deadman reads at the entry fill (prints BEFORE the fill; the leg
    # high reader is `observed_at > entry_at`, so these never enter the since-high window)
    t.add(-4.0, 9.90, aggressor=1)
    t.add(-3.0, 9.85, aggressor=1)
    t.add(-2.0, 9.95, aggressor=1)
    t.add(-1.0, 9.98, aggressor=1)
    t.add(1.0, 10.0, aggressor=1)
    t.add(10.0, 10.2, aggressor=1)
    t.add(30.0, 10.5, aggressor=1)           # the leg high
    t.add(31.0, 10.48, 100, aggressor=1)
    t.add(32.0, 10.47, 100, aggressor=1)
    t.add(33.0, 10.49, 100, aggressor=1)
    t.add(34.0, 10.48, 100, aggressor=1)
    return t


# ── arming ─────────────────────────────────────────────────────────────────────

def test_the_first_armed_tick_emits_the_armed_receipt_and_never_moves_the_state(monkeypatch):
    tape = _quiet_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le()
    sess = _sess()
    out = _tick(env, le, seconds=36.0, sess=sess)
    assert out["action"] is None
    assert sess.state == "live_entered"
    ev = le["exit_verdict"]
    assert ev["phase"] == "armed"
    assert ev["leg_high"]["price"] == 10.5 and ev["leg_high"]["tie_rule"] == "first_occurrence"
    armed = env.events("live_exit_verdict_armed")
    assert len(armed) == 1
    r = armed[0]
    assert r["n_since_high"] == 4 and r["min_prints"] == {"feature": 3, "binding": 4}
    assert r["window_s_binding"] == 15.0 and r["tick_deadman_window_prints"] == settings.chili_momentum_g4_reentry_tape_window_prints
    assert r["sell_fraction"] == pytest.approx(11 / 31) and "8/32" in r["sell_fraction_derivation"]
    assert r["bbo_source"] == "iqfeed_l1" and r["bbo_age_s"] == 0.12 and r["as_of"] == env.now.isoformat()
    assert r["opinion_exit_armed"]["reason"] == "breakout_failed_fast_bail"
    assert r["derivation"] == lr._EXIT_VERDICT_DERIVATION
    # the second armed tick: no second armed receipt, the frontier advanced
    _tick(env, le, seconds=39.0, sess=sess)
    assert len(env.events("live_exit_verdict_armed")) == 1
    assert le["exit_verdict"]["frontier_at"] == env.now.isoformat()


def test_arming_is_idempotent_per_reason_and_a_second_reason_is_appended_with_the_verdict_marker(monkeypatch):
    env = Env(monkeypatch, tape=FakeTape(), now=T_ENTRY + timedelta(seconds=40))
    sess = _sess()
    le = _le(armed=False)
    assert lr._arm_opinion_exit(_DB, sess, le, reason="breakout_failed_fast_bail",
                                prior_event="live_bailout", inputs={"bid": 8.96}) is True
    assert lr._arm_opinion_exit(_DB, sess, le, reason="breakout_failed_fast_bail",
                                prior_event="live_bailout", inputs={"bid": 8.95}) is False
    assert lr._arm_opinion_exit(_DB, sess, le, reason="momentum_break_bars",
                                prior_event="live_momentum_break_exit", inputs={"bid": 8.90}) is True
    receipts = env.events("live_opinion_exit_armed")
    assert len(receipts) == 2
    assert receipts[0]["armed_exit"] == "exit_verdict_f" and receipts[0]["superseded"] == "momentum_break_stop"
    assert receipts[1]["reasons"] == ["breakout_failed_fast_bail", "momentum_break_bars"]
    assert lr._exit_verdict_active(sess, le) is True


def test_a_crypto_leg_is_named_unavailable_once_and_stays_on_the_fallback(monkeypatch):
    env = Env(monkeypatch, tape=FakeTape(), now=T_ENTRY + timedelta(seconds=40))
    sess = _sess(symbol="BTC-USD")
    le = _le(armed=False)
    assert lr._arm_opinion_exit(_DB, sess, le, reason="topping_tail_runner_exit",
                                prior_event="live_bailout", inputs={}) is True
    assert env.events("live_opinion_exit_armed")[0]["armed_exit"] == "momentum_break_stop"
    assert lr._exit_verdict_active(sess, le) is False       # the elif is never entered
    assert lr._exit_verdict_supported(sess, le) is False
    # a direct pass still reports the binding ONCE
    assert lr._exit_verdict_tick(_DB, sess, le, as_of=env.now, bid=1, ask=1, mid=1, qty=1, avg=1, stop_px=1) is None
    assert lr._exit_verdict_tick(_DB, sess, le, as_of=env.now, bid=1, ask=1, mid=1, qty=1, avg=1, stop_px=1) is None
    assert [p["binding"] for p in env.events("live_exit_verdict_unavailable")] == ["no_equity_tape"]


def test_an_unreadable_entry_fill_anchor_is_the_other_named_fallback(monkeypatch):
    env = Env(monkeypatch, tape=FakeTape(), now=T_ENTRY)
    sess = _sess()
    le = _le()
    le["entry_filled_at_utc"] = "not-a-time"
    assert lr._exit_verdict_supported(sess, le) is False
    lr._exit_verdict_tick(_DB, sess, le, as_of=env.now, bid=1, ask=1, mid=1, qty=1, avg=1, stop_px=1)
    assert env.events("live_exit_verdict_unavailable")[0]["binding"] == "entry_fill_anchor_missing"


# ── the first verdict: the partial intent ──────────────────────────────────────

def test_d_fires_and_the_partial_intent_is_written_before_any_adapter_call(monkeypatch):
    tape = _quiet_tape()
    tape.sellers_took_it(35.0, 10.45)
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0)
    sess = _sess()
    out = _tick(env, le, seconds=37.0, sess=sess)
    assert out["action"] == "partial" and (out["f"], out["R"]) == (11.0, 20.0)
    ev = le["exit_verdict"]
    assert ev["phase"] == "partial_shrink_pending"
    assert ev["partial"]["pending_qty"] == 11.0 and ev["partial"]["Q"] == 31.0
    assert ev["partial"]["decision_as_of"] == env.now.isoformat() and ev["partial"]["decision_bid"] == 10.1
    assert ev["partial"]["shrink"]["predecessor_oid"] == "dm-oid-1"
    assert lr._exit_verdict_pending_partial_qty(le) == 11.0
    assert sess.state == "live_entered"
    # I3: the intent was committed and the wake scheduled; no adapter was touched by the tick
    assert env.calls[-1] == "wake" and "commit" in env.calls
    p = env.events("live_exit_verdict_partial")
    assert len(p) == 1
    assert p[0]["fraction"] == pytest.approx(11 / 31) and p[0]["can_split"] is True
    assert p[0]["verdict"]["fired"] is True and p[0]["verdict"]["binding"] == "all_three"
    assert p[0]["verdict"]["n_since_high"] == 8 and p[0]["leg_high"]["price"] == 10.5
    assert p[0]["phase"] == "partial_shrink_pending"
    # the frontier is NOT advanced during the shrink (fill-latency rewind)
    frontier = ev["frontier_at"]
    _tick(env, le, seconds=40.0, sess=sess)
    assert le["exit_verdict"]["frontier_at"] == frontier
    assert len(env.events("live_exit_verdict_partial")) == 1


def test_cannot_split_means_the_whole_position_on_the_verdict(monkeypatch):
    tape = _quiet_tape()
    tape.sellers_took_it(35.0, 10.45)
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=1.0)
    out = _tick(env, le, seconds=37.0)
    assert out["action"] == "whole_cannot_split"
    assert le["exit_verdict"]["phase"] == "armed"      # -> exited when the whole fill lands
    assert lr._exit_verdict_pending_partial_qty(le) == 0.0
    x = env.events("live_exit_verdict_exit")
    assert len(x) == 1 and x[0]["kind"] == "whole_cannot_split" and x[0]["can_split"] is False
    assert x[0]["remaining_qty"] == 1.0


def test_a_stale_tape_disarms_the_verdict_and_reports_once(monkeypatch):
    tape = _quiet_tape()
    tape.sellers_took_it(35.0, 10.45)                     # last print at +36.5 s
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0)
    out = _tick(env, le, seconds=50.0)                    # 13.5 s after the last print > 7.5 s
    assert out["action"] is None and out["stale"] is True
    assert out["tape_frontier_age_s"] == pytest.approx(13.5)
    assert le["exit_verdict"]["phase"] == "armed"
    u = env.events("live_exit_verdict_unreadable")
    assert len(u) == 1 and u[0]["why"] == "stale_tape" and u[0]["stale_tape_bound_s"] == 7.5
    _tick(env, le, seconds=53.0)
    assert len(env.events("live_exit_verdict_unreadable")) == 1     # on change only
    # the tape speaks again => the verdict fires on the next fresh tick
    tape.sellers_took_it(54.0, 10.44)
    out = _tick(env, le, seconds=56.0)
    assert out["stale"] is False and out["action"] == "partial"


def test_a_read_timeout_is_unreadable_and_fails_open(monkeypatch):
    tape = _quiet_tape()
    tape.fail_with = {"why": "timeout", "error": "OperationalError"}
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0)
    out = _tick(env, le, seconds=37.0)
    assert out == {"action": None, "unreadable": "timeout"}
    u = env.events("live_exit_verdict_unreadable")
    assert len(u) == 1 and u[0]["why"] == "timeout" and u[0]["error"] == "OperationalError"
    assert le["exit_verdict"]["leg_high"] is None            # retried next pass


# ── the shrink pulse ───────────────────────────────────────────────────────────

def _shrunk_leg(monkeypatch, *, qty=31.0):
    tape = _quiet_tape()
    tape.sellers_took_it(35.0, 10.45)
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=qty)
    out = _tick(env, le, seconds=37.0)
    assert out["action"] == "partial"
    return env, le


def _pulse(env, le, adapter, *, seconds):
    env.now = T_ENTRY + timedelta(seconds=seconds)
    pos = le["position"]
    return lr._service_exit_verdict_partial(_DB, _sess(), adapter, le, as_of=env.now, bid=10.1, ask=10.11, mid=10.105,
                                            product_id="SKYQ", qty=pos["quantity"], avg=pos["avg_entry_price"],
                                            stop_px=pos["stop_price"])


def test_the_shrink_cancels_the_tranche_first_then_the_deadman(monkeypatch):
    env, le = _shrunk_leg(monkeypatch)
    le["scale_limit_order_id"] = "oco-1"
    le["scale_limit_qty"] = 5.0
    adapter = ScriptedAdapter()
    adapter.truth["chili_dm_21605_1_abc"] = [_order("dm-oid-1", "chili_dm_21605_1_abc", status="canceled")]

    def _clamp(db, sess, ad, *, le, requested_qty, reason):
        adapter.log.append(f"tranche_cancel:{reason}")
        le.pop("scale_limit_order_id", None)
        return float(requested_qty)

    monkeypatch.setattr(lr, "_cancel_scale_limit_and_clamp", _clamp)
    out = _pulse(env, le, adapter, seconds=38.0)
    assert out["ok"] is True and out["cancelled"] is True
    assert adapter.log[:3] == ["tranche_cancel:tape_sellers_took_it", "cancel:dm-oid-1", "truth:chili_dm_21605_1_abc"]
    assert le["exit_verdict"]["partial"]["tranche_cancelled"] is True
    assert le["exit_verdict"]["partial"]["shrink"]["cancel_terminal_at_utc"] == env.now.isoformat()
    assert le["exit_verdict"]["phase"] == "partial_shrink_pending"


def test_a_cancel_that_is_not_terminal_stays_in_shrink_with_an_attempt_and_a_backoff(monkeypatch):
    env, le = _shrunk_leg(monkeypatch)
    adapter = ScriptedAdapter()
    adapter.truth["chili_dm_21605_1_abc"] = [_order("dm-oid-1", "chili_dm_21605_1_abc", status="accepted")]
    out = _pulse(env, le, adapter, seconds=38.0)
    assert out["ok"] is False and out["why"] == "deadman_cancel_not_terminal"
    partial = le["exit_verdict"]["partial"]
    assert partial["attempts"]["shrink"] == 1 and partial["pending_qty"] == 11.0
    assert le["exit_verdict"]["phase"] == "partial_shrink_pending"
    f = env.events("live_exit_verdict_partial_failed")
    assert f[-1]["stage"] == "shrink" and f[-1]["attempt"] == 1 and f[-1]["fallback"] == "retry"
    # backoff: the very next pulse is deferred (5 s * 2^0), no second cancel
    out2 = _pulse(env, le, adapter, seconds=39.0)
    assert out2["deferred"] is True and adapter.cancel_calls == ["dm-oid-1"]


def test_the_attempt_cap_re_arms_the_opinion_with_the_q_stop_intact(monkeypatch):
    env, le = _shrunk_leg(monkeypatch)
    le["exit_verdict"]["partial"]["attempts"]["shrink"] = lr._EXIT_SUBMIT_MAX_ATTEMPTS
    adapter = ScriptedAdapter()
    out = _pulse(env, le, adapter, seconds=38.0)
    assert out["abandoned"] is True and adapter.cancel_calls == []
    assert le["exit_verdict"]["phase"] == "armed" and lr._exit_verdict_pending_partial_qty(le) == 0.0
    f = env.events("live_exit_verdict_partial_failed")[-1]
    assert f["stage"] == "shrink" and f["fallback"] == "rearm" and f["why"] == "shrink_attempts_exhausted"
    assert le["deadman_stop"]["qty"] == 31.0


def test_the_deadman_filling_during_the_cancel_race_abandons_the_partial(monkeypatch):
    env, le = _shrunk_leg(monkeypatch)
    adapter = ScriptedAdapter()
    adapter.truth["chili_dm_21605_1_abc"] = [_order("dm-oid-1", "chili_dm_21605_1_abc", status="filled", filled=31.0)]
    out = _pulse(env, le, adapter, seconds=38.0)
    assert out["abandoned"] is True and out["deadman_filled"] == 31.0
    assert lr._exit_verdict_pending_partial_qty(le) == 0.0
    assert le["exit_verdict"]["phase"] == "armed"     # the maintenance call books the fill this tick
    f = env.events("live_exit_verdict_partial_failed")[-1]
    assert f["fallback"] == "deadman_filled" and f["filled_size"] == 31.0


def test_terminal_cancel_then_certification_advances_to_sell_pending(monkeypatch):
    env, le = _shrunk_leg(monkeypatch)
    adapter = ScriptedAdapter()
    adapter.truth["chili_dm_21605_1_abc"] = [_order("dm-oid-1", "chili_dm_21605_1_abc", status="canceled")]
    out = _pulse(env, le, adapter, seconds=38.0)
    assert out == {"ok": True, "cancelled": True, "want_qty": 20.0}
    assert adapter.cancel_calls == ["dm-oid-1"]
    # the :43928 maintenance call (faked here; the real recursion is proven on the DB) re-armed R
    le["deadman_stop"] = {"order_id": "dm-oid-2", "client_order_id": "chili_dm_21605_2_def", "qty": 20.0, "phase": "submitted"}
    env.now = T_ENTRY + timedelta(seconds=38.4)
    lr._certify_exit_verdict_cover(_DB, _sess(), le, deadman_state={"ok": True, "protected": True},
                                   as_of=env.now, bid=10.1)
    assert le["exit_verdict"]["phase"] == "partial_sell_pending"
    s = env.events("live_exit_verdict_partial_shrunk")
    assert len(s) == 1
    assert s[0]["R"] == 20.0 and s[0]["f"] == 11.0 and s[0]["path"] == "cancel_rearm"
    assert s[0]["predecessor_cid"] == "chili_dm_21605_1_abc" and s[0]["successor_cid"] == "chili_dm_21605_2_def"
    assert s[0]["naked_window_s"] == pytest.approx(0.4) and s[0]["latency_s"] == pytest.approx(1.4)
    assert env.calls[-1] == "wake"
    # a generation of the WRONG size does not certify
    le2 = dict(le)
    le2["exit_verdict"] = {**le["exit_verdict"], "phase": "partial_shrink_pending"}
    le2["deadman_stop"] = {"order_id": "x", "client_order_id": "y", "qty": 31.0}
    lr._certify_exit_verdict_cover(_DB, _sess(), le2, deadman_state={"ok": True, "protected": True}, as_of=env.now, bid=10.1)
    assert le2["exit_verdict"]["phase"] == "partial_shrink_pending"


def test_protection_unavailable_after_the_cancel_is_the_full_close_fallback(monkeypatch):
    env, le = _shrunk_leg(monkeypatch)
    lr._certify_exit_verdict_cover(_DB, _sess(), le,
                                   deadman_state={"ok": False, "unprotected": True, "full_close_queued": True,
                                                  "error": "deadman_post_active_certification_failed"},
                                   as_of=env.now, bid=10.1)
    assert lr._exit_verdict_pending_partial_qty(le) == 0.0
    f = env.events("live_exit_verdict_partial_failed")[-1]
    assert f["stage"] == "shrink" and f["fallback"] == "full_close"


def test_the_sell_pending_phase_asks_the_tick_for_the_f_post(monkeypatch):
    env, le = _shrunk_leg(monkeypatch)
    le["exit_verdict"]["phase"] = "partial_sell_pending"
    le["deadman_stop"]["qty"] = 20.0
    out = _tick(env, le, seconds=39.0)
    assert out["action"] == "partial_sell"
    assert le["exit_verdict"]["frontier_at"] == (T_ENTRY + timedelta(seconds=37.0)).isoformat()   # still rewound


# ── the f fill -> the runner ───────────────────────────────────────────────────

def _runner_leg(monkeypatch):
    env, le = _shrunk_leg(monkeypatch)
    le["exit_verdict"]["phase"] = "partial_sell_pending"
    le["deadman_stop"]["qty"] = 20.0
    le["pending_exit_reason"] = "tape_sellers_took_it"
    le["pending_exit_quantity"] = 11.0
    le["pending_exit_is_scale_out"] = True
    env.now = T_ENTRY + timedelta(seconds=41.0)
    pnl = lr._verdict_partial_to_runner(_DB, _sess(), le=le, filled_quantity=11.0, entry_price=10.0,
                                        fill_price=10.42, reason="tape_sellers_took_it", as_of=env.now)
    return env, le, pnl


def test_the_fill_starts_the_runner_without_a_state_change_or_a_breakeven_move(monkeypatch):
    env, le, pnl = _runner_leg(monkeypatch)
    assert pnl == pytest.approx((10.42 - 10.0) * 11.0)
    pos = le["position"]
    assert pos["partial_taken"] is True and pos["quantity"] == 20.0
    assert pos["stop_price"] == 9.0                      # UNCHANGED: the resting floor mirror
    assert "pending_exit_reason" not in le and "pending_exit_is_scale_out" not in le
    ev = le["exit_verdict"]
    assert ev["phase"] == "runner" and ev["partial"]["pending_qty"] == 0.0
    assert ev["partial"]["fill_price"] == 10.42 and ev["partial"]["pnl_usd"] == pytest.approx(pnl)
    assert ev["frontier_at"] == ev["partial"]["decision_as_of"]        # rewound to the decision
    assert ev["runner"]["runner_high"] == 10.42 and ev["runner"]["saw_new_high"] is False
    assert ev["runner"]["ratchets"] == 0 and ev["runner"]["base_window_prints"] == 255
    assert ev["runner"]["level"] == 9.85 and ev["runner"]["level_source"] == "swing_low_prev"
    names = [e for e, _ in env.emitted]
    assert names.index("live_partial_exit_filled") < names.index("live_exit_verdict_runner_started")
    r = env.events("live_exit_verdict_runner_started")[0]
    assert r["frontier_rewound_to"] == ev["partial"]["decision_as_of"]
    assert r["trail_authority"] == "tick_deadman" and r["stop_price_unchanged"] is True
    assert r["runner_qty"] == 20.0 and r["partial_qty"] == 11.0 and r["resting_stop"] == 9.0
    assert env.events("live_partial_exit_filled")[0]["reason"] == "tape_sellers_took_it"


def test_no_print_base_falls_back_to_the_resting_stop(monkeypatch):
    env, le = _shrunk_leg(monkeypatch)
    le["exit_verdict"]["phase"] = "partial_sell_pending"
    monkeypatch.setattr(EG, "signed_tape_accel_features", lambda *a, **k: None)
    lr._verdict_partial_to_runner(_DB, _sess(), le=le, filled_quantity=11.0, entry_price=10.0,
                                  fill_price=10.42, reason="tape_sellers_took_it", as_of=env.now)
    assert le["exit_verdict"]["runner"]["level"] == 9.0
    assert le["exit_verdict"]["runner"]["level_source"] == "resting_stop"


# ── the runner ─────────────────────────────────────────────────────────────────

def test_a_print_at_or_below_the_level_in_the_flight_span_exits_on_the_first_runner_tick(monkeypatch):
    env, le, _ = _runner_leg(monkeypatch)
    level = le["exit_verdict"]["runner"]["level"]
    # the crossing print landed DURING the partial's flight (+38 s < the fill at +41 s):
    # the frontier was rewound to the decision (+37 s), so the first runner tick sees it.
    env.tape.add(38.0, level - 0.01, 200, aggressor=-1)
    out = _tick(env, le, seconds=44.0)
    assert out["action"] == "runner_deadman"
    x = out["exit_receipt"]
    assert x["crossing_print"]["price"] == pytest.approx(level - 0.01)
    assert x["level"] == level and x["remaining_qty"] == 20.0 and x["resting_stop"] == 9.0
    assert x["batch_window"]["frontier_at"] == le["exit_verdict"]["partial"]["decision_as_of"]


def test_the_ratchet_receipt_only_on_a_move_and_the_level_never_lowers(monkeypatch):
    env, le, _ = _runner_leg(monkeypatch)
    level0 = le["exit_verdict"]["runner"]["level"]
    assert level0 == 9.85
    # N = 8 prints for the ratchet reads so the count-halves swing low can actually rise
    # inside a short synthetic tape (N = 255 reuses the whole tape and its pre-entry base).
    monkeypatch.setattr(settings, "chili_momentum_g4_reentry_tape_window_prints", 8)
    # six new highs above the partial fill: the FIRST one reads swing_low_prev = 10.45
    # (the front half of the last 8 prints); every later read is lower or equal => refused.
    for i, px in enumerate([10.50, 10.55, 10.60, 10.62, 10.63, 10.64]):
        env.tape.add(42.0 + i * 0.4, px, 100, aggressor=1)
    out = _tick(env, le, seconds=45.0)
    assert out["action"] is None
    runner = le["exit_verdict"]["runner"]
    assert runner["saw_new_high"] is True and runner["runner_high"] == 10.64
    assert runner["level"] == 10.45 and runner["ratchets"] == 1
    rec = env.events("live_tick_deadman_ratchet")
    assert len(rec) == 1                                   # one MOVE => one receipt
    assert rec[0]["old_level"] == 9.85 and rec[0]["new_level"] == 10.45
    assert rec[0]["new_high_print"]["price"] == 10.50 and rec[0]["base_window_prints"] == 8
    assert rec[0]["prints_in_batch"] == 6 and rec[0]["ratchets"] == 1
    # a later print BELOW the level is the tick deadman, never a lowered level
    env.tape.add(46.0, 10.30, 100, aggressor=-1)
    out = _tick(env, le, seconds=47.0)
    assert out["action"] == "runner_deadman"
    assert le["exit_verdict"]["runner"]["level"] == 10.45
    assert len(env.events("live_tick_deadman_ratchet")) == 1


def test_d2_only_after_a_print_above_the_partial_fill(monkeypatch):
    env, le, _ = _runner_leg(monkeypatch)
    # D fires below the partial fill: sellers took it, but NO new high => no D2
    env.tape.sellers_took_it(43.0, 10.40)
    out = _tick(env, le, seconds=45.0)
    assert out["verdict"]["fired"] is True and out["action"] is None
    assert le["exit_verdict"]["runner"]["saw_new_high"] is False
    # a print ABOVE the partial fill, then D again => D2 ends the runner
    env.tape.add(46.0, 10.70, 400, aggressor=1)        # new leg high too
    env.tape.sellers_took_it(47.0, 10.65)
    out = _tick(env, le, seconds=49.0)
    assert out["action"] == "runner_verdict"
    assert out["exit_receipt"]["kind"] == "runner_second_verdict" and out["exit_receipt"]["remaining_qty"] == 20.0
    assert le["exit_verdict"]["leg_high"]["price"] == 10.70    # the D anchor moved with the new leg high


def test_a_stale_tape_never_holds_the_runner_walk(monkeypatch):
    """Review of #1385 (major): stale is a do-not-DECIDE rule for D/D2, never a do-not-WALK
    rule. The old early return ran BEFORE the runner walk and after the frontier had moved,
    so a print <= the level inside a > 7.5-s tick gap (79.5% of live runner ticks) was
    dropped forever. Now the crossing print exits the runner on the stale tick itself; the
    stale receipt still fires once and says the walk continued."""
    env, le, _ = _runner_leg(monkeypatch)
    level = le["exit_verdict"]["runner"]["level"]
    env.tape.add(42.0, level - 0.05, 200, aggressor=-1)
    out = _tick(env, le, seconds=60.0)                 # 18 s after the last print
    assert out["stale"] is True and out["action"] == "runner_deadman"
    assert out["exit_receipt"]["crossing_print"]["price"] == pytest.approx(level - 0.05)
    assert out["exit_receipt"]["stale"] is True
    u = env.events("live_exit_verdict_unreadable")
    assert u[-1]["why"] == "stale_tape" and u[-1]["walks_and_executions_continue"] is True


# ── the failed-sell edges and the recover pulse ────────────────────────────────

def test_a_zero_fill_terminal_f_order_goes_back_to_armed_and_re_covers_q(monkeypatch):
    env, le = _shrunk_leg(monkeypatch)
    le["exit_verdict"]["phase"] = "partial_sell_pending"
    le["deadman_stop"]["qty"] = 20.0
    lr._exit_verdict_partial_failed(_DB, _sess(), le, stage="sell", why="terminal_no_fill", fallback="rearm",
                                    as_of=env.now, bid=10.1, to_phase="armed", clear_pending=True, recover=True)
    ev = le["exit_verdict"]
    assert ev["phase"] == "armed" and ev["partial"]["pending_qty"] == 0.0 and ev["recover"]["want_qty"] == 31.0
    # the tick will not open a new partial while the re-cover is pending
    out = _tick(env, le, seconds=40.0)
    assert out["action"] is None and out.get("recover_pending") is True
    # the cover pulse cancels the R generation so the head guard re-arms Q
    adapter = ScriptedAdapter()
    adapter.truth["chili_dm_21605_1_abc"] = [_order("dm-oid-1", "chili_dm_21605_1_abc", status="canceled")]
    out = _pulse(env, le, adapter, seconds=41.0)
    assert out == {"ok": True, "cancelled": True, "want_qty": 31.0}
    le["deadman_stop"] = {"order_id": "dm-oid-3", "client_order_id": "chili_dm_21605_3_ghi", "qty": 31.0}
    lr._certify_exit_verdict_cover(_DB, _sess(), le, deadman_state={"ok": True, "protected": True}, as_of=env.now, bid=10.1)
    assert "recover" not in le["exit_verdict"]
    assert env.events("live_exit_verdict_deadman_recovered")[0]["covered_qty"] == 31.0


def test_the_sell_cap_forces_the_whole_position_on_the_next_tick(monkeypatch):
    env, le = _shrunk_leg(monkeypatch)
    le["exit_verdict"]["phase"] = "partial_sell_pending"
    lr._exit_verdict_partial_failed(_DB, _sess(), le, stage="sell", why="exit_retry_cap_exceeded",
                                    fallback="whole_partial_failed", as_of=env.now, bid=10.1, to_phase="armed",
                                    clear_pending=True, force_whole="whole_partial_failed")
    out = _tick(env, le, seconds=42.0)
    assert out["action"] == "whole_partial_failed" and out["kind"] == "whole_partial_failed"


# ── the phase table is enforced on every write ─────────────────────────────────

def test_illegal_edges_raise_at_the_writer(monkeypatch):
    with pytest.raises(ValueError):
        lr._ev_assert_transition("runner", "partial_shrink_pending")
    with pytest.raises(ValueError):
        lr._ev_assert_transition("armed", "runner")
    # the completer asserts partial_sell_pending -> runner: an armed leg cannot start a runner
    env, le = _shrunk_leg(monkeypatch)
    le["exit_verdict"]["phase"] = "armed"
    with pytest.raises(ValueError):
        lr._verdict_partial_to_runner(_DB, _sess(), le=le, filled_quantity=11.0, entry_price=10.0,
                                      fill_price=10.42, reason="tape_sellers_took_it", as_of=env.now)


# ── recycle and the receipts on the existing exits ─────────────────────────────

def test_recycle_clears_the_marker_and_the_two_fixed_in_passing_families():
    for key in ("exit_verdict", "bailout_breach_pending_utc", "bailout_breach_trigger",
                "fpb_bucket", "fpb_fire", "failed_pop_break_dbg", "opinion_exit_armed"):
        assert key in lr._RECYCLE_ENTRY_STATE_KEYS, key


def test_the_exit_verdict_receipt_summarises_the_marker_and_fails_open():
    assert lr._exit_verdict_receipt({}) is None
    le = _le()
    le["exit_verdict"] = {"phase": "runner", "partial": {"f": 11.0, "R": 20.0, "fill_price": 10.42, "pending_qty": 0.0},
                          "runner": {"level": 9.8, "ratchets": 2, "runner_high": 10.6}, "last": {"verdict": {"fired": False}}}
    r = lr._exit_verdict_receipt(le)
    assert r["phase"] == "runner" and r["armed_reason"] == "breakout_failed_fast_bail"
    assert r["partial"]["f"] == 11.0 and r["runner"]["ratchets"] == 2 and r["last_verdict"]["fired"] is False

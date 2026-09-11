"""EXIT VERDICT G -- the per-leg machine on fakes (2026-09-10, [21]/[44]/[47] + Amendments 1-3).

DB-free. `_emit`, `_commit_le`, `_utcnow` and the two bounded tape readers are faked (the
`_fake_env` pattern of tests/test_opinion_exits_ask_the_tape.py). Every edge of the machine in
docs/DESIGN/EXIT_VERDICT_F.md is driven here:

    the first held tick after the FILL arms (no opinion needed) -> the deadman base at the fill
    -> every held tick: the walk over EVERY print (deadman first), the MONOTONE ratchet, G (the
       accel rollover while the print is above entry), D (the since-high verdict)
    -> the EARLIER of G and D => the WHOLE position (exit_pending), one receipt
       `live_exit_verdict_fired`; the deadman => `live_tick_deadman_exit`
    -> exit_pending never decides again (never a second exit); a cleared pending exit with
       shares still held re-submits the SAME decision
    -> stale = do-not-DECIDE (G, D) and never do-not-WALK; `accel_prev` survives a quiet tape
    -> an unreadable batch never advances the frontier; the frontier is the LAST WALKED print
    -> -USD / unreadable anchor are named unavailable once (the #1377 fallback judges them)

The whole exit through the real exit seam (claim tables) is proven in
tests/test_exit_verdict_g_whole_exit_seam.py.

Runnable: pytest tests/test_exit_verdict_f_state_machine.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import settings
from app.services.trading.momentum_neural import entry_gates as EG
from app.services.trading.momentum_neural import exit_verdict as EV
from app.services.trading.momentum_neural import live_runner as lr

T_ENTRY = datetime(2026, 9, 10, 14, 0, 0)
E0 = 1_800_000_000.0
#: the machine's write-ahead flushes the session transaction; nothing else is a DB here
_DB = SimpleNamespace(flush=lambda: None)


# ── a synthetic tape and the two readers over it ────────────────────────────────

class FakeTape:
    """Rows `(price, size, bid, ask, epoch, observed_at, id)`, oldest-first; the readers
    apply the SAME bounds the SQL does (symbol-agnostic here: as-of and TUPLE bounds)."""

    def __init__(self) -> None:
        self.rows: list[tuple] = []
        self.reads: list[tuple[str, dict]] = []
        self.fail_with: dict | None = None
        self._next_id = 10_000

    def add(self, seconds: float, px: float, size: float = 100.0, *, aggressor: int = 0) -> tuple:
        at = T_ENTRY + timedelta(seconds=seconds)
        if aggressor > 0:
            b, a = px - 0.02, px
        elif aggressor < 0:
            b, a = px, px + 0.02
        else:
            b, a = px - 0.01, px + 0.01
        row = (px, size, b, a, E0 + seconds, at, self._next_id)
        self._next_id += 1
        self.rows.append(row)
        self.rows.sort(key=lambda r: (r[5], r[6]))
        return row

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

    def leg_prints_between(self, symbol, *, db=None, after=None, as_of=None, after_id=None, err=None, timeout_ms=2000):
        self.reads.append(("between", {"after": after, "after_id": after_id, "as_of": as_of}))
        if self._fail(err):
            return None
        a, b = self._t(after), self._t(as_of)
        if after_id is not None:
            return [r for r in self.rows if (r[5], r[6]) > (a, after_id) and r[5] <= b]
        return [r for r in self.rows if a < r[5] <= b]

    def leg_prints_since_high(self, symbol, *, db=None, hi_at=None, hi_id=None, as_of=None, err=None, timeout_ms=2000):
        self.reads.append(("since", {"hi_at": hi_at, "hi_id": hi_id, "as_of": as_of}))
        if self._fail(err):
            return None
        a, b = self._t(hi_at), self._t(as_of)
        return [r for r in self.rows if (r[5], r[6]) > (a, hi_id) and r[5] <= b]

    def signed_tape_accel_features(self, symbol, *, db=None, as_of=None, window_prints=None, **_):
        self.reads.append(("feats", {"as_of": as_of, "window_prints": window_prints, **_}))
        b = self._t(as_of)
        upto = [r for r in self.rows if r[5] <= b]
        n = int(window_prints or 255)
        contract = _.get("feature_contract", "legacy_time_split")
        count = contract == "count_v1"
        cfg = _.get("settings_obj", settings)
        available = self._t(_.get("available_by") or b)
        f = EG._signed_tape_features(
            upto[-n:], window_s=None if count else 15., tick_rate_floor_pctile=0.,
            split="count" if count else "time", window_mode="prints",
            gap_trim_s=getattr(cfg, "chili_momentum_g4_reentry_max_print_age_seconds", 14.69) if count else None,
            gap_discontinuity_mult=getattr(cfg, "chili_momentum_tape_gap_discontinuity_p90_mult", 7.82),
            as_of_ts=E0 + (available-T_ENTRY).total_seconds() if count else None,
        )
        if f is not None:
            f.update(feature_contract=contract, window_kind="prints", window_prints=n, window_s=None)
        return f


class Env:
    def __init__(self, monkeypatch, *, tape: FakeTape, now: datetime):
        self.emitted: list[tuple[str, dict]] = []
        self.commits: list[dict] = []
        self.now = now
        self.tape = tape
        self.calls: list[str] = []
        monkeypatch.setattr(lr, "_emit", self._emit)
        monkeypatch.setattr(lr, "_commit_le", self._commit)
        monkeypatch.setattr(lr, "_utcnow", lambda: self.now)
        monkeypatch.setattr(lr, "_schedule_exit_continuation", lambda sid: self.calls.append("wake") or True)
        monkeypatch.setattr(lr, "_safe_transition", self._no_transition)
        monkeypatch.setattr(EG, "leg_prints_between", tape.leg_prints_between)
        monkeypatch.setattr(EG, "leg_prints_since_high", tape.leg_prints_since_high)
        monkeypatch.setattr(EG, "signed_tape_accel_features", tape.signed_tape_accel_features)

    def _emit(self, db, sess, ev, payload):
        self.emitted.append((ev, dict(payload)))

    def _commit(self, sess, le):
        self.commits.append(dict(le))
        self.calls.append("commit")

    def _no_transition(self, db, sess, new_state):
        raise AssertionError(f"state transition attempted: {new_state}")

    def events(self, name: str) -> list[dict]:
        return [p for e, p in self.emitted if e == name]


def _sess(symbol="SKYQ", state="live_entered"):
    return SimpleNamespace(id=21605, state=state, symbol=symbol)


#: [65] the symbol-day tape-cycle ledger the pre-entry ticks leave in `le` (the
#: `PullbackCycleScanner.to_dict()` shape): three COMPLETED cycles whose continued-pullback
#: depths are ~0.10 / 0.15 / 0.25, so the base = 10.0 - median = 9.85 exactly -- the SAME level
#: the old count-half read gives on `_prefill`, so the machine tests below keep their
#: prices; the tests that tell the two bases apart build their own ledger.
LEDGER_CYCLES = (
    {"k": 0, "spike_low": 9.60, "hi": 10.15, "pb_low": 10.00},
    {"k": 1, "spike_low": 10.00, "hi": 10.30, "pb_low": 10.20},
    {"k": 2, "spike_low": 10.20, "hi": 10.50, "pb_low": 10.25},
)


def _ledger(cycles=LEDGER_CYCLES, *, caught_up=True) -> dict:
    return {
        "v": 1, "pullback_frac": 0.5, "max_cycles": 16, "n_prints": 4000,
        "n_cycles": len(cycles), "cycles": [dict(c) for c in cycles],
        "last_observed_at": (T_ENTRY - timedelta(seconds=2)).isoformat(),
        "day": "2026-09-10", "feed": {"fed": 12, "reads": 1, "caught_up": caught_up},
    }


def _le(qty=10.0, *, opinion=False, stop=9.0, ledger=True):
    le: dict = {
        "position": {"quantity": qty, "original_quantity": qty, "avg_entry_price": 10.0,
                     "stop_price": stop, "high_water_mark": 10.3},
        "entry_filled_at_utc": T_ENTRY.isoformat(),
        # the [48] envelope `select_held_bbo` writes on the held tick (build B, merged)
        "last_held_execution_bbo": {"bbo_selector_version": "test", "bbo_source": "iqfeed_l1",
                                    "bbo_age_s": 0.12, "bbo_fallback_engaged": False},
        "deadman_stop": {"order_id": "dm-oid-1", "client_order_id": "chili_dm_21605_1_abc",
                         "stop_price": 8.98, "qty": qty, "phase": "submitted"},
    }
    if ledger:
        le["tape_cycle_state"] = _ledger()
    if opinion:
        le["opinion_exit_armed"] = {"reason": "breakout_failed_fast_bail",
                                    "at_utc": (T_ENTRY + timedelta(seconds=40)).isoformat(),
                                    "reasons": ["breakout_failed_fast_bail"],
                                    "prior_event": "live_bailout", "inputs": {}}
    return le


PROD = SimpleNamespace(base_increment=1.0, base_min_size=1.0)


def _tick(env: Env, le, *, seconds: float, bid=10.1, sess=None):
    env.now = T_ENTRY + timedelta(seconds=seconds)
    sess = sess or _sess()
    pos = le["position"]
    return lr._exit_verdict_tick(_DB, sess, le, as_of=env.now, bid=bid, ask=bid + 0.01, mid=bid + 0.005,
                                 qty=pos["quantity"], avg=pos["avg_entry_price"], stop_px=pos["stop_price"], prod=PROD)


def _after_submit(le, out):
    """What the tick's elif does right after an action: the pending exit is recorded and the
    exit seam owns the leg from here (the pending-exit branch runs before the elif)."""
    assert out["action"]
    le["pending_exit_reason"] = le["exit_verdict"]["exit"]["reason"]


def _prefill(t: FakeTape) -> None:
    """The base the tick deadman reads at the entry fill (prints BEFORE the fill; the batch
    reader is `observed_at > entry_at`, so these never enter the walk)."""
    t.add(-4.0, 9.90, aggressor=1)
    t.add(-3.0, 9.85, aggressor=1)
    t.add(-2.0, 9.95, aggressor=1)
    t.add(-1.0, 9.98, aggressor=1)


def _quiet_tape() -> FakeTape:
    """A leg that rises to 10.5 at +30 s and then prints a few buyer-held ticks."""
    t = FakeTape()
    _prefill(t)
    t.add(1.0, 10.0, aggressor=1)
    t.add(10.0, 10.2, aggressor=1)
    t.add(30.0, 10.5, aggressor=1)           # the leg high
    t.add(31.0, 10.48, 100, aggressor=1)
    t.add(32.0, 10.47, 100, aggressor=1)
    t.add(33.0, 10.49, 100, aggressor=1)
    t.add(34.0, 10.48, 100, aggressor=1)
    return t


def _spike_tape() -> FakeTape:
    """The measured shape: the spike right after the fill -- bought hard for 3 s, then sold
    quietly while the print is still ABOVE the entry. G rolls over; D does not (no buy share
    to fall from: the since-high prints are all sells)."""
    t = FakeTape()
    _prefill(t)
    t.add(1.0, 10.05, 300, aggressor=1)
    t.add(2.0, 10.20, 900, aggressor=1)
    t.add(3.0, 10.35, 1200, aggressor=1)     # the spike high
    return t


def _spike_sells(t: FakeTape) -> None:
    for i, px in enumerate([10.34, 10.32, 10.30, 10.29, 10.28, 10.27, 10.26, 10.25]):
        t.add(5.0 + i, px, 400 + 100 * min(i, 2), aggressor=-1)


# ── arming from the fill ───────────────────────────────────────────────────────

def test_the_first_held_tick_after_the_fill_arms_without_an_opinion(monkeypatch):
    tape = _quiet_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le()
    sess = _sess()
    assert lr._exit_verdict_active(sess, le) is True          # no `opinion_exit_armed` needed
    out = _tick(env, le, seconds=36.0, sess=sess)
    assert out["action"] is None and out["phase"] == "armed"
    assert sess.state == "live_entered"
    ev = le["exit_verdict"]
    assert ev["phase"] == "armed" and ev["entry_at"] == T_ENTRY.isoformat() and ev["entry_px"] == 10.0
    assert ev["leg_high"]["price"] == 10.5 and ev["leg_high"]["tie_rule"] == "first_occurrence"
    assert ev["prints_since_entry"] == 7 and ev["prints_since_high"] == 4
    assert {k: ev["deadman"][k] for k in ("level", "level_source", "base_as_of", "base_window_prints", "ratchets", "derivation")} == {"level": 9.85, "level_source": "cont_depth_p50", "base_as_of": T_ENTRY.isoformat(),
                             "base_window_prints": settings.chili_momentum_g4_reentry_tape_window_prints,
                             "ratchets": 0, "derivation": lr._TICK_DEADMAN_DERIVATION}
    # [65] the base read the ledger's continued-pullback depths; the count-half is context
    b = ev["deadman"]["base"]
    assert b["binding"] == "cont_depth_p50" and b["fallback_reason"] is None
    assert b["n_cycles"] == 3 and b["cont_depth_p50"] == pytest.approx(0.15)
    assert b["resting_stop"] == 9.0 and b["risk_R"] == pytest.approx(1.0)
    assert b["distance_R"] == pytest.approx(0.15) and b["ledger_lag_s"] == pytest.approx(-2.0)
    assert b["count_half_context"] == {"level": 9.85, "level_source": "swing_low_prev",
                                       "window_prints": settings.chili_momentum_g4_reentry_tape_window_prints,
                                       "binding": False}
    assert ev["deadman"]["ratchet"] == {"active": False, "binding": EV.TICK_DEADMAN_RATCHET_FALLBACK}
    assert ev["exit_fraction"] == 1.0
    armed = env.events("live_exit_verdict_armed")
    assert len(armed) == 1
    r = armed[0]
    assert r["n_since_high"] == 4 and r["min_prints"] == {"feature": 3, "binding": 4}
    assert r["window_s_binding"] is None and r["window_prints"] == settings.chili_momentum_g4_reentry_tape_window_prints
    assert ev["deadman"]["base_feature_geometry"]["split"] == "count"
    assert r["deadman"]["level"] == 9.85 and r["deadman"]["level_source"] == "cont_depth_p50"
    rb = r["deadman"]["base"]
    assert rb["base_source"] == "cont_depth_p50" and rb["binding"] == "cont_depth_p50"
    for key in ("cont_depth_p50", "n_cycles", "resting_stop", "distance_R", "depths", "ledger", "count_half_context"):
        assert key in rb, key
    assert r["deadman"]["ratchet"]["binding"] == EV.TICK_DEADMAN_RATCHET_FALLBACK
    assert r["exit_fraction"] == 1.0 and "+157.52" in r["exit_fraction_derivation"]
    assert r["trigger_order"] == ["tick_deadman", "accel_rollover", "since_high_verdict"]
    assert r["bbo_source"] == "iqfeed_l1" and r["bbo_age_s"] == 0.12 and r["bbo_fallback_engaged"] is False
    assert r["as_of"] == env.now.isoformat() and r["opinion_exit_armed"] is None
    assert r["derivation"] == lr._EXIT_VERDICT_DERIVATION
    assert r["seconds_since_entry"] == pytest.approx(36.0) and r["prints_since_entry"] == 7
    # the second tick: no second armed receipt; the frontier is the LAST WALKED PRINT
    _tick(env, le, seconds=39.0, sess=sess)
    assert len(env.events("live_exit_verdict_armed")) == 1
    last = tape.rows[-1]
    assert le["exit_verdict"]["frontier_at"] == last[5].isoformat() and le["exit_verdict"]["frontier_id"] == last[6]


def test_an_opinion_that_fires_is_a_receipt_beside_a_machine_already_running(monkeypatch):
    env = Env(monkeypatch, tape=_quiet_tape(), now=T_ENTRY + timedelta(seconds=36))
    sess = _sess()
    le = _le()
    _tick(env, le, seconds=36.0, sess=sess)
    assert le["exit_verdict"]["phase"] == "armed"
    assert lr._arm_opinion_exit(_DB, sess, le, reason="breakout_failed_fast_bail",
                                prior_event="live_bailout", inputs={"bid": 8.96}) is True
    r = env.events("live_opinion_exit_armed")[0]
    assert r["armed_exit"] == "exit_verdict_g_all" and r["superseded"] == "momentum_break_stop"
    out = _tick(env, le, seconds=39.0, sess=sess)
    assert out["action"] is None and out["receipt"]["opinion_exit_armed"]["reason"] == "breakout_failed_fast_bail"
    assert le["exit_verdict"]["phase"] == "armed"                # the opinion did not decide


def test_a_crypto_leg_is_named_unavailable_once_and_stays_on_the_fallback(monkeypatch):
    env = Env(monkeypatch, tape=FakeTape(), now=T_ENTRY + timedelta(seconds=40))
    sess = _sess(symbol="BTC-USD")
    le = _le()
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
    assert lr._exit_verdict_supported(sess, le) is False and lr._exit_verdict_active(sess, le) is False
    lr._exit_verdict_tick(_DB, sess, le, as_of=env.now, bid=1, ask=1, mid=1, qty=1, avg=1, stop_px=1)
    assert env.events("live_exit_verdict_unavailable")[0]["binding"] == "entry_fill_anchor_missing"


# ── G: the whole position at the accel rollover ────────────────────────────────

def test_full_exit_at_g_the_accel_rolls_over_while_the_print_is_above_entry(monkeypatch):
    tape = _spike_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0)
    out_a = _tick(env, le, seconds=4.0)                    # the spike is still being bought
    assert out_a["action"] is None and out_a["rollover"]["binding"] == "no_previous_evaluation"
    assert le["exit_verdict"]["accel_prev"] == out_a["accel_now"] > 0
    _spike_sells(tape)                                     # sold quietly, still above 10.0
    out = _tick(env, le, seconds=12.5, bid=10.24)
    assert out["action"] == "accel_rollover" and out["phase"] == "exit_pending"
    assert out["rollover"]["fired"] is True and out["rollover"]["binding"] == "rollover_above_entry"
    assert out["verdict"]["fired"] is False               # D did not fire: G was the earlier
    ev = le["exit_verdict"]
    assert ev["phase"] == "exit_pending"
    x = ev["exit"]
    assert x["trigger"] == "accel_rollover" and x["reason"] == "tape_accel_rollover" and x["cid_tag"] == "ta"
    assert x["accel_prev"] > 0 and x["accel_now"] <= 0 and x["exit_fraction"] == 1.0
    assert x["prints_since_entry"] == 11 and x["prints_since_high"] == 8 and x["bid"] == 10.24
    assert x["decided_as_of"] == env.now.isoformat() and x["binding"] == "rollover_above_entry"
    # the decision is durable BEFORE the caller submits (write-ahead)
    assert env.commits[-1]["exit_verdict"]["phase"] == "exit_pending"
    f = env.events("live_exit_verdict_fired")
    assert len(f) == 1
    r = f[0]
    assert r["trigger"] == "accel_rollover" and r["reason"] == "tape_accel_rollover"
    assert r["accel_prev"] > 0 and r["accel_now"] <= 0
    assert r["prints_since_entry"] == 11 and r["prints_since_high"] == 8
    assert r["bid"] == 10.24 and r["exit_fraction"] == 1.0 and "+157.52" in r["exit_fraction_derivation"]
    assert r["binding"] == "rollover_above_entry" and r["remaining_qty"] == 31.0
    assert r["last_print"] == 10.25 and r["entry_px"] == 10.0 and r["leg_high"]["price"] == 10.35
    assert r["level"] == 9.85 and r["phase"] == "exit_pending"
    assert r["bbo_source"] == "iqfeed_l1" and r["derivation"] == lr._EXIT_VERDICT_DERIVATION
    assert env.events("live_tick_deadman_exit") == []


def test_g_does_not_fire_below_the_entry_and_falls_through_to_d(monkeypatch):
    tape = _spike_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0)
    _tick(env, le, seconds=4.0)
    # the same rollover shape but the prints are BELOW the entry: not a spike sale
    for i, px in enumerate([9.99, 9.97, 9.96, 9.95, 9.94, 9.93, 9.92, 9.91]):
        tape.add(5.0 + i, px, 500, aggressor=-1)
    out = _tick(env, le, seconds=12.5, bid=9.90)
    assert out["rollover"]["fired"] is False and out["rollover"]["binding"] == "print_not_above_entry"
    assert out["action"] is None                            # D did not fire here either (no buys to lose)
    assert le["exit_verdict"]["phase"] == "armed"


# ── D: the whole position at the since-high verdict when G never fires ─────────

def _grind_tape() -> FakeTape:
    """A leg that grinds up on an even tape (prints <= 6 s apart: no halt-gap restriction),
    tops at 10.5 at +30 s and holds -- the N-print accel is FLAT (0), so G can never roll
    over from a positive previous evaluation; only D can decide the stall."""
    t = FakeTape()
    _prefill(t)
    for i, px in enumerate([10.00, 10.10, 10.20, 10.30, 10.40, 10.50]):
        t.add(1.0 + 6.0 * i + (0.0 if i == 0 else -1.0), px, 100, aggressor=1)   # +1, +6, ..., +30
    t.add(31.0, 10.48, 100, aggressor=1)
    t.add(32.0, 10.47, 100, aggressor=1)
    t.add(33.0, 10.49, 100, aggressor=1)
    t.add(34.0, 10.48, 100, aggressor=1)
    return t


def test_full_exit_at_d_when_g_never_fires(monkeypatch):
    tape = _grind_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0)
    out_a = _tick(env, le, seconds=34.5)
    assert out_a["action"] is None and out_a["accel_now"] <= 0    # never positive: G can never roll over
    tape.sellers_took_it(35.0, 10.45)
    out = _tick(env, le, seconds=37.0, bid=10.42)
    assert out["action"] == "since_high_verdict"
    assert out["rollover"]["fired"] is False and out["rollover"]["binding"] == "prev_not_positive"
    assert out["verdict"]["fired"] is True
    assert out["verdict"]["binding"] == "all_three" and out["n_since_high"] == 8
    x = le["exit_verdict"]["exit"]
    assert x["trigger"] == "since_high_verdict" and x["reason"] == "tape_sellers_took_it" and x["cid_tag"] == "tv"
    assert x["binding"] == "all_three" and x["exit_fraction"] == 1.0 and x["prints_since_high"] == 8
    r = env.events("live_exit_verdict_fired")[0]
    assert r["trigger"] == "since_high_verdict" and r["verdict"]["fired"] is True
    assert r["prints_since_high"] == 8 and r["prints_since_entry"] == 14 and r["bid"] == 10.42
    assert r["accel_prev"] is not None and r["accel_now"] is not None   # the pair is reported even for D
    assert r["exit_fraction"] == 1.0


# ── EARLIER-of semantics ───────────────────────────────────────────────────────

def test_when_g_and_d_both_fire_on_one_tick_g_is_the_trigger(monkeypatch):
    tape = _spike_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0)
    _tick(env, le, seconds=4.0)
    # the since-high prints: two small buys then sells with lower lows (D's shape) -- and the
    # N-print accel has rolled over too (G's shape). One tick, both true: G is the trigger.
    tape.add(5.0, 10.34, 50, aggressor=1)
    tape.add(6.0, 10.34, 50, aggressor=1)
    tape.add(7.0, 10.30, 100, aggressor=-1)
    tape.add(8.0, 10.28, 100, aggressor=-1)
    for i, px in enumerate([10.27, 10.26, 10.25, 10.24]):
        tape.add(9.0 + i, px, 600, aggressor=-1)
    # Establish D's counterfactual directly. Production must not perform this
    # independent DB read after G has already decided the same-tick winner.
    high = le["exit_verdict"]["leg_high"]
    rows = tape.leg_prints_since_high(
        "SKYQ", hi_at=high["observed_at"], hi_id=high["id"],
        as_of=T_ENTRY + timedelta(seconds=12.5),
    )
    assert lr._ev_since_high_verdict(rows, window_s=15, tick_rate_floor_pctile=0)["fired"]
    out = _tick(env, le, seconds=12.5)
    assert out["rollover"]["fired"] is True
    assert out["verdict"]["binding"] == "not_evaluated_rollover_precedence"
    assert out["n_since_high"] is None
    assert out["action"] == "accel_rollover"
    assert le["exit_verdict"]["exit"]["trigger"] == "accel_rollover"
    assert [p["trigger"] for p in env.events("live_exit_verdict_fired")] == ["accel_rollover"]


def test_when_d_fires_first_in_time_a_later_g_can_never_fire(monkeypatch):
    tape = FakeTape()
    _prefill(tape)
    tape.add(20.0, 10.50, 2000, aggressor=1)               # the high, bought hard (accel > 0 now)
    tape.sellers_took_it(21.0, 10.48)                      # ...but the since-high prints are weak
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0)
    out = _tick(env, le, seconds=23.0)                     # the FIRST pass: D can decide it
    assert out["rollover"]["binding"] == "no_previous_evaluation"    # G cannot exist yet
    assert out["action"] == "since_high_verdict"
    assert len(env.events("live_exit_verdict_armed")) == 1 and len(env.events("live_exit_verdict_fired")) == 1
    _after_submit(le, out)
    # later the acceleration rolls over: a G that would have fired -- the leg is already decided
    for i, px in enumerate([10.40, 10.38, 10.36, 10.34, 10.32, 10.30]):
        tape.add(24.0 + i, px, 700, aggressor=-1)
    out2 = _tick(env, le, seconds=30.0)
    assert out2 == {"action": None, "phase": "exit_pending"}
    assert len(env.events("live_exit_verdict_fired")) == 1
    assert le["exit_verdict"]["exit"]["trigger"] == "since_high_verdict"


# ── never a second exit ────────────────────────────────────────────────────────

def test_exit_pending_never_decides_again_and_the_frontier_stays(monkeypatch):
    tape = _spike_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0)
    _tick(env, le, seconds=4.0)
    _spike_sells(tape)
    out = _tick(env, le, seconds=12.5)
    assert out["action"] == "accel_rollover"
    _after_submit(le, out)
    frontier = (le["exit_verdict"]["frontier_at"], le["exit_verdict"]["frontier_id"])
    n_reads = len(tape.reads)
    # D fires, a print breaks the deadman level, the tape goes quiet: none of it is a decision
    tape.sellers_took_it(13.0, 10.24)
    tape.add(15.0, 9.50, 900, aggressor=-1)
    for s in (14.0, 15.5, 40.0):
        out = _tick(env, le, seconds=s)
        assert out == {"action": None, "phase": "exit_pending"}
    assert len(tape.reads) == n_reads                                  # no read at all
    assert (le["exit_verdict"]["frontier_at"], le["exit_verdict"]["frontier_id"]) == frontier
    assert len(env.events("live_exit_verdict_fired")) == 1
    assert env.events("live_tick_deadman_exit") == [] and env.events("live_tick_deadman_ratchet") == []


def test_a_cleared_pending_exit_with_shares_still_held_resubmits_the_same_decision(monkeypatch):
    tape = _spike_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0)
    _tick(env, le, seconds=4.0)
    _spike_sells(tape)
    out = _tick(env, le, seconds=12.5)
    _after_submit(le, out)
    # the seam cleared the pending exit without a fill (retry-cap reconcile that found shares)
    le.pop("pending_exit_reason", None)
    out = _tick(env, le, seconds=14.0)
    assert out["action"] == "resubmit" and out["phase"] == "exit_pending"
    assert out["exit"]["trigger"] == "accel_rollover" and out["exit"]["reason"] == "tape_accel_rollover"
    assert len(env.events("live_exit_verdict_fired")) == 1            # no new decision receipt
    # ...but not when the shares are gone (the fill reconciled the position away)
    le["position"]["quantity"] = 0.0
    assert _tick(env, le, seconds=15.0) == {"action": None, "phase": "exit_pending"}
    # ...and not while an exit order is recorded on the leg (the poll owns it)
    le["position"]["quantity"] = 31.0
    le["exit_order_id"] = "exit-1"
    assert _tick(env, le, seconds=16.0) == {"action": None, "phase": "exit_pending"}


# ── the tick deadman: first, per print, never withheld ─────────────────────────

def test_the_deadman_fires_before_the_trigger_when_a_print_breaks_the_level(monkeypatch):
    tape = _quiet_tape()
    crossing = tape.add(20.0, 9.80, 200, aggressor=-1)     # <= the 9.85 base, long before any G / D
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0)
    out = _tick(env, le, seconds=21.0, bid=9.79)
    assert out["action"] == "tick_deadman" and out["phase"] == "exit_pending"
    ev = le["exit_verdict"]
    assert ev["phase"] == "exit_pending"
    x = ev["exit"]
    assert x["trigger"] == "tick_deadman" and x["reason"] == "tick_deadman_stop" and x["cid_tag"] == "td"
    assert x["crossing_print"]["price"] == 9.80 and x["level"] == 9.85 and x["exit_fraction"] == 1.0
    # the walk STOPPED at the crossing print: it is the frontier, and only 3 prints were walked
    assert ev["frontier_at"] == crossing[5].isoformat() and ev["frontier_id"] == crossing[6]
    assert ev["prints_since_entry"] == 3 and ev["prints_since_high"] == 1
    r = env.events("live_tick_deadman_exit")[0]
    assert r["level"] == 9.85 and r["level_source"] == "cont_depth_p50"
    assert r["deadman_base"]["binding"] == "cont_depth_p50" and r["deadman_base"]["n_cycles"] == 3
    assert r["deadman_base"]["distance_R"] == pytest.approx(0.15)
    assert r["ratchet"]["binding"] == EV.TICK_DEADMAN_RATCHET_FALLBACK
    assert r["crossing_print"]["price"] == 9.80 and r["crossing_print"]["observed_at"] == crossing[5].isoformat()
    assert r["prints_scanned"] == 3 and r["batch_window"]["frontier_at"] == T_ENTRY.isoformat()
    assert r["resting_stop"] == 9.0 and r["remaining_qty"] == 31.0 and r["stale"] is False
    assert r["prints_since_entry"] == 3 and r["prints_since_high"] == 1 and r["exit_fraction"] == 1.0
    assert r["binding"] == "print_at_or_below_level" and r["trigger"] == "tick_deadman"
    assert env.events("live_exit_verdict_fired") == []
    # the ARMED receipt is not emitted on a tick that ended the leg in the walk
    assert env.events("live_exit_verdict_armed") == []


def test_the_deadman_wins_inside_a_batch_that_would_also_fire_g(monkeypatch):
    tape = _spike_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0)
    _tick(env, le, seconds=4.0)
    _spike_sells(tape)
    tape.add(12.2, 9.80, 900, aggressor=-1)                # the crossing print at the END of the batch
    out = _tick(env, le, seconds=12.5)
    assert out["action"] == "tick_deadman"
    assert "rollover" not in out                            # G was never evaluated: the walk decided
    assert env.events("live_exit_verdict_fired") == [] and len(env.events("live_tick_deadman_exit")) == 1


def test_a_crossing_print_inside_a_slow_tick_gap_exits_on_the_stale_tick_itself(monkeypatch):
    """Review of #1385 (major): stale is a do-not-DECIDE rule, never a do-not-WALK rule."""
    tape = _quiet_tape()
    tape.add(20.0, 9.80, 200, aggressor=-1)
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0)
    out = _tick(env, le, seconds=40.0)                      # 20 s after the last print > 7.5 s
    assert out["stale"] is True and out["action"] == "tick_deadman"
    assert out["exit_receipt"]["stale"] is True and out["exit_receipt"]["crossing_print"]["price"] == 9.80


# ── [65] no pre-trigger ratchet: the floor is the base until the trigger ───────

def _ratchet_tape() -> FakeTape:
    t = FakeTape()
    _prefill(t)
    for i, px in enumerate([10.00, 10.10, 10.20, 10.30, 10.50, 10.45, 10.42, 10.44, 10.46]):
        t.add(1.0 + i, px, 100, aggressor=1)               # 1-s cadence: no halt-gap restriction
    return t


def test_the_floor_never_ratchets_before_the_trigger_and_the_rolling_candidate_is_shadow(monkeypatch):
    """The tape that USED to ratchet the floor 9.85 -> 10.10 -> 10.42 (the count-half low of
    8 prints rising without a new high). [65]: the rolling count-half minimum is not a
    completed low -- the floor stays at the base, no `live_tick_deadman_ratchet` is emitted,
    the candidate is recorded on the evaluation receipt with the named fallback, and the
    print that used to end the leg (10.41 <= 10.42) no longer does."""
    monkeypatch.setattr(settings, "chili_momentum_g4_reentry_tape_window_prints", 8)
    tape = _ratchet_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0)
    out = _tick(env, le, seconds=9.5)
    assert out["action"] is None and le["exit_verdict"]["leg_high"]["price"] == 10.5
    dm = le["exit_verdict"]["deadman"]
    assert dm["level"] == 9.85 and dm["ratchets"] == 0 and dm["level_source"] == "cont_depth_p50"
    assert env.events("live_tick_deadman_ratchet") == []
    shadow = env.events("live_exit_evaluation")[-1]["observations"]["ratchet"]
    assert shadow["moved"] is False and shadow["binding"] == EV.TICK_DEADMAN_RATCHET_FALLBACK
    assert shadow["candidate"] == 10.10 and shadow["source_key"] == "swing_low_prev"   # what USED to bind
    assert shadow["level"] == 9.85 and shadow["completed_pivot_claim"] is False
    for i, px in enumerate([10.43, 10.41, 10.44, 10.47]):
        tape.add(10.0 + i, px, 100, aggressor=1)
    out = _tick(env, le, seconds=13.5)
    assert out["action"] is None and le["exit_verdict"]["deadman"]["level"] == 9.85
    assert env.events("live_exit_evaluation")[-1]["observations"]["ratchet"]["candidate"] == 10.42
    tape.add(15.0, 10.41, 100, aggressor=-1)             # at/below the OLD ratcheted 10.42
    out = _tick(env, le, seconds=15.5)
    assert out["action"] is None and le["exit_verdict"]["phase"] == "armed"
    assert env.events("live_tick_deadman_ratchet") == [] and env.events("live_tick_deadman_exit") == []
    # the floor still decides: the first print at or below the BASE ends the leg
    tape.add(16.0, 9.85, 300, aggressor=-1)
    out = _tick(env, le, seconds=16.5)
    assert out["action"] == "tick_deadman"
    assert out["exit_receipt"]["level"] == 9.85 and out["exit_receipt"]["ratchets"] == 0


def test_an_unreadable_count_half_read_is_context_only_the_ledger_base_still_binds(monkeypatch):
    tape = _quiet_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    real = tape.signed_tape_accel_features
    # the count-half read (as_of = the fill) answers None; the tick read still works
    monkeypatch.setattr(EG, "signed_tape_accel_features",
                        lambda symbol, **kw: None if kw.get("as_of") == T_ENTRY else real(symbol, **kw))
    le = _le(qty=31.0)
    _tick(env, le, seconds=36.0)
    dm = le["exit_verdict"]["deadman"]
    assert dm["initial_level_source"] == "cont_depth_p50" and dm["initial_level"] == 9.85
    assert dm["base"]["count_half_context"]["level_source"] == "resting_stop"
    assert dm["level"] == dm["initial_level"]                  # never ratcheted


@pytest.mark.parametrize("ledger,reason", [
    (None, "no_tape_cycle_state"),
    ("other_day", "tape_cycle_ledger_other_day"),
    ("partial", "tape_cycle_ledger_not_caught_up"),
    ("empty", "no_completed_cycles"),
])
def test_without_a_usable_ledger_the_base_is_the_named_resting_stop_fallback(monkeypatch, ledger, reason):
    tape = _quiet_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0, ledger=False)
    if ledger == "other_day":
        le["tape_cycle_state"] = {**_ledger(), "day": "2026-09-09"}      # yesterday's ledger
    elif ledger == "partial":
        le["tape_cycle_state"] = _ledger(caught_up=False)
    elif ledger == "empty":
        le["tape_cycle_state"] = _ledger(cycles=())
    _tick(env, le, seconds=36.0)
    dm = le["exit_verdict"]["deadman"]
    assert dm["level"] == 9.0 and dm["level_source"] == "resting_stop"
    assert dm["base"]["fallback_reason"] == reason and dm["base"]["binding"] == "named_fallback_resting_stop"
    assert dm["base"]["count_half_context"]["level"] == 9.85          # reported, never binding
    armed = env.events("live_exit_verdict_armed")[0]["deadman"]["base"]
    assert armed["fallback_reason"] == reason and armed["base_source"] == "resting_stop"
    # 9.80 is below the old count-half base (9.85) and above the resting stop: no exit
    tape.add(37.0, 9.80, 200, aggressor=-1)
    assert _tick(env, le, seconds=38.0)["action"] is None


def test_the_ledger_base_binds_where_the_old_count_half_base_would_have_stopped_the_leg(monkeypatch):
    """Today's shape: the micro-pullback right after the fill prints through the count-half
    low (9.85) but stays inside the day's CONTINUED pullback depth. The ledger here has depths
    0.30 / 0.35 / 0.40 => base 10.0 - 0.35 = 9.65: the 9.80 print is walked, the leg holds; a
    print at the base ends it."""
    tape = _quiet_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0, ledger=False)
    le["tape_cycle_state"] = _ledger(cycles=(
        {"k": 0, "hi": 10.60, "pb_low": 10.30}, {"k": 1, "hi": 10.90, "pb_low": 10.55},
        {"k": 2, "hi": 11.20, "pb_low": 10.80},
    ))
    tape.add(20.0, 9.80, 200, aggressor=-1)                    # <= 9.85 (old), > 9.65 (new)
    out = _tick(env, le, seconds=21.0)
    assert out["action"] is None
    dm = le["exit_verdict"]["deadman"]
    assert dm["level"] == pytest.approx(9.65) and dm["level_source"] == "cont_depth_p50"
    assert dm["base"]["count_half_context"]["level"] == 9.85
    assert dm["base"]["distance_R"] == pytest.approx(0.35)
    tape.add(22.0, 9.64, 200, aggressor=-1)
    out = _tick(env, le, seconds=23.0)
    assert out["action"] == "tick_deadman" and out["exit_receipt"]["level"] == pytest.approx(9.65)
    assert out["exit_receipt"]["deadman_base"]["cont_depth_p50"] == pytest.approx(0.35)


def test_the_expected_ledger_day_is_the_fills_session_day_in_the_feeds_own_key():
    """The same key `_feed_tape_cycle_state` writes: 04:00 ET of the ET date, as a UTC date."""
    from datetime import timezone

    assert lr._tape_cycle_day_key_at(T_ENTRY) == "2026-09-10"                     # 10:00 ET
    assert lr._tape_cycle_day_key_at(datetime(2026, 9, 10, 7, 59)) == "2026-09-09"    # 03:59 ET
    assert lr._tape_cycle_day_key_at(datetime(2026, 9, 10, 8, 0)) == "2026-09-10"     # 04:00 ET
    assert lr._tape_cycle_day_key_at(datetime(2026, 9, 10, 23, 59, tzinfo=timezone.utc)) == "2026-09-10"
    assert lr._tape_cycle_day_key_at(None) is None


def test_a_resting_stop_above_the_continued_depth_is_the_binding_floor(monkeypatch):
    tape = _quiet_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0, stop=9.9, ledger=False)               # position stop 9.9 > 10.0 - 0.35
    le["tape_cycle_state"] = _ledger(cycles=(
        {"k": 0, "hi": 10.60, "pb_low": 10.30}, {"k": 1, "hi": 10.90, "pb_low": 10.55},
        {"k": 2, "hi": 11.20, "pb_low": 10.80},
    ))
    _tick(env, le, seconds=36.0)
    b = le["exit_verdict"]["deadman"]["base"]
    assert le["exit_verdict"]["deadman"]["level"] == 9.9
    assert b["binding"] == "resting_stop_at_or_above_cont_depth_p50" and b["fallback_reason"] is None
    assert b["cont_candidate"] == pytest.approx(9.65) and b["distance_R"] == pytest.approx(1.0)


def test_the_base_read_at_the_fill_is_delivery_bounded_by_the_tick(monkeypatch):
    tape = _quiet_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0)
    _tick(env, le, seconds=36.0)
    base_reads = [p for k, p in tape.reads if k == "feats" and p.get("as_of") == T_ENTRY]
    assert len(base_reads) == 1
    assert base_reads[0]["available_by"] == T_ENTRY + timedelta(seconds=36.0)
    assert base_reads[0]["window_prints"] == settings.chili_momentum_g4_reentry_tape_window_prints
    # the tick read (G + ratchet) is at the tick itself
    tick_reads = [p for k, p in tape.reads if k == "feats" and p.get("as_of") == env.now]
    assert len(tick_reads) == 1 and "available_by" not in tick_reads[0]


# ── stale: withholds G and D, keeps accel_prev, never the walk ─────────────────

def test_a_stale_tape_withholds_g_and_keeps_the_previous_accel_for_the_next_fresh_tick(monkeypatch):
    tape = _spike_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0)
    _tick(env, le, seconds=4.0)
    prev = le["exit_verdict"]["accel_prev"]
    assert prev > 0
    _spike_sells(tape)                                     # last print at +12 s
    out = _tick(env, le, seconds=28.0)                     # 16 s > independent 14.69 s: stale
    assert out["stale"] is True and out["action"] is None
    assert out["rollover"]["condition_fired_before_freshness"] is True and out["withheld"] == "stale_tape"
    assert le["exit_verdict"]["phase"] == "armed"
    assert le["exit_verdict"]["accel_prev"] == prev         # NOT advanced on a withheld tick
    u = env.events("live_exit_verdict_unreadable")
    assert len(u) == 1 and u[0]["why"] == "stale_tape" and u[0]["walks_and_executions_continue"] is True
    assert u[0]["stale_tape_bound_s"] == 14.69 and u[0]["tape_frontier_age_s"] == pytest.approx(16.0)
    _tick(env, le, seconds=28.5)
    assert len(env.events("live_exit_verdict_unreadable")) == 1    # on change only
    # the walk still ran on the stale ticks: the frontier is the last sell print
    assert le["exit_verdict"]["frontier_at"] == tape.rows[-1][5].isoformat()
    # the tape speaks again. The feature's own halt-gap rule restarts its window at the
    # discontinuity, so ONE post-gap print is below the feature floor (accel missing, no
    # decision); at three prints the window exists again and the rollover that happened
    # before the quiet decides -- because `accel_prev` (> 0) was never advanced meanwhile.
    tape.add(29.0, 10.24, 600, aggressor=-1)
    out = _tick(env, le, seconds=29.2)
    assert out["stale"] is False and out["action"] is None
    assert out["rollover"]["binding"] == "accel_missing" and le["exit_verdict"]["accel_prev"] == prev
    tape.add(29.3, 10.24, 600, aggressor=-1)
    tape.add(29.6, 10.23, 600, aggressor=-1)
    out = _tick(env, le, seconds=30.0)
    assert out["stale"] is False and out["action"] == "accel_rollover"
    assert out["rollover"]["accel_prev"] == prev and out["rollover"]["accel_now"] <= 0
    assert le["exit_verdict"]["exit"]["accel_prev"] == prev


def test_a_stale_tape_withholds_d_too(monkeypatch):
    tape = _quiet_tape()
    tape.sellers_took_it(35.0, 10.45)                     # last print at +36.5 s
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0)
    out = _tick(env, le, seconds=52.0)                    # 15.5 s > independent 14.69 s
    assert out["stale"] is True and out["action"] is None
    assert out["verdict"]["fired"] is True and out["withheld"] == "stale_tape"
    assert le["exit_verdict"]["phase"] == "armed"
    tape.sellers_took_it(54.0, 10.44)
    out = _tick(env, le, seconds=56.0)
    assert out["stale"] is False and out["action"] == "since_high_verdict"


# ── the frontier: the LAST WALKED print, never the tick's as_of ────────────────

def test_an_unreadable_batch_never_advances_the_frontier_and_nothing_is_skipped(monkeypatch):
    tape = _quiet_tape()
    tape.add(20.0, 9.80, 200, aggressor=-1)               # the crossing print the timeout would hide
    tape.fail_with = {"why": "timeout", "error": "OperationalError"}
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0)
    out = _tick(env, le, seconds=21.0)
    assert out == {"action": None, "unreadable": "timeout"}
    ev = le["exit_verdict"]
    assert ev["frontier_at"] == T_ENTRY.isoformat() and ev["frontier_id"] is None   # untouched
    assert ev["prints_since_entry"] == 0
    u = env.events("live_exit_verdict_unreadable")
    assert len(u) == 1 and u[0]["why"] == "timeout" and u[0]["error"] == "OperationalError"
    _tick(env, le, seconds=24.0)
    assert len(env.events("live_exit_verdict_unreadable")) == 1     # on change only
    assert le["exit_verdict"]["frontier_at"] == T_ENTRY.isoformat()
    # the read recovers: the WHOLE gap is walked, the hidden crossing print decides
    tape.fail_with = None
    out = _tick(env, le, seconds=27.0)
    assert out["action"] == "tick_deadman" and out["exit_receipt"]["prints_scanned"] == 3
    assert out["exit_receipt"]["batch_window"]["frontier_at"] == T_ENTRY.isoformat()


def test_the_frontier_is_the_last_walked_print_so_a_late_print_is_still_walked(monkeypatch):
    tape = _quiet_tape()                                   # last print at +34 s
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0)
    _tick(env, le, seconds=36.0)
    last = tape.rows[-1]
    ev = le["exit_verdict"]
    assert ev["frontier_at"] == last[5].isoformat() and ev["frontier_id"] == last[6]
    assert ev["frontier_at"] != env.now.isoformat()
    # a print observed at +35 s (before the previous as_of) but delivered after that tick:
    # with the frontier at the last WALKED print it is read; at as_of it would be lost
    late = tape.add(35.0, 9.80, 200, aggressor=-1)
    out = _tick(env, le, seconds=38.0)
    assert out["action"] == "tick_deadman" and out["exit_receipt"]["crossing_print"]["id"] == late[6]
    reads = [p for k, p in tape.reads if k == "between"]
    assert reads[-1]["after"] == last[5].isoformat() and reads[-1]["after_id"] == last[6]


def test_the_since_high_read_failing_after_the_walk_is_unreadable_but_the_walk_stands(monkeypatch):
    tape = _quiet_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0)
    real = tape.leg_prints_since_high

    def _fail(symbol, *, err=None, **kw):
        if isinstance(err, dict):
            err.update({"why": "timeout", "error": "OperationalError"})
        return None

    monkeypatch.setattr(EG, "leg_prints_since_high", _fail)
    out = _tick(env, le, seconds=36.0)
    assert out["action"] is None and out["unreadable"] == "timeout"
    assert out["verdict"]["binding"] == "since_high_unreadable"
    ev = le["exit_verdict"]
    assert ev["prints_since_entry"] == 7 and ev["leg_high"]["price"] == 10.5     # the walk ran
    assert ev["frontier_at"] == tape.rows[-1][5].isoformat()                     # past WALKED prints only
    # a second failing tick: the SAME unreadable, receipt on change only (not every tick)
    out = _tick(env, le, seconds=37.5)
    assert out["action"] is None and out["unreadable"] == "timeout"
    assert len(env.events("live_exit_verdict_unreadable")) == 1
    monkeypatch.setattr(EG, "leg_prints_since_high", real)
    out = _tick(env, le, seconds=39.0)
    assert out["action"] is None and out["n_batch"] == 0 and "unreadable" not in out
    assert "unreadable_why" not in le["exit_verdict"]                            # cleared once the reads succeed


# ── the marker, the receipts, the recycle ──────────────────────────────────────

def test_recycle_clears_the_marker_and_the_two_fixed_in_passing_families():
    for key in ("exit_verdict", "bailout_breach_pending_utc", "bailout_breach_trigger",
                "fpb_bucket", "fpb_fire", "failed_pop_break_dbg", "opinion_exit_armed"):
        assert key in lr._RECYCLE_ENTRY_STATE_KEYS, key


def test_the_exit_verdict_receipt_summarises_the_marker_and_fails_open(monkeypatch):
    assert lr._exit_verdict_receipt({}) is None
    tape = _spike_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0, opinion=True)
    _tick(env, le, seconds=4.0)
    _spike_sells(tape)
    _tick(env, le, seconds=12.5)
    r = lr._exit_verdict_receipt(le)
    assert r["phase"] == "exit_pending" and r["armed_reason"] == "breakout_failed_fast_bail"
    assert r["deadman"]["level"] == 9.85 and r["deadman"]["ratchets"] == 0
    assert r["exit"]["trigger"] == "accel_rollover" and r["exit"]["reason"] == "tape_accel_rollover"
    assert r["exit"]["accel_prev"] > 0 and r["exit"]["accel_now"] <= 0
    assert r["last_rollover"]["fired"] is True and r["last_verdict"]["fired"] is False
    assert r["exit_fraction"] == 1.0 and r["prints_since_entry"] == 11 and r["entry_px"] == 10.0
    assert lr._exit_verdict_receipt({"exit_verdict": {"phase": "armed", "last": "junk"}})["phase"] == "armed"


def test_illegal_edges_raise_at_the_writer():
    with pytest.raises(ValueError):
        lr._ev_assert_transition("exit_pending", "armed")
    with pytest.raises(ValueError):
        lr._ev_assert_transition("armed", "runner")
    with pytest.raises(ValueError):
        lr._ev_assert_transition("exited", "armed")

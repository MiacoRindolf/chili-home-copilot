"""[10] NCRA handoff tail -- the EXIT-TAIL RECEIPT (2026-09-11).

ANG SUGAT. Sa NCRA 07-29 ang buong -$133.20 ng doctrine arm ay nasa BUNTOT pagkatapos ng
desisyon (-$59.20 sa 5.296 s na confirm dwell + -$96.20 sa 6.17 s na bailout-to-fill), at ang
automated exit path ay WALANG resibo ng sandaling tinanggap ng broker ang order
(`live_exit_submitted` = operator flatten lang). Kaya ang buntot ay nasusukat lang sa lateral join.
Baseline (14 d live, scratchpad t10b_tail_baseline.sql): 09-11 tape exits n=22 dec->fill p50
21.16 s / p90 26.00 s, dec->freeze p50 0.83 s, 23 id_lost, tail -$57.78 (21 priced).

ANG RESIBO (walang bagong threshold -- aritmetika lang sa mga stamp):
  1. ang DESISYON, set-once kada exit episode, sa unang pasok sa `_submit_live_market_exit`;
  2. `live_exit_order_posted` sa sandaling TINANGGAP ng broker (ok + order_id);
  3. `exit_tail` sa `live_exit_filled` / `live_partial_exit_filled`.

Runnable: pytest tests/test_exit_tail_receipt.py -v
"""
from __future__ import annotations

import ast
import pathlib
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.trading.execution_family_registry import EXECUTION_FAMILY_ROBINHOOD_SPOT
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural import pending_partial_retirement as retirement

_SRC = pathlib.Path(lr.__file__)
T0 = datetime(2026, 9, 11, 13, 40, 0)


class _Clock:
    def __init__(self, at: datetime):
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at = self.at + timedelta(seconds=seconds)


class _FakeAdapter:
    """A non-Alpaca venue: the REAL impl posts to it (no deadman handoff on this family)."""

    def __init__(self, *, order_ids=("lim_1", "lim_2", "lim_3")):
        self.calls: list[tuple[str, dict]] = []
        self._ids = list(order_ids)

    def get_position_quantity(self, product_id):
        return 1000.0

    def _next(self, kind, kwargs):
        self.calls.append((kind, kwargs))
        oid = self._ids.pop(0) if self._ids else None
        return {"ok": True, "order_id": oid, "client_order_id": kwargs.get("client_order_id")}

    def place_limit_order_gtc(self, **kwargs):
        return self._next("limit", kwargs)

    def place_market_order(self, **kwargs):
        return self._next("market", kwargs)


@pytest.fixture
def seam(monkeypatch):
    """The real `_submit_live_market_exit` on a robinhood_spot session, DB collaborators stubbed,
    a controllable clock, every emit and commit recorded."""
    clock = _Clock(T0)
    rec = {"emits": [], "commits": 0}

    def _emit(db, sess, ev, payload=None):
        rec["emits"].append((ev, dict(payload or {})))

    def _commit(sess, le):
        rec["commits"] += 1

    monkeypatch.setattr(lr, "_utcnow", clock)
    monkeypatch.setattr(lr, "_emit", _emit)
    monkeypatch.setattr(lr, "_commit_le", _commit)
    monkeypatch.setattr(lr, "_record_live_exit_intent_safe", lambda *a, **k: None)
    monkeypatch.setattr(
        lr, "_cancel_scale_limit_and_clamp",
        lambda db, sess, adapter, *, le, requested_qty, reason: requested_qty,
    )
    monkeypatch.setattr(lr, "_schedule_exit_continuation", lambda *a, **k: False)
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.market_profile.market_session_now",
        lambda *_a, **_k: "regular",
    )
    sess = SimpleNamespace(id=4101, symbol="NCRA", execution_family=EXECUTION_FAMILY_ROBINHOOD_SPOT,
                           correlation_id="t10")
    adapter = _FakeAdapter()

    def submit(le, *, bid, reason="breakout_failed_fast_bail", qty=100.0):
        return lr._submit_live_market_exit(
            None, sess, adapter, le=le, product_id="NCRA", quantity=qty,
            client_order_id=f"chili_ml_b_{sess.id}_{uuid.uuid4().hex[:12]}",
            reason=reason, bid=bid, ask=bid + 0.02, mid=bid + 0.01,
        )

    return SimpleNamespace(clock=clock, rec=rec, sess=sess, adapter=adapter, submit=submit)


def _long_le(**extra) -> dict:
    le = {"side_long": True,
          "position": {"product_id": "NCRA", "side": "long", "quantity": 100.0,
                       "avg_entry_price": 2.60, "stop_price": 2.40}}
    le.update(extra)
    return le


def _posted(rec) -> list[dict]:
    return [p for e, p in rec["emits"] if e == "live_exit_order_posted"]


# ── 1. the decision: set once per episode ─────────────────────────────────────────────


def test_the_decision_is_stamped_once_and_a_later_pass_does_not_move_it(monkeypatch):
    clock = _Clock(T0)
    monkeypatch.setattr(lr, "_utcnow", clock)
    le: dict = {}
    assert lr._exit_tail_stamp_decision(le, reason="breakout_failed_fast_bail", bid=2.56) is True
    clock.advance(5.296)
    assert lr._exit_tail_stamp_decision(le, reason="bailout", bid=2.48) is False
    assert le[lr._EXIT_TAIL_DECIDED_AT_KEY] == T0.isoformat()
    assert le[lr._EXIT_TAIL_DECIDED_BID_KEY] == 2.56
    assert le[lr._EXIT_TAIL_DECIDED_REASON_KEY] == "breakout_failed_fast_bail"


def test_a_backoff_defer_stamps_the_decision_and_commits_it_once(seam):
    """`exit_retry_backoff` returns from the impl before any commit: the stamp must still be
    durable, and the seam still commits ONCE (merged with the proof invalidation)."""
    le = _long_le(exit_next_retry_at_utc=(T0 + timedelta(seconds=5)).isoformat())
    out = seam.submit(le, bid=2.56)
    assert out["error"] == "exit_retry_backoff" and out["deferred"] is True
    assert le[lr._EXIT_TAIL_DECIDED_AT_KEY] == T0.isoformat()
    assert le[lr._EXIT_TAIL_DECIDED_BID_KEY] == 2.56
    assert seam.rec["commits"] == 1
    assert _posted(seam.rec) == [] and seam.adapter.calls == []


# ── 2. live_exit_order_posted: the broker's acceptance, with the tail so far ───────────


def test_the_post_receipt_measures_decision_to_submit_from_the_first_decision(seam):
    le = _long_le(exit_next_retry_at_utc=(T0 + timedelta(seconds=5)).isoformat())
    seam.submit(le, bid=2.56)                    # T0: decided, deferred (backoff)
    seam.clock.advance(6.17)
    out = seam.submit(le, bid=2.50)              # T0+6.17: the real impl posts
    assert out["ok"] is True and out["order_id"] == "lim_1"
    assert len(seam.adapter.calls) == 1
    posted = _posted(seam.rec)
    assert len(posted) == 1
    p = posted[0]
    assert p["decided_at_utc"] == T0.isoformat()
    assert p["decided_reason"] == "breakout_failed_fast_bail"
    assert p["bid_at_decision"] == 2.56
    assert p["bid_at_post"] == 2.50
    assert p["submitted_at_utc"] == (T0 + timedelta(seconds=6.17)).isoformat()
    assert p["decision_to_submit_s"] == 6.17
    assert p["decision_to_attempt_s"] == 6.17 and p["attempt_to_post_s"] == 0.0
    assert p["order_id"] == "lim_1" and p["order_type"] == "limit"
    assert p["limit_price"] is not None and p["limit_price"] <= 2.50
    assert p["first_post"] is True and p["n_posts"] == 1
    assert p["binding"] == "decision_stamp"
    assert p["via_handoff_recovery"] is False
    # the set-once stamps did not move; the first post is kept for the fill receipt
    assert le[lr._EXIT_TAIL_DECIDED_AT_KEY] == T0.isoformat()
    assert le[lr._EXIT_TAIL_FIRST_POSTED_KEY]["order_id"] == "lim_1"
    # the impl's own (last-post) stamp is untouched and agrees on this single post
    assert le["pending_exit_submitted_at_utc"] == (T0 + timedelta(seconds=6.17)).isoformat()


def test_a_repeg_counts_a_post_and_keeps_the_first_submitted_at(seam):
    le = _long_le()
    seam.submit(le, bid=2.56)                    # T0: decided + posted (no backoff)
    first_at = le[lr._EXIT_TAIL_FIRST_POSTED_KEY]["at_utc"]
    le.pop("exit_order_id", None)                # the poll cancelled the unfilled limit
    le.pop("exit_next_retry_at_utc", None)
    seam.clock.advance(3.0)
    seam.submit(le, bid=2.52)                    # the repeg re-enters the seam
    posted = _posted(seam.rec)
    assert [p["first_post"] for p in posted] == [True, False]
    assert posted[1]["n_posts"] == 2
    assert posted[1]["decision_to_submit_s"] == 3.0     # this acceptance, from the decision
    assert le[lr._EXIT_TAIL_FIRST_POSTED_KEY]["at_utc"] == first_at
    assert le[lr._EXIT_TAIL_DECIDED_AT_KEY] == T0.isoformat()


def test_an_ok_without_a_broker_order_id_is_not_a_post(seam):
    """That shape is the `missing_exit_order_id` failure `_live_exit_submit_succeeded` already
    records; the receipt must not call it an acceptance."""
    seam.adapter._ids = [None]
    le = _long_le()
    out = seam.submit(le, bid=2.56)
    assert out["ok"] is True and not out.get("order_id")
    assert _posted(seam.rec) == []
    assert lr._EXIT_TAIL_FIRST_POSTED_KEY not in le
    assert le[lr._EXIT_TAIL_DECIDED_AT_KEY] == T0.isoformat()   # the decision still counts


# ── 3. the fill: exit_tail, then the episode ends ────────────────────────────────────


@pytest.fixture
def completers(monkeypatch):
    rec = {"emits": [], "transitions": []}
    monkeypatch.setattr(lr, "_emit", lambda db, sess, ev, payload=None: rec["emits"].append((ev, dict(payload or {}))))
    monkeypatch.setattr(lr, "_commit_le", lambda sess, le: None)
    monkeypatch.setattr(lr, "_safe_transition", lambda db, sess, st: rec["transitions"].append(st))
    monkeypatch.setattr(lr, "_record_live_exit_ledger_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_record_live_partial_exit_ledger_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_record_fill_outcome_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_finalize_live_decision_after_exit", lambda *a, **k: None)
    return rec


def _episode_le(clock: _Clock) -> dict:
    """NCRA 07-29 shape: decided at bid 2.56, first accepted 5.296 s later, a freeze + a
    handback on the way, one literal-BBO block."""
    le = _long_le()
    le[lr._EXIT_TAIL_DECIDED_AT_KEY] = clock.at.isoformat()
    le[lr._EXIT_TAIL_DECIDED_BID_KEY] = 2.56
    le[lr._EXIT_TAIL_DECIDED_REASON_KEY] = "breakout_failed_fast_bail"
    posted_at = clock.at + timedelta(seconds=5.296)
    le[lr._EXIT_TAIL_FIRST_POSTED_KEY] = {"at_utc": posted_at.isoformat(), "order_id": "o-1"}
    le[lr._EXIT_TAIL_N_POSTS_KEY] = 1
    le[lr._EXIT_TAIL_N_RELEASE_BLOCKED_KEY] = 1
    le[lr._EXIT_TAIL_N_HANDBACK_KEY] = 1
    le[lr._EXIT_TAIL_N_LITERAL_BBO_BLOCKED_KEY] = 1
    le["pending_exit_reason"] = "bailout"
    le["pending_exit_submitted_at_utc"] = posted_at.isoformat()
    le["exit_order_id"] = "o-1"
    return le


def test_the_full_fill_carries_the_tail_and_ends_the_episode(monkeypatch, completers):
    clock = _Clock(T0)
    monkeypatch.setattr(lr, "_utcnow", clock)
    le = _episode_le(clock)
    clock.advance(11.466)                                  # 5.296 s + 6.17 s
    sess = SimpleNamespace(id=4101, symbol="NCRA", execution_family=EXECUTION_FAMILY_ROBINHOOD_SPOT)
    lr._complete_confirmed_live_exit(
        None, sess, le=le, quantity=100.0, entry_price=2.60, fill_price=2.48,
        reason="bailout", slip_bps=6.0,
    )
    filled = [p for e, p in completers["emits"] if e == "live_exit_filled"]
    assert len(filled) == 1
    tail = filled[0]["exit_tail"]
    assert tail["decided_at_utc"] == T0.isoformat()
    assert tail["decided_reason"] == "breakout_failed_fast_bail"
    assert tail["bid_at_decision"] == 2.56
    assert tail["submitted_at_source"] == "live_exit_order_posted"
    assert tail["decision_to_submit_s"] == 5.296
    assert tail["submit_to_fill_s"] == 6.17
    assert tail["decision_to_fill_s"] == 11.466
    assert tail["tail_usd"] == pytest.approx((2.48 - 2.56) * 100.0)      # -8.00
    assert tail["tail_binding"] == "(fill_price - bid_at_decision) * quantity"
    assert (tail["n_posts"], tail["n_release_blocked"], tail["n_pre_place_handback"],
            tail["n_literal_bbo_blocked"]) == (1, 1, 1, 1)
    assert tail["binding"] == "decision_stamp"
    for key in lr._EXIT_TAIL_KEYS:
        assert key not in le, key                         # the episode is over


def test_an_exit_posted_before_this_receipt_names_its_fallback_clock(monkeypatch, completers):
    """A leg that was in flight at the deploy has no decision stamp and no first post: the
    receipt says so instead of inventing a number."""
    clock = _Clock(T0)
    monkeypatch.setattr(lr, "_utcnow", clock)
    le = _long_le(pending_exit_reason="stop", pending_exit_submitted_at_utc=T0.isoformat())
    clock.advance(4.0)
    tail = lr._exit_tail_receipt(le, fill_price=2.50, quantity=100.0)
    assert tail["binding"] == "no_decision_stamp"
    assert tail["submitted_at_source"] == "pending_exit_submitted_at_utc_named_fallback"
    assert tail["submit_to_fill_s"] == 4.0
    assert tail["decision_to_submit_s"] is None and tail["decision_to_fill_s"] is None
    assert tail["tail_usd"] is None and tail["tail_binding"] == "bid_at_decision_missing"


def test_a_short_leg_is_not_priced_against_the_wrong_side(monkeypatch):
    monkeypatch.setattr(lr, "_utcnow", _Clock(T0))
    le = {"side_long": False, "position": {"side": "short", "quantity": 10.0, "avg_entry_price": 5.0},
          lr._EXIT_TAIL_DECIDED_AT_KEY: T0.isoformat(), lr._EXIT_TAIL_DECIDED_BID_KEY: 5.10}
    tail = lr._exit_tail_receipt(le, fill_price=5.20, quantity=10.0)
    assert tail["tail_usd"] is None and tail["tail_binding"] == "short_side_not_measured"


def test_a_partial_of_a_whole_exit_keeps_the_episode_and_a_scale_out_ends_it(monkeypatch, completers):
    clock = _Clock(T0)
    monkeypatch.setattr(lr, "_utcnow", clock)
    le = _episode_le(clock)
    clock.advance(8.0)
    sess = SimpleNamespace(id=4101, symbol="NCRA", execution_family=EXECUTION_FAMILY_ROBINHOOD_SPOT)
    lr._apply_confirmed_live_partial_exit(
        None, sess, le=le, filled_quantity=40.0, entry_price=2.60, fill_price=2.50, reason="bailout",
    )
    partial = [p for e, p in completers["emits"] if e == "live_partial_exit_filled"]
    assert len(partial) == 1
    tail = partial[0]["exit_tail"]
    assert tail["quantity"] == 40.0
    assert tail["tail_usd"] == pytest.approx((2.50 - 2.56) * 40.0)
    assert tail["decision_to_fill_s"] == 8.0
    # the remainder is the SAME decision: the stamp survives for the final fill
    assert le[lr._EXIT_TAIL_DECIDED_AT_KEY] == T0.isoformat()
    assert "pending_exit_submitted_at_utc" not in le

    # a deliberate scale-out leaves a HELD runner: that ends the episode
    le["pending_exit_is_scale_out"] = True
    lr._scale_out_to_runner(
        None, sess, le=le, filled_quantity=20.0, entry_price=2.60, fill_price=2.70, reason="target",
    )
    for key in lr._EXIT_TAIL_KEYS:
        assert key not in le, key


# ── 4. the handoff counters ─────────────────────────────────────────────────────────


def test_the_handback_counts_a_hop_and_keeps_the_decision(monkeypatch):
    from tests.test_exit_pre_place_handback import _frozen_le, _patch, _proof, _sess

    _patch(monkeypatch)
    le = _frozen_le()
    le[lr._EXIT_PRE_PLACE_PROOF_KEY] = _proof()
    le[lr._EXIT_TAIL_DECIDED_AT_KEY] = T0.isoformat()
    out = lr._poll_live_exit_fill(None, _sess(), None, le=le, reason="tape_accel_rollover", quantity=400.0)
    assert out["why"] == "pre_place_handback"
    assert le[lr._EXIT_TAIL_N_HANDBACK_KEY] == 1
    assert le[lr._EXIT_TAIL_DECIDED_AT_KEY] == T0.isoformat()


def test_the_real_phase_one_freeze_counts_a_release_block_and_stamps_the_decision(db, monkeypatch):
    """The shipped Alpaca path: the verdict's whole exit enters the seam, the deadman handoff
    freezes phase 1 (`live_deadman_stop_release_blocked`), nothing is posted yet. The decision
    is stamped at the decision tick's bid and the freeze is counted; pulse 2 does not move it."""
    from tests.test_exit_verdict_g_whole_exit_seam import (
        _decided_marker, _resting_deadman_session, _submit,
    )
    from tests.test_momentum_emergency_exit_recovery import TEST_ALPACA_ACCOUNT_ID

    clock = _Clock(datetime(2026, 9, 10, 14, 0, 41))
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_alpaca_expected_account_id", TEST_ALPACA_ACCOUNT_ID, raising=False)
    monkeypatch.setattr(lr, "_venue_broker_connected", lambda _family: True)
    monkeypatch.setattr(lr, "_record_live_exit_ledger_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_record_live_partial_exit_ledger_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_record_fill_outcome_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_record_live_exit_intent_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_utcnow", clock)
    emits: list[tuple[str, dict]] = []
    monkeypatch.setattr(lr, "_emit", lambda db, sess, ev, payload: emits.append((ev, dict(payload))))
    monkeypatch.setattr("app.services.trading.momentum_neural.market_profile.market_session_now",
                        lambda _symbol, **_k: "regular")

    sess, le, adapter, _oid, _cid = _resting_deadman_session(
        db, symbol="EVTAIL", deadman_qty=10.0, marker=_decided_marker("tick_deadman"),
    )
    reason, tag = lr._EXIT_VERDICT_ACTIONS["tick_deadman"]
    le["pending_exit_reason"] = reason
    cid = f"chili_ml_{tag}_{sess.id}_{uuid.uuid4().hex[:12]}"
    out1 = _submit(db, sess, le, adapter, symbol="EVTAIL", reason=reason, cid=cid)
    assert out1.get("error") == "deadman_successor_intent_frozen_for_next_pulse", out1
    assert le[lr._EXIT_TAIL_DECIDED_AT_KEY] == clock.at.isoformat()
    assert le[lr._EXIT_TAIL_DECIDED_BID_KEY] == 9.95
    assert le[lr._EXIT_TAIL_DECIDED_REASON_KEY] == reason
    assert le[lr._EXIT_TAIL_N_RELEASE_BLOCKED_KEY] >= 1
    blocks = [p for e, p in emits if e == "live_deadman_stop_release_blocked"]
    assert le[lr._EXIT_TAIL_N_RELEASE_BLOCKED_KEY] == len(blocks)
    assert lr._EXIT_TAIL_FIRST_POSTED_KEY not in le and adapter.limit_calls == [] == adapter.market_calls

    decided = le[lr._EXIT_TAIL_DECIDED_AT_KEY]
    clock.advance(1.05)
    _submit(db, sess, le, adapter, symbol="EVTAIL", reason=reason, cid=cid)
    assert le[lr._EXIT_TAIL_DECIDED_AT_KEY] == decided        # pulse 2 is the same decision


def test_the_real_alpaca_post_records_the_acceptance(db, monkeypatch):
    """The real Alpaca impl end to end (owner transport, literal-BBO refresh, the POST): the
    acceptance lands in `exit_tail_first_posted` with the broker order id and the literal bid."""
    from tests.test_momentum_emergency_exit_recovery import TEST_ALPACA_ACCOUNT_ID, _direct_submit

    # that module's autouse boundaries, which do not follow its helper here
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_alpaca_expected_account_id", TEST_ALPACA_ACCOUNT_ID, raising=False)
    monkeypatch.setattr(lr, "_venue_broker_connected", lambda _family: True)
    monkeypatch.setattr(lr, "_record_live_exit_ledger_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_record_fill_outcome_safe", lambda *a, **k: None)
    out, adapter, le = _direct_submit(
        db, monkeypatch, session="regular", quote_independent=False, reason="stop",
    )
    assert out["ok"] is True, out
    assert len(adapter.market_calls) + len(adapter.limit_calls) == 1
    assert le[lr._EXIT_TAIL_DECIDED_REASON_KEY] == "stop"
    assert le[lr._EXIT_TAIL_DECIDED_BID_KEY] == 9.95
    post = le[lr._EXIT_TAIL_FIRST_POSTED_KEY]
    assert post["order_id"] == out["order_id"]
    assert post["reason"] == "stop"
    assert post["bid_at_post"] == 9.95            # the strict execution-BBO re-read priced it
    assert post["literal_bbo_bid"] == 9.95        # THIS attempt's literal-post refresh
    assert le[lr._EXIT_TAIL_N_POSTS_KEY] == 1


# ── 5. ownership and source pins ─────────────────────────────────────────────────────


def test_every_tail_key_is_cleared_on_recycle():
    assert set(lr._EXIT_TAIL_KEYS) <= set(lr._RECYCLE_ENTRY_STATE_KEYS)
    # the AST pins elsewhere read the STRING CONSTANTS of the tuple: the keys are literals there
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    literal_keys: set[str] = set()
    for node in ast.walk(tree):
        target = node.target if isinstance(node, ast.AnnAssign) else (
            node.targets[0] if isinstance(node, ast.Assign) and node.targets else None)
        if isinstance(target, ast.Name) and target.id == "_RECYCLE_ENTRY_STATE_KEYS":
            literal_keys = {e.value for e in ast.walk(node.value)
                            if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    assert set(lr._EXIT_TAIL_KEYS) <= literal_keys
    le = {key: 1 for key in lr._EXIT_TAIL_KEYS}
    le["trade_cycles"] = 2
    cleared = lr._reset_entry_state_on_recycle(le)
    assert set(lr._EXIT_TAIL_KEYS) <= set(cleared)
    assert le == {"trade_cycles": 2}


def test_no_tail_key_can_move_a_pending_partial_retirement_binding():
    """`pending_partial_retirement.binding()` freezes every `pending_exit_*` key into the
    retirement identity; a receipt counter there could fail a retirement. The prefix differs."""
    assert not any(key.startswith("pending_exit_") for key in lr._EXIT_TAIL_KEYS)
    le = _long_le(pending_exit_reason="target", pending_exit_is_scale_out=True)
    sess = SimpleNamespace(id=1, user_id=1, mode="live", execution_family="alpaca_spot", symbol="NCRA",
                           state="live_entered", correlation_id="c", variant_id=1, risk_snapshot_json={})
    before = retirement.digest(retirement.binding(sess, le))
    for key in lr._EXIT_TAIL_KEYS:
        le[key] = 7
    assert retirement.digest(retirement.binding(sess, le)) == before


def test_the_post_receipt_sits_at_the_single_post_success_point():
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    impl = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_submit_live_market_exit_impl")
    posts = [n for n in ast.walk(impl) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr in {"place_limit_order_gtc", "place_market_order"}]
    assert len(posts) == 2, "the two POSTs the receipt covers"
    notes = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_exit_tail_note_post"]
    assert len(notes) == 1
    ok_blocks = [n for n in ast.walk(impl) if isinstance(n, ast.If)
                 and ast.unparse(n.test) == "result.get('ok')"]
    assert any(any(c is notes[0] for c in ast.walk(b)) for b in ok_blocks)


def test_the_tail_is_read_before_the_pending_stamps_are_popped():
    src = _SRC.read_text(encoding="utf-8")
    for fn, event in (("_complete_confirmed_live_exit", '"live_exit_filled"'),
                      ("_apply_confirmed_live_partial_exit", '"live_partial_exit_filled"')):
        body = src[src.index(f"def {fn}("):]
        body = body[:body.index("\ndef ", 10)]
        read = body.index("_exit_tail_receipt(")
        assert read < body.index('le.pop("pending_exit_submitted_at_utc", None)'), fn
        assert read < body.index(event), fn
        assert '"exit_tail"' in body, fn


def test_the_receipt_introduces_no_threshold():
    """Arithmetic on existing stamps: the only numbers in the helpers are the rounding digits
    and the zero/one of counting."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    helpers = {"_exit_tail_ts", "_exit_tail_span_s", "_exit_tail_int", "_exit_tail_bump",
               "_exit_tail_counts", "_exit_tail_stamp_decision", "_exit_tail_clear",
               "_exit_tail_note_post", "_exit_tail_receipt"}
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in helpers:
            found.add(node.name)
            nums = {c.value for c in ast.walk(node) if isinstance(c, ast.Constant)
                    and isinstance(c.value, (int, float)) and not isinstance(c.value, bool)}
            assert nums <= {0, 0.0, 1, 3, 4}, (node.name, nums)
    assert found == helpers

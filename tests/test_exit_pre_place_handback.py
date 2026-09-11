"""[20] Ang PATUNAY ang pumapalit sa orasan sa missing-order-id poll (2026-09-11).

ANG SUGAT. Ang pending-first na exit (burst_window_exit, momentum_break_stop, at mula 09-11
ang bawat #1385 verdict exit) ay nagtatakda ng ``pending_exit_reason`` BAGO ang submit. Sa
Alpaca na may resting deadman, ang submit na iyon ay ang SADYANG two-phase handoff: ang phase 1
ay nagfi-freeze ng successor intent at bumabalik ng ``deferred`` + ``pre_place_blocked`` --
WALANG order sa broker. Ang susunod na pulse ay pumapasok sa pending-exit poll, walang exit
order id, at naghihintay ng grace = max(backoff, 5.0 s) bago ibalik ang session sa submit path.

ANG SUKAT (trading_automation_events, 2026-08-28 10:55Z -> 09-11 10:55Z, bounded read-only):
  * 23/23 na ``live_exit_order_id_lost`` ay naunahan ng
    ``deadman_successor_intent_frozen_for_next_pulse``; pagkatapos ng #1310, 65/65 na
    missing-order-id poll ay sumunod sa freeze na iyon.
  * freeze -> order_id_lost p50 8.38 s / p90 9.58 s (kabuuang 195.0 s).
  * freeze -> fill: via grace p50 18.94 s / p90 22.76 s (n=23) laban sa direktang daan
    (stop/bailout/trail/target) p50 10.31 s / p90 19.54 s (n=70).
  * bid drift habang naghihintay: random walk, net -$12.34 sa 19 na may BBO (pinakamasama
    TNON 22129 -$47.28, LBGJ 22135 -$28.00, WYHG 20268 -$22.96) -- ORAS ang gastos.

ANG AYOS. Ang ``_submit_live_market_exit`` (ang ISANG seam ng bawat exit call site) ay
nagsusulat ng ``exit_pre_place_block_proof`` kapag PINATUTUNAYAN ng attempt na walang exit
instruction na nasa broker o papunta pa lang; ang poll ay nagbabalik agad (walang orasan).
Walang patunay -> ang lumang grace bilang NAMED fallback (``binding="grace_seconds"``).

Runnable: pytest tests/test_exit_pre_place_handback.py -v
"""
from __future__ import annotations

import types
import uuid
from datetime import timedelta

import pytest

import app.services.trading.momentum_neural.live_runner as lr

FREEZE_ERROR = "deadman_successor_intent_frozen_for_next_pulse"


def _freeze_result() -> dict:
    """Ang eksaktong hugis ng phase-1 `_block` + `_abort_deadman_handoff_and_reprotect`."""
    return {
        "ok": False,
        "error": FREEZE_ERROR,
        "deferred": True,
        "pre_place_blocked": True,
        "deadman_order_id": "dm-oid-1",
        "deadman_client_order_id": "chili_dm_22135_1_abc123",
        "deadman_rearmed": True,
        "deadman_retained_active": True,
    }


def _patch(monkeypatch):
    calls = {"emit": [], "payloads": {}, "commits": 0, "woke": [], "transition": []}

    def _commit(sess, le):
        calls["commits"] += 1

    def _emit(db, sess, ev, payload=None):
        calls["emit"].append(ev)
        calls["payloads"][ev] = dict(payload or {})

    monkeypatch.setattr(lr, "_commit_le", _commit)
    monkeypatch.setattr(lr, "_emit", _emit)
    monkeypatch.setattr(lr, "_safe_transition", lambda db, sess, state: calls["transition"].append(state))
    monkeypatch.setattr(
        lr, "_schedule_exit_continuation", lambda sid: calls["woke"].append(int(sid)) or True
    )
    return calls


def _sess(sid: int = 22135, symbol: str = "LBGJ"):
    return types.SimpleNamespace(id=sid, symbol=symbol, execution_family="alpaca_spot")


def _frozen_le(reason: str = "tape_accel_rollover") -> dict:
    """Ang le PAGKATAPOS ng phase 1: itinakda ng pending-first path ang reason, ibinalik ng
    `_block` ang attempt, at ang handoff ay ``intent_frozen`` na walang successor request."""
    return {
        "position": {"quantity": 400.0, "avg_entry_price": 2.80},
        "pending_exit_reason": reason,
        "exit_submit_attempts": 0,
        "deadman_released_for_close": {
            "version": 1,
            "phase": "intent_frozen",
            "reason": reason,
            "successor_intent_pre_cancel": {"base_size": "400", "order_type": "limit"},
            "successor_order_request": None,
        },
        "last_deadman_release_block": _freeze_result(),
    }


def _submit(le: dict, *, reason: str = "tape_accel_rollover", sid: int = 22135):
    return lr._submit_live_market_exit(
        None, _sess(sid), None, le=le, product_id="LBGJ", quantity=400.0,
        client_order_id=f"chili_ml_ta_{sid}_{uuid.uuid4().hex[:12]}",
        reason=reason, bid=2.78, ask=2.80, mid=2.79,
    )


def _proof(reason: str = "tape_accel_rollover", *, error: str = FREEZE_ERROR) -> dict:
    return {
        "proof_contract": lr._EXIT_PRE_PLACE_PROOF_CONTRACT,
        "error": error,
        "reason": reason,
        "recorded_at_utc": lr._utcnow().isoformat(),
        "attempts": 0,
    }


def _grace(attempts: int = 0) -> float:
    return max(
        lr._exit_submit_backoff_seconds(max(1, attempts)),
        lr._EXIT_SUBMIT_BACKOFF_BASE_SECONDS,
    )


# ── 1. ang seam ang sumusulat ng patunay ───────────────────────────────────────────


def test_wrapper_stamps_proof_on_deferred_pre_place_blocked_result(monkeypatch):
    calls = _patch(monkeypatch)
    le = _frozen_le()
    frozen = _freeze_result()
    monkeypatch.setattr(lr, "_submit_live_market_exit_impl", lambda db, sess, adapter, **kw: frozen)

    out = _submit(le)

    assert out is frozen, "the exit result is returned untouched"
    proof = le[lr._EXIT_PRE_PLACE_PROOF_KEY]
    assert proof["proof_contract"] == "exit_pre_place_block_v1"
    assert proof["error"] == FREEZE_ERROR
    assert proof["reason"] == "tape_accel_rollover"
    assert proof["attempts"] == 0
    assert proof["recorded_at_utc"]
    # the deferred branch of `_live_exit_submit_succeeded` does not commit, so the seam must
    assert calls["commits"] >= 1
    # the existing 0.5-s continuation still wakes the next pulse
    assert calls["woke"] == [22135]


def test_wrapper_clears_stale_proof_before_every_attempt(monkeypatch):
    calls = _patch(monkeypatch)
    le = _frozen_le()
    le[lr._EXIT_PRE_PLACE_PROOF_KEY] = _proof()
    seen: dict = {}

    def _impl(db, sess, adapter, **kw):
        seen["proof_visible_to_impl"] = lr._EXIT_PRE_PLACE_PROOF_KEY in kw["le"]
        kw["le"]["exit_order_id"] = "succ-oid-1"
        return {"ok": True, "order_id": "succ-oid-1", "client_order_id": kw["client_order_id"]}

    monkeypatch.setattr(lr, "_submit_live_market_exit_impl", _impl)

    _submit(le)

    assert seen["proof_visible_to_impl"] is False, "every new attempt invalidates the old proof"
    assert lr._EXIT_PRE_PLACE_PROOF_KEY not in le
    assert calls["commits"] >= 1, "the invalidation is committed, not left in memory"


def test_invalidation_is_durable_even_when_the_impl_returns_without_commit(monkeypatch):
    """`exit_retry_backoff` returns before any commit; the popped proof must still persist."""
    calls = _patch(monkeypatch)
    le = _frozen_le()
    le[lr._EXIT_PRE_PLACE_PROOF_KEY] = _proof()
    monkeypatch.setattr(
        lr, "_submit_live_market_exit_impl",
        lambda db, sess, adapter, **kw: {"ok": False, "error": "exit_retry_backoff", "deferred": True},
    )

    _submit(le)

    assert lr._EXIT_PRE_PLACE_PROOF_KEY not in le
    assert calls["commits"] == 1, "exactly the invalidation commit; a backoff is not a proof"


@pytest.mark.parametrize(
    "case",
    [
        "exit_order_id_on_session",
        "active_owner_transport",
        "successor_request_finalized",
        "handoff_unreadable",
        "captured_paper_post_commit",
        "order_id_on_result",
        "order_posted_on_result",
        "backoff_deferral_not_pre_place",
        "retry_cap_history_block_not_deferred",
    ],
)
def test_wrapper_does_not_stamp_when_an_instruction_may_be_in_flight(monkeypatch, case):
    _patch(monkeypatch)
    le = _frozen_le()
    result = _freeze_result()
    if case == "exit_order_id_on_session":
        le["exit_order_id"] = "oid-live"
    elif case == "active_owner_transport":
        le["alpaca_active_exit_owner_transport"] = {
            "transport_kind": "ordinary_exit", "client_order_id": "chili_ml_x", "phase": "leased",
        }
    elif case == "successor_request_finalized":
        le["deadman_released_for_close"]["phase"] = "successor_ready"
        le["deadman_released_for_close"]["successor_order_request"] = {"base_size": "400"}
    elif case == "handoff_unreadable":
        le["deadman_released_for_close"] = "garbage"
    elif case == "captured_paper_post_commit":
        # the sealed lane stages its POST for AFTER the commit: an instruction on its way
        result = {"ok": False, "deferred": True, "pre_place_blocked": True,
                  "captured_paper_exit_transport_post_commit_required": True,
                  "broker_calls": 0, "order_posted": False}
    elif case == "order_id_on_result":
        result = {**result, "order_id": "oid-x"}
    elif case == "order_posted_on_result":
        result = {**result, "order_posted": True}
    elif case == "backoff_deferral_not_pre_place":
        result = {"ok": False, "error": "exit_retry_backoff", "deferred": True}
    elif case == "retry_cap_history_block_not_deferred":
        result = {"ok": False, "error": "exit_retry_transport_history_incomplete",
                  "cap_exceeded": True, "pre_place_blocked": True}
    monkeypatch.setattr(lr, "_submit_live_market_exit_impl", lambda db, sess, adapter, **kw: result)

    _submit(le)

    assert lr._EXIT_PRE_PLACE_PROOF_KEY not in le, case


def test_wrapper_never_masks_the_exit_result_when_the_stamp_cannot_commit(monkeypatch):
    """Isang test double na sess (walang risk_snapshot_json): ang resulta at ang gising ay buo."""
    woke: list[int] = []
    monkeypatch.setattr(lr, "_schedule_exit_continuation", lambda sid: woke.append(int(sid)) or True)
    frozen = _freeze_result()
    monkeypatch.setattr(lr, "_submit_live_market_exit_impl", lambda db, sess, adapter, **kw: frozen)
    le = _frozen_le()

    out = lr._submit_live_market_exit(None, types.SimpleNamespace(id=7), None, le=le, reason="burst_window_exit")

    assert out is frozen
    assert woke == [7]


# ── 2. ang poll ay nagbabalik agad kapag may patunay ──────────────────────────────


def test_poll_with_proof_hands_back_immediately_without_clock(monkeypatch):
    calls = _patch(monkeypatch)
    le = _frozen_le()
    le[lr._EXIT_PRE_PLACE_PROOF_KEY] = _proof()
    assert "pending_exit_submitted_at_utc" not in le  # the pending-first shape: age 0, no stamp

    out = lr._poll_live_exit_fill(
        None, _sess(), None, le=le, reason="tape_accel_rollover", quantity=400.0,
    )

    assert out == {"filled": False, "pending": True, "why": "pre_place_handback"}
    receipt = calls["payloads"]["live_exit_pre_place_handback"]
    assert receipt["binding"] == "pre_place_blocked_proof"
    assert receipt["block_error"] == FREEZE_ERROR
    assert receipt["reason"] == "tape_accel_rollover"
    assert receipt["grace_seconds_skipped"] == round(_grace(0), 2)
    assert receipt["pending_age_seconds"] is None
    assert receipt["block_age_seconds"] is not None and receipt["block_age_seconds"] >= 0.0
    assert receipt["exit_submit_attempts"] == 0
    assert receipt["continuation_scheduled"] is True
    # the same three pops as the generic failure block, plus the consumed proof
    for key in ("pending_exit_reason", "pending_exit_quantity", "pending_exit_submitted_at_utc",
                lr._EXIT_PRE_PLACE_PROOF_KEY):
        assert key not in le, key
    # a deliberate boundary, NOT a failure
    assert "live_exit_submit_failed" not in calls["emit"]
    assert "last_exit_submit_failed" not in le
    # and no clock at all
    assert "live_exit_order_id_lost" not in calls["emit"]
    assert "live_exit_pending_unconfirmed" not in calls["emit"]
    # attempts untouched (the `_block` already restored it); the position never leaves the book
    assert le["exit_submit_attempts"] == 0
    assert le["position"] == {"quantity": 400.0, "avg_entry_price": 2.80}
    assert calls["transition"] == []
    # the frozen handoff stays: the next pulse's durable-handoff priority branch runs phase 2
    assert le["deadman_released_for_close"]["phase"] == "intent_frozen"
    assert calls["woke"] == [22135]
    assert calls["commits"] >= 1


def test_poll_without_proof_keeps_named_grace_fallback(monkeypatch):
    calls = _patch(monkeypatch)
    monkeypatch.setattr(lr, "_broker_position_confirms_zero", lambda sess: False)
    le = _frozen_le()
    le["pending_exit_submitted_at_utc"] = lr._utcnow().isoformat()

    out = lr._poll_live_exit_fill(None, _sess(), None, le=le, reason="tape_accel_rollover", quantity=400.0)

    assert out["why"] == "missing_exit_order_id"
    assert calls["payloads"]["live_exit_pending_unconfirmed"]["binding"] == "grace_seconds"
    assert "live_exit_pre_place_handback" not in calls["emit"]
    assert le["pending_exit_reason"] == "tape_accel_rollover"

    le["pending_exit_submitted_at_utc"] = (lr._utcnow() - timedelta(seconds=_grace() + 1.0)).isoformat()
    out = lr._poll_live_exit_fill(None, _sess(), None, le=le, reason="tape_accel_rollover", quantity=400.0)

    assert out["why"] == "missing_exit_order_id_escalated"
    assert calls["payloads"]["live_exit_order_id_lost"]["binding"] == "grace_seconds"
    assert "live_exit_pre_place_handback" not in calls["emit"]


def test_poll_proof_ignored_when_active_owner_transport_present(monkeypatch):
    """Isang durable owner lease na hindi pa napatunayang na-release = maaaring lumilipad."""
    calls = _patch(monkeypatch)
    le = _frozen_le()
    le[lr._EXIT_PRE_PLACE_PROOF_KEY] = _proof()
    le["alpaca_active_exit_owner_transport"] = {
        "transport_kind": "ordinary_exit", "client_order_id": "chili_ml_x", "phase": "submit_indeterminate",
    }

    out = lr._poll_live_exit_fill(None, _sess(), None, le=le, reason="tape_accel_rollover", quantity=400.0)

    assert out["why"] == "missing_exit_order_id"
    assert "live_exit_pre_place_handback" not in calls["emit"]
    assert le["pending_exit_reason"] == "tape_accel_rollover"
    assert calls["woke"] == []


def test_poll_proof_for_another_exit_reason_is_not_honoured(monkeypatch):
    calls = _patch(monkeypatch)
    le = _frozen_le(reason="tick_deadman_stop")
    le[lr._EXIT_PRE_PLACE_PROOF_KEY] = _proof(reason="burst_window_exit")

    out = lr._poll_live_exit_fill(None, _sess(), None, le=le, reason="tick_deadman_stop", quantity=400.0)

    assert out["why"] == "missing_exit_order_id"
    assert "live_exit_pre_place_handback" not in calls["emit"]
    assert le["pending_exit_reason"] == "tick_deadman_stop"


def test_poll_proof_with_an_unknown_contract_is_not_honoured(monkeypatch):
    calls = _patch(monkeypatch)
    le = _frozen_le()
    le[lr._EXIT_PRE_PLACE_PROOF_KEY] = {**_proof(), "proof_contract": "something_else"}

    out = lr._poll_live_exit_fill(None, _sess(), None, le=le, reason="tape_accel_rollover", quantity=400.0)

    assert out["why"] == "missing_exit_order_id"
    assert "live_exit_pre_place_handback" not in calls["emit"]


def test_handback_respects_an_armed_broker_backoff(monkeypatch):
    """Ang handback ay nangyayari pa rin; ang gising lang ang iginagalang ang backoff."""
    calls = _patch(monkeypatch)
    le = _frozen_le()
    le[lr._EXIT_PRE_PLACE_PROOF_KEY] = _proof()
    le["exit_next_retry_at_utc"] = (lr._utcnow() + timedelta(seconds=10)).isoformat()

    out = lr._poll_live_exit_fill(None, _sess(), None, le=le, reason="tape_accel_rollover", quantity=400.0)

    assert out["why"] == "pre_place_handback"
    assert "pending_exit_reason" not in le
    assert calls["woke"] == []
    assert calls["payloads"]["live_exit_pre_place_handback"]["continuation_scheduled"] is False


# ── 3. ang LBGJ 22135 na hugis, dulo hanggang dulo ─────────────────────────────────


def test_lbgj_22135_shape_end_to_end(monkeypatch):
    """LBGJ 22135 2026-09-11: desisyon 09:44:51 (bid 2.78) -> fill 09:45:41 sa 2.69, DALAWANG
    freeze na ikot, bawat isa 9.5 s sa orasan. Ngayon: ZERO orasan sa pagitan ng phase 1 at 2."""
    calls = _patch(monkeypatch)
    sess = _sess()
    le = {
        "position": {"quantity": 400.0, "avg_entry_price": 2.80},
        "exit_submit_attempts": 0,
        "deadman_stop": {"order_id": "dm-oid-1", "client_order_id": "chili_dm_22135_1_abc123"},
    }

    def _phase_one(db, s, adapter, **kw):
        # what `_release_deadman_at_literal_submit` + `_block` do to the session JSON
        live = kw["le"]
        attempts = int(live.get("exit_submit_attempts") or 0) + 1
        live["exit_submit_attempts"] = max(0, attempts - 1)
        live.pop("exit_next_retry_at_utc", None)
        live["deadman_released_for_close"] = {
            "version": 1, "phase": "intent_frozen", "reason": kw["reason"],
            "successor_intent_pre_cancel": {"base_size": "400", "order_type": "limit"},
            "successor_order_request": None,
        }
        block = _freeze_result()
        live["last_deadman_release_block"] = block
        return block

    # ── pulse 1: the verdict decides; pending-first; the seam freezes phase 1 ──
    le["pending_exit_reason"] = "tape_accel_rollover"
    monkeypatch.setattr(lr, "_submit_live_market_exit_impl", _phase_one)
    out1 = _submit(le)
    assert out1["error"] == FREEZE_ERROR
    assert le[lr._EXIT_PRE_PLACE_PROOF_KEY]["error"] == FREEZE_ERROR
    assert lr._live_exit_submit_succeeded(
        None, sess, adapter=None, le=le, result=out1, reason="tape_accel_rollover"
    ) is False
    assert le["pending_exit_reason"] == "tape_accel_rollover", "the deferred branch keeps it"

    # ── pulse 2 (0.5-s continuation): the poll hands back on its FIRST look ──
    out2 = lr._poll_live_exit_fill(None, sess, None, le=le, reason="tape_accel_rollover", quantity=400.0)
    assert out2["why"] == "pre_place_handback"
    assert "pending_exit_reason" not in le

    # ── pulse 3: the durable-handoff priority branch runs phase 2 through the same seam ──
    def _phase_two(db, s, adapter, **kw):
        live = kw["le"]
        assert lr._EXIT_PRE_PLACE_PROOF_KEY not in live, "the consumed proof is gone"
        live["deadman_released_for_close"]["phase"] = "successor_ready"
        live["deadman_released_for_close"]["successor_order_request"] = {"base_size": "400"}
        live["exit_order_id"] = "succ-oid-1"
        live["exit_client_order_id"] = kw["client_order_id"]
        live["pending_exit_reason"] = kw["reason"]
        live["pending_exit_quantity"] = 400.0
        return {"ok": True, "order_id": "succ-oid-1", "client_order_id": kw["client_order_id"]}

    monkeypatch.setattr(lr, "_submit_live_market_exit_impl", _phase_two)
    out3 = _submit(le)
    assert out3["ok"] is True
    assert lr._EXIT_PRE_PLACE_PROOF_KEY not in le
    assert lr._live_exit_submit_succeeded(
        None, sess, adapter=None, le=le, result=out3, reason="tape_accel_rollover"
    ) is True

    # the whole freeze -> POST chain never touched the grace clock and never recorded a failure
    for ev in ("live_exit_pending_unconfirmed", "live_exit_order_id_lost", "live_exit_submit_failed"):
        assert ev not in calls["emit"], ev
    assert calls["emit"].count("live_exit_pre_place_handback") == 1
    assert calls["woke"] == [22135, 22135], "phase-1 continuation + handback continuation"


# ── 4. ang patunay ay pag-aari ng leg ──────────────────────────────────────────────


def test_the_proof_is_cleared_on_recycle():
    assert lr._EXIT_PRE_PLACE_PROOF_KEY in lr._RECYCLE_ENTRY_STATE_KEYS


# ── 5. ang TUNAY na phase-1 freeze, sa tunay na claim tables ─────────────────────


@pytest.fixture
def _alpaca_boundaries(monkeypatch):
    from app.config import settings
    from tests.test_exit_verdict_g_whole_exit_seam import NOW
    from tests.test_momentum_emergency_exit_recovery import TEST_ALPACA_ACCOUNT_ID

    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_alpaca_expected_account_id", TEST_ALPACA_ACCOUNT_ID, raising=False)
    monkeypatch.setattr(lr, "_venue_broker_connected", lambda _family: True)
    monkeypatch.setattr(lr, "_record_live_exit_ledger_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_record_live_partial_exit_ledger_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_record_fill_outcome_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_record_live_exit_intent_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_utcnow", lambda: NOW)


def test_the_real_phase_one_freeze_writes_the_proof_and_the_next_poll_hands_back(
    db, monkeypatch, _alpaca_boundaries
):
    """Hindi hinuhulaan ang hugis ng le: ang TUNAY na `_submit_live_market_exit_impl` ang
    nagfi-freeze (scripted Alpaca, tunay na owner claim), at ang patunay ay dapat lumabas."""
    from tests.test_exit_verdict_g_whole_exit_seam import (
        _decided_marker,
        _emit_log,
        _resting_deadman_session,
        _session_hours,
    )

    log = _emit_log(monkeypatch)
    _session_hours(monkeypatch, "regular")
    woke: list[int] = []
    monkeypatch.setattr(lr, "_schedule_exit_continuation", lambda sid: woke.append(int(sid)) or True)
    symbol = "PPHB"
    sess, le, adapter, _oid, _cid = _resting_deadman_session(
        db, symbol=symbol, deadman_qty=10.0, marker=_decided_marker("accel_rollover"),
    )
    reason, tag = lr._EXIT_VERDICT_ACTIONS["accel_rollover"]
    le["pending_exit_reason"] = reason  # the #1385 verdict block is pending-first

    out1 = lr._submit_live_market_exit(
        db, sess, adapter, le=le, product_id=symbol, quantity=10.0,
        client_order_id=f"chili_ml_{tag}_{sess.id}_{uuid.uuid4().hex[:12]}",
        reason=reason, bid=9.95, ask=9.97, mid=9.96,
    )

    assert out1.get("error") == FREEZE_ERROR, out1
    assert adapter.limit_calls == [] and adapter.market_calls == []
    proof = le.get(lr._EXIT_PRE_PLACE_PROOF_KEY)
    assert isinstance(proof, dict), sorted(le)
    assert proof["error"] == FREEZE_ERROR and proof["reason"] == reason
    assert (le["deadman_released_for_close"] or {}).get("successor_order_request") is None
    assert not le.get("alpaca_active_exit_owner_transport")
    # the proof is on the ROW, not only in memory
    db.flush()
    db.refresh(sess)
    persisted = (sess.risk_snapshot_json or {}).get(lr.KEY_LIVE_EXEC) or {}
    assert (persisted.get(lr._EXIT_PRE_PLACE_PROOF_KEY) or {}).get("error") == FREEZE_ERROR
    assert lr._live_exit_submit_succeeded(db, sess, adapter=adapter, le=le, result=out1, reason=reason) is False
    assert le["pending_exit_reason"] == reason

    # ── the next pulse: the pending-exit poll ──
    poll = lr._poll_live_exit_fill(db, sess, adapter, le=le, reason=reason, quantity=10.0)

    assert poll == {"filled": False, "pending": True, "why": "pre_place_handback"}
    names = [ev for ev, _ in log]
    assert "live_exit_pre_place_handback" in names
    for ev in ("live_exit_pending_unconfirmed", "live_exit_order_id_lost", "live_exit_submit_failed"):
        assert ev not in names, ev
    assert "pending_exit_reason" not in le and lr._EXIT_PRE_PLACE_PROOF_KEY not in le
    assert adapter.limit_calls == [] and adapter.market_calls == [], "the handback places nothing"
    assert woke == [int(sess.id), int(sess.id)]

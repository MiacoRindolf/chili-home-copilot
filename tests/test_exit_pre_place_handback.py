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

REVIEW FIXES (2026-09-11):
  * ang patunay ay ALLOWLIST ng isa -- ang phase-1 freeze na may certified-active na retained
    deadman (98/98 na freeze sa window); ang bawat block na ang sariling code ay nagsasabing
    maaaring may order sa broker o papunta ay HINDI patunay (seksyon 1b, 5b);
  * ang gising ay iniuulat kung ano ang TALAGANG nag-bind (loop 1.05 s, batch 0.5 s), at ang
    batch-mode wake na hiniling mula sa loob ng sariling wake tick ay hindi na nalulunod
    (seksyon 6);
  * ang TUNAY na susunod na ``tick_live_session`` ang nagpapatakbo ng phase 2 sa
    durable-handoff priority branch (seksyon 5);
  * ang handback ay hindi nagre-refund ng attempt: ang retry cap pa rin ang hangganan
    (seksyon 7); ang recycle ay tunay na nag-aalis ng patunay (seksyon 4).

Runnable: pytest tests/test_exit_pre_place_handback.py -v
"""
from __future__ import annotations

import types
import uuid
from collections import deque
from datetime import timedelta

import pytest

import app.services.trading.momentum_neural.live_runner as lr

FREEZE_ERROR = "deadman_successor_intent_frozen_for_next_pulse"


def _freeze_result() -> dict:
    """Ang eksaktong hugis ng phase-1 `_block` + `_abort_deadman_handoff_and_reprotect` sa
    retained branch (ang deadman ay sinertipikahang active ng strict CID read)."""
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

    def _wake(sid, receipt=None):
        calls["woke"].append(int(sid))
        if isinstance(receipt, dict):
            receipt.update({"driver": "test_double", "delay_s": None})
        return True

    monkeypatch.setattr(lr, "_commit_le", _commit)
    monkeypatch.setattr(lr, "_emit", _emit)
    monkeypatch.setattr(lr, "_safe_transition", lambda db, sess, state: calls["transition"].append(state))
    monkeypatch.setattr(lr, "_schedule_exit_continuation", _wake)
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
        "basis": lr._EXIT_PRE_PLACE_PROOF_BASIS,
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


def test_wrapper_stamps_proof_on_the_retained_deadman_freeze(monkeypatch):
    calls = _patch(monkeypatch)
    le = _frozen_le()
    frozen = _freeze_result()
    monkeypatch.setattr(lr, "_submit_live_market_exit_impl", lambda db, sess, adapter, **kw: frozen)

    out = _submit(le)

    assert out is frozen, "the exit result is returned untouched"
    proof = le[lr._EXIT_PRE_PLACE_PROOF_KEY]
    assert proof["proof_contract"] == "exit_pre_place_block_v1"
    assert proof["basis"] == lr._EXIT_PRE_PLACE_PROOF_BASIS
    assert proof["error"] == FREEZE_ERROR
    assert proof["reason"] == "tape_accel_rollover"
    assert proof["deadman_order_id"] == "dm-oid-1"
    assert proof["attempts"] == 0
    assert proof["recorded_at_utc"]
    # the deferred branch of `_live_exit_submit_succeeded` does not commit, so the seam must
    assert calls["commits"] >= 1
    # the existing continuation still wakes the next pulse
    assert calls["woke"] == [22135]


def test_the_allowlist_is_the_phase_one_freeze_only():
    assert lr._EXIT_PRE_PLACE_PROOF_ERRORS == frozenset({FREEZE_ERROR})


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
        "handoff_missing",
        "captured_paper_post_commit",
        "order_id_on_result",
        "order_posted_on_result",
        "backoff_deferral_not_pre_place",
        "retry_cap_history_block_not_deferred",
        "retry_cap_exceeded",
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
    elif case == "handoff_missing":
        le.pop("deadman_released_for_close")
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
    elif case == "retry_cap_exceeded":
        result = {"ok": False, "error": "exit_retry_cap_exceeded", "cap_exceeded": True, "attempts": 8}
    monkeypatch.setattr(lr, "_submit_live_market_exit_impl", lambda db, sess, adapter, **kw: result)

    _submit(le)

    assert lr._EXIT_PRE_PLACE_PROOF_KEY not in le, case


# ── 1b. [review] ang mga block na ang SARILING code ay nagsasabing maaaring may order ──


@pytest.mark.parametrize(
    "error, extra, handoff_phase",
    [
        # a strict 404 may be propagation lag after an accepted-but-ack-lost POST
        ("prior_exit_cid_absent_without_terminal_truth", {}, "intent_frozen"),
        ("prior_exit_cid_truth_unknown", {}, "intent_frozen"),
        # more than one prior sell order IS working at the broker
        ("multiple_prior_exit_orders_actionable", {}, "intent_frozen"),
        # another worker's durable owner lease is live (the session mirror rolled back)
        ("alpaca_owner_transport_cid_absent_lease_active",
         {"client_order_id": "chili_ml_ta_22135_lease", "transport_kind": "ordinary_exit"}, "intent_frozen"),
        ("alpaca_owner_transport_cid_truth_unknown",
         {"client_order_id": "chili_ml_ta_22135_lease", "transport_kind": "ordinary_exit"}, "intent_frozen"),
        # the resting scale-out limit may still be working
        ("alpaca_scale_limit_release_unconfirmed", {}, "intent_frozen"),
        # blocks AFTER the deadman was cancelled: `deadman_terminal` also carries
        # successor_order_request None (alpaca_orphan_claims.py) -- a naked leg, not a proof
        ("deadman_handoff_broker_remainder_unreadable", {}, "deadman_terminal"),
        ("deadman_successor_quantity_generation_mismatch", {}, "deadman_terminal"),
        # BBO blocks run BEFORE the prior-transport history is checked: this attempt did not
        # transport, but that is not "nothing at the broker"
        ("execution_bbo_stale_at_exit_place", {}, "intent_frozen"),
        ("frozen_exit_limit_not_marketable_at_literal_post", {}, "intent_frozen"),
    ],
)
def test_blocks_whose_own_code_says_an_order_may_be_live_are_not_a_proof(
    monkeypatch, error, extra, handoff_phase
):
    _patch(monkeypatch)
    le = _frozen_le()
    le["deadman_released_for_close"]["phase"] = handoff_phase
    result = {"ok": False, "error": error, "deferred": True, "pre_place_blocked": True, **extra}
    monkeypatch.setattr(lr, "_submit_live_market_exit_impl", lambda db, sess, adapter, **kw: result)

    _submit(le)

    assert lr._EXIT_PRE_PLACE_PROOF_KEY not in le, error


@pytest.mark.parametrize(
    "mutate",
    [
        # the re-protect branch: the retained deadman was NOT certified active and the
        # replacement deadman's POST is indeterminate -- a sell stop that may be in flight
        lambda r: r.update(deadman_rearmed=False, deadman_reprotect_error="deadman_submit_indeterminate")
        or r.pop("deadman_retained_active"),
        # a replacement re-armed OK is still not the retained generation the proof rests on
        lambda r: r.pop("deadman_retained_active"),
        lambda r: r.update(deadman_reprotect_error="scale_limit_release_unconfirmed_before_deadman_reprotect"),
        # the `_owner_transport_block` shape: a durable transport is named on the result
        lambda r: r.update(client_order_id="chili_ml_ta_x", transport_kind="ordinary_exit"),
    ],
    ids=["reprotect_indeterminate", "replacement_not_retained", "reprotect_error", "transport_markers"],
)
def test_the_freeze_itself_is_a_proof_only_with_the_retained_deadman_certified(monkeypatch, mutate):
    _patch(monkeypatch)
    le = _frozen_le()
    result = _freeze_result()
    mutate(result)
    monkeypatch.setattr(lr, "_submit_live_market_exit_impl", lambda db, sess, adapter, **kw: result)

    _submit(le)

    assert lr._EXIT_PRE_PLACE_PROOF_KEY not in le


def test_wrapper_never_masks_the_exit_result_when_the_stamp_cannot_commit(monkeypatch):
    """Isang test double na sess (walang risk_snapshot_json): ang resulta at ang gising ay buo."""
    woke: list[int] = []
    monkeypatch.setattr(lr, "_schedule_exit_continuation", lambda sid, **_k: woke.append(int(sid)) or True)
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
    assert receipt["proof_basis"] == lr._EXIT_PRE_PLACE_PROOF_BASIS
    assert receipt["block_error"] == FREEZE_ERROR
    assert receipt["reason"] == "tape_accel_rollover"
    assert receipt["grace_seconds_skipped"] == round(_grace(0), 2)
    assert receipt["pending_age_seconds"] is None
    assert receipt["block_age_seconds"] is not None and receipt["block_age_seconds"] >= 0.0
    assert receipt["exit_submit_attempts"] == 0
    assert receipt["continuation_scheduled"] is True
    assert receipt["continuation_driver"] == "test_double"
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


def test_poll_proof_naming_a_block_outside_the_allowlist_is_not_honoured(monkeypatch):
    """[review] Defence in depth on the READ side: a proof dict that names any block other
    than the phase-1 freeze (e.g. an ack-lost prior CID) takes the named grace, not the handback."""
    calls = _patch(monkeypatch)
    le = _frozen_le()
    le[lr._EXIT_PRE_PLACE_PROOF_KEY] = _proof(error="prior_exit_cid_absent_without_terminal_truth")

    out = lr._poll_live_exit_fill(None, _sess(), None, le=le, reason="tape_accel_rollover", quantity=400.0)

    assert out["why"] == "missing_exit_order_id"
    assert "live_exit_pre_place_handback" not in calls["emit"]
    assert calls["payloads"]["live_exit_pending_unconfirmed"]["binding"] == "grace_seconds"
    assert le["pending_exit_reason"] == "tape_accel_rollover"


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
    receipt = calls["payloads"]["live_exit_pre_place_handback"]
    assert receipt["continuation_scheduled"] is False
    assert receipt["continuation_driver"] == "skipped_armed_backoff"
    assert receipt["continuation_delay_s"] is None


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

    # ── pulse 2 (the continuation): the poll hands back on its FIRST look ──
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
    """[review] Behaviour, not tuple membership: the real recycle step drops the proof and
    reports it, while cross-cycle bookkeeping survives."""
    le = {
        lr._EXIT_PRE_PLACE_PROOF_KEY: _proof(),
        "pending_exit_reason": "tape_accel_rollover",
        "trade_cycles": 3,
        "realized_pnl_usd": -12.5,
    }

    cleared = lr._reset_entry_state_on_recycle(le)

    assert lr._EXIT_PRE_PLACE_PROOF_KEY not in le
    assert lr._EXIT_PRE_PLACE_PROOF_KEY in cleared
    assert "pending_exit_reason" not in le
    assert le["trade_cycles"] == 3 and le["realized_pnl_usd"] == -12.5


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
    monkeypatch.setattr(lr, "is_kill_switch_active", lambda: False)
    monkeypatch.setattr(lr, "_utcnow", lambda: NOW)


def _real_frozen_leg(db, monkeypatch, *, symbol: str, certify_retained: bool):
    """A held Alpaca leg on the REAL owner claim with one resting deadman.

    ``certify_retained``: the broker's strict CID read reports the deadman with Alpaca's own
    lifecycle (``alpaca_status: new``) -- the lane's shape (98/98 freezes took the retained
    branch). Without it the retained deadman is not certifiable and the abort path re-protects.
    """
    from tests.test_exit_verdict_g_whole_exit_seam import _decided_marker, _resting_deadman_session
    from tests.test_momentum_emergency_exit_recovery import _order

    sess, le, adapter, oid, cid = _resting_deadman_session(
        db, symbol=symbol, deadman_qty=10.0, marker=_decided_marker("accel_rollover"),
    )
    le["position"]["target_price"] = 12.0
    if certify_retained:
        active = _order(oid=oid, cid=cid, symbol=symbol, side="sell", status="open", filled=0.0,
                        avg=None, qty=10.0, order_type="stop", time_in_force="gtc",
                        position_intent="sell_to_close",
                        raw_overrides={"stop_price": 7.5, "alpaca_status": "new"})
        adapter.truth[cid] = deque([active, active, adapter.cid_orders[cid]])
    reason, tag = lr._EXIT_VERDICT_ACTIONS["accel_rollover"]
    le["pending_exit_reason"] = reason  # the #1385 verdict block is pending-first
    out1 = lr._submit_live_market_exit(
        db, sess, adapter, le=le, product_id=symbol, quantity=10.0,
        client_order_id=f"chili_ml_{tag}_{sess.id}_{uuid.uuid4().hex[:12]}",
        reason=reason, bid=9.95, ask=9.97, mid=9.96,
    )
    return sess, le, adapter, oid, reason, out1


def test_the_real_phase_one_freeze_writes_the_proof_and_the_next_poll_hands_back(
    db, monkeypatch, _alpaca_boundaries
):
    """Hindi hinuhulaan ang hugis ng le: ang TUNAY na `_submit_live_market_exit_impl` ang
    nagfi-freeze (scripted Alpaca, tunay na owner claim), at ang patunay ay dapat lumabas."""
    from tests.test_exit_verdict_g_whole_exit_seam import _emit_log, _session_hours

    log = _emit_log(monkeypatch)
    _session_hours(monkeypatch, "regular")
    woke: list[int] = []
    monkeypatch.setattr(lr, "_schedule_exit_continuation", lambda sid, **_k: woke.append(int(sid)) or True)
    sess, le, adapter, _oid, reason, out1 = _real_frozen_leg(db, monkeypatch, symbol="PPHB", certify_retained=True)

    assert out1.get("error") == FREEZE_ERROR, out1
    assert out1.get("deadman_retained_active") is True and out1.get("deadman_rearmed") is True
    assert adapter.limit_calls == [] and adapter.market_calls == []
    proof = le.get(lr._EXIT_PRE_PLACE_PROOF_KEY)
    assert isinstance(proof, dict), sorted(le)
    assert proof["error"] == FREEZE_ERROR and proof["reason"] == reason
    assert proof["basis"] == lr._EXIT_PRE_PLACE_PROOF_BASIS
    assert (le["deadman_released_for_close"] or {}).get("phase") == "intent_frozen"
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


def test_the_real_next_tick_runs_phase_two_through_the_durable_handoff_priority_branch(
    db, monkeypatch, _alpaca_boundaries
):
    """[review] The routing claim, EXERCISED: after the handback, the REAL `tick_live_session`
    (no mocked submit) takes the durable-handoff priority branch -- it cancels the deadman and
    POSTs the frozen successor ONCE, with the frozen CID and the whole quantity."""
    from tests.test_exit_verdict_g_whole_exit_seam import _emit_log, _session_hours
    from tests.test_momentum_emergency_exit_recovery import _run_tick

    log = _emit_log(monkeypatch)
    _session_hours(monkeypatch, "regular")
    monkeypatch.setattr(lr, "_schedule_exit_continuation", lambda sid, **_k: True)
    sess, le, adapter, deadman_oid, reason, out1 = _real_frozen_leg(
        db, monkeypatch, symbol="PPNT", certify_retained=True,
    )
    assert out1.get("error") == FREEZE_ERROR, out1
    frozen_cid = le["deadman_released_for_close"]["successor_intent_pre_cancel"]["client_order_id"]
    assert lr._poll_live_exit_fill(db, sess, adapter, le=le, reason=reason, quantity=10.0)["why"] == (
        "pre_place_handback"
    )
    db.commit()
    adapter.limit_results.append(lambda _o, cid: {"ok": True, "order_id": "succ-oid-1", "client_order_id": cid})

    out = _run_tick(db, sess, adapter)

    assert out.get("deadman_handoff_priority_serviced") is True, out
    assert adapter.cancel_calls == [deadman_oid], "phase 2 cancels the resting deadman"
    assert len(adapter.limit_calls) == 1 and adapter.market_calls == [], "exactly one close POST"
    posted = adapter.limit_calls[0]
    assert posted["client_order_id"] == frozen_cid, "the frozen successor identity, not a new one"
    assert posted["base_size"] == "10" and posted["position_intent"] == "sell_to_close"
    le2 = sess.risk_snapshot_json[lr.KEY_LIVE_EXEC]
    assert le2["exit_order_id"] == "succ-oid-1"
    assert le2["pending_exit_reason"] == reason
    names = [ev for ev, _ in log]
    for ev in ("live_exit_pending_unconfirmed", "live_exit_order_id_lost", "live_exit_submit_failed"):
        assert ev not in names, ev


def test_the_real_freeze_on_the_reprotect_branch_is_not_a_proof_and_keeps_the_named_grace(
    db, monkeypatch, _alpaca_boundaries
):
    """[review] On the REAL code: when the retained deadman cannot be certified active, the
    abort path re-protects and the replacement deadman (a sell stop) is `submitting` with no
    broker id -- an instruction that may be in flight. No proof, so the next poll takes the
    named grace fallback, never the handback."""
    from tests.test_exit_verdict_g_whole_exit_seam import _emit_log, _session_hours

    log = _emit_log(monkeypatch)
    _session_hours(monkeypatch, "regular")
    monkeypatch.setattr(lr, "_schedule_exit_continuation", lambda sid, **_k: True)
    monkeypatch.setattr(lr, "_broker_position_confirms_zero", lambda sess: False)
    sess, le, adapter, _oid, reason, out1 = _real_frozen_leg(db, monkeypatch, symbol="PPRP", certify_retained=False)

    assert out1.get("error") == FREEZE_ERROR, out1
    assert out1.get("deadman_retained_active") is not True
    assert out1.get("deadman_reprotect_error"), out1
    assert (le.get("deadman_stop") or {}).get("phase") == "submitting"
    assert lr._EXIT_PRE_PLACE_PROOF_KEY not in le

    poll = lr._poll_live_exit_fill(db, sess, adapter, le=le, reason=reason, quantity=10.0)

    assert poll["why"] == "missing_exit_order_id"
    names = [ev for ev, _ in log]
    assert "live_exit_pre_place_handback" not in names
    payloads = {ev: p for ev, p in log}
    assert payloads["live_exit_pending_unconfirmed"]["binding"] == "grace_seconds"
    assert le["pending_exit_reason"] == reason


# ── 6. [review] ang gising: ang TALAGANG nag-bind ang iniuulat ────────────────────


class _FakeTimer:
    armed: list["_FakeTimer"] = []

    def __init__(self, interval, function, args=None, kwargs=None):
        self.interval = float(interval)
        self.function = function
        self.args = tuple(args or ())
        self.daemon = False
        self.name = ""
        self.started = False

    def start(self):
        self.started = True
        _FakeTimer.armed.append(self)


@pytest.fixture
def _owner_process(monkeypatch):
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.wake_ownership.process_owns_momentum_execution",
        lambda: True,
    )


@pytest.fixture
def _batch_driver(monkeypatch, _owner_process):
    """Batch mode: no live loop, timers allowed (not the pytest/replay refusal), fake Timer."""
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.live_runner_loop.schedule_live_runner_stop_confirmation",
        lambda sid: False,
    )
    monkeypatch.setenv("CHILI_PYTEST", "0")
    monkeypatch.delenv("CHILI_DIAGNOSTIC_REPLAY_ISOLATED", raising=False)
    monkeypatch.setattr(lr.threading, "Timer", _FakeTimer)
    _FakeTimer.armed = []
    yield
    with lr._stop_confirm_wake_lock:
        lr._stop_confirm_wake_inflight.clear()
        lr._stop_confirm_wake_rearm.clear()
    lr._stop_confirm_wake_ctx.sid = None


def _patch_without_wake(monkeypatch):
    calls = {"emit": [], "payloads": {}}

    def _emit(db, sess, ev, payload=None):
        calls["emit"].append(ev)
        calls["payloads"][ev] = dict(payload or {})

    monkeypatch.setattr(lr, "_commit_le", lambda sess, le: None)
    monkeypatch.setattr(lr, "_emit", _emit)
    return calls


def test_loop_mode_handback_reports_the_loops_1_05_s_not_0_5_s(monkeypatch, _owner_process):
    """The deployed driver: the wake goes through the loop's stop-confirm timer, which arms at
    `_STOP_CONFIRM_DELAY_S` whatever delay the caller was written with. The receipt says so."""
    from app.services.trading.momentum_neural import live_runner_loop as loop_mod

    scheduled: list[int] = []
    monkeypatch.setattr(loop_mod, "schedule_live_runner_stop_confirmation",
                        lambda sid: scheduled.append(int(sid)) or True)
    calls = _patch_without_wake(monkeypatch)
    le = _frozen_le()
    le[lr._EXIT_PRE_PLACE_PROOF_KEY] = _proof()

    lr._poll_live_exit_fill(None, _sess(), None, le=le, reason="tape_accel_rollover", quantity=400.0)

    receipt = calls["payloads"]["live_exit_pre_place_handback"]
    assert scheduled == [22135]
    assert receipt["continuation_scheduled"] is True
    assert receipt["continuation_driver"] == "live_loop_stop_confirm_timer"
    assert receipt["continuation_delay_s"] == loop_mod._STOP_CONFIRM_DELAY_S == 1.05
    assert receipt["continuation_delay_s"] != lr._EXIT_CONTINUATION_WAKE_DELAY_S


def test_batch_mode_fresh_wake_arms_the_0_5_s_timer(monkeypatch, _batch_driver):
    calls = _patch_without_wake(monkeypatch)
    le = _frozen_le()
    le[lr._EXIT_PRE_PLACE_PROOF_KEY] = _proof()

    lr._poll_live_exit_fill(None, _sess(), None, le=le, reason="tape_accel_rollover", quantity=400.0)

    receipt = calls["payloads"]["live_exit_pre_place_handback"]
    assert receipt["continuation_driver"] == "batch_daemon_timer"
    assert receipt["continuation_delay_s"] == lr._EXIT_CONTINUATION_WAKE_DELAY_S == 0.5
    assert [(t.interval, t.args) for t in _FakeTimer.armed] == [(0.5, (22135,))]


def test_batch_mode_wake_requested_inside_its_own_wake_tick_is_rearmed_after_the_tick(
    monkeypatch, _batch_driver
):
    """The handback runs INSIDE the phase-1 continuation's own wake tick, whose sid stays in
    `_stop_confirm_wake_inflight` until its `finally`. It used to be dropped (phase 2 then
    waited a full scheduler cadence); now ONE follow-up is armed after the tick -- the batch
    mirror of the loop's `guarantee_after_inflight`."""
    from app.services.trading.momentum_neural import captured_paper_dispatcher as cpd

    calls = _patch_without_wake(monkeypatch)
    le = _frozen_le()
    le[lr._EXIT_PRE_PLACE_PROOF_KEY] = _proof()
    inside: dict = {}

    def _tick(_session_factory, sid):
        inside["out"] = lr._poll_live_exit_fill(
            None, _sess(int(sid)), None, le=le, reason="tape_accel_rollover", quantity=400.0,
        )
        inside["armed_during_tick"] = list(_FakeTimer.armed)

    monkeypatch.setattr(cpd, "run_live_runner_tick_two_phase", _tick)
    with lr._stop_confirm_wake_lock:
        lr._stop_confirm_wake_inflight.add(22135)  # the phase-1 continuation's timer fired

    lr._stop_confirm_wake_tick(22135)

    assert inside["out"]["why"] == "pre_place_handback"
    receipt = calls["payloads"]["live_exit_pre_place_handback"]
    assert receipt["continuation_scheduled"] is True
    assert receipt["continuation_driver"] == "batch_timer_after_inflight"
    assert receipt["continuation_delay_s"] == 0.5
    assert inside["armed_during_tick"] == [], "nothing re-enters while the tick still owns the sid"
    assert [(t.interval, t.function, t.args) for t in _FakeTimer.armed] == [
        (0.5, lr._stop_confirm_wake_tick, (22135,))
    ], "exactly one follow-up, armed after the tick returned"
    assert 22135 in lr._stop_confirm_wake_inflight, "the follow-up owns the sid until it runs"
    assert 22135 not in lr._stop_confirm_wake_rearm
    assert lr._stop_confirm_wake_ctx.sid is None


def test_batch_mode_dedupe_is_unchanged_for_a_caller_that_is_not_the_wake_tick(monkeypatch, _batch_driver):
    """An armed timer for the sid will run a tick anyway: another thread's request is still
    deduplicated, and records no re-arm."""
    calls = _patch_without_wake(monkeypatch)
    le = _frozen_le()
    le[lr._EXIT_PRE_PLACE_PROOF_KEY] = _proof()
    with lr._stop_confirm_wake_lock:
        lr._stop_confirm_wake_inflight.add(22135)

    lr._poll_live_exit_fill(None, _sess(), None, le=le, reason="tape_accel_rollover", quantity=400.0)

    receipt = calls["payloads"]["live_exit_pre_place_handback"]
    assert receipt["continuation_scheduled"] is False
    assert receipt["continuation_driver"] == "batch_inflight_dedupe"
    assert _FakeTimer.armed == []
    assert 22135 not in lr._stop_confirm_wake_rearm


# ── 7. [review] ang retry budget: ang cap pa rin ang hangganan ────────────────────


def test_the_handback_never_refunds_a_consumed_attempt(monkeypatch):
    """A phase 2 blocked at the literal BBO consumes an attempt (it is not restored). The grace
    used to space those cycles 5,5,10,20,40,80,160,300 s (620 s over the 8-attempt budget); the
    handback does not, so the ONLY bound is the cap -- and the handback must leave the consumed
    budget exactly where it was, reporting the clock it skipped."""
    calls = _patch(monkeypatch)
    le = _frozen_le()
    le["exit_submit_attempts"] = 5
    le[lr._EXIT_PRE_PLACE_PROOF_KEY] = _proof()

    out = lr._poll_live_exit_fill(None, _sess(), None, le=le, reason="tape_accel_rollover", quantity=400.0)

    assert out["why"] == "pre_place_handback"
    assert le["exit_submit_attempts"] == 5
    receipt = calls["payloads"]["live_exit_pre_place_handback"]
    assert receipt["exit_submit_attempts"] == 5
    assert receipt["grace_seconds_skipped"] == round(_grace(5), 2) == 80.0
    assert sum(_grace(a) for a in range(lr._EXIT_SUBMIT_MAX_ATTEMPTS)) == 620.0


def test_at_the_cap_the_submit_is_not_a_proof_so_no_handback_can_follow(monkeypatch):
    _patch(monkeypatch)
    le = _frozen_le()
    le["exit_submit_attempts"] = lr._EXIT_SUBMIT_MAX_ATTEMPTS

    out = lr._submit_live_market_exit(
        None, _sess(), None, le=le, product_id="LBGJ", quantity=400.0,
        client_order_id="chili_ml_ta_22135_cap", reason="tape_accel_rollover",
        bid=2.78, ask=2.80, mid=2.79,
    )

    assert out["error"] == "exit_retry_cap_exceeded"
    assert lr._EXIT_PRE_PLACE_PROOF_KEY not in le

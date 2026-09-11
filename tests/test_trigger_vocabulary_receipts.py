"""[3] Trigger vocabulary receipts: every leg names its trigger, and the receipts can read it.

WHAT WAS WRONG (measured on the live book 2026-09-11, 30 d):

  * "97% of entries record no trigger" (1,081 / 1,110) was read from
    ``live_entry_submitted``, whose payload never carried one (0 / 146 in 30 d). The
    fill always did: ``live_entry_filled.trigger_reason`` 90 / 90. The TRADES were
    never trigger-less; two readers were blind.
  * The recycle appended the closed cycle AFTER ``_reset_entry_state_on_recycle`` had
    popped the leg's identity, so 61 / 61 closed cycles (32 sessions) recorded
    ``entry_order_id: null`` and no trigger, sizing or front-side tilt.
  * ``last_entry_trigger_reason`` (5b8b828fd) is written at DECISION time; it named a
    different trigger than the last fill in 13 / 32 sessions since 09-08.
  * The outcome extractor's 40-event window missed the last fill in 54 / 55 sessions
    (p50 417 newer events), while ``le["entry_fill_event_id"]`` equalled the last fill's
    id in 55 / 55.

None of this changes a decision; it only fixes receipts.

Pure tests first; the extraction and tick tests use the truncating ``db`` fixture
(TEST_DATABASE_URL, _test DB).
"""

from __future__ import annotations

import ast
import inspect
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from app.config import settings
from app.models.trading import (
    MomentumStrategyVariant,
    TradingAutomationEvent,
    TradingAutomationSession,
)
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural import outcome_extract as oe
from app.services.trading.momentum_neural.live_fsm import (
    STATE_LIVE_EXITED,
    STATE_LIVE_FINISHED,
    STATE_WATCHING_LIVE,
)
from app.services.trading.momentum_neural.risk_policy import RISK_SNAPSHOT_KEY


_SIZING = {"risk_usd": 390.0, "qty": 138, "stop_px": 4.12, "basis": "risk_first"}
_TILT = {
    "mult": 0.7593,
    "strength": 0.62,
    "ofi": -0.11,
    "er": 0.41,
    "vwap_dist_bps": 212.0,
    "inputs": {"n_prints": 40, "window": "prints"},
}


def _le_leg_about_to_recycle(**over) -> dict:
    """``le`` exactly as the recycle branch sees it: the leg that just closed still
    carries its identity, trade_cycles has just been incremented."""
    le = {
        # the leg's identity (all in _RECYCLE_ENTRY_STATE_KEYS)
        "entry_order_id": "a1b2c3d4-0000-4000-8000-00000000abcd",
        "entry_client_order_id": "chili_ml_e_20417_corr1234_0123456789",
        "entry_decision_packet_id": 88123,
        "entry_trigger_reason": "vwap_reclaim",
        "entry_trigger_debug": {"reclaim_px": 4.18},
        "entry_sizing": deepcopy(_SIZING),
        "frontside_size_tilt": deepcopy(_TILT),
        "entry_submitted": True,
        "position": {"quantity": 0.0},
        "exit_order_id": "exit-1",
        # P&L side (NOT in the reset set)
        "realized_pnl_usd": -12.5,
        "last_exit_reason": "stop",
        "last_exit_entry_price": 4.20,
        "last_exit_notional_basis_usd": 579.6,
        "stopout_cycles": 1,
        "trade_cycles": 1,
        # durable decision copy (NOT in the reset set)
        "last_entry_trigger_reason": "vwap_reclaim",
    }
    le.update(over)
    return le


# ── 1. the closed-cycle ledger records the leg BEFORE the reset erases it ──────


def test_closed_cycle_records_entry_order_id_and_trigger_before_recycle_reset():
    le = _le_leg_about_to_recycle()
    before = deepcopy(le)

    # runner order: append, THEN reset
    assert lr._append_closed_cycle(le, now_iso="2026-09-11T13:31:02+00:00") is True
    cleared = lr._reset_entry_state_on_recycle(le)

    cyc = le["closed_cycles"][-1]
    assert cyc["entry_order_id"] == before["entry_order_id"], (
        "61 / 61 cycles recorded null here because the append ran after the reset"
    )
    assert cyc["entry_trigger_reason"] == "vwap_reclaim"
    assert cyc["entry_client_order_id"] == before["entry_client_order_id"]
    assert cyc["entry_decision_packet_id"] == 88123
    assert cyc["cycle_index"] == 1
    assert cyc["closed_at_utc"] == "2026-09-11T13:31:02+00:00"
    # the P&L side is unchanged by moving the append earlier
    assert cyc["realized_pnl_usd_cumulative"] == pytest.approx(-12.5)
    assert cyc["last_exit_reason"] == "stop"
    assert cyc["last_exit_entry_price"] == pytest.approx(4.20)
    assert cyc["last_exit_notional_basis_usd"] == pytest.approx(579.6)
    assert cyc["stopout_cycles"] == 1

    # ...and the reset still clears EXACTLY what it cleared without the append
    control = deepcopy(before)
    control_cleared = lr._reset_entry_state_on_recycle(control)
    assert cleared == control_cleared
    for k in lr._CLOSED_CYCLE_ENTRY_IDENTITY_KEYS:
        assert k not in le, f"{k} must still be cleared on recycle"
    # the only key the append adds is the ledger itself
    assert set(le) - set(control) == {"closed_cycles"}


def test_every_copied_identity_key_is_one_the_reset_clears():
    """The copy exists BECAUSE the reset erases these; a key the reset does not clear
    would not need copying (and would mean the reset set drifted)."""
    for k in lr._CLOSED_CYCLE_ENTRY_IDENTITY_KEYS:
        assert k in lr._RECYCLE_ENTRY_STATE_KEYS, k
    assert "closed_cycles" not in lr._RECYCLE_ENTRY_STATE_KEYS


def test_closed_cycle_append_is_idempotent_and_bounded():
    le = _le_leg_about_to_recycle()
    assert lr._append_closed_cycle(le, now_iso="t1") is True
    assert lr._append_closed_cycle(le, now_iso="t2") is False, "same cycle_index twice"
    assert len(le["closed_cycles"]) == 1
    assert le["closed_cycles"][0]["closed_at_utc"] == "t1"

    # bounded to the newest _CLOSED_CYCLES_MAX
    le2 = {"closed_cycles": [{"cycle_index": i} for i in range(lr._CLOSED_CYCLES_MAX)]}
    le2["trade_cycles"] = lr._CLOSED_CYCLES_MAX
    assert lr._append_closed_cycle(le2, now_iso="t") is True
    assert len(le2["closed_cycles"]) == lr._CLOSED_CYCLES_MAX
    assert le2["closed_cycles"][0]["cycle_index"] == 1
    assert le2["closed_cycles"][-1]["cycle_index"] == lr._CLOSED_CYCLES_MAX


def test_closed_cycle_append_survives_junk_ledger_entries():
    """The old inline append did int(c.get("cycle_index", -1)) and raised on a junk
    entry, skipping the whole append; the helper reads indices defensively."""
    le = _le_leg_about_to_recycle(
        trade_cycles=3,
        closed_cycles=[None, "junk", {"cycle_index": "nope"}, {"cycle_index": 2}],
    )
    assert lr._append_closed_cycle(le, now_iso="t") is True
    assert le["closed_cycles"][-1]["cycle_index"] == 3
    assert lr._append_closed_cycle(le, now_iso="t") is False


def test_closed_cycle_carries_entry_sizing_and_frontside_tilt():
    le = _le_leg_about_to_recycle()
    lr._append_closed_cycle(le, now_iso="t")
    cyc = le["closed_cycles"][-1]
    assert cyc["entry_sizing"] == _SIZING
    assert cyc["frontside_size_tilt"] == _TILT
    # a deep copy — a later in-place edit of the live dict cannot rewrite history
    le["entry_sizing"]["qty"] = 1
    le["frontside_size_tilt"]["inputs"]["n_prints"] = 0
    assert cyc["entry_sizing"]["qty"] == 138
    assert cyc["frontside_size_tilt"]["inputs"]["n_prints"] == 40


def test_a_blank_trigger_is_recorded_as_null_not_as_a_name():
    le = _le_leg_about_to_recycle(entry_trigger_reason="")
    lr._append_closed_cycle(le, now_iso="t")
    assert le["closed_cycles"][-1]["entry_trigger_reason"] is None


def test_the_recycle_branch_appends_before_it_resets():
    """ORDER pin on the runner itself (same style as the reaper's recycle-stamp pin)."""
    src = inspect.getsource(lr.tick_live_session)
    i_append = src.index("_append_closed_cycle(le, now_iso=")
    i_reset = src.index("_reset_entry_state_on_recycle(le)")
    i_trans = src.index("_safe_transition(db, sess, STATE_WATCHING_LIVE)", i_reset)
    assert i_append < i_reset < i_trans
    # the old inline append (after the reset) is gone — the helper is the one writer
    assert 'le["closed_cycles"] =' not in src


# ── 2. the submission receipt names its trigger ──────────────────────────────


def _emit_payload_dicts(fn, event_type: str) -> list[ast.Dict]:
    tree = ast.parse(inspect.getsource(fn).lstrip())
    out: list[ast.Dict] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_emit"):
            continue
        if len(node.args) < 4:
            continue
        et = node.args[2]
        if isinstance(et, ast.Constant) and et.value == event_type and isinstance(node.args[3], ast.Dict):
            out.append(node.args[3])
    return out


def test_submitted_payload_carries_trigger_reason():
    dicts = _emit_payload_dicts(lr.tick_live_session, "live_entry_submitted")
    assert len(dicts) == 1, "exactly one live_entry_submitted emit (the phantom one is gone)"
    payload = dicts[0]
    keys = [k.value for k in payload.keys if isinstance(k, ast.Constant)]
    assert "trigger_reason" in keys, "0 / 146 submissions in 30 d carried a trigger key"
    val = payload.values[keys.index("trigger_reason")]
    assert ast.unparse(val) == "le.get('entry_trigger_reason')"


def test_the_fill_payload_still_carries_trigger_reason():
    """The fill is the per-trade truth (90 / 90); the extractor depends on it."""
    dicts = _emit_payload_dicts(lr.tick_live_session, "live_entry_filled")
    assert dicts, "live_entry_filled emit not found"
    for payload in dicts:
        keys = [k.value for k in payload.keys if isinstance(k, ast.Constant)]
        assert "trigger_reason" in keys


# ── 3. the outcome readers ────────────────────────────────────────────────────


def test_closed_cycle_summary_carries_per_cycle_trigger_and_outcome_row_summary_has_entry_trigger_reason():
    le = {
        "closed_cycles": [
            {"cycle_index": 1, "realized_pnl_usd_cumulative": -12.5, "last_exit_reason": "stop",
             "entry_order_id": "o-1", "entry_client_order_id": "c-1",
             "entry_trigger_reason": "abcd_break_tick_ok"},
            {"cycle_index": 2, "realized_pnl_usd_cumulative": 30.0, "last_exit_reason": "target",
             "entry_order_id": "o-2", "entry_client_order_id": "c-2",
             "entry_trigger_reason": "vwap_reclaim"},
        ]
    }
    summary = oe.closed_cycle_summary(le)
    per = summary["cycles"]
    assert [c["entry_trigger_reason"] for c in per] == ["abcd_break_tick_ok", "vwap_reclaim"]
    assert [c["entry_order_id"] for c in per] == ["o-1", "o-2"]
    assert [c["entry_client_order_id"] for c in per] == ["c-1", "c-2"]
    assert per[1]["realized_pnl_usd_this_cycle"] == pytest.approx(42.5)  # unchanged math

    extracted = {
        "session_id": 1, "user_id": None, "variant_id": 1, "symbol": "WYHG", "mode": "live",
        "execution_family": "alpaca_spot", "terminal_state": "live_finished",
        "terminal_at_utc": "2026-09-11T14:00:00", "outcome_class": "stop_loss",
        "entry_occurred": True, "closed_cycles_v1": summary,
        "entry_trigger_reason": "vwap_reclaim",
        "entry_trigger_reason_source": oe.ENTRY_TRIGGER_SOURCE_FILL_EVENT,
        "entry_trigger_fill_event_id": 991,
    }
    row = oe.outcome_row_from_extracted(extracted)
    s = row.extracted_summary_json
    assert s["entry_trigger_reason"] == "vwap_reclaim"
    assert s["entry_trigger_reason_source"] == "fill_event"
    assert s["entry_trigger_fill_event_id"] == 991
    assert s["closed_cycles_v1"]["cycles"][0]["entry_trigger_reason"] == "abcd_break_tick_ok"

    # no trigger read -> no key (same rule as closed_cycles_v1 on clean cancels)
    extracted2 = dict(extracted, entry_trigger_reason=None, entry_trigger_reason_source=None,
                      entry_trigger_fill_event_id=None, closed_cycles_v1={"count": 0})
    s2 = oe.outcome_row_from_extracted(extracted2).extracted_summary_json
    assert "entry_trigger_reason" not in s2 and "entry_trigger_reason_source" not in s2


def test_paper_mode_reads_the_paper_exec_trigger():
    out = oe.final_leg_entry_trigger(
        None, session_id=1, mode="paper",
        exec_dict={"entry_trigger_reason": "wick_reclaim"}, events=[],
    )
    assert out == {
        "entry_trigger_reason": "wick_reclaim",
        "entry_trigger_reason_source": oe.ENTRY_TRIGGER_SOURCE_PAPER_EXEC,
        "entry_trigger_fill_event_id": None,
    }


class _Ev:
    def __init__(self, event_type, payload, id_=None):
        self.event_type = event_type
        self.payload_json = payload
        self.id = id_


def test_live_fallbacks_carry_their_source_tag_without_a_db():
    # no fill event, no pointer -> le's per-decision key
    out = oe.final_leg_entry_trigger(
        None, session_id=1, mode="live",
        exec_dict={"entry_trigger_reason": "orb_break_tick_ok",
                   "last_entry_trigger_reason": "abcd_break"},
        events=[],
    )
    assert out["entry_trigger_reason"] == "orb_break_tick_ok"
    assert out["entry_trigger_reason_source"] == oe.ENTRY_TRIGGER_SOURCE_LIVE_EXEC
    # recycle cleared it ("" from a blank pass counts as absent) -> labelled decision copy
    out = oe.final_leg_entry_trigger(
        None, session_id=1, mode="live",
        exec_dict={"entry_trigger_reason": "", "last_entry_trigger_reason": "abcd_break"},
        events=[],
    )
    assert out["entry_trigger_reason"] == "abcd_break"
    assert out["entry_trigger_reason_source"] == oe.ENTRY_TRIGGER_SOURCE_DECISION_COPY
    # nothing at all
    out = oe.final_leg_entry_trigger(None, session_id=1, mode="live", exec_dict={}, events=[])
    assert out["entry_trigger_reason"] is None and out["entry_trigger_reason_source"] is None


def test_the_newest_fill_wins_even_over_le_and_never_reaches_back_past_it():
    events = [  # newest first
        _Ev("live_entry_trigger_wait", {"reason": "volume_below_1p5x_avg"}),
        _Ev("live_entry_filled", {"trigger_reason": "vwap_reclaim"}, 902),
        _Ev("live_entry_filled", {"trigger_reason": "abcd_break_tick_ok"}, 901),
    ]
    out = oe.final_leg_entry_trigger(
        None, session_id=1, mode="live",
        exec_dict={"entry_trigger_reason": "momentum_continuation",  # a later unfilled decision
                   "last_entry_trigger_reason": "momentum_continuation"},
        events=events,
    )
    assert out["entry_trigger_reason"] == "vwap_reclaim"
    assert out["entry_trigger_reason_source"] == oe.ENTRY_TRIGGER_SOURCE_FILL_EVENT
    assert out["entry_trigger_fill_event_id"] == 902

    # newest fill with a blank trigger: do NOT name the OLDER leg's trigger for it
    events[1] = _Ev("live_entry_filled", {"trigger_reason": ""}, 902)
    out = oe.final_leg_entry_trigger(
        None, session_id=1, mode="live",
        exec_dict={"last_entry_trigger_reason": "wick_reclaim"}, events=events,
    )
    assert out["entry_trigger_reason"] == "wick_reclaim"
    assert out["entry_trigger_reason_source"] == oe.ENTRY_TRIGGER_SOURCE_DECISION_COPY


# ── 4. extraction against the DB (fill event read by the durable pointer) ─────

_seq = 0


def _variant(db):
    global _seq
    _seq += 1
    v = MomentumStrategyVariant(
        family="test_family",
        variant_key=f"trigvocab_{_seq}",
        label="trigger vocabulary receipts",
        params_json={},
    )
    db.add(v)
    db.flush()
    return v


def _session(db, *, le, state=STATE_LIVE_FINISHED, symbol="WYHG"):
    v = _variant(db)
    sess = TradingAutomationSession(
        user_id=None,
        venue="test",
        execution_family="alpaca_spot",
        mode="live",
        symbol=symbol,
        variant_id=v.id,
        state=state,
        risk_snapshot_json={"momentum_live_execution": dict(le)},
        correlation_id="corr-trigvocab",
    )
    db.add(sess)
    db.flush()
    return sess


def _event(db, sess, event_type, payload, ts):
    ev = TradingAutomationEvent(
        session_id=sess.id, ts=ts, event_type=event_type, payload_json=payload,
        correlation_id="corr-trigvocab",
    )
    db.add(ev)
    db.flush()
    return ev


def test_extract_prefers_fill_event_trigger_over_decision_copy(db):
    t0 = datetime(2026, 9, 10, 13, 30, 0)
    sess = _session(db, le={
        "realized_pnl_usd": -9.0, "last_exit_entry_price": 4.2, "last_exit_reason": "stop",
        "last_entry_trigger_reason": "abcd_break",  # decision copy, written later
    })
    fill = _event(db, sess, "live_entry_filled",
                  {"order_id": "o-1", "trigger_reason": "vwap_reclaim"}, t0)
    _event(db, sess, "live_exit_filled", {"reason": "stop"}, t0 + timedelta(seconds=40))
    sess.risk_snapshot_json = {"momentum_live_execution": dict(
        sess.risk_snapshot_json["momentum_live_execution"], entry_fill_event_id=fill.id)}
    db.flush()

    out = oe.extract_momentum_session_outcome(db, sess)
    assert out["entry_trigger_reason"] == "vwap_reclaim"
    assert out["entry_trigger_reason_source"] == "fill_event"
    assert out["entry_trigger_fill_event_id"] == fill.id


def test_extract_reads_the_fill_by_pointer_when_it_is_outside_the_event_window(db):
    """54 / 55 live sessions: the last fill had >= 40 newer events behind it, so the
    window alone would have fallen through to the decision copy nearly every time."""
    t0 = datetime(2026, 9, 10, 13, 30, 0)
    sess = _session(db, le={
        "realized_pnl_usd": 14.0, "last_exit_entry_price": 4.2, "last_exit_reason": "target",
        "last_entry_trigger_reason": "momentum_continuation",  # a later unfilled decision
    })
    fill = _event(db, sess, "live_entry_filled",
                  {"order_id": "o-1", "trigger_reason": "double_bottom_break_tick_ok"}, t0)
    for i in range(45):  # the trigger-wait flood after the leg recycled
        _event(db, sess, "live_entry_trigger_wait", {"reason": "volume_below_1p5x_avg"},
               t0 + timedelta(seconds=60 + i))
    sess.risk_snapshot_json = {"momentum_live_execution": dict(
        sess.risk_snapshot_json["momentum_live_execution"], entry_fill_event_id=fill.id)}
    db.flush()

    window = oe.load_recent_automation_events(db, int(sess.id))
    assert all(ev.event_type != "live_entry_filled" for ev in window), "fill must be outside"

    out = oe.extract_momentum_session_outcome(db, sess)
    assert out["entry_trigger_reason"] == "double_bottom_break_tick_ok"
    assert out["entry_trigger_reason_source"] == "fill_event"
    assert out["entry_trigger_fill_event_id"] == fill.id


def test_a_pointer_to_another_sessions_fill_is_not_trusted(db):
    t0 = datetime(2026, 9, 10, 13, 30, 0)
    other = _session(db, le={}, symbol="OTHR")
    foreign = _event(db, other, "live_entry_filled", {"trigger_reason": "orb_break_tick_ok"}, t0)
    sess = _session(db, le={
        "realized_pnl_usd": -1.0, "last_exit_entry_price": 4.2,
        "entry_fill_event_id": foreign.id,
        "last_entry_trigger_reason": "wick_reclaim",
    })
    out = oe.extract_momentum_session_outcome(db, sess)
    assert out["entry_trigger_reason"] == "wick_reclaim"
    assert out["entry_trigger_reason_source"] == "decision_copy"
    assert out["entry_trigger_fill_event_id"] is None


def test_extract_falls_back_to_le_then_decision_copy_with_source_tag(db):
    sess = _session(db, le={
        "realized_pnl_usd": -3.0, "last_exit_entry_price": 4.2,
        "entry_trigger_reason": "pullback_break_tick_ok",
        "last_entry_trigger_reason": "pullback_break_tick_ok",
    })
    out = oe.extract_momentum_session_outcome(db, sess)
    assert (out["entry_trigger_reason"], out["entry_trigger_reason_source"]) == (
        "pullback_break_tick_ok", "live_exec")

    sess2 = _session(db, le={
        "realized_pnl_usd": -3.0, "last_exit_entry_price": 4.2,
        "last_entry_trigger_reason": "sub_vwap_trap_tick",
    })
    out2 = oe.extract_momentum_session_outcome(db, sess2)
    assert (out2["entry_trigger_reason"], out2["entry_trigger_reason_source"]) == (
        "sub_vwap_trap_tick", "decision_copy")


# ── 5. end to end through the runner's recycle branch ─────────────────────────


def test_tick_recycle_books_the_leg_identity_into_the_closed_cycle(
    monkeypatch, db, stable_non_alpaca_account_identity
):
    from app.services.trading.momentum_neural.persistence import (
        create_trading_automation_session,
    )
    from tests.test_momentum_live_runner import _mk_adapter, _uid
    from tests.test_momentum_paper_runner import _seed_live_eligible_row

    monkeypatch.setattr(lr, "_venue_broker_connected", lambda ef: True)
    monkeypatch.setattr(
        lr, "runner_boundary_risk_ok", lambda *_a, **_k: (True, {"allowed": True})
    )
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_recycle_entry_state_reset_enabled", True)

    symbol = "TRGV-USD"
    vid, _ = _seed_live_eligible_row(db, symbol=symbol)
    db.commit()
    uid = _uid(db, "trigvocab_e2e")
    le = _le_leg_about_to_recycle(trade_cycles=0, stopout_cycles=0)
    le["position"] = {"quantity": 221.0, "avg_entry_price": 2.21, "product_id": symbol}
    sess = create_trading_automation_session(
        db, user_id=uid, symbol=symbol, variant_id=vid, mode="live",
        state=STATE_LIVE_EXITED,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_live_execution": le,
        },
    )
    db.commit()

    ad = _mk_adapter()
    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active",
               return_value=False):
        lr.tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)

    assert sess.state == STATE_WATCHING_LIVE
    le_after = sess.risk_snapshot_json["momentum_live_execution"]
    # the reset still ran
    for k in ("entry_order_id", "entry_trigger_reason", "entry_sizing", "frontside_size_tilt",
              "entry_submitted", "position"):
        assert k not in le_after, f"{k} must be cleared after recycle"
    cyc = le_after["closed_cycles"][-1]
    assert cyc["cycle_index"] == 1
    assert cyc["entry_order_id"] == "a1b2c3d4-0000-4000-8000-00000000abcd"
    assert cyc["entry_client_order_id"] == "chili_ml_e_20417_corr1234_0123456789"
    assert cyc["entry_decision_packet_id"] == 88123
    assert cyc["entry_trigger_reason"] == "vwap_reclaim"
    assert cyc["entry_sizing"] == _SIZING
    assert cyc["frontside_size_tilt"] == _TILT
    assert cyc["realized_pnl_usd_cumulative"] == pytest.approx(-12.5)

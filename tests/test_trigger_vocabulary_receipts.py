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

REVIEW FIXES (same day):

  * The pointer is not always the FINAL leg's fill: recovery adoption (owner-claim /
    paused / self-heal) emits no ``live_entry_filled`` and never moves it — in 5 of the
    12 sessions (30 d) whose final leg is still on ``le`` it named an EARLIER leg's
    order (19471, 19480, 20245, 20994, 22028). The extractor now binds the trigger to
    the final leg's own order: its ``live_entry_submitted`` receipt first, the fill
    only when it is that order's fill.
  * Closed-cycle OID/CID must never become order authority to the non-Alpaca
    terminalization walker (a recycled RH/Coinbase session wedged in quarantine).
  * A submission receipt is not a setup trace (the audit doubled its findings).
  * Paper names the FILLED leg's trigger (6 / 17 paper sessions had a later
    submission after the last fill).
  * The broker-truth join covers every leg, all-or-nothing (it replaced a CUMULATIVE
    self-report with ONE leg's P&L).

None of this changes a decision; it only fixes receipts.

Pure tests first; the extraction and tick tests use the truncating ``db`` fixture
(TEST_DATABASE_URL, _test DB).
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.config import settings
from app.models.trading import (
    MomentumStrategyVariant,
    TradingAutomationEvent,
    TradingAutomationSession,
)
from app.services.trading.momentum_neural import automation_query as aq
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural import outcome_extract as oe
from app.services.trading.momentum_neural.live_fsm import (
    STATE_LIVE_CANCELLED,
    STATE_LIVE_EXITED,
    STATE_LIVE_FINISHED,
    STATE_WATCHING_LIVE,
)
from app.services.trading.momentum_neural.risk_policy import RISK_SNAPSHOT_KEY
from app.services.trading.momentum_neural.setup_trace_audit import (
    audit_setup_trace_events,
)


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


# ── 1b. the ledger is a RECEIPT, never order authority (review finding) ──────
#
# Before [3] the closed cycle recorded entry_order_id=null and no CID, so the non-Alpaca
# terminalization walker (which collects every *_order_id / client_order_id key it can
# reach) saw nothing there. From [3] the cycle carries both, and the recycle reset has
# already removed that leg's entry_orders_resolved / order expectation — so a walked
# closed leg is an unprovable filled entry: every cancel quarantines with
# `terminalization_filled_entry_adoption_authority_unproven` and applies operator_pause,
# the session never terminalizes, and its realized P&L is never booked.

_CLOSED_OID = "leg1-entry-oid"
_CLOSED_CID = "chili_ml_e_78101_leg1"


def _recycled_non_alpaca_le() -> dict:
    le = {
        "entry_order_id": _CLOSED_OID,
        "entry_client_order_id": _CLOSED_CID,
        "entry_order_ids_all": [_CLOSED_OID],
        "entry_orders_resolved": {_CLOSED_OID: "adopted"},
        "entry_trigger_reason": "vwap_reclaim",
        "entry_submitted": True,
        "entry_want_qty": 10.0,
        "position": None,
        "realized_pnl_usd": -3.0,
        "last_exit_reason": "stop",
        "last_exit_entry_price": 4.2,
        "stopout_cycles": 1,
        "trade_cycles": 1,
    }
    lr._append_closed_cycle(le, now_iso="2026-09-11T13:31:02+00:00")
    lr._reset_entry_state_on_recycle(le)
    return le


def test_the_terminalization_walker_never_collects_closed_cycle_identities():
    from tests.test_non_alpaca_terminalization_truth import _session

    le = _recycled_non_alpaca_le()
    # the receipt itself still names the leg's order (that is the point of [3])...
    assert le["closed_cycles"][-1]["entry_order_id"] == _CLOSED_OID
    assert le["closed_cycles"][-1]["entry_client_order_id"] == _CLOSED_CID
    # ...and the walker treats it as audit-only
    ids = aq._collect_non_alpaca_persisted_order_identities(_session(live_exec=le))
    assert ids["order_ids"] == [] and ids["client_order_ids"] == []
    assert ids["order_expectations"] == {} and ids["resolved_order_outcomes"] == {}
    assert ids["malformed"] is False
    assert ids["identity_loss"] is True, "same as the pre-[3] null-id ledger"


def test_a_recycled_non_alpaca_session_still_terminalizes(monkeypatch):
    from tests.test_non_alpaca_terminalization_truth import (
        _Db,
        _StrictAdapter,
        _configure,
        _order,
        _session,
    )

    session = _session(state=STATE_WATCHING_LIVE, live_exec=_recycled_non_alpaca_le())
    db = _Db(session)
    events: list = []
    # the closed leg's entry order is FILLED at the broker (that is what a leg is); the
    # account is flat and nothing is working.
    adapter = _StrictAdapter(
        orders={
            _CLOSED_OID: _order(oid=_CLOSED_OID, status="filled", filled=10.0, cid=_CLOSED_CID)
        },
        open_orders=[],
        position_quantity=0.0,
    )
    _configure(monkeypatch, session, events, adapter)
    monkeypatch.setattr(aq.settings, "chili_momentum_adopt_on_cancel_fill_enabled", True)

    first = aq.cancel_automation_session(db, user_id=1, session_id=session.id)
    assert first.get("quarantine_reason") != (
        "terminalization_filled_entry_adoption_authority_unproven"
    ), first
    # the identity-loss visibility grace elapses (backdate the first observation rather
    # than sleep through it)
    obs = session.risk_snapshot_json["momentum_live_execution"][
        "non_alpaca_identity_loss_observation"
    ]
    obs["first_observed_at_utc"] = (
        datetime.utcnow()
        - timedelta(seconds=aq._NON_ALPACA_IDENTITY_LOSS_VISIBILITY_GRACE_SECONDS + 1.0)
    ).isoformat()
    second = aq.cancel_automation_session(db, user_id=1, session_id=session.id)
    assert session.state == STATE_LIVE_CANCELLED, second


# ── 2. the outcome readers ────────────────────────────────────────────────────


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
    assert [c["entry_trigger_reason_source"] for c in per] == ["closed_cycle", "closed_cycle"]
    assert [c["entry_order_id"] for c in per] == ["o-1", "o-2"]
    assert [c["entry_client_order_id"] for c in per] == ["c-1", "c-2"]
    assert per[1]["realized_pnl_usd_this_cycle"] == pytest.approx(42.5)  # unchanged math

    extracted = {
        "session_id": 1, "user_id": None, "variant_id": 1, "symbol": "WYHG", "mode": "live",
        "execution_family": "alpaca_spot", "terminal_state": "live_finished",
        "terminal_at_utc": "2026-09-11T14:00:00", "outcome_class": "stop_loss",
        "entry_occurred": True, "closed_cycles_v1": summary,
        "entry_trigger_reason": "vwap_reclaim",
        "entry_trigger_reason_source": oe.ENTRY_TRIGGER_SOURCE_SUBMITTED_EVENT,
        "entry_trigger_event_id": 991,
        "entry_trigger_order_id": "o-2",
        "entry_trigger_stale_fill_event_id": 901,
    }
    row = oe.outcome_row_from_extracted(extracted)
    s = row.extracted_summary_json
    assert s["entry_trigger_reason"] == "vwap_reclaim"
    assert s["entry_trigger_reason_source"] == "submitted_event"
    assert s["entry_trigger_event_id"] == 991
    assert s["entry_trigger_order_id"] == "o-2"
    assert s["entry_trigger_stale_fill_event_id"] == 901
    assert s["closed_cycles_v1"]["cycles"][0]["entry_trigger_reason"] == "abcd_break_tick_ok"

    # no trigger read -> no key (same rule as closed_cycles_v1 on clean cancels)
    extracted2 = dict(extracted, entry_trigger_reason=None, entry_trigger_reason_source=None,
                      entry_trigger_event_id=None, entry_trigger_order_id=None,
                      entry_trigger_stale_fill_event_id=None, closed_cycles_v1={"count": 0})
    s2 = oe.outcome_row_from_extracted(extracted2).extracted_summary_json
    assert "entry_trigger_reason" not in s2 and "entry_trigger_reason_source" not in s2
    assert "entry_trigger_order_id" not in s2


def test_paper_mode_reads_the_fill_time_stamp_before_the_decision_key():
    """pe["entry_trigger_reason"] is written at WATCHING -> PENDING_ENTRY and every
    later decision overwrites it, filled or not (6 of 17 paper sessions with a fill had
    a later submission). The fill-time stamp is the filled leg's."""
    out = oe.final_leg_entry_trigger(
        None, session_id=1, mode="paper",
        exec_dict={"entry_fill_trigger_reason": "wick_reclaim",
                   "entry_trigger_reason": "abcd_break"},  # a later, unfilled decision
        events=[],
    )
    assert out["entry_trigger_reason"] == "wick_reclaim"
    assert out["entry_trigger_reason_source"] == oe.ENTRY_TRIGGER_SOURCE_PAPER_FILL
    assert out["entry_trigger_event_id"] is None

    # no fill stamp (a session from before it existed): labelled as a DECISION value
    out = oe.final_leg_entry_trigger(
        None, session_id=1, mode="paper",
        exec_dict={"entry_trigger_reason": "abcd_break"}, events=[],
    )
    assert out["entry_trigger_reason"] == "abcd_break"
    assert out["entry_trigger_reason_source"] == oe.ENTRY_TRIGGER_SOURCE_PAPER_DECISION


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
    assert out["entry_trigger_event_id"] == 902

    # newest fill with a blank trigger: do NOT name the OLDER leg's trigger for it
    events[1] = _Ev("live_entry_filled", {"trigger_reason": ""}, 902)
    out = oe.final_leg_entry_trigger(
        None, session_id=1, mode="live",
        exec_dict={"last_entry_trigger_reason": "wick_reclaim"}, events=events,
    )
    assert out["entry_trigger_reason"] == "wick_reclaim"
    assert out["entry_trigger_reason_source"] == oe.ENTRY_TRIGGER_SOURCE_DECISION_COPY


# ── 2b. the trigger is bound to the FINAL leg's own order (review findings) ────


def test_final_filled_leg_identity_names_the_proven_filled_order():
    # still on le and adopted (every fill path marks it) -> the current leg
    leg = oe.final_filled_leg_identity({
        "entry_order_id": "o-2", "entry_client_order_id": "c-2",
        "entry_orders_resolved": {"o-2": "adopted"},
        "closed_cycles": [{"cycle_index": 1, "entry_order_id": "o-1"}],
    })
    assert (leg["leg"], leg["order_id"], leg["client_order_id"]) == ("current", "o-2", "c-2")
    # recycled -> the last closed cycle
    leg = oe.final_filled_leg_identity({
        "closed_cycles": [
            {"cycle_index": 1, "entry_order_id": "o-1", "entry_client_order_id": "c-1"},
            {"cycle_index": 2, "entry_order_id": "o-2", "entry_client_order_id": "c-2",
             "entry_trigger_reason": "abcd_break"},
        ],
    })
    assert (leg["leg"], leg["order_id"], leg["cycle_trigger"]) == (
        "closed_cycle", "o-2", "abcd_break")
    # a later order that is merely RESTING (unresolved) after the recycle is not a
    # filled leg; anchoring on it would reject the real last fill
    leg = oe.final_filled_leg_identity({
        "entry_order_id": "o-3", "entry_orders_resolved": {},
        "closed_cycles": [{"cycle_index": 2, "entry_order_id": "o-2"}],
    })
    assert (leg["leg"], leg["order_id"]) == ("closed_cycle", "o-2")
    # a pre-[3] cycle (null identity) -> unknown
    leg = oe.final_filled_leg_identity({"closed_cycles": [{"cycle_index": 1,
                                                           "entry_order_id": None}]})
    assert leg["leg"] is None and leg["order_id"] is None


def test_an_adopted_final_leg_is_not_labelled_with_the_previous_legs_fill():
    """Reviewer case: leg 1 fills normally (the pointer), recycles; leg 2 is submitted on
    wick_reclaim as o-2, the session is paused, o-2's fill is adopted while paused —
    which emits no live_entry_filled and never moves the pointer."""
    events = [  # newest first
        _Ev("live_entry_fill_adopted_while_paused", {"order_id": "o-2", "filled_size": 50}),
        _Ev("live_recycled", {}),
        _Ev("live_exit_filled", {"reason": "stop"}),
        _Ev("live_entry_filled", {"order_id": "o-1", "trigger_reason": "abcd_break_tick_ok"}, 901),
    ]
    le = {"entry_order_id": "o-2", "entry_client_order_id": "c-2",
          "entry_orders_resolved": {"o-2": "adopted"},  # _adopt_recovered_primary_fill_for_safety
          "entry_trigger_reason": "pullback_break_tick_ok",  # decided while o-2 rested
          "last_entry_trigger_reason": "pullback_break_tick_ok",
          "entry_fill_event_id": 901,
          "closed_cycles": [{"cycle_index": 1, "entry_order_id": "o-1",
                             "entry_trigger_reason": "abcd_break_tick_ok"}]}
    submitted = {
        "by_order_id": {"o-2": {"event_id": 950, "order_id": "o-2", "client_order_id": "c-2",
                                "trigger_reason": "wick_reclaim"}},
        "by_client_order_id": {"c-2": {"event_id": 950, "order_id": "o-2",
                                       "client_order_id": "c-2", "trigger_reason": "wick_reclaim"}},
    }
    out = oe.final_leg_entry_trigger(None, session_id=1, mode="live", exec_dict=le,
                                     events=events, submitted=submitted)
    assert out["entry_trigger_reason"] == "wick_reclaim"
    assert out["entry_trigger_reason_source"] == oe.ENTRY_TRIGGER_SOURCE_SUBMITTED_EVENT
    assert out["entry_trigger_event_id"] == 950
    assert out["entry_trigger_order_id"] == "o-2"

    # a pre-[3] submission receipt (no trigger): the earlier leg's fill is STILL refused;
    # what remains is le's decision-time value, labelled as such
    out = oe.final_leg_entry_trigger(None, session_id=1, mode="live", exec_dict=le,
                                     events=events, submitted=oe._empty_submitted())
    assert out["entry_trigger_reason"] == "pullback_break_tick_ok"
    assert out["entry_trigger_reason_source"] == oe.ENTRY_TRIGGER_SOURCE_LIVE_EXEC
    assert out["entry_trigger_stale_fill_event_id"] == 901
    assert out["entry_trigger_event_id"] is None


def test_the_submission_is_matched_by_broker_order_id_before_client_id():
    """The late-fill sweep re-points entry_order_id at the abandoned order WITHOUT
    touching entry_client_order_id, so the client id can name a newer submission."""
    submitted = {
        "by_order_id": {
            "o-1": {"event_id": 11, "order_id": "o-1", "client_order_id": "c-1",
                    "trigger_reason": "abcd_break_tick_ok"},
            "o-2": {"event_id": 12, "order_id": "o-2", "client_order_id": "c-2",
                    "trigger_reason": "momentum_continuation"},
        },
        "by_client_order_id": {
            "c-1": {"event_id": 11, "order_id": "o-1", "client_order_id": "c-1",
                    "trigger_reason": "abcd_break_tick_ok"},
            "c-2": {"event_id": 12, "order_id": "o-2", "client_order_id": "c-2",
                    "trigger_reason": "momentum_continuation"},
        },
    }
    rec = oe._match_leg_submission(submitted, order_id="o-1", client_order_id="c-2")
    assert rec["event_id"] == 11
    # the client id alone never binds a submission whose own order is a DIFFERENT one
    only_c2 = {"by_order_id": {}, "by_client_order_id": dict(submitted["by_client_order_id"])}
    assert oe._match_leg_submission(only_c2, order_id="o-1", client_order_id="c-2") is None
    # ...but it does when the leg's order id is unknown (ack-lost, bound later)
    assert oe._match_leg_submission(only_c2, order_id=None, client_order_id="c-2")["event_id"] == 12


def test_a_recycled_final_leg_accepts_its_own_fill_and_refuses_an_older_one():
    le = {"closed_cycles": [
        {"cycle_index": 1, "entry_order_id": "o-1", "entry_trigger_reason": "abcd_break"},
        {"cycle_index": 2, "entry_order_id": "o-2", "entry_trigger_reason": "vwap_reclaim"},
    ], "entry_trigger_reason": "momentum_continuation",  # a post-recycle decision
       "last_entry_trigger_reason": "momentum_continuation"}
    own = [_Ev("live_entry_filled", {"order_id": "o-2", "trigger_reason": "vwap_reclaim"}, 22)]
    out = oe.final_leg_entry_trigger(None, session_id=1, mode="live", exec_dict=le, events=own)
    assert (out["entry_trigger_reason"], out["entry_trigger_reason_source"]) == (
        "vwap_reclaim", "fill_event")
    assert out["entry_trigger_order_id"] == "o-2"

    older = [_Ev("live_entry_filled", {"order_id": "o-1", "trigger_reason": "abcd_break"}, 21)]
    out = oe.final_leg_entry_trigger(None, session_id=1, mode="live", exec_dict=le, events=older)
    # the runner's copy on the cycle — never le's post-recycle decision
    assert (out["entry_trigger_reason"], out["entry_trigger_reason_source"]) == (
        "vwap_reclaim", "closed_cycle")
    assert out["entry_trigger_stale_fill_event_id"] == 21


# ── 3. extraction against the DB (fill event read by the durable pointer) ─────

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


def _set_le(db, sess, **over):
    sess.risk_snapshot_json = {"momentum_live_execution": dict(
        sess.risk_snapshot_json["momentum_live_execution"], **over)}
    db.flush()


def test_extract_prefers_fill_event_trigger_over_decision_copy(db):
    t0 = datetime(2026, 9, 10, 13, 30, 0)
    sess = _session(db, le={
        "realized_pnl_usd": -9.0, "last_exit_entry_price": 4.2, "last_exit_reason": "stop",
        "last_entry_trigger_reason": "abcd_break",  # decision copy, written later
    })
    fill = _event(db, sess, "live_entry_filled",
                  {"order_id": "o-1", "trigger_reason": "vwap_reclaim"}, t0)
    _event(db, sess, "live_exit_filled", {"reason": "stop"}, t0 + timedelta(seconds=40))
    _set_le(db, sess, entry_fill_event_id=fill.id)

    out = oe.extract_momentum_session_outcome(db, sess)
    assert out["entry_trigger_reason"] == "vwap_reclaim"
    assert out["entry_trigger_reason_source"] == "fill_event"
    assert out["entry_trigger_event_id"] == fill.id


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
    _set_le(db, sess, entry_fill_event_id=fill.id)

    window = oe.load_recent_automation_events(db, int(sess.id))
    assert all(ev.event_type != "live_entry_filled" for ev in window), "fill must be outside"

    out = oe.extract_momentum_session_outcome(db, sess)
    assert out["entry_trigger_reason"] == "double_bottom_break_tick_ok"
    assert out["entry_trigger_reason_source"] == "fill_event"
    assert out["entry_trigger_event_id"] == fill.id


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
    assert out["entry_trigger_event_id"] is None


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


def test_extract_refuses_a_stale_pointer_and_reads_the_final_legs_submission_22028(db):
    """Session 22028 (2026-09-10), in shape: leg 1 order 3ad3f45f filled normally on
    abcd_break_tick_ok (the pointer, 1757383) and recycled; the final order 19b7c310 was
    submitted 22:27:25 on wedge_break_tick, candidates kept firing while it rested, and
    the fill was adopted by owner claim 22:56:36 — no live_entry_filled, pointer never
    moved, le.entry_trigger_reason = pullback_break_tick_ok. The PR as first written
    returned abcd_break_tick_ok with source 'fill_event'."""
    t0 = datetime(2026, 9, 10, 22, 0, 0)
    leg1_oid, leg2_oid = "3ad3f45f-5b27-47b4-88a9-aaaae93238a1", "19b7c310-91ea-4cb5-8f8f-061a8bd74037"
    leg1_cid, leg2_cid = "chili_ml_e_22028_70935da1_1111", "chili_ml_e_22028_70935da1_75ef"
    sess = _session(db, le={
        "realized_pnl_usd": -40.0, "last_exit_entry_price": 4.2, "last_exit_reason": "stop",
        "entry_order_id": leg2_oid, "entry_client_order_id": leg2_cid,
        "entry_orders_resolved": {leg2_oid: "adopted"},
        "entry_trigger_reason": "pullback_break_tick_ok",
        "last_entry_trigger_reason": "pullback_break_tick_ok",
        "closed_cycles": [{"cycle_index": 1, "entry_order_id": leg1_oid,
                           "entry_client_order_id": leg1_cid,
                           "entry_trigger_reason": "abcd_break_tick_ok"}],
    })
    sub1 = _event(db, sess, "live_entry_submitted", {
        "client_order_id": leg1_cid, "result": {"ok": True, "order_id": leg1_oid},
        "trigger_reason": "abcd_break_tick_ok"}, t0)
    fill1 = _event(db, sess, "live_entry_filled", {
        "order_id": leg1_oid, "trigger_reason": "abcd_break_tick_ok"}, t0 + timedelta(seconds=2))
    sub2 = _event(db, sess, "live_entry_submitted", {
        "client_order_id": leg2_cid, "result": {"ok": True, "order_id": leg2_oid},
        "trigger_reason": "wedge_break_tick"}, t0 + timedelta(minutes=27))
    for i in range(45):  # candidates kept firing while the order rested
        _event(db, sess, "live_entry_candidate_detected", {"trigger_reason": "absorption_snap_tick"},
               t0 + timedelta(minutes=28, seconds=i))
    _event(db, sess, "alpaca_owner_claim_primary_fill_adopted",
           {"order_id": leg2_oid, "client_order_id": leg2_cid}, t0 + timedelta(minutes=56))
    _set_le(db, sess, entry_fill_event_id=fill1.id)

    out = oe.extract_momentum_session_outcome(db, sess)
    assert out["entry_trigger_reason"] == "wedge_break_tick"
    assert out["entry_trigger_reason_source"] == "submitted_event"
    assert out["entry_trigger_event_id"] == sub2.id
    assert out["entry_trigger_order_id"] == leg2_oid
    # the closed leg is bound to its own submission too
    cyc = out["closed_cycles_v1"]["cycles"][0]
    assert (cyc["entry_trigger_reason"], cyc["entry_trigger_reason_source"],
            cyc["entry_trigger_event_id"]) == ("abcd_break_tick_ok", "submitted_event", sub1.id)

    # the same session with pre-[3] receipts (no trigger on the submission): the
    # earlier leg's fill is refused and recorded; le's value is labelled decision-time
    for ev in (sub1, sub2):
        ev.payload_json = {k: v for k, v in ev.payload_json.items() if k != "trigger_reason"}
    db.flush()
    out = oe.extract_momentum_session_outcome(db, sess)
    assert out["entry_trigger_reason"] == "pullback_break_tick_ok"
    assert out["entry_trigger_reason_source"] == "live_exec"
    assert out["entry_trigger_stale_fill_event_id"] == fill1.id
    cyc = out["closed_cycles_v1"]["cycles"][0]
    assert (cyc["entry_trigger_reason"], cyc["entry_trigger_reason_source"]) == (
        "abcd_break_tick_ok", "closed_cycle")


def test_every_closed_leg_is_bound_to_its_own_submission(db):
    """The runner's per-cycle copy is le's value at the recycle — a decision-time value
    for a leg adopted while the session kept deciding. The receipt of the leg's own
    order wins; a leg whose submission carried no trigger keeps the copy, labelled."""
    t0 = datetime(2026, 9, 11, 13, 30, 0)
    sess = _session(db, state=STATE_LIVE_CANCELLED, le={
        "realized_pnl_usd": 5.0, "last_exit_entry_price": 4.2, "trade_cycles": 3,
        "closed_cycles": [
            {"cycle_index": 1, "realized_pnl_usd_cumulative": -5.0,
             "entry_order_id": "o-1", "entry_client_order_id": "c-1",
             "entry_trigger_reason": "abcd_break_tick_ok"},
            {"cycle_index": 2, "realized_pnl_usd_cumulative": 9.0,
             "entry_order_id": "o-2", "entry_client_order_id": "c-2",
             "entry_trigger_reason": "pullback_break_tick_ok"},  # stale copy (adopted leg)
            {"cycle_index": 3, "realized_pnl_usd_cumulative": 5.0,
             "entry_order_id": "o-3", "entry_client_order_id": "c-3",
             "entry_trigger_reason": "vwap_reclaim"},
        ],
    })
    s1 = _event(db, sess, "live_entry_submitted", {
        "client_order_id": "c-1", "result": {"ok": True, "order_id": "o-1"},
        "trigger_reason": "abcd_break_tick_ok"}, t0)
    s2 = _event(db, sess, "live_entry_submitted", {
        "client_order_id": "c-2", "result": {"ok": True, "order_id": "o-2"},
        "trigger_reason": "wedge_break_tick"}, t0 + timedelta(minutes=5))
    _event(db, sess, "live_entry_submitted", {  # a pre-[3] receipt
        "client_order_id": "c-3", "result": {"ok": True, "order_id": "o-3"}},
        t0 + timedelta(minutes=10))
    # another session's submission on the same order id is never read
    other = _session(db, le={}, symbol="OTHR")
    _event(db, other, "live_entry_submitted", {
        "client_order_id": "c-3", "result": {"ok": True, "order_id": "o-3"},
        "trigger_reason": "orb_break_tick_ok"}, t0)

    out = oe.extract_momentum_session_outcome(db, sess)
    per = out["closed_cycles_v1"]["cycles"]
    assert [(c["entry_trigger_reason"], c["entry_trigger_reason_source"]) for c in per] == [
        ("abcd_break_tick_ok", "submitted_event"),
        ("wedge_break_tick", "submitted_event"),
        ("vwap_reclaim", "closed_cycle"),
    ]
    assert [c["entry_trigger_event_id"] for c in per] == [s1.id, s2.id, None]
    # the final leg is the last closed cycle
    assert out["entry_trigger_order_id"] == "o-3"
    assert (out["entry_trigger_reason"], out["entry_trigger_reason_source"]) == (
        "vwap_reclaim", "closed_cycle")


# ── 4. the broker-truth join covers every leg (review finding) ────────────────


class _TradesDb:
    """Stands in for `trading_trades`: closed pnl by broker_order_id."""

    def __init__(self, pnl_by_oid):
        self.pnl_by_oid = dict(pnl_by_oid)
        self.asked: list[str] = []

    def execute(self, _stmt, params):
        oid = params["oid"]
        self.asked.append(oid)
        row = (self.pnl_by_oid[oid],) if oid in self.pnl_by_oid else None
        return SimpleNamespace(fetchone=lambda: row)


def test_broker_truth_single_leg_is_unchanged():
    db = _TradesDb({"o-1": 12.5})
    le = {"entry_order_id": "o-1", "entry_orders_resolved": {"o-1": "adopted"},
          "realized_pnl_usd": 12.0}
    assert oe._broker_truth_realized_for_session(db, None, le) == pytest.approx(12.5)
    assert db.asked == ["o-1"]


def test_broker_truth_sums_every_leg_instead_of_replacing_the_cumulative_with_one():
    """`realized_pnl_usd` is CUMULATIVE across legs; the old join matched only
    le.entry_order_id and so replaced leg1 + leg2 with leg2 alone."""
    db = _TradesDb({"o-1": -20.0, "o-2": 30.0})
    le = {"entry_order_id": "o-2", "entry_orders_resolved": {"o-2": "adopted"},
          "realized_pnl_usd": 10.0,
          "closed_cycles": [{"cycle_index": 1, "entry_order_id": "o-1"}]}
    assert oe._broker_truth_realized_for_session(db, None, le) == pytest.approx(10.0)
    assert db.asked == ["o-1", "o-2"]


def test_broker_truth_joins_a_recycled_session_through_its_closed_cycles():
    """The recycle pops le.entry_order_id, so the old join never ran for a session that
    ended in watching_live after its last leg (arm expired, cancelled)."""
    db = _TradesDb({"o-1": -20.0, "o-2": 30.0})
    le = {"realized_pnl_usd": 10.0, "closed_cycles": [
        {"cycle_index": 1, "entry_order_id": "o-1"},
        {"cycle_index": 2, "entry_order_id": "o-2"},
    ]}
    assert oe._broker_truth_realized_for_session(db, None, le) == pytest.approx(10.0)


def test_broker_truth_is_all_or_nothing():
    # a pre-[3] cycle: the leg happened and cannot be joined -> the self-report stands
    db = _TradesDb({"o-2": 30.0})
    le = {"entry_order_id": "o-2", "entry_orders_resolved": {"o-2": "adopted"},
          "closed_cycles": [{"cycle_index": 1, "entry_order_id": None}]}
    assert oe._broker_truth_realized_for_session(db, None, le) is None
    assert db.asked == [], "no query when a leg cannot be named"
    # a leg with no Trade row -> None, never a partial sum
    db = _TradesDb({"o-1": -20.0})
    le = {"entry_order_id": "o-2", "entry_orders_resolved": {"o-2": "adopted"},
          "closed_cycles": [{"cycle_index": 1, "entry_order_id": "o-1"}]}
    assert oe._broker_truth_realized_for_session(db, None, le) is None
    # a zero-fill (void) submission on le is not a leg
    db = _TradesDb({"o-1": -20.0})
    le = {"entry_order_id": "o-9", "entry_orders_resolved": {"o-9": "void"},
          "closed_cycles": [{"cycle_index": 1, "entry_order_id": "o-1"}]}
    assert oe._broker_truth_realized_for_session(db, None, le) == pytest.approx(-20.0)
    assert oe.session_leg_entry_order_ids({}) == []


# ── 5. a submission receipt is not a setup trace (review finding) ─────────────


def test_a_submission_receipt_with_a_trigger_is_not_a_setup_trace():
    base = {"client_order_id": "c-1", "order_type": "limit", "limit_price": "4.21",
            "result": {"ok": True, "order_id": "o-1"}, "sizing": dict(_SIZING)}
    pre = [{"session_id": 1, "ts": "2026-09-11T13:30:00", "event_type": "live_entry_submitted",
            "payload_json": dict(base)}]
    post = [{"session_id": 1, "ts": "2026-09-11T13:30:00", "event_type": "live_entry_submitted",
             "payload_json": dict(base, trigger_reason="abcd_break_tick_ok")}]
    r_pre, r_post = audit_setup_trace_events(pre), audit_setup_trace_events(post)
    assert r_post.traces_seen == r_pre.traces_seen == 0
    assert r_post.findings == r_pre.findings == []
    assert r_post.lifecycle_summary == r_pre.lifecycle_summary
    # control: the same alias on a wait event IS a setup trace and is audited
    wait = audit_setup_trace_events([{
        "session_id": 1, "event_type": "live_entry_trigger_wait",
        "payload_json": {"trigger_reason": "abcd_break_tick_ok"}}])
    assert wait.traces_seen == 1
    assert "setup_alias_missing_structural_stop" in wait.finding_reasons


# ── 6. end to end through the runner ──────────────────────────────────────────


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
    # ...and the cycle was booked BEFORE it (an append after the reset would read null)
    cyc = le_after["closed_cycles"][-1]
    assert cyc["cycle_index"] == 1
    assert cyc["entry_order_id"] == "a1b2c3d4-0000-4000-8000-00000000abcd"
    assert cyc["entry_client_order_id"] == "chili_ml_e_20417_corr1234_0123456789"
    assert cyc["entry_decision_packet_id"] == 88123
    assert cyc["entry_trigger_reason"] == "vwap_reclaim"
    assert cyc["entry_sizing"] == _SIZING
    assert cyc["frontside_size_tilt"] == _TILT
    assert cyc["realized_pnl_usd_cumulative"] == pytest.approx(-12.5)


def test_tick_submission_receipt_names_its_trigger_and_binds_the_filled_leg(
    monkeypatch, db, stable_non_alpaca_account_identity
):
    """Behaviour, not source text: a real PENDING_ENTRY tick submits, the next tick
    fills, and the outcome extractor reads the trigger bound to THAT order even after
    a later decision has overwritten le's copies."""
    from tests.test_momentum_limit_entry import _mk_pending_entry_session
    from tests.test_momentum_live_runner import _mk_adapter
    from app.services.trading.venue.coinbase_spot import (
        reset_duplicate_client_order_guard_for_tests,
    )

    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(lr, "_venue_broker_connected", lambda _ef: True)
    monkeypatch.setattr(
        lr, "runner_boundary_risk_ok", lambda *_a, **_k: (True, {"allowed": True})
    )
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.risk_policy._account_equity_usd",
        lambda *_args, **_kwargs: 10_000.0,
    )
    monkeypatch.setattr(settings, "brain_enable_decision_ledger", False)
    monkeypatch.setattr(settings, "chili_coinbase_maker_only_enabled", False)
    monkeypatch.setattr(settings, "brain_decision_packet_required_for_runners", False)

    sess = _mk_pending_entry_session(db, "SOL-USD")
    snap = dict(sess.risk_snapshot_json)
    snap["momentum_live_execution"] = dict(
        snap["momentum_live_execution"], entry_trigger_reason="vwap_reclaim"
    )
    sess.risk_snapshot_json = snap
    db.commit()

    ad = _mk_adapter()  # places "ord-entry-1", then reports it FILLED
    with patch("app.services.trading.momentum_neural.live_runner.is_kill_switch_active",
               return_value=False):
        lr.tick_live_session(db, sess.id, adapter_factory=lambda: ad)
        db.commit()
        submitted = (
            db.query(TradingAutomationEvent)
            .filter(TradingAutomationEvent.session_id == sess.id,
                    TradingAutomationEvent.event_type == "live_entry_submitted")
            .all()
        )
        assert len(submitted) == 1, [s.payload_json for s in submitted]
        sub = submitted[0]
        assert sub.payload_json["trigger_reason"] == "vwap_reclaim", (
            "0 / 146 submissions in 30 d carried a trigger key"
        )
        assert sub.payload_json["result"]["order_id"] == "ord-entry-1"

        lr.tick_live_session(db, sess.id, adapter_factory=lambda: ad)
        db.commit()
    db.refresh(sess)
    fills = (
        db.query(TradingAutomationEvent)
        .filter(TradingAutomationEvent.session_id == sess.id,
                TradingAutomationEvent.event_type == "live_entry_filled")
        .all()
    )
    assert len(fills) == 1
    assert fills[0].payload_json["trigger_reason"] == "vwap_reclaim"
    assert fills[0].payload_json["order_id"] == "ord-entry-1"
    le_now = sess.risk_snapshot_json["momentum_live_execution"]
    assert le_now["entry_fill_event_id"] == fills[0].id
    assert le_now["entry_orders_resolved"]["ord-entry-1"] == "adopted"

    # a later decision overwrites le's decision-time copies (the resting-order class)
    snap = dict(sess.risk_snapshot_json)
    snap["momentum_live_execution"] = dict(
        le_now, entry_trigger_reason="momentum_continuation",
        last_entry_trigger_reason="momentum_continuation",
    )
    sess.risk_snapshot_json = snap
    sess.state = STATE_LIVE_FINISHED
    db.flush()

    out = oe.extract_momentum_session_outcome(db, sess)
    assert out["entry_trigger_reason"] == "vwap_reclaim"
    assert out["entry_trigger_reason_source"] == "submitted_event"
    assert out["entry_trigger_event_id"] == sub.id
    assert out["entry_trigger_order_id"] == "ord-entry-1"


def test_paper_fill_stamps_the_filled_legs_trigger(monkeypatch, db):
    """The paper runner writes pe.entry_trigger_reason at the DECISION; the fill now
    stamps the filled leg's own copy, which a later decision cannot overwrite."""
    from app.models.trading import MomentumSymbolViability
    from app.services.trading.momentum_neural.operator_actions import (
        create_paper_draft_session,
    )
    from app.services.trading.momentum_neural.paper_runner import tick_paper_session
    from tests.test_momentum_paper_runner import (
        PAPER_FILL_SYMBOL,
        _entry_gate_pass_df,
        _seed_live_eligible_row,
        _uid,
    )

    monkeypatch.setattr(settings, "chili_momentum_paper_runner_enabled", True)
    ohlcv = _entry_gate_pass_df()
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.entry_gates.fetch_ohlcv_df",
        lambda *_args, **_kwargs: ohlcv,
    )
    monkeypatch.setattr(
        "app.services.trading.market_data.fetch_ohlcv_df",
        lambda *_args, **_kwargs: ohlcv,
    )
    vid, _ = _seed_live_eligible_row(db, symbol=PAPER_FILL_SYMBOL)
    via = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == PAPER_FILL_SYMBOL,
                MomentumSymbolViability.variant_id == vid)
        .one()
    )
    via.viability_score = 0.95
    via.paper_eligible = True
    via.regime_snapshot_json = {
        "atr_pct": 0.02,
        "chop_expansion": "trend",
        "volatility_regime": "normal",
        "meta": {"atr_pct": 0.02, "chop_expansion": "trend"},
    }
    db.commit()
    uid = _uid(db, "trigvocab_paper")
    r = create_paper_draft_session(db, user_id=uid, symbol=PAPER_FILL_SYMBOL, variant_id=vid)
    sid = r["session_id"]
    db.commit()

    def qfn(_s: str) -> dict:
        return {"mid": 125.0, "bid": 124.95, "ask": 125.05, "source": "massive"}

    pe = {}
    for _ in range(6):
        tick_paper_session(db, sid, quote_fn=qfn)
        db.commit()
        sess = db.query(TradingAutomationSession).filter_by(id=sid).one()
        pe = (sess.risk_snapshot_json or {}).get("momentum_paper_execution") or {}
        if isinstance(pe.get("position"), dict):
            break
    assert isinstance(pe.get("position"), dict), "the paper runner never filled"
    stamped = pe.get("entry_fill_trigger_reason")
    assert stamped and stamped == str(pe.get("entry_trigger_reason")).strip()

    # a later, unfilled decision overwrites the decision key; the fill stamp stands
    later = dict(pe, entry_trigger_reason="momentum_continuation")
    out = oe.final_leg_entry_trigger(db, session_id=sid, mode="paper", exec_dict=later,
                                     events=[])
    assert out["entry_trigger_reason"] == stamped
    assert out["entry_trigger_reason_source"] == oe.ENTRY_TRIGGER_SOURCE_PAPER_FILL

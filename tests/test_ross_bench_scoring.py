"""Known-answer tests for the Ross Parity Bench scorer.

The four anchor cases below are lifted from ``ross_master_ledger.json``'s ``xref``
layer and their divergence stage is the answer that audit already established.  The
event fixtures are SYNTHETIC and built inline on purpose: this suite must never touch a
database, and it must keep working when the live tape ages out of IQFeed's 180-day
retention.  Each fixture's timestamps and counts are transcribed from the ledger's
``mechanism`` prose for that symbol-day so a reader can check the fixture against the
source of truth without running anything.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.trading.momentum_neural.ross_bench_scoring import (
    ACCOUNT_MIXED,
    ACCOUNT_UNKNOWN,
    DIVERGENCE_SCHEMA,
    LIVENESS_TIER2_REASON,
    NOT_ALIVE_MECHANISM_MARKERS,
    PARITY_INDEX_ENVELOPE_SCHEMA,
    PARITY_INDEX_SCHEMA,
    STAGE_ARMED_NO_CANDIDATE,
    STAGE_ARM_UNCONFIRMED,
    STAGE_CANDIDATE_NO_SUBMIT,
    STAGE_ELIGIBLE_NOT_ARMED,
    STAGE_FILLED_EXITED_WORSE,
    STAGE_FILLED_PARITY,
    STAGE_FILLED_UNCOMPARABLE,
    STAGE_NOT_ADMITTED,
    STAGE_NOT_ALIVE,
    STAGE_NO_ARM_ATTEMPT,
    STAGE_ORDER,
    STAGE_RUNNER_NEVER_STARTED,
    STAGE_SUBMIT_NO_FILL,
    XREF_VERDICT_PLACEMENT,
    XREF_VERDICTS_PLACEABLE,
    XREF_VERDICTS_UNPLACEABLE,
    Stage,
    as_float,
    classify_events,
    classify_first_divergence,
    divergence_record,
    events_in_window,
    normalize_account,
    normalize_window,
    ross_parity_index,
    sentinel_zero_to_none,
    stage_from_xref_verdict,
)

UTC = timezone.utc


def _ts(day: str, hhmmss: str) -> datetime:
    """``_ts("2026-06-26", "13:21:06")`` -> tz-aware UTC datetime."""
    return datetime.fromisoformat(f"{day}T{hhmmss}+00:00")


def ev(when: datetime, event_type: str, **payload):
    """One event in the replay-receipt shape.

    Keys match scripts/replay_v3_fsm_window.py:1132-1136 exactly: ``ts`` is a string
    (the driver writes ``str(e.ts)``), ``event_type``, ``payload``.
    """
    return {"ts": when.isoformat(), "event_type": event_type, "payload": dict(payload)}


def spread(start: datetime, count: int, step_s: float, event_type: str, **payload):
    """``count`` copies of one event on a fixed cadence — the shape the ledger describes
    when it says "live_entry_backside_bench_veto x5 13:21:38-13:23:06Z"."""
    return [ev(start + timedelta(seconds=step_s * i), event_type, **payload)
            for i in range(count)]


# ═══════════════════════════════════════════════════════════════════════════════════
# ANCHOR 1 — SDOT 2026-06-26 -> armed_no_candidate(bench_veto)
#
# Ledger mechanism (xref, chili_verdict=armed_no_entry): session 9198 armed 13:21:06Z,
# the tick triggers fired on Ross's exact level and were vetoed by the backside bench —
# live_entry_backside_bench_veto x5 on deep_reclaim_tick_ok 13:21:38-13:23:06Z and x5 on
# abcd_break_tick_ok 13:23:39-13:26:42Z, plus one "Not live-eligible per neural
# viability" block at 13:22:22Z and 3 pullback_too_deep waits 13:24:05-13:24:35Z;
# benched_at_hod 17.8626. Reaped 13:26:42Z, never re-armed. Ross +$5,885.15, CHILI $0.
# ═══════════════════════════════════════════════════════════════════════════════════

SDOT_WINDOW = (_ts("2026-06-26", "13:20:00"), _ts("2026-06-26", "13:30:00"))


def sdot_events():
    evs = [
        ev(_ts("2026-06-26", "13:21:06"), "live_arm_requested", viability_score=0.66),
        ev(_ts("2026-06-26", "13:21:07"), "live_arm_confirmed"),
        ev(_ts("2026-06-26", "13:21:08"), "live_runner_started"),
    ]
    evs += spread(_ts("2026-06-26", "13:21:38"), 5, 22.0,
                  "live_entry_backside_bench_veto",
                  reason="benched_backside_below_vwap",
                  blocked_trigger="deep_reclaim_tick_ok", benched_at_hod=17.8626)
    evs.append(ev(_ts("2026-06-26", "13:22:22"), "live_blocked_by_risk",
                  reason="not_live_eligible_per_neural_viability"))
    evs += spread(_ts("2026-06-26", "13:23:39"), 5, 36.6,
                  "live_entry_backside_bench_veto",
                  reason="benched_backside_below_vwap",
                  blocked_trigger="abcd_break_tick_ok", benched_at_hod=17.8626)
    # The three waits carry a detector_rejects side-map that disagrees with the binding
    # reason. It is present so the "we do not read it" rule is actually exercised.
    evs += spread(_ts("2026-06-26", "13:24:05"), 3, 15.0,
                  "live_entry_trigger_wait", reason="pullback_too_deep",
                  detector_rejects={"premarket_tickbreak_unconfirmed": 102})
    evs.append(ev(_ts("2026-06-26", "13:26:42"), "live_recycled"))
    return evs


SDOT_CASE = {
    "symbol": "SDOT", "date": "2026-06-26", "account": "main",
    "ross_pnl_usd": 5885.15, "xref_verdict": "armed_no_entry",
    "replay": {"pnl_usd": 0.0, "entries": 0, "fills": []},
}


def test_sdot_recorded_is_armed_no_candidate_bench_veto():
    recorded, _ = classify_first_divergence(
        SDOT_CASE, sdot_events(), sdot_events(), SDOT_WINDOW)
    assert recorded.base == STAGE_ARMED_NO_CANDIDATE
    assert recorded.qualifier == "bench_veto"
    assert recorded == "armed_no_candidate(bench_veto)"
    assert recorded.source == "events"
    assert recorded.detail["bench_veto_count"] == 10
    assert recorded.detail["blocked_triggers"] == {
        "deep_reclaim_tick_ok": 5, "abcd_break_tick_ok": 5}


def test_sdot_bench_veto_outranks_the_trigger_wait_that_also_fired():
    """SDOT has BOTH bench vetoes and pullback_too_deep waits. A trigger that fired and
    was refused is a decision against a real setup; a wait is the absence of one. The
    label must name the refusal, with the waits kept in the evidence."""
    stage = classify_events(sdot_events(), source="events")
    assert stage.qualifier == "bench_veto"
    assert stage.detail["trigger_wait_count"] == 3
    assert stage.detail["trigger_wait_reasons"] == {"pullback_too_deep": 3}


def test_detector_rejects_is_recorded_as_present_but_never_binds():
    """scripts/nightly_replay_report.py:192-209 measured detector_rejects as 100% wrong
    in two sessions (XPON 225/225, OLOX 74). It must never appear in a label."""
    waits_only = [
        ev(_ts("2026-06-26", "13:21:06"), "live_arm_requested"),
        ev(_ts("2026-06-26", "13:21:07"), "live_arm_confirmed"),
        ev(_ts("2026-06-26", "13:21:08"), "live_runner_started"),
    ] + spread(_ts("2026-06-26", "13:22:00"), 4, 5.0, "live_entry_trigger_wait",
               reason="volume_below_1p5x_avg",
               detector_rejects={"premarket_tickbreak_unconfirmed": 102})
    stage = classify_events(waits_only, source="events")
    assert stage == "armed_no_candidate(trigger_wait:volume_below_1p5x_avg)"
    assert stage.detail["detector_rejects_present"] is True
    assert "premarket_tickbreak_unconfirmed" not in str(stage)


# ═══════════════════════════════════════════════════════════════════════════════════
# ANCHOR 2 — ZDAI 2026-06-26 -> armed_no_candidate(silent)
#
# Ledger mechanism: session 9185 armed 11:54:19Z, "received 20 ticks in 5 min and
# emitted ZERO decision events (no trigger_wait, no veto, no candidate)", last_mid 3.65
# at 11:59:20Z, reaped by auto-arm at 11:59:25Z (+5:06).
# ═══════════════════════════════════════════════════════════════════════════════════

ZDAI_WINDOW = (_ts("2026-06-26", "11:50:00"), _ts("2026-06-26", "12:05:00"))


def zdai_events():
    return [
        ev(_ts("2026-06-26", "11:54:19"), "live_arm_requested", viability_score=0.443),
        ev(_ts("2026-06-26", "11:54:20"), "live_arm_confirmed"),
        ev(_ts("2026-06-26", "11:54:21"), "live_runner_started"),
        # 5 minutes of ticks and nothing else. The reaper is the next and last row.
        ev(_ts("2026-06-26", "11:59:25"), "live_recycled", reason="auto_arm_reap"),
    ]


ZDAI_CASE = {
    "symbol": "ZDAI", "date": "2026-06-26", "account": "small",
    "ross_pnl_usd": 1095.08, "xref_verdict": "armed_no_entry",
    "replay": {"pnl_usd": 0.0, "entries": 0, "fills": []},
}


def test_zdai_recorded_is_armed_no_candidate_silent():
    recorded, _ = classify_first_divergence(
        ZDAI_CASE, zdai_events(), zdai_events(), ZDAI_WINDOW)
    assert recorded == "armed_no_candidate(silent)"
    assert recorded.detail["decision_event_count"] == 0
    assert recorded.detail["trigger_wait_count"] == 0
    assert recorded.detail["bench_veto_count"] == 0


def test_lifecycle_rows_are_not_decision_events():
    """"Silent" is only meaningful if arm/runner/reap rows do not count as decisions —
    otherwise every armed session would look like it decided something."""
    stage = classify_events(zdai_events(), source="events")
    assert stage.detail["event_count"] == 4
    assert stage.detail["decision_event_count"] == 0


# ═══════════════════════════════════════════════════════════════════════════════════
# ANCHOR 3 — UPC 2026-06-29 -> arm_unconfirmed
#
# Ledger mechanism: session 9498 live_arm_requested 12:38:45Z with live_eligible=true
# and risk allowed=true, "but confirm_live_arm never succeeded -- no live_arm_confirmed
# event, so no runner, no submit"; the 15-min token (expires 12:53:43Z) covered Ross's
# 12:45-12:50Z entry, then sat unconfirmed until the batch sweep expired it at 00:01:24Z.
# WHICH rejection fired is NOT persisted (auto_arm.py logs INFO only) -> no qualifier.
# ═══════════════════════════════════════════════════════════════════════════════════

UPC_WINDOW = (_ts("2026-06-29", "12:30:00"), _ts("2026-06-29", "13:00:00"))


def upc_events():
    return [
        ev(_ts("2026-06-29", "12:38:45"), "live_arm_requested",
           live_eligible=True, risk_allowed=True, viability_score=0.58),
        # The sweep that expired the token fired the NEXT day and is outside the window.
        ev(_ts("2026-06-30", "00:01:24"), "live_arm_expired",
           reason="expires_at_utc_passed"),
    ]


UPC_CASE = {
    "symbol": "UPC", "date": "2026-06-29", "account": "main",
    "ross_pnl_usd": 39000.0, "xref_verdict": "armed_no_entry",
    "replay": {"pnl_usd": 0.0, "entries": 0, "fills": []},
}


def test_upc_recorded_is_arm_unconfirmed():
    recorded, _ = classify_first_divergence(
        UPC_CASE, upc_events(), upc_events(), UPC_WINDOW)
    assert recorded == STAGE_ARM_UNCONFIRMED
    assert recorded.qualifier is None, (
        "auto_arm.py logs the confirm rejection at INFO only, so the reason is not "
        "persisted; a qualifier here would be fabricated"
    )
    assert recorded.detail["event_count"] == 1
    assert recorded.detail["window_stats"]["dropped_after"] == 1


# ═══════════════════════════════════════════════════════════════════════════════════
# ANCHOR 4 — SLE 2026-08-18 -> runner_never_started(no_bbo)
#
# Ledger mechanism: "each arm before Ross's break of 6 died on live_blocked_by_risk/
# live_declined no_bbo 46-97 s after confirmation - 14003 (armed 13:01:29Z -> no_bbo
# 13:02:16Z), 14006 (13:07:40Z -> 13:08:24Z), 14009 (13:13:38Z -> 13:15:15Z) - so no
# runner existed at the 13:11Z break". Session 14015's runner started 13:22:11Z, AFTER
# the break, and is deliberately outside the window.
# ═══════════════════════════════════════════════════════════════════════════════════

SLE_WINDOW = (_ts("2026-08-18", "13:00:00"), _ts("2026-08-18", "13:20:00"))


def sle_events():
    return [
        # 14003 — 47 s from confirm to death
        ev(_ts("2026-08-18", "13:01:29"), "live_arm_requested"),
        ev(_ts("2026-08-18", "13:01:30"), "live_arm_confirmed"),
        ev(_ts("2026-08-18", "13:02:16"), "live_declined", reason="no_bbo",
           decline_class="no_bbo"),
        # 14006 — 44 s
        ev(_ts("2026-08-18", "13:07:40"), "live_arm_requested"),
        ev(_ts("2026-08-18", "13:07:41"), "live_arm_confirmed"),
        ev(_ts("2026-08-18", "13:08:24"), "live_blocked_by_risk", reason="no_bbo"),
        # 14009 — 96 s
        ev(_ts("2026-08-18", "13:13:38"), "live_arm_requested"),
        ev(_ts("2026-08-18", "13:13:39"), "live_arm_confirmed"),
        ev(_ts("2026-08-18", "13:15:15"), "live_declined", reason="no_bbo",
           decline_class="no_bbo"),
        # 14015 — the runner that DID start, 2 min after Ross's break, outside the window
        ev(_ts("2026-08-18", "13:22:11"), "live_runner_started"),
    ]


SLE_CASE = {
    "symbol": "SLE", "date": "2026-08-18", "account": "small+main",
    "ross_pnl_usd": -71.29, "xref_verdict": "armed_no_entry",
    "replay": {"pnl_usd": 0.0, "entries": 0, "fills": []},
}


def test_sle_recorded_is_runner_never_started_no_bbo():
    recorded, _ = classify_first_divergence(
        SLE_CASE, sle_events(), sle_events(), SLE_WINDOW)
    assert recorded == "runner_never_started(no_bbo)"
    assert recorded.detail["blocker_reasons"] == {"no_bbo": 3}


def test_sle_window_is_load_bearing_not_decorative():
    """Drop the window and session 14015's 13:22:11Z runner is suddenly in scope, which
    moves the answer one rung down the ladder. The bench's claim is about the arms that
    covered the 13:11Z break, so the window is part of the measurement."""
    unwindowed = classify_events(sle_events(), source="events")
    assert unwindowed.base == STAGE_ARMED_NO_CANDIDATE
    assert unwindowed != "runner_never_started(no_bbo)"
    windowed, _ = classify_first_divergence(
        SLE_CASE, sle_events(), sle_events(), SLE_WINDOW)
    assert windowed.base == STAGE_RUNNER_NEVER_STARTED
    assert windowed.rung < unwindowed.rung


def test_no_bbo_label_follows_the_payload_reason_not_the_decline_class():
    """Pre-#1269 tape hard-coded reason="no_bbo"; after it, decline_class stays "no_bbo"
    while reason carries the truth (live_runner.py:22397-22418). The scorer reads
    ``reason``, so post-#1269 tape labels the real cause instead of the class."""
    modern = [
        ev(_ts("2026-09-01", "13:01:29"), "live_arm_requested"),
        ev(_ts("2026-09-01", "13:01:30"), "live_arm_confirmed"),
        ev(_ts("2026-09-01", "13:02:16"), "live_declined",
           decline_class="no_bbo", reason="execution_bbo_unavailable"),
    ]
    stage = classify_events(modern, source="events")
    assert stage == "runner_never_started(execution_bbo_unavailable)"


# ═══════════════════════════════════════════════════════════════════════════════════
# The recorded side with NO session: mapping from xref_verdict
# ═══════════════════════════════════════════════════════════════════════════════════

def test_not_in_universe_maps_to_not_admitted_and_keeps_provenance():
    case = {"symbol": "QNRX", "date": "2025-11-11",
            "xref_verdict": "not_in_universe",
            "mechanism": "not_in_universe: CHILI's momentum lane did not exist on 2025-11-11."}
    recorded, replay = classify_first_divergence(case, [], [], None)
    assert recorded == STAGE_NOT_ADMITTED
    assert recorded.source == "xref_verdict"
    assert recorded.detail["recorded_stage_source"] == "xref_verdict"
    assert replay.source == "no_replay_events"


@pytest.mark.parametrize("mechanism", [
    # INLF 2026-07-28, verbatim shape from the ledger
    "Control loop dead: last momentum_live_loop_heartbeat 2026-07-28 13:37:48Z",
    # CLRO 2026-08-06 — the SAME fact written with a hyphen, which is why the marker
    # match normalises separators instead of testing for a literal substring
    "Lane dark at entry: no captured-paper control-loop heartbeat between 05:48:11Z",
    # JEM 2026-06-30
    "Same arm gap: CHILI had no live session on any symbol from 03:32:24Z to 13:20:05Z",
])
def test_never_armed_with_a_dead_lane_maps_to_not_alive(mechanism):
    stage = stage_from_xref_verdict("never_armed", mechanism)
    assert stage == STAGE_NOT_ALIVE
    assert stage.source == "xref_verdict"
    assert stage.detail["mechanism_matched"] in NOT_ALIVE_MECHANISM_MARKERS


def test_never_armed_with_a_live_lane_maps_to_eligible_not_armed():
    """RMCF 2026-08-12: "SELECTION WAS ON TIME, ARM PATH ABSENT" — the lane was up and
    the symbol was the #1 board name. That is a different failure from a dead loop and
    must not be collapsed into it."""
    stage = stage_from_xref_verdict(
        "never_armed",
        "never_armed: SELECTION WAS ON TIME, ARM PATH ABSENT. RMCF entered "
        "momentum_viability_history at 2026-08-12 11:14:18Z live_eligible=True",
    )
    assert stage == STAGE_ELIGIBLE_NOT_ARMED
    assert stage.detail["mechanism_matched"] is None


def test_verdict_asserting_a_session_without_events_is_unknown_not_guessed():
    """armed_no_entry says a session existed. With no events supplied there is nothing to
    place it on the ladder with, and picking a rung anyway would be invention.

    But it must not print as a BARE ``unknown`` either: the label carries the verdict, the
    measured reason for the refusal, and the rungs the verdict still constrains it to.
    """
    stage = stage_from_xref_verdict("armed_no_entry", "session 9198 armed 13:21:06Z")
    assert stage.base == "unknown"
    assert stage == "unknown(armed_no_entry)"
    assert stage.qualifier == "armed_no_entry"
    assert stage.source == "xref_verdict"
    assert stage.rung is None
    assert "SESSION-DAY judgement" in stage.detail["unplaceable_reason"]
    assert "rossbench_export_recorded_events.py" in stage.detail["unplaceable_reason"]
    assert stage.detail["rung_bounds"] == [STAGE_NO_ARM_ATTEMPT, STAGE_SUBMIT_NO_FILL]
    assert stage.detail["placement_basis"] == "refused"


# ═══════════════════════════════════════════════════════════════════════════════════
# The verdict-placement table
# ═══════════════════════════════════════════════════════════════════════════════════
# Before this table existed, ``stage_from_xref_verdict`` mapped 2 of the 4 tokens the
# manifest emits and dropped the other 2 on the floor as a bare "unknown".
# MEASURED 2026-09-04 over scripts/build_ross_manifest.build() (418 windows):
# never_armed 65, not_in_universe 29, armed_no_entry 13, entered_wrong_leg 1.

MANIFEST_VERDICT_TOKENS = ("never_armed", "not_in_universe", "armed_no_entry",
                           "entered_wrong_leg")


@pytest.mark.parametrize("token", MANIFEST_VERDICT_TOKENS + ("unknown_no_data",))
def test_every_verdict_the_corpus_emits_is_in_the_placement_table(token):
    """The original defect, asserted directly: a token nobody wrote down gets dropped."""
    assert token in XREF_VERDICT_PLACEMENT
    assert token in (XREF_VERDICTS_PLACEABLE + XREF_VERDICTS_UNPLACEABLE)


@pytest.mark.parametrize("token", MANIFEST_VERDICT_TOKENS + ("unknown_no_data", "novel_token"))
def test_no_verdict_token_ever_produces_a_bare_unknown(token):
    """A blank-looking cell for a row that HAS a verdict is the failure this prevents."""
    stage = stage_from_xref_verdict(token, "some mechanism prose")
    assert str(stage) != "unknown"
    if stage.base == "unknown":
        assert stage.qualifier == token
        assert stage.detail["unplaceable_reason"]


def test_an_absent_verdict_is_the_one_legitimate_bare_unknown():
    """"we had nothing" is a different statement from "we had a verdict we cannot place"."""
    stage = stage_from_xref_verdict(None)
    assert str(stage) == "unknown"
    assert stage.source == "unavailable"      # NOT xref_verdict — there was no verdict
    assert stage.detail["unplaceable_reason"] == "no xref_verdict on this case"


def test_entered_wrong_leg_is_placed_at_filled_uncomparable():
    """AEHL 2026-08-31, the corpus's only such row: "The only fill was 19192 ... filled
    301 @ 5.98 at 11:55:04Z ... 53 min after his exit". A fill happened, so every rung
    through submit_no_fill cleared; it was a different leg, so the two P&Ls describe
    different trades — which is exactly what filled_uncomparable means."""
    stage = stage_from_xref_verdict(
        "entered_wrong_leg",
        "The only fill was 19192: abcd_break candidate 11:52:25.525Z -> "
        "live_entry_submitted 11:55:04.132Z -> filled 301 @ 5.98, 53 min after his exit",
    )
    assert stage == STAGE_FILLED_UNCOMPARABLE
    assert stage.source == "xref_verdict"
    assert stage.rung == STAGE_ORDER.index(STAGE_FILLED_UNCOMPARABLE)
    assert stage.detail["placement_basis"] == "definitional"


def test_a_placed_verdict_never_borrows_the_events_source_label():
    """Every stage derived from prose must stay tagged xref_verdict, whatever its rung."""
    for token in XREF_VERDICTS_PLACEABLE:
        assert stage_from_xref_verdict(token, "prose").source == "xref_verdict"


def test_unknown_no_data_is_refused_with_its_own_reason_not_a_rung():
    stage = stage_from_xref_verdict("unknown_no_data", "")
    assert stage == "unknown(unknown_no_data)"
    assert "no CHILI evidence" in stage.detail["unplaceable_reason"]
    assert "rung_bounds" not in stage.detail   # no evidence bounds nothing


def test_an_unaudited_token_is_reported_verbatim_not_mapped():
    stage = stage_from_xref_verdict("some_new_verdict", "prose")
    assert stage == "unknown(some_new_verdict)"
    assert stage.detail["placement_basis"] == "unknown_token"
    assert "not in this module's vocabulary" in stage.detail["unplaceable_reason"]


def test_the_placement_table_is_internally_consistent():
    """Every stage the table names must be a real rung, and the two lists must partition it."""
    assert set(XREF_VERDICTS_PLACEABLE) | set(XREF_VERDICTS_UNPLACEABLE) == set(
        XREF_VERDICT_PLACEMENT)
    assert not set(XREF_VERDICTS_PLACEABLE) & set(XREF_VERDICTS_UNPLACEABLE)
    for token, spec in XREF_VERDICT_PLACEMENT.items():
        if spec.get("stage") is not None:
            assert spec["stage"] in STAGE_ORDER, token
        for name in spec.get("stages", ()):
            assert name in STAGE_ORDER, token
        bounds = spec.get("rung_bounds")
        if bounds:
            lo, hi = bounds
            assert STAGE_ORDER.index(lo) < STAGE_ORDER.index(hi), token
        if spec.get("basis") == "refused":
            assert spec.get("unplaceable_reason"), token
        else:
            assert spec.get("unplaceable_reason") is None, token


def test_a_wrong_leg_row_counts_as_live_and_an_armed_no_entry_row_does_not():
    """recorded_liveness asks "was the lane up?". A row that FILLED plainly was; a row whose
    verdict cannot be placed inside the window plainly cannot answer it either way."""
    idx = ross_parity_index([
        _pcase("AEHL", "2026-08-31", "main", 975.63, 0.0, verdict="entered_wrong_leg"),
        _pcase("IPST", "2026-08-17", "main", 100.0, 0.0, verdict="armed_no_entry"),
    ], {})
    live = idx["recorded_liveness"]
    assert live["denominator"] == 1
    assert live["numerator"] == 1
    assert [c["case_id"] for c in live["cases_live"]] == ["AEHL@2026-08-31"]
    excluded = {(c["case_id"], c["reason"]) for c in idx["excluded_cases"]}
    assert ("IPST@2026-08-17", "recorded_stage_unknown") in excluded


# ═══════════════════════════════════════════════════════════════════════════════════
# Schema ids
# ═══════════════════════════════════════════════════════════════════════════════════

def test_the_metric_block_and_the_reports_envelope_do_not_share_a_schema_id():
    """They used to, with different shapes, and the reporter nests one inside the other —
    so one file declared two objects under one id. Renamed 2026-09-04."""
    assert PARITY_INDEX_SCHEMA == "chili.ross_parity_index_metrics.v1"
    assert PARITY_INDEX_ENVELOPE_SCHEMA == "chili.ross_parity_index.v1"
    assert PARITY_INDEX_SCHEMA != PARITY_INDEX_ENVELOPE_SCHEMA
    assert ross_parity_index([], {})["schema"] == PARITY_INDEX_SCHEMA


# ═══════════════════════════════════════════════════════════════════════════════════
# The rest of the ladder
# ═══════════════════════════════════════════════════════════════════════════════════

def _armed_prefix(day="2026-08-20", t="14:00:00"):
    base = _ts(day, t)
    return [
        ev(base, "live_arm_requested"),
        ev(base + timedelta(seconds=1), "live_arm_confirmed"),
        ev(base + timedelta(seconds=2), "live_runner_started"),
    ]


def test_no_arm_request_at_all_is_the_ladder_floor():
    stage = classify_events(
        [ev(_ts("2026-08-20", "14:00:00"), "live_runner_started")], source="events")
    assert stage == STAGE_NO_ARM_ATTEMPT


def test_candidate_no_submit_names_the_veto_that_fired_after_the_candidate():
    base = _ts("2026-08-20", "14:00:00")
    evs = _armed_prefix() + [
        # A refusal BEFORE the candidate belongs to an earlier tick and must not be the
        # label, or the report blames a gate that was later cleared.
        ev(base + timedelta(seconds=5), "live_entry_spread_cost_veto",
           reason="spread_cost_too_high"),
        ev(base + timedelta(seconds=20), "live_entry_candidate_detected",
           trigger="abcd_break_tick_ok", viability_score=0.61),
        ev(base + timedelta(seconds=25), "live_entry_deferred_final_bbo",
           reason="execution_bbo_above_planned_limit"),
        ev(base + timedelta(seconds=30), "live_entry_deferred_final_bbo",
           reason="execution_bbo_above_planned_limit"),
    ]
    stage = classify_events(evs, source="events")
    assert stage == "candidate_no_submit(execution_bbo_above_planned_limit)"
    assert "spread_cost_too_high" not in stage.detail["veto_reasons"]


def test_submit_no_fill_names_the_terminal():
    base = _ts("2026-08-20", "14:00:00")
    evs = _armed_prefix() + [
        ev(base + timedelta(seconds=20), "live_entry_candidate_detected"),
        ev(base + timedelta(seconds=21), "live_entry_submitted"),
        ev(base + timedelta(seconds=90), "live_entry_terminal_zero_fill",
           reason="ack_timeout_no_fill"),
    ]
    stage = classify_events(evs, source="events")
    assert stage == "submit_no_fill(ack_timeout_no_fill)"


def _filled_events(exit_type="live_bos_exit", exit_reason=None, pnl=None,
                   day="2026-08-20", t="14:00:00"):
    base = _ts(day, t)
    payload = {}
    if exit_reason is not None:
        payload["reason"] = exit_reason
    if pnl is not None:
        payload["pnl_usd"] = pnl
    return _armed_prefix(day=day, t=t) + [
        ev(base + timedelta(seconds=20), "live_entry_candidate_detected"),
        ev(base + timedelta(seconds=21), "live_entry_submitted"),
        ev(base + timedelta(seconds=23), "live_entry_filled", fill_price=6.10),
        ev(base + timedelta(seconds=300), exit_type, **payload),
    ]


def test_filled_exited_worse_carries_the_exit_event():
    stage = classify_events(_filled_events(), source="events",
                            chili_pnl_usd=120.0, ross_pnl_usd=5885.15)
    assert stage == "filled_exited_worse(live_bos_exit)"
    assert stage.detail["chili_pnl_usd"] == 120.0


def test_filled_exit_label_prefers_the_reason_when_the_event_is_generic():
    stage = classify_events(
        _filled_events(exit_type="live_exit_filled", exit_reason="trail_stop"),
        source="events", chili_pnl_usd=-40.0, ross_pnl_usd=5885.15)
    assert stage == "filled_exited_worse(trail_stop)"


def test_filled_parity_when_chili_matched_or_beat_ross():
    stage = classify_events(_filled_events(), source="events",
                            chili_pnl_usd=6000.0, ross_pnl_usd=5885.15)
    assert stage.base == STAGE_FILLED_PARITY


def test_an_unstated_ross_pnl_is_uncomparable_never_worse():
    """pnl_usd 0 in the ledger means "not stated" (30 of 187 rows). Scoring it as a real
    zero would make every CHILI loss look like a divergence from a flat day."""
    case = {"symbol": "X", "date": "2026-08-20", "ross_pnl_usd": 0,
            "replay": {"pnl_usd": -40.0, "entries": 1}}
    _, replay = classify_first_divergence(case, [], _filled_events(), None)
    assert replay.base == STAGE_FILLED_UNCOMPARABLE
    assert replay.detail["ross_pnl_usd"] is None


def test_pnl_falls_back_to_the_exit_payload_when_the_case_states_none():
    stage = classify_events(_filled_events(pnl=77.5), source="events",
                            chili_pnl_usd=None, ross_pnl_usd=50.0)
    assert stage.base == STAGE_FILLED_PARITY
    assert stage.detail["chili_pnl_usd"] == 77.5


def test_the_two_sides_are_classified_independently():
    """The bench exists to see a replay get FURTHER than the live lane did. If the two
    sides were coupled, that result would be invisible. The replay fixture is placed
    inside SDOT's own window on purpose — the window applies to BOTH sides."""
    replay_evs = _filled_events(day="2026-06-26", t="13:21:00")
    recorded, replay = classify_first_divergence(
        SDOT_CASE, sdot_events(), replay_evs, SDOT_WINDOW)
    assert recorded.base == STAGE_ARMED_NO_CANDIDATE
    assert replay.base == STAGE_FILLED_EXITED_WORSE  # SDOT_CASE replay pnl 0.0 < 5885.15
    assert replay.rung > recorded.rung
    assert replay.source == "events"


def test_stage_order_is_the_ladder_and_every_stage_is_in_it():
    assert STAGE_ORDER.index(STAGE_NOT_ADMITTED) < STAGE_ORDER.index(STAGE_ARM_UNCONFIRMED)
    assert STAGE_ORDER.index(STAGE_ARM_UNCONFIRMED) < STAGE_ORDER.index(
        STAGE_RUNNER_NEVER_STARTED)
    assert STAGE_ORDER.index(STAGE_RUNNER_NEVER_STARTED) < STAGE_ORDER.index(
        STAGE_ARMED_NO_CANDIDATE)
    assert STAGE_ORDER.index(STAGE_ARMED_NO_CANDIDATE) < STAGE_ORDER.index(
        STAGE_CANDIDATE_NO_SUBMIT)
    assert STAGE_ORDER.index(STAGE_CANDIDATE_NO_SUBMIT) < STAGE_ORDER.index(
        STAGE_SUBMIT_NO_FILL)
    assert Stage("armed_no_candidate(silent)", source="events").rung == STAGE_ORDER.index(
        STAGE_ARMED_NO_CANDIDATE)


def test_stage_is_a_plain_string_for_equality_and_json():
    import json as _json
    s = Stage("arm_unconfirmed", source="events", detail={"a": 1})
    assert s == "arm_unconfirmed"
    assert _json.dumps({"stage": s}) == '{"stage": "arm_unconfirmed"}'
    assert s.to_dict()["source"] == "events"


# ═══════════════════════════════════════════════════════════════════════════════════
# Input shapes the scorer has to survive
# ═══════════════════════════════════════════════════════════════════════════════════

class _OrmRow:
    """The third event shape: a TradingAutomationEvent-alike whose payload is TEXT.

    The driver itself has to branch on this (replay_v3_fsm_window.py:1128-1131), because
    payload_json is JSONB in some deployments and TEXT in others.
    """

    def __init__(self, ts, event_type, payload_json):
        self.ts = ts
        self.event_type = event_type
        self.payload_json = payload_json


def test_orm_shaped_events_with_a_string_payload_classify_identically():
    rows = [
        _OrmRow(datetime(2026, 8, 18, 13, 1, 29), "live_arm_requested", "{}"),
        _OrmRow(datetime(2026, 8, 18, 13, 1, 30), "live_arm_confirmed", None),
        _OrmRow(datetime(2026, 8, 18, 13, 2, 16), "live_declined",
                '{"reason": "no_bbo", "decline_class": "no_bbo"}'),
    ]
    assert classify_events(rows, source="events") == "runner_never_started(no_bbo)"


def test_naive_orm_timestamps_compare_against_a_tz_aware_window():
    """ORM rows come back naive; the window is aware. Without the naive-means-UTC
    normalisation this comparison raises TypeError and the whole case is lost."""
    rows = [
        _OrmRow(datetime(2026, 8, 18, 13, 1, 29), "live_arm_requested", "{}"),
        _OrmRow(datetime(2026, 8, 18, 13, 1, 30), "live_arm_confirmed", "{}"),
        _OrmRow(datetime(2026, 8, 18, 13, 30, 0), "live_runner_started", "{}"),
    ]
    kept, stats = events_in_window(rows, SLE_WINDOW)
    assert (stats["kept"], stats["dropped_after"], stats["no_ts"]) == (2, 1, 0)
    assert classify_events(kept, source="events").base == STAGE_RUNNER_NEVER_STARTED


def test_an_unparseable_timestamp_is_kept_and_counted_never_silently_dropped():
    kept, stats = events_in_window(
        [{"ts": "garbage", "event_type": "live_arm_requested", "payload": {}}],
        SLE_WINDOW)
    assert len(kept) == 1
    assert stats["no_ts"] == 1


def test_an_unrecognised_window_shape_raises_rather_than_disabling_the_filter():
    """Silently treating an unknown window as "no window" would widen the measurement
    without saying so. ``None`` is the explicit way to ask for no filtering."""
    with pytest.raises(TypeError, match="window shape"):
        normalize_window(object())
    assert normalize_window(None) == (None, None)


def test_divergence_record_keeps_the_provenance_the_label_alone_would_lose():
    import json as _json
    case = {"symbol": "QNRX", "date": "2025-11-11", "account": "main",
            "ross_pnl_usd": 5000.0, "xref_verdict": "not_in_universe",
            "mechanism": "CHILI's momentum lane did not exist on 2025-11-11"}
    recorded, replay = classify_first_divergence(case, [], [], None)
    doc = divergence_record(case, recorded, replay)
    assert doc["schema"] == DIVERGENCE_SCHEMA
    assert doc["recorded"]["stage"] == STAGE_NOT_ADMITTED
    assert doc["recorded_stage_source"] == "xref_verdict"
    assert doc["replay_advanced"] is None, (
        "the replay side is 'unknown' here, and unknown is not 'did not advance'")
    _json.dumps(doc)


def test_divergence_record_flags_a_replay_that_got_further_than_the_live_lane():
    replay_evs = _filled_events(day="2026-06-26", t="13:21:00")
    recorded, replay = classify_first_divergence(
        SDOT_CASE, sdot_events(), replay_evs, SDOT_WINDOW)
    doc = divergence_record(SDOT_CASE, recorded, replay)
    assert doc["replay_advanced"] is True
    assert doc["account"] == "main"
    assert doc["ross_pnl_usd"] == pytest.approx(5885.15)


def test_the_index_is_json_serialisable_because_it_is_a_report_payload():
    import json as _json
    case = dict(SDOT_CASE)
    case["recorded_stage"] = Stage("armed_no_candidate(bench_veto)", source="events")
    blob = _json.dumps(ross_parity_index([case], {"small": 2000.0}))
    assert '"pooled": null' in blob
    assert '"blended_score": null' in blob


# ═══════════════════════════════════════════════════════════════════════════════════
# Sentinels and vocabulary
# ═══════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("raw,expected", [
    (0, None), (0.0, None), ("0", None), (None, None), ("", None),
    (5885.15, 5885.15), (-71.29, -71.29),
])
def test_sentinel_zero_to_none_on_ross_fields(raw, expected):
    assert sentinel_zero_to_none(raw) == expected


def test_as_float_keeps_a_measured_zero():
    """The CHILI side must NOT be sentinelled: a replay pnl of 0.0 is computed from the
    mined fills and means "captured nothing", which Capture has to be able to add up."""
    assert as_float(0.0) == 0.0
    assert as_float("nope") is None


@pytest.mark.parametrize("raw,expected", [
    ("main", "main"), ("big", "main"), ("BIG", "main"), ("small", "small"),
    (None, ACCOUNT_UNKNOWN), ("", ACCOUNT_UNKNOWN), ("brokerage", ACCOUNT_UNKNOWN),
    ("small+main", ACCOUNT_MIXED), ("main+small", ACCOUNT_MIXED),
    ("main+big", "main"),
])
def test_account_vocabulary(raw, expected):
    assert normalize_account(raw) == expected


# ═══════════════════════════════════════════════════════════════════════════════════
# ross_parity_index
# ═══════════════════════════════════════════════════════════════════════════════════

def _pcase(symbol, date, account, ross, chili, *, filled=None, verdict=None,
           mechanism=None, equity=None):
    case = {"symbol": symbol, "date": date, "account": account,
            "ross_pnl_usd": ross,
            "replay": {"pnl_usd": chili,
                       "entries": (1 if (filled if filled is not None
                                         else chili not in (None, 0)) else 0)}}
    if verdict is not None:
        case["xref_verdict"] = verdict
    if mechanism is not None:
        case["mechanism"] = mechanism
    if equity is not None:
        case["ross_equity_usd"] = equity
    return case


def test_capture_is_never_pooled_across_accounts():
    idx = ross_parity_index([
        _pcase("UPC", "2026-06-29", "main", 39000.0, 0.0),
        _pcase("SDOT", "2026-06-26", "big", 5885.15, 0.0),   # big collapses into main
        _pcase("ZDAI", "2026-06-26", "small", 1095.08, 250.0),
    ], {})
    assert idx["schema"] == PARITY_INDEX_SCHEMA
    assert idx["capture"]["pooled"] is None
    by = idx["capture"]["by_account"]
    assert set(by) == {"main", "small"}
    assert by["main"]["denominator_usd"] == pytest.approx(39000.0 + 5885.15)
    assert by["main"]["numerator_usd"] == 0.0
    assert by["main"]["ratio"] == 0.0
    assert by["small"]["denominator_usd"] == pytest.approx(1095.08)
    assert by["small"]["ratio"] == pytest.approx(250.0 / 1095.08)


def test_a_compound_account_gets_its_own_bucket_rather_than_being_assigned():
    idx = ross_parity_index(
        [_pcase("SLE", "2026-08-18", "small+main", 500.0, 0.0)], {})
    assert set(idx["capture"]["by_account"]) == {ACCOUNT_MIXED}


def test_equity_normalized_is_null_when_no_row_states_an_equity():
    idx = ross_parity_index([_pcase("UPC", "2026-06-29", "main", 39000.0, 0.0)], {})
    assert idx["capture"]["by_account"]["main"]["equity_normalized"] is None


def test_equity_normalized_uses_the_row_first_and_publishes_its_coverage():
    idx = ross_parity_index([
        _pcase("UPC", "2026-06-29", "main", 39000.0, 3900.0, equity=390000.0),
        _pcase("MSS", "2026-07-24", "main", 1000.0, 0.0),  # no equity stated
    ], {})
    norm = idx["capture"]["by_account"]["main"]["equity_normalized"]
    assert norm["denominator_pct_equity"] == pytest.approx(10.0)
    assert norm["numerator_pct_equity"] == pytest.approx(1.0)
    assert norm["cases_with_equity"] == ["UPC@2026-06-29"]
    assert norm["cases_without_equity"] == ["MSS@2026-07-24"]
    # The USD figures still cover BOTH rows — only the percentage is subset-scoped.
    assert idx["capture"]["by_account"]["main"]["denominator_usd"] == pytest.approx(40000.0)


def test_the_equity_argument_is_a_per_account_fallback_not_a_scalar():
    idx = ross_parity_index(
        [_pcase("UPC", "2026-06-29", "main", 39000.0, 0.0)], {"main": 390000.0})
    norm = idx["capture"]["by_account"]["main"]["equity_normalized"]
    assert norm["denominator_pct_equity"] == pytest.approx(10.0)
    with pytest.raises(TypeError, match="pool"):
        ross_parity_index([], 390000.0)


def test_avoidance_counts_a_loss_chili_simply_did_not_take():
    idx = ross_parity_index([
        _pcase("SLE", "2026-08-18", "main", -71.29, 0.0, filled=False),   # avoided
        _pcase("AAA", "2026-08-19", "main", -500.0, -120.0, filled=True),  # lost less
        _pcase("BBB", "2026-08-20", "main", -100.0, -900.0, filled=True),  # lost more
    ], {})
    av = idx["avoidance"]
    assert (av["numerator"], av["denominator"]) == (2, 3)
    assert av["ratio"] == pytest.approx(2 / 3)
    ids = {c["case_id"] for c in av["cases_avoided"]}
    assert ids == {"SLE@2026-08-18", "AAA@2026-08-19"}
    fully = {c["case_id"] for c in av["cases_avoided"] if c["avoided_fully"]}
    assert fully == {"SLE@2026-08-18"}


def test_precision_scores_only_the_entries_chili_actually_took():
    idx = ross_parity_index([
        _pcase("AAA", "2026-08-19", "main", 100.0, 60.0, filled=True),
        _pcase("BBB", "2026-08-20", "main", 100.0, -30.0, filled=True),
        _pcase("CCC", "2026-08-21", "main", 100.0, 0.0, filled=True),    # scratch
        _pcase("DDD", "2026-08-22", "main", 100.0, 0.0, filled=False),   # never entered
    ], {})
    pr = idx["precision"]
    assert (pr["numerator"], pr["denominator"]) == (1, 3)
    assert [c["case_id"] for c in pr["cases_scratch"]] == ["CCC@2026-08-21"]
    assert "DDD@2026-08-22" not in {c["case_id"] for c in pr["cases"]}


def test_liveness_is_null_in_tier1_and_recorded_liveness_is_reported_separately():
    idx = ross_parity_index([
        _pcase("QNRX", "2025-11-11", "main", 5000.0, 0.0,
               verdict="not_in_universe", mechanism="lane did not exist"),
        _pcase("INLF", "2026-07-28", "main", 1000.0, 0.0,
               verdict="never_armed", mechanism="Control loop dead 13:37:48Z"),
        _pcase("RMCF", "2026-08-12", "main", 1000.0, 0.0,
               verdict="never_armed", mechanism="SELECTION WAS ON TIME, ARM PATH ABSENT"),
    ], {})
    assert idx["tier"] == 1
    assert idx["liveness"]["value"] is None
    assert idx["liveness"]["numerator"] is None
    assert idx["liveness"]["reason"] == LIVENESS_TIER2_REASON
    rl = idx["recorded_liveness"]
    assert (rl["numerator"], rl["denominator"]) == (1, 3)
    assert [c["case_id"] for c in rl["cases_live"]] == ["RMCF@2026-08-12"]


def test_recorded_liveness_prefers_a_stage_the_caller_already_measured():
    case = _pcase("SDOT", "2026-06-26", "main", 5885.15, 0.0,
                  verdict="not_in_universe", mechanism="lane did not exist")
    case["recorded_stage"] = Stage("armed_no_candidate(bench_veto)", source="events")
    idx = ross_parity_index([case], {})
    assert idx["recorded_liveness"]["numerator"] == 1


def test_the_four_numbers_are_never_blended():
    idx = ross_parity_index([_pcase("A", "2026-08-19", "main", 100.0, 50.0)], {})
    assert idx["blended_score"] is None
    for key in ("capture", "avoidance", "precision", "liveness"):
        assert key in idx
    assert "score" not in idx


def test_an_empty_denominator_reports_none_not_zero():
    """A bucket with nothing in it has no rate. Reporting 0.0 would read as a measured
    failure rather than as absent evidence."""
    idx = ross_parity_index([], {})
    assert idx["avoidance"]["ratio"] is None
    assert idx["precision"]["ratio"] is None
    assert idx["recorded_liveness"]["ratio"] is None
    assert idx["capture"]["by_account"] == {}


def test_a_case_without_a_replay_receipt_is_excluded_not_scored_as_zero():
    """"No receipt" and "CHILI took nothing" are different facts, and only the second one
    belongs in a capture numerator."""
    case = {"symbol": "AAA", "date": "2026-08-19", "account": "main",
            "ross_pnl_usd": 1000.0}
    idx = ross_parity_index([case], {})
    assert idx["capture"]["by_account"] == {}
    reasons = {(c["case_id"], c["reason"]) for c in idx["excluded_cases"]}
    assert ("AAA@2026-08-19", "no_replay_pnl_usd_on_case") in reasons


def test_the_anchor_cases_score_end_to_end():
    """All four anchors together: none of them produced a CHILI fill, so Capture is 0 of
    Ross's stated wins, Precision has no denominator, and every one of them is a
    lifecycle-or-gate divergence rather than an economic one."""
    cases = [SDOT_CASE, ZDAI_CASE, UPC_CASE, SLE_CASE]
    windows = [SDOT_WINDOW, ZDAI_WINDOW, UPC_WINDOW, SLE_WINDOW]
    events = [sdot_events(), zdai_events(), upc_events(), sle_events()]
    stages = []
    for case, evs, win in zip(cases, events, windows):
        recorded, _ = classify_first_divergence(case, evs, [], win)
        stages.append(str(recorded))
    assert stages == [
        "armed_no_candidate(bench_veto)",
        "armed_no_candidate(silent)",
        "arm_unconfirmed",
        "runner_never_started(no_bbo)",
    ]
    idx = ross_parity_index(cases, {})
    by = idx["capture"]["by_account"]
    assert by["main"]["numerator_usd"] == 0.0
    assert by["main"]["denominator_usd"] == pytest.approx(5885.15 + 39000.0)
    assert by["small"]["denominator_usd"] == pytest.approx(1095.08)
    assert ACCOUNT_MIXED not in by, "SLE's Ross pnl is negative, so it is an Avoidance row"
    assert idx["avoidance"]["denominator"] == 1
    assert idx["precision"]["denominator"] == 0
    assert idx["liveness"]["value"] is None


# ═══════════════════════════════════════════════════════════════════════════════════
# TIER-1 RECEIPTS — the harness seeds admission, so the replay ladder starts lower
#
# MEASURED 2026-09-04, SDOT 2026-06-26 on robinhood_agentic_mcp, the first receipt with
# real decisions: 18 states, 7 fills, +$3.50, 4,190 events on the sim clock — graded
# ``no_arm_attempt`` because ``seed_replay_session`` writes a queued_live row and never
# emits live_arm_requested / live_arm_confirmed. Those rungs are fixtures on that side.
# ═══════════════════════════════════════════════════════════════════════════════════

TIER1_WINDOW = (_ts("2026-06-26", "13:05:00"), _ts("2026-06-26", "14:05:00"))


def tier1_events():
    day = "2026-06-26"
    return [
        ev(_ts(day, "13:05:01"), "live_runner_started"),
        ev(_ts(day, "13:05:01"), "live_watch_started"),
        ev(_ts(day, "13:23:21"), "live_entry_candidate_detected", trigger="momentum_ok_tick_stream"),
        ev(_ts(day, "13:23:22"), "live_entry_pending_place"),
        ev(_ts(day, "13:23:23"), "live_entry_submitted"),
        ev(_ts(day, "13:23:23"), "live_entry_filled", order_id="replay_mock-00000004"),
        ev(_ts(day, "13:24:34"), "live_exit_filled", reason="scale_out_limit", pnl_usd=3.5),
    ]


def test_a_seeded_receipt_is_no_arm_attempt_under_the_default_ladder():
    """Unchanged default: without the flag the arm rungs are still measured."""
    stage = classify_events(tier1_events(), source="events", chili_pnl_usd=3.5, ross_pnl_usd=5885.15)
    assert stage == STAGE_NO_ARM_ATTEMPT
    assert "admission" not in stage.detail


def test_a_seeded_receipt_with_fills_is_graded_at_the_money_rungs():
    stage = classify_events(
        tier1_events(), source="events", chili_pnl_usd=3.5, ross_pnl_usd=5885.15,
        harness_supplied_admission=True,
    )
    assert str(stage).startswith("filled"), stage
    assert stage.detail["admission"] == "harness_supplied"


def test_a_seeded_receipt_that_never_started_the_runner_is_runner_never_started():
    evs = [ev(_ts("2026-06-26", "13:05:01"), "live_blocked_by_risk", reason="no_bbo")]
    stage = classify_events(evs, source="events", harness_supplied_admission=True)
    assert str(stage).startswith(STAGE_RUNNER_NEVER_STARTED), stage
    assert stage.detail["admission"] == "harness_supplied"


def test_classify_first_divergence_applies_the_flag_to_the_replay_side_only():
    recorded, replay = classify_first_divergence(
        SDOT_CASE, sdot_events(), tier1_events(), TIER1_WINDOW,
        replay_admission_supplied=True,
    )
    # recorded side: real arm events, full ladder, unchanged
    assert str(recorded).startswith(STAGE_ARMED_NO_CANDIDATE)
    assert "admission" not in recorded.detail
    # replay side: seeded admission, graded from runner_never_started down
    assert str(replay).startswith("filled"), replay
    assert replay.detail["admission"] == "harness_supplied"


# ═══════════════════════════════════════════════════════════════════════════════════
# candidate_no_submit names the gate that stopped the SUBMIT (2026-09-04, SDOT alpaca)
# ═══════════════════════════════════════════════════════════════════════════════════

def alpaca_sdot_events():
    """26 attempts, each stopped by the adaptive-risk builder; 1,259 sticky-bench vetoes
    around them. The volume rule named the bench; the attempt rule names the blocker."""
    day = "2026-06-26"
    evs = [ev(_ts(day, "13:05:01"), "live_runner_started"), ev(_ts(day, "13:05:01"), "live_watch_started")]
    t = _ts(day, "13:06:00")
    for i in range(26):
        evs += spread(t, 48, 1.0, "live_entry_backside_bench_veto", reason="benched_backside_sticky")
        t = t + timedelta(seconds=48)
        evs.append(ev(t, "live_entry_candidate_detected", trigger="momentum_ok_tick_stream"))
        evs.append(ev(t + timedelta(seconds=1), "live_entry_pending_place"))
        evs.append(ev(t + timedelta(seconds=2), "live_entry_adaptive_risk_blocked",
                      reason="adaptive_risk_builder_source_invalid"))
        t = t + timedelta(seconds=3)
    evs += spread(t, 11, 1.0, "live_entry_backside_bench_veto", reason="benched_backside_sticky")
    return evs


def test_candidate_no_submit_names_the_attempt_blocker_not_the_loudest_veto():
    stage = classify_events(alpaca_sdot_events(), source="events", harness_supplied_admission=True)
    assert str(stage) == f"{STAGE_CANDIDATE_NO_SUBMIT}(adaptive_risk_builder_source_invalid)", stage
    assert stage.detail["attempts"] == 26
    assert stage.detail["attempt_blockers"] == {"adaptive_risk_builder_source_invalid": 26}
    assert stage.detail["qualifier_rule"] == "first_refusal_after_pending_place"
    # the loud veto is still on the record, as a count, not as the name
    assert stage.detail["veto_reasons"]["benched_backside_sticky"] > 1000


def test_candidate_no_submit_without_an_attempt_keeps_the_volume_rule():
    day = "2026-06-26"
    evs = [ev(_ts(day, "13:05:01"), "live_runner_started"),
           ev(_ts(day, "13:10:00"), "live_entry_candidate_detected")]
    evs += spread(_ts(day, "13:10:01"), 5, 1.0, "live_entry_backside_bench_veto",
                  reason="benched_backside_sticky")
    stage = classify_events(evs, source="events", harness_supplied_admission=True)
    assert str(stage) == f"{STAGE_CANDIDATE_NO_SUBMIT}(benched_backside_sticky)", stage
    assert stage.detail["qualifier_rule"] == "top_refusal_after_candidate"
    assert "attempts" not in stage.detail

"""Pins for ``scripts/rossbench_timeline.py`` — the Ross Parity Bench per-case timeline.

WHY THESE TESTS EXIST
---------------------
The timeline is the UNIT of the bench: every aggregate the bench is allowed to state has to
be traceable to one line of one timeline. So the properties pinned here are not stylistic —
each one is a way the timeline could quietly lie:

  A. STAGE LADDER      — a ladder whose order disagrees with the runner's own
                         ``CANONICAL_ENTRY_SPINE`` would name the wrong second as the
                         divergence. A non-advancing event (a cancel) must never outrank a
                         fill, or the report reads "CHILI got further than Ross".
  B. THE ZERO SENTINEL — the ledger uses ``0`` as NULL (measured on all 187 rows: entry_px
                         67 zeros, exit_px 103, shares 118, pnl_usd 30). A 0 rendered as a
                         real dollar figure corrupts Avoidance and Capture.
  C. NARRATIVE CLOCKS  — ``entry_time_et`` is prose. 132/157 rows lead with a clock; of the
                         14 whose only clock is mid-sentence, 12 explicitly say "not stated"
                         / "unknown" / "bounded". A mid-sentence clock is a BOUND, and
                         promoting it to a pin invents a fill time.
  D. CODE REFS         — a text search for an event type lands on a CONSUMER
                         (``live_replay_audit.py`` merely counts ``live_arm_requested``),
                         not on the branch that made the decision. The resolver must be
                         AST-shaped and must be honest about an unverified tree.
  E. NOTHING DROPPED   — a pin that cannot be placed, an event outside the window, an event
                         type outside the vocabulary: all three must appear in the artifacts.
                         A silent drop is how "measuring silence" starts.
  F. DENSITY           — one row per second, including the silent ones, because the measured
                         gap in the 2026-09-03 master ledger was mostly UPTIME, and uptime is
                         only visible as rows where CHILI did nothing.
  G. THE PINS CONTRACT — THE ONE THAT ALREADY BROKE. ``PIN_ALIASES`` was first written
                         against the raw ross_master_ledger instead of against the pinner's
                         ``chili.ross_event_pins.v1``, and every real pin row normalised to
                         ``kind=None, stage='unmapped', t_utc=None`` — the whole Ross column
                         blank, with nothing raising. Section G therefore builds its fixture
                         by calling the PRODUCER'S OWN functions
                         (``rossbench_pin_ross_events.pin_event`` / ``pin_window``) rather
                         than by hand-typing key names, so a rename over there fails HERE
                         instead of silently re-emptying the column.

NO DATABASE IS TOUCHED and no app module is imported: everything here runs on temp files, an
in-memory synthetic tape, and pure functions.

Runnable: pytest tests/test_rossbench_timeline.py -v
"""
from __future__ import annotations

import ast
import json
import pathlib
import sys
from datetime import datetime, timedelta

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = _ROOT / "scripts" / "rossbench_timeline.py"
for _p in (str(_ROOT), str(_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rossbench_timeline as tlm  # noqa: E402

# The canonical order the runner itself declares (replay_parity.py:59-66). Duplicated here
# ON PURPOSE: if someone reorders the ladder in the module, this literal is the thing that
# fails, and it names its own source.
CANONICAL_ENTRY_SPINE = (
    "live_arm_confirmed",
    "live_watch_started",
    "live_entry_candidate_detected",
    "live_entry_submitted",
    "live_entry_filled",
    "live_exit_filled",
)

WIN_START = datetime(2026, 6, 30, 13, 30, 0)   # 09:30 ET (EDT)
WIN_END = datetime(2026, 6, 30, 13, 40, 0)     # 09:40 ET
ET_DATE = datetime(2026, 6, 30).date()


# ─── A. THE STAGE LADDER ────────────────────────────────────────────────────────────────

def test_ladder_order_matches_canonical_entry_spine():
    ranks = [tlm.STAGE_RANK[tlm.stage_for_event(et)] for et in CANONICAL_ENTRY_SPINE]
    assert ranks == sorted(ranks), (
        f"the ladder disagrees with CANONICAL_ENTRY_SPINE (replay_parity.py:59-66): {ranks}"
    )
    assert len(set(ranks)) == len(CANONICAL_ENTRY_SPINE), (
        "each event of the spine must occupy its OWN rank — collapsing two of them would "
        "make a case that reached one but not the other read as level"
    )


def test_partial_sits_between_fill_and_exit():
    # A partial is not an exit. Collapsing them hides the exit-geometry lever the
    # 2026-09-01 partial-exit study identified.
    assert (tlm.STAGE_RANK[tlm.stage_for_event("live_entry_filled")]
            < tlm.STAGE_RANK[tlm.stage_for_event("live_partial_exit_filled")]
            < tlm.STAGE_RANK[tlm.stage_for_event("live_exit_filled")])


def test_every_mapped_stage_is_in_the_vocabulary():
    for event_type, stage in tlm.EVENT_TYPE_STAGE.items():
        assert stage in tlm.DIVERGENCE_STAGES, f"{event_type} -> unknown stage {stage!r}"


def test_non_advancing_stages_carry_no_rank():
    # THE INVERSION GUARD: a cancel/cooldown must not be able to outrank a fill.
    for stage in tlm.NON_ADVANCING_STAGES:
        assert stage not in tlm.STAGE_RANK, (
            f"{stage!r} has a rank; a non-advancing event would then be able to read as "
            "'further along' than a real fill"
        )


def test_refusal_markers_win_over_lifecycle_substrings():
    # live_runner.py:5221 emits exactly this: it contains "entry" but it is a REFUSAL.
    assert tlm.stage_for_event("live_entry_void_while_paused_blocked") == tlm.STAGE_BLOCKED
    assert tlm.stage_for_event("live_entry_benched_at_hod") == tlm.STAGE_BLOCKED
    # ...but the exact load-bearing name is never overridden by a substring rule.
    assert tlm.stage_for_event("live_entry_filled") == tlm.STAGE_FILLED


def test_unknown_event_type_is_unmapped_not_guessed():
    assert tlm.stage_for_event("some_event_nobody_mapped") == tlm.STAGE_UNMAPPED
    assert tlm.stage_for_event("") == tlm.STAGE_UNMAPPED


def test_ross_entry_is_a_fill_not_a_submission():
    # A Ross pin is an EXECUTED trade recovered from video. If an entry pin ranked as
    # ``submitted``, a CHILI run that submitted and never filled would read as level with a
    # Ross trade that actually printed.
    assert tlm.stage_for_pin_kind("entry") == tlm.STAGE_FILLED
    assert tlm.stage_for_pin_kind("add") == tlm.STAGE_FILLED
    assert tlm.stage_for_pin_kind("partial") == tlm.STAGE_MANAGED
    assert tlm.stage_for_pin_kind("exit") == tlm.STAGE_EXITED
    assert tlm.stage_for_pin_kind("no_trade") == tlm.STAGE_NO_TRADE
    assert tlm.STAGE_RANK[tlm.stage_for_pin_kind("entry")] > tlm.STAGE_RANK[tlm.STAGE_SUBMITTED]


# ─── B. THE ZERO SENTINEL ────────────────────────────────────────────────────────────────

def _pin(**raw):
    return tlm.normalize_pin(raw, et_date=ET_DATE, win_start_utc=WIN_START, win_end_utc=WIN_END)


def test_zero_is_read_as_the_null_sentinel_it_is():
    # Measured on all 187 ledger rows: entry_px 67 zeros, exit_px 103, shares 118,
    # pnl_usd 30. A 0 pnl rendered as "$0.00" claims a flat trade that was never recorded.
    pin = _pin(t_et="9:31", kind="entry", entry_px=0, shares=0, pnl_usd=0, confidence="inferred")
    assert pin["price_usd"] is None
    assert pin["shares"] is None
    assert pin["pnl_usd"] is None
    assert "$" not in tlm.render_pin(pin)


def test_a_real_number_survives():
    pin = _pin(t_et="9:31", kind="entry", entry_px=8.0, shares=1000, pnl_usd=-7500.0)
    assert pin["price_usd"] == 8.0
    assert pin["shares"] == 1000.0
    assert pin["pnl_usd"] == -7500.0
    rendered = tlm.render_pin(pin)
    assert "$8.00" in rendered and "x1,000" in rendered and "-7,500.00" in rendered


def test_confidence_is_never_dropped_from_the_rendered_cell():
    assert "inferred" in tlm.render_pin(_pin(t_et="9:31", kind="entry", confidence="inferred"))
    assert "confidence?" in tlm.render_pin(_pin(t_et="9:31", kind="entry"))


def test_unknown_confidence_word_is_kept_verbatim_and_flagged():
    pin = _pin(t_et="9:31", kind="entry", confidence="pretty sure")
    assert pin["pin_confidence"] == "pretty sure"
    assert pin["pin_confidence_known_vocabulary"] is False
    assert "conf-vocab?" in tlm.render_pin(pin)


# ─── C. NARRATIVE CLOCKS ─────────────────────────────────────────────────────────────────

def test_leading_clock_is_a_pin():
    parsed = tlm.parse_narrative_clock("9:41 HOD break")
    assert parsed.et_time == (9, 41, 0)
    assert parsed.clock_position == "leading"
    assert parsed.approx_marker is False


def test_tilde_is_preserved_not_discarded():
    parsed = tlm.parse_narrative_clock("~9:41")
    assert parsed.et_time == (9, 41, 0)
    assert parsed.approx_marker is True


def test_seconds_are_read_when_present():
    assert tlm.parse_narrative_clock("09:41:37 add").et_time == (9, 41, 37)


def test_leading_clock_with_a_bound_marker_is_still_a_pin_but_tagged():
    # 20 of the 132 leading-clock ledger rows also carry a bound marker, e.g.
    # "~06:00-07:15 (headline 06:00; multiple scalps, first fill time not stated)".
    parsed = tlm.parse_narrative_clock("~06:00-07:15 (headline 06:00; first fill time not stated)")
    assert parsed.et_time == (6, 0, 0)
    assert parsed.narrative_bound is True


def test_mid_sentence_clock_is_a_bound_not_a_pin():
    # Measured: 12 of the 14 mid-sentence rows say "not stated"/"unknown"/"bounded"
    # outright. Promoting that number to a fill time invents evidence.
    parsed = tlm.parse_narrative_clock("premarket, before ~09:00 (clock time not stated)")
    assert parsed.et_time is None
    assert parsed.bound_hint == "09:00"
    assert parsed.reason == "mid_sentence_clock_is_a_bound_not_a_pin"


def test_no_clock_at_all():
    parsed = tlm.parse_narrative_clock("not stated ('ADBB hit the scanner')")
    assert parsed.et_time is None
    assert parsed.reason == "no_clock_in_text"
    assert tlm.parse_narrative_clock(None).reason == "no_clock_text"


def test_meridiem_is_resolved_against_the_window_never_a_constant():
    # 13:00-15:00 ET window; a bare "1:15" literally reads 01:15 ET (outside), and the
    # +12h reading lands inside. The window is a NAMED INPUT, not an invented cutoff hour.
    win_start = datetime(2026, 6, 30, 17, 0, 0)   # 13:00 ET
    win_end = datetime(2026, 6, 30, 19, 0, 0)     # 15:00 ET
    parsed = tlm.parse_narrative_clock("1:15 second leg")
    instant, inferred = tlm.resolve_pin_instant(
        parsed, et_date=ET_DATE, win_start_utc=win_start, win_end_utc=win_end)
    assert inferred is True
    assert instant == datetime(2026, 6, 30, 17, 15, 0)


def test_a_clock_that_already_fits_is_never_shifted():
    parsed = tlm.parse_narrative_clock("9:31")
    instant, inferred = tlm.resolve_pin_instant(
        parsed, et_date=ET_DATE, win_start_utc=WIN_START, win_end_utc=WIN_END)
    assert inferred is False
    assert instant == datetime(2026, 6, 30, 13, 31, 0)


def test_a_clock_that_fits_neither_reading_keeps_the_literal_one():
    parsed = tlm.parse_narrative_clock("11:00")
    instant, inferred = tlm.resolve_pin_instant(
        parsed, et_date=ET_DATE, win_start_utc=WIN_START, win_end_utc=WIN_END)
    assert inferred is False
    assert instant == datetime(2026, 6, 30, 15, 0, 0)   # 11:00 ET, outside the window


def test_dst_is_honoured_on_both_sides_of_the_boundary():
    # The ledger spans 2026-03..2026-08. A hardcoded "ET+4" (or the "+5" several ledger
    # notes assert) is wrong for part of the corpus by construction.
    summer = tlm.to_et(datetime(2026, 6, 30, 13, 30))
    winter = tlm.to_et(datetime(2026, 1, 30, 14, 30))
    assert summer.strftime("%H:%M") == "09:30"
    assert winter.strftime("%H:%M") == "09:30"
    assert summer.utcoffset() != winter.utcoffset()


def test_utc_pin_wins_over_the_narrative_and_records_its_source():
    pin = _pin(t_utc="2026-06-30T13:33:00", t_et="9:31", kind="entry")
    assert pin["t_source"] == "t_utc"
    assert pin["t_utc"] == "2026-06-30T13:33:00"


def test_alias_table_records_which_producer_key_fired():
    pin = _pin(entry_time_et="9:31", event="entry", entry_px=8.0, pnl_confidence="approx")
    assert pin["_aliases"]["t_et"] == "entry_time_et"
    assert pin["_aliases"]["kind"] == "event"
    assert pin["_aliases"]["price"] == "entry_px"
    assert pin["_aliases"]["pin_confidence"] == "pnl_confidence"


# ─── D. CODE REFS ────────────────────────────────────────────────────────────────────────

def _tree(tmp_path, files: dict[str, str]) -> pathlib.Path:
    for rel, body in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return tmp_path


def test_emit_call_wins_over_a_bare_literal_in_a_consumer(tmp_path):
    # This is the real failure mode, verified in the live tree: grepping
    # "live_arm_requested" hits live_replay_audit.py:258, which only COUNTS the event.
    root = _tree(tmp_path, {
        "app/a_consumer.py": 'LIFECYCLE = ["live_entry_filled", "live_exit_filled"]\n',
        "app/z_emitter.py": (
            "def go(db, sess):\n"
            "    _emit(db, sess, \"live_entry_filled\", {\"avg\": 1.0})\n"
        ),
    })
    refs = tlm.resolve_code_refs(["live_entry_filled"], root)
    ref = refs["live_entry_filled"]
    assert ref is not None
    assert ref.file == "app/z_emitter.py"      # NOT the alphabetically-first consumer
    assert ref.line == 2
    assert ref.func == "_emit"
    assert ref.ambiguous is False


def test_keyword_event_type_form_is_found(tmp_path):
    # alpaca_reconcile.py:4116-4119 builds the ORM object directly with event_type=...
    root = _tree(tmp_path, {
        "app/orm.py": (
            "def go(db):\n"
            "    db.add(TradingAutomationEvent(\n"
            "        session_id=1,\n"
            "        event_type=\"live_exit_filled\",\n"
            "        payload_json={},\n"
            "    ))\n"
        ),
    })
    ref = tlm.resolve_code_refs(["live_exit_filled"], root)["live_exit_filled"]
    assert ref is not None and ref.file == "app/orm.py"
    assert ref.line == 2 and ref.literal_line == 4


def test_two_emitters_are_flagged_ambiguous_and_ordered_deterministically(tmp_path):
    root = _tree(tmp_path, {
        "app/one.py": 'def a(db, s):\n    _emit(db, s, "live_bailout", {})\n',
        "app/two.py": 'def b(db, s):\n    _emit(db, s, "live_bailout", {})\n',
    })
    ref = tlm.resolve_code_refs(["live_bailout"], root)["live_bailout"]
    assert ref.ambiguous is True
    assert ref.file == "app/one.py"                 # sorted -> stable across runs
    assert ref.other_sites == ("app/two.py:2",)
    assert "+1 more" in ref.render()


def test_a_computed_event_type_resolves_to_nothing_rather_than_to_a_guess(tmp_path):
    root = _tree(tmp_path, {
        "app/dyn.py": 'def a(db, s, suffix):\n    _emit(db, s, f"live_{suffix}", {})\n',
    })
    assert tlm.resolve_code_refs(["live_entry_filled"], root)["live_entry_filled"] is None


def test_unverified_tree_taints_every_ref(tmp_path):
    root = _tree(tmp_path, {"app/e.py": 'def a(db, s):\n    _emit(db, s, "live_recycled", {})\n'})
    ref = tlm.resolve_code_refs(["live_recycled"], root, verified=False,
                                verification_note="stale build tree")["live_recycled"]
    assert ref.verified is False
    assert "[UNVERIFIED]" in ref.render()


def test_the_resolver_finds_a_real_emit_site_in_this_tree():
    # End-to-end against the ACTUAL build tree: live_runner.py:32824 is
    # ``_emit(db, sess, "live_watch_started", {"product_id": product_id})``.
    refs = tlm.resolve_code_refs(
        ["live_watch_started"], _ROOT,
        scan_roots=("app/services/trading/momentum_neural",),
    )
    ref = refs["live_watch_started"]
    assert ref is not None, "no _emit site found for live_watch_started in this tree"
    line = (_ROOT / ref.file).read_text(encoding="utf-8").splitlines()[ref.line - 1]
    assert "_emit(" in line and "live_watch_started" in line


@pytest.mark.parametrize("head, dirty, ok", [
    ("abc123", False, True),
    ("abc123", True, False),        # right revision, but the working tree had moved
    ("deadbee", False, False),      # the stale-build-tree incident
])
def test_build_tree_verification(head, dirty, ok):
    doc = {"tree": {"head": head, "dirty": dirty}}
    result = tlm.verify_build_tree("/nowhere", doc, head_reader=lambda _d: "abc123")
    assert result.ok is ok
    if not ok:
        assert result.note


def test_verification_fails_when_the_receipt_does_not_say_what_ran():
    result = tlm.verify_build_tree("/nowhere", {"tree": {}}, head_reader=lambda _d: "abc123")
    assert result.ok is False
    assert "tree.head" in (result.note or "")


# ─── E/F. THE GRID ───────────────────────────────────────────────────────────────────────

def _tick(second, **kw):
    return tlm.TapeTick(ts=datetime(2026, 6, 30, 13, 30, second), **kw)


def _build(*, ticks=(), pins=(), events=(), refs=None,
           win_start=WIN_START, win_end=datetime(2026, 6, 30, 13, 30, 10)):
    return tlm.build_timeline(
        case_id="t", symbol="CELZ", win_start=win_start, win_end=win_end,
        ticks=list(ticks), pins=list(pins), events=list(events), code_refs=dict(refs or {}),
    )


def test_the_grid_is_dense_one_row_per_second():
    tl = _build()
    assert len(tl.rows) == 10
    assert [r.t_utc.second for r in tl.rows] == list(range(10))
    # The silent seconds are the point: uptime is only visible as rows where nothing happened.
    assert all(not r.ross and not r.chili for r in tl.rows)


def test_an_empty_window_is_refused_rather_than_producing_zero_rows():
    with pytest.raises(ValueError):
        _build(win_start=WIN_START, win_end=WIN_START)


def test_cum_vol_is_a_monotone_running_sum():
    tl = _build(ticks=[_tick(0, price=8.0, size=100), _tick(0, price=8.1, size=50),
                       _tick(4, price=8.2, size=25)])
    cum = [r.cum_vol for r in tl.rows]
    assert cum == sorted(cum)
    assert cum[0] == 150 and cum[3] == 150 and cum[4] == 175 and cum[-1] == 175
    assert tl.rows[0].last == 8.1, "the LAST print of the second sets the price"
    assert tl.rows[0].prints == 2


def test_carry_forward_is_aged_not_silent():
    # A silently carried price reads as a live one; BBO staleness has been a live-lane
    # defect in this repo more than once, so the age is data.
    tl = _build(ticks=[_tick(0, price=8.0, size=10, bid=7.99, ask=8.01)])
    assert tl.rows[0].last_is_carried is False and tl.rows[0].last_age_s == 0
    assert tl.rows[3].last == 8.0
    assert tl.rows[3].last_is_carried is True and tl.rows[3].last_age_s == 3
    assert tl.rows[3].quote_is_carried is True and tl.rows[3].quote_age_s == 3
    assert "stale 3s" in tlm.render_markdown(tl)


def test_a_second_with_no_tape_at_all_reports_no_price():
    tl = _build()
    assert tl.rows[0].last is None and tl.rows[0].last_age_s is None
    assert tl.rows[0].bid is None and tl.rows[0].quote_age_s is None


def test_first_divergence_is_marked_exactly_once_and_names_a_direction():
    pins = [_pin(t_utc="2026-06-30T13:30:02", kind="entry", entry_px=8.0, confidence="approx")]
    tl = _build(pins=pins)
    marked = [r for r in tl.rows if r.first_divergence]
    assert len(marked) == 1
    assert marked[0].t_utc == datetime(2026, 6, 30, 13, 30, 2)
    div = tl.meta["first_divergence"]
    assert div["direction"] == "ross_ahead"
    assert div["ross_stage"] == tlm.STAGE_FILLED and div["chili_stage"] == tlm.STAGE_ABSENT


def test_no_divergence_when_both_sides_hold_the_same_stage():
    pins = [_pin(t_utc="2026-06-30T13:30:02", kind="entry", entry_px=8.0)]
    events = [{"ts": "2026-06-30T13:30:02", "event_type": "live_entry_filled",
               "payload": {"fill_price": 8.0}}]
    tl = _build(pins=pins, events=events)
    assert tl.meta["first_divergence"] is None
    assert not any(r.first_divergence for r in tl.rows)


def test_a_cancel_does_not_let_chili_outrank_a_ross_fill():
    # THE INVERSION: without the non-advancing rule this window would report "chili_ahead".
    pins = [_pin(t_utc="2026-06-30T13:30:02", kind="entry", entry_px=8.0)]
    events = [{"ts": "2026-06-30T13:30:01", "event_type": "live_cancelled", "payload": {}}]
    tl = _build(pins=pins, events=events)
    assert tl.meta["first_divergence"]["direction"] == "ross_ahead"
    assert tl.rows[-1].chili_rank < tl.rows[-1].ross_rank


def test_money_divergence_requires_a_fill_on_one_side():
    # Watching-vs-absent is a real divergence but it is not yet the money line.
    pins = [_pin(t_utc="2026-06-30T13:30:01", kind="watch"),
            _pin(t_utc="2026-06-30T13:30:05", kind="entry", entry_px=8.0)]
    tl = _build(pins=pins)
    assert tl.meta["first_divergence"]["t_utc"].endswith("13:30:01")
    assert tl.meta["first_money_divergence"]["t_utc"].endswith("13:30:05")
    assert sum(1 for r in tl.rows if r.first_money_divergence) == 1


def test_chili_ahead_is_reported_when_chili_traded_and_ross_did_not():
    events = [{"ts": "2026-06-30T13:30:03", "event_type": "live_entry_filled",
               "payload": {"fill_price": 8.0}}]
    tl = _build(events=events)
    assert tl.meta["first_divergence"]["direction"] == "chili_ahead"


def test_an_unplaced_pin_is_surfaced_not_dropped():
    pins = [_pin(t_et="not stated ('hit the scanner')", kind="entry", entry_px=0),
            _pin(t_et="9:31", kind="entry", entry_px=8.0)]   # 13:31Z: outside the 10s window
    tl = _build(pins=pins)
    unplaced = tl.meta["unplaced_pins"]
    assert len(unplaced) == 2
    reasons = {p["placement_reason"] for p in unplaced}
    assert reasons == {"no_clock_in_text", "outside_window"}
    assert tl.meta["counts"]["pins_placed"] == 0
    md = tlm.render_markdown(tl)
    assert "Ross pins with no line (2)" in md
    assert "no_clock_in_text" in md


def test_an_out_of_window_event_is_surfaced_not_dropped():
    events = [{"ts": "2026-06-30T12:00:00", "event_type": "live_cooldown_started", "payload": {}}]
    tl = _build(events=events)
    assert tl.meta["counts"]["events_out_of_window"] == 1
    assert tl.meta["out_of_window_events"][0]["placement_reason"] == "outside_window"
    assert "CHILI events outside the window (1)" in tlm.render_markdown(tl)


def test_an_unmapped_event_type_is_counted_and_named():
    events = [{"ts": "2026-06-30T13:30:01", "event_type": "brand_new_thing", "payload": {}}]
    tl = _build(events=events)
    assert tl.meta["unmapped_event_types"] == {"brand_new_thing": 1}
    assert "`brand_new_thing` x1" in tlm.render_markdown(tl)


def test_pin_schema_drift_is_visible_as_a_named_missing_field():
    tl = _build(pins=[_pin(t_utc="2026-06-30T13:30:01", kind="entry", entry_px=8.0)])
    assert tl.meta["pin_alias_usage"]["price"] == {"entry_px": 1}
    assert "pin_confidence" in tl.meta["pin_fields_never_supplied"]
    assert "pin fields NO pin supplied" in tlm.render_markdown(tl)


def test_unresolved_code_refs_are_named():
    events = [{"ts": "2026-06-30T13:30:01", "event_type": "live_entry_filled", "payload": {}}]
    tl = _build(events=events, refs={"live_entry_filled": None})
    assert tl.meta["code_refs_unresolved"] == ["live_entry_filled"]
    assert "no resolvable `_emit` site (1)" in tlm.render_markdown(tl)


# ─── G. THE PINS CONTRACT — chili.ross_event_pins.v1 ─────────────────────────────────────
# THE FIXTURES BELOW ARE BUILT BY THE PRODUCER, NOT BY HAND. Hand-written pin dicts are what
# let the first alias table ship: they agreed with the table because the same author wrote
# both, and neither agreed with the file the pinner actually emits. So every row here comes
# out of ``rossbench_pin_ross_events.pin_window`` — the same call
# ``scripts/rossbench_pin_ross_events.py:1788`` makes in its own main loop — over an in-memory
# synthetic tape. If a key is renamed on that side, these tests stop finding it.

_PINNER_PATH = _ROOT / "scripts" / "rossbench_pin_ross_events.py"
try:
    import rossbench_pin_ross_events as pinner  # noqa: E402
    _PINNER_IMPORT_ERROR = None
except ImportError as _exc:                     # producer absent/unimportable: say so by name
    pinner = None
    _PINNER_IMPORT_ERROR = repr(_exc)

requires_pinner = pytest.mark.skipif(
    pinner is None, reason=f"scripts/rossbench_pin_ross_events.py: {_PINNER_IMPORT_ERROR}")

# The case: one Ross round trip inside a five-minute window. The STATED clocks and the PINNED
# seconds are deliberately DIFFERENT (Ross narrates "9:31", the print lands at 9:31:18) —
# equal values would let a test pass while the timeline read the wrong field.
PIN_DAY = datetime(2026, 6, 30).date()
PIN_WIN_START = datetime(2026, 6, 30, 13, 31, 0)    # 09:31 ET (EDT)
PIN_WIN_END = datetime(2026, 6, 30, 13, 36, 0)      # 09:36 ET
STATED_ENTRY_ET = "09:31:15"
STATED_EXIT_ET = "09:34:40"
STATED_ENTRY_UTC = datetime(2026, 6, 30, 13, 31, 15)
STATED_EXIT_UTC = datetime(2026, 6, 30, 13, 34, 40)
ENTRY_PIN_UTC = datetime(2026, 6, 30, 13, 31, 18)
EXIT_PIN_UTC = datetime(2026, 6, 30, 13, 34, 43)
ENTRY_PX = 8.00
EXIT_PX = 8.60
# Not a threshold: the halfwidth is an argument to build_search_window and is asserted back
# out of the rendered cell, so the number here is the test's own input, not a tuned constant.
TEST_HALFWIDTH_S = 810.0


def _tape(instant, price, n=2):
    """A synthetic hydrated-tape slice in the producer's own row shape (its SELECT column
    list is at rossbench_pin_ross_events.py:_TAPE_SLICE_SQL; ``_row_ts`` reads
    ``observed_at`` and ``spread_at`` reads ``bid``/``ask``)."""
    return [{"observed_at": instant + timedelta(seconds=i), "price": price, "size": 100,
             "bid": price - 0.01, "ask": price + 0.01, "source": "iqfeed_lookup_hist"}
            for i in range(n)]


def _ross_leg(leg, clock_et, px, *, symbol="CELZ", ordinal=1):
    stated = pinner.parse_stated_time(f"{clock_et} narrated")
    event = pinner.RossEvent(
        pin_id=f"vid7::{symbol}::2026-06-30::{leg}::r{ordinal}", row_index=ordinal,
        ledger_path="trades", ledger_src="batch.json", video_id="vid7",
        day=PIN_DAY, date_raw="2026-06-30", symbol=symbol, account="main", side="long",
        leg=leg, stated=stated, ross_px=px, setup=None, confidence="approx",
        frame_audited=False,
    )
    return event, stated


def _real_pin_row(*, symbol="CELZ", ordinal=1, entry_rows=None, exit_rows=None,
                  legs=("entry", "exit"), halfwidth=TEST_HALFWIDTH_S):
    """ONE emitted ``chili.ross_event_pins.v1`` window row, produced by the real pinner."""
    entry_ev, entry_stated = _ross_leg("entry", STATED_ENTRY_ET, ENTRY_PX,
                                       symbol=symbol, ordinal=ordinal)
    exit_ev, exit_stated = _ross_leg("exit", STATED_EXIT_ET, EXIT_PX,
                                     symbol=symbol, ordinal=ordinal)
    slices = {
        "entry": (entry_ev, entry_stated,
                  _tape(ENTRY_PIN_UTC, ENTRY_PX) if entry_rows is None else entry_rows),
        "exit": (exit_ev, exit_stated,
                 _tape(EXIT_PIN_UTC, EXIT_PX) if exit_rows is None else exit_rows),
    }
    leg_pins = {}
    kept_events = []
    for name in legs:
        event, stated, rows = slices[name]
        search = pinner.build_search_window(PIN_DAY, stated, halfwidth)
        leg_pins[name] = pinner.pin_event(event, rows, search)
        kept_events.append(event)
    window = pinner.RossWindow(
        manifest_id=f"vid7::{symbol}::2026-06-30::ml{ordinal}",
        manifest_id_basis="layer4_exact",
        ledger_manifest_id=f"vid7::{symbol}::2026-06-30::ml{ordinal}",
        row_index=ordinal, ledger_path="trades", ledger_src="batch.json", video_id="vid7",
        day=PIN_DAY, date_raw="2026-06-30", symbol=symbol, symbol_verbatim=symbol,
        symbol_note=None, account="main", side="long", setup=None, confidence="approx",
        entry_px=ENTRY_PX, exit_px=EXIT_PX, legs=tuple(kept_events),
    )
    return pinner.pin_window(window, leg_pins)


def _normalise(rows, *, symbol="CELZ", manifest_id=None):
    """The exact chain ``main()`` runs: expand -> select -> normalise."""
    legs, expansion = tlm.expand_pin_rows(rows)
    kept, dropped = tlm.select_case_pins(legs, symbol=symbol, manifest_id=manifest_id)
    pins = [tlm.normalize_pin(p, et_date=PIN_DAY, win_start_utc=PIN_WIN_START,
                              win_end_utc=PIN_WIN_END) for p in kept]
    return pins, expansion, dropped


def test_the_producer_module_is_present_and_importable():
    # A skip is silent, and silence is the failure mode this whole section exists for. The
    # file's EXISTENCE is asserted unconditionally so deleting or breaking it cannot make
    # section G quietly evaporate.
    assert _PINNER_PATH.is_file(), f"{_PINNER_PATH} is missing — nothing produces pins"
    assert pinner is not None, f"cannot import the pins producer: {_PINNER_IMPORT_ERROR}"


@requires_pinner
def test_a_real_producer_row_fills_the_ross_column():
    """THE REGRESSION TEST. Measured before the fix, on a full offline pin run of the real
    ledger (157 window rows): kind=None 157/157, stage='unmapped' 157/157, t_utc 0/157,
    price 0/157, known confidence vocabulary 0/157 — the Ross column entirely blank. This
    test fails if that can happen again."""
    row = _real_pin_row()
    doc = pinner.build_pins_doc([row], [], provenance={"built_by": "test"})
    assert doc["schema"] == tlm.PINS_SCHEMA, "the timeline is aliased against THIS schema"

    pins, expansion, dropped = _normalise(doc["pins"])
    assert expansion["window_rows_expanded"] == 1
    assert expansion["legs_out"] == 2 and dropped == []

    health = tlm.ross_column_health(pins)
    assert health["empty"] is False, f"the Ross column came out EMPTY: {health}"
    assert health["renderable"] == 2
    assert health["with_kind"] == 2 and health["with_instant"] == 2
    assert health["with_price"] == 2
    assert health["with_known_confidence_vocabulary"] == 2
    assert health["instants_tape_pinned"] == 2

    by_stage = {p["stage"]: p for p in pins}
    assert set(by_stage) == {tlm.STAGE_FILLED, tlm.STAGE_EXITED}, (
        "a Ross entry is a FILL and a Ross exit is an EXIT; anything else means the 'kind' "
        "alias stopped reading the producer's 'leg'"
    )
    assert by_stage[tlm.STAGE_FILLED]["t_utc"] == ENTRY_PIN_UTC.isoformat()
    assert by_stage[tlm.STAGE_EXITED]["t_utc"] == EXIT_PIN_UTC.isoformat()
    assert by_stage[tlm.STAGE_FILLED]["price_usd"] == ENTRY_PX
    assert by_stage[tlm.STAGE_EXITED]["price_usd"] == EXIT_PX
    assert by_stage[tlm.STAGE_FILLED]["manifest_id"] == row["manifest_id"]


@requires_pinner
def test_the_rendered_timeline_actually_shows_both_ross_events():
    row = _real_pin_row()
    pins, _, _ = _normalise([row])
    tl = tlm.build_timeline(case_id="c", symbol="CELZ", win_start=PIN_WIN_START,
                            win_end=PIN_WIN_END, ticks=[], pins=pins, events=[], code_refs={})
    assert tl.meta["counts"]["pins_placed"] == 2
    assert tl.meta["counts"]["pins_unplaced"] == 0
    assert tl.meta["ross_column_health"]["empty"] is False
    md = tlm.render_markdown(tl)
    assert "THE ROSS COLUMN IS EMPTY" not in md
    assert "ENTRY $8.00" in md and "EXIT $8.60" in md
    assert "2/2 pin(s) rendered" in md
    # Ross ends the window in-and-out; CHILI did nothing, so the divergence is ross_ahead.
    assert tl.meta["first_divergence"]["direction"] == "ross_ahead"
    assert tl.meta["first_money_divergence"]["t_et"] == "09:31:18"


@requires_pinner
def test_every_contract_key_the_producer_promises_is_read_by_name():
    """THE RENAME FENCE. ``PIN_ROW_REQUIRED_KEYS`` is the producer's own list of the keys its
    consumers join and grade on (rossbench_pin_ross_events.py:1326-1333). Every one of them
    must be reachable from this module — through ``PIN_ALIASES`` or by a direct read — or the
    rename is exactly as silent as the one that emptied the column."""
    required = set(pinner.PIN_ROW_REQUIRED_KEYS)
    aliased = {a for aliases in tlm.PIN_ALIASES.values() for a in aliases}
    # Keys the expander/normaliser read directly rather than through the alias table.
    direct = {
        "manifest_id", "manifest_id_basis", "ledger_manifest_id", "symbol", "date", "legs",
        "entry_ts_utc_pinned", "exit_ts_utc_pinned",
        "entry_pin_method", "entry_pin_confidence", "exit_pin_method", "exit_pin_confidence",
        "ross_entry_px", "ross_exit_px", "legs_stated", "grading_anchor_source",
        "search_window_utc", "stated", "pin_method", "pin_id",
    }
    # Promised by the producer, read by a DIFFERENT consumer — named so the rename still has
    # to come through this test rather than being silently absent from both sets.
    read_elsewhere = {"tape": "scripts/ross_replay_bench.py check_pin_sources"}

    # ``expand_pin_rows`` builds the per-side names with an f-string (f"{side}_pin_method"),
    # so those eight are spelled by their SUFFIX in the source, not in full.
    built_per_side = {"entry_ts_utc_pinned", "exit_ts_utc_pinned", "entry_pin_method",
                      "exit_pin_method", "entry_pin_confidence", "exit_pin_confidence",
                      "ross_entry_px", "ross_exit_px"}
    source = _SRC.read_text(encoding="utf-8")
    for key in sorted(direct - built_per_side):
        assert f'"{key}"' in source, (
            f"{key!r} is claimed here as directly-read but never appears in {_SRC.name} — a "
            "stale entry in this set would hide a real gap"
        )
    for pattern in ('f"{side}_ts_utc_pinned"', 'f"{side}_pin_method"',
                    'f"{side}_pin_confidence"', 'f"ross_{side}_px"'):
        assert pattern in source, f"expand_pin_rows no longer builds {pattern}"

    unread = sorted(k for k in required if k not in aliased and k not in direct
                    and k not in read_elsewhere)
    assert unread == [], (
        f"the producer promises {unread} and this module reads none of them. Add each to "
        "PIN_ALIASES (if it is a per-leg field) or to expand_pin_rows (if it is window-level) "
        "— leaving it unread is how the Ross column went blank on 2026-09-04."
    )


@requires_pinner
def test_a_window_row_yields_BOTH_legs_not_just_the_anchor():
    """One row carries the entry AND the exit. The alias table can only read one instant per
    row, so without the expander the exit vanishes — and a half-populated Ross column is
    worse than an empty one, because it looks correct."""
    row = _real_pin_row()
    assert row["leg"] == "entry", "the producer's row-level anchor is the entry leg"

    anchor_only = tlm.normalize_pin(row, et_date=PIN_DAY, win_start_utc=PIN_WIN_START,
                                    win_end_utc=PIN_WIN_END)
    assert anchor_only["stage"] == tlm.STAGE_FILLED   # the entry, and NOTHING else

    legs, expansion = tlm.expand_pin_rows([row])
    assert expansion["by_leg"] == {"entry": 1, "exit": 1}
    stages = {tlm.normalize_pin(leg, et_date=PIN_DAY, win_start_utc=PIN_WIN_START,
                                win_end_utc=PIN_WIN_END)["stage"] for leg in legs}
    assert stages == {tlm.STAGE_FILLED, tlm.STAGE_EXITED}


@requires_pinner
def test_an_exit_leg_never_borrows_the_entry_price():
    # The window row carries ross_entry_px AND ross_exit_px. Putting either in the shared
    # price alias tuple would let the exit line render the entry's dollars.
    row = {k: v for k, v in _real_pin_row().items() if k != "legs"}
    legs, _ = tlm.expand_pin_rows([row])
    prices = {leg["leg"]: tlm.normalize_pin(leg, et_date=PIN_DAY, win_start_utc=PIN_WIN_START,
                                            win_end_utc=PIN_WIN_END)["price_usd"]
              for leg in legs}
    assert prices == {"entry": ENTRY_PX, "exit": EXIT_PX}
    assert "ross_entry_px" not in tlm.PIN_ALIASES["price"]
    assert "ross_exit_px" not in tlm.PIN_ALIASES["price"]


@requires_pinner
def test_the_tape_confirmed_second_outranks_the_stated_one():
    row = _real_pin_row()
    entry = [leg for leg in tlm.expand_pin_rows([row])[0] if leg["leg"] == "entry"][0]
    assert entry["stated_utc"] != entry["pin_second_utc"], "fixture must distinguish the two"
    pin = tlm.normalize_pin(entry, et_date=PIN_DAY, win_start_utc=PIN_WIN_START,
                            win_end_utc=PIN_WIN_END)
    assert pin["_aliases"]["t_utc"] == "pin_second_utc"
    assert pin["t_utc"] == ENTRY_PIN_UTC.isoformat()
    assert pin["instant_is_tape_pinned"] is True
    assert pin["pin_confidence"] == "tape_confirmed"
    assert "stated-time" not in tlm.render_pin(pin)


@requires_pinner
def test_a_leg_the_tape_did_not_confirm_says_so_in_the_cell():
    # An empty slice is a real, common outcome — measured on a full --offline pin run (which
    # reads no tape at all by design) 157/157 window rows came back
    # entry_pin_confidence='unpinned', and a hydrated run produces the same thing for any
    # symbol-day the corpus does not cover. Such a pin is still PLACED (Ross did trade; only
    # WHEN is uncertain) but it must never read as a measured second.
    row = _real_pin_row(entry_rows=[])
    entry = [leg for leg in tlm.expand_pin_rows([row])[0] if leg["leg"] == "entry"][0]
    pin = tlm.normalize_pin(entry, et_date=PIN_DAY, win_start_utc=PIN_WIN_START,
                            win_end_utc=PIN_WIN_END)
    assert pin["pin_confidence"] == "unpinned"
    assert pin["_aliases"]["t_utc"] == "grading_anchor_utc"
    assert pin["t_utc"] == STATED_ENTRY_UTC.isoformat()
    assert pin["instant_is_tape_pinned"] is False
    cell = tlm.render_pin(pin)
    assert "unpinned" in cell
    assert f"stated-time +/-{TEST_HALFWIDTH_S:,.0f}s" in cell


@requires_pinner
def test_both_confidence_vocabularies_are_known_and_kept_apart():
    assert set(tlm.TAPE_PIN_CONFIDENCE_VOCABULARY) == set(pinner.PIN_CONFIDENCES), (
        "the tape-pin vocabulary is the producer's closed enum; a word added there and not "
        "here renders as 'conf-vocab?' forever"
    )
    assert not set(tlm.TAPE_PIN_CONFIDENCE_VOCABULARY) & set(tlm.LEDGER_CONFIDENCE_VOCABULARY)
    for word in tlm.TAPE_PIN_CONFIDENCE_VOCABULARY:
        assert tlm.confidence_vocabulary_of(word) == "tape_pin"
    for word in tlm.LEDGER_CONFIDENCE_VOCABULARY:
        assert tlm.confidence_vocabulary_of(word) == "ledger"
    assert tlm.confidence_vocabulary_of("pretty sure") is None


@requires_pinner
def test_the_two_confidences_are_rendered_as_different_facts():
    # "the ledger is sure Ross bought" and "the tape confirms the second" are independent.
    row = _real_pin_row(entry_rows=[])
    entry = [leg for leg in tlm.expand_pin_rows([row])[0] if leg["leg"] == "entry"][0]
    pin = tlm.normalize_pin(entry, et_date=PIN_DAY, win_start_utc=PIN_WIN_START,
                            win_end_utc=PIN_WIN_END)
    assert pin["pin_confidence"] == "unpinned"          # the tape
    assert pin["ledger_confidence"] == "approx"         # the transcript
    cell = tlm.render_pin(pin)
    assert "unpinned" in cell and "ledger:approx" in cell


@requires_pinner
def test_a_window_row_that_states_no_leg_is_still_visible():
    # The producer emits these deliberately ("no_stated_leg"): the manifest has a window for
    # the row, and an absent pin row is indistinguishable from a failed join.
    row = _real_pin_row(legs=())
    assert row["leg"] is None and row["entry_ts_utc_pinned"] is None
    legs, expansion = tlm.expand_pin_rows([row])
    assert expansion["window_rows_with_no_stated_leg"] == 1
    assert len(legs) == 1
    pin = tlm.normalize_pin(legs[0], et_date=PIN_DAY, win_start_utc=PIN_WIN_START,
                            win_end_utc=PIN_WIN_END)
    tl = tlm.build_timeline(case_id="c", symbol="CELZ", win_start=PIN_WIN_START,
                            win_end=PIN_WIN_END, ticks=[], pins=[pin], events=[], code_refs={})
    assert tl.meta["counts"]["pins_unplaced"] == 1
    assert tl.meta["unplaced_pins"][0]["placement_reason"]


@requires_pinner
def test_a_pin_for_another_symbol_is_never_placed_on_this_cases_grid():
    """The pins file is CORPUS-WIDE (measured: 157 window rows over 72 symbol-days). A pin
    for another ticker that happens to fall inside this window would move first_divergence."""
    mine = _real_pin_row(symbol="CELZ")
    other = _real_pin_row(symbol="ZDAI")
    pins, _, dropped = _normalise([mine, other], symbol="CELZ")
    assert {p["kind"] for p in pins} == {"entry", "exit"}
    assert len(pins) == 2
    assert [d["reason"] for d in dropped] == ["other_symbol", "other_symbol"]
    assert {d["symbol"] for d in dropped} == {"ZDAI"}


@requires_pinner
def test_manifest_id_selects_one_wave_of_a_multi_wave_symbol_day():
    """MEASURED on a full offline pin run of the real ledger (2026-09-04): 72 symbol-days,
    38 of them carrying more than one window row, up to 8 on one (HYFM 2026-08-03). Without
    a row selector a single-wave case would place every wave of the day on its grid."""
    wave1 = _real_pin_row(ordinal=1)
    wave2 = _real_pin_row(ordinal=2)
    assert wave1["manifest_id"] != wave2["manifest_id"]
    pins, _, dropped = _normalise([wave1, wave2], manifest_id=wave2["manifest_id"])
    assert len(pins) == 2 and len(dropped) == 2
    assert {p["manifest_id"] for p in pins} == {wave2["manifest_id"]}
    assert {d["reason"] for d in dropped} == {"other_manifest_id"}


def test_a_hand_written_flat_pin_row_passes_through_the_expander():
    # A per-case pins file written by hand is a legitimate input; the expander must not
    # require the window shape.
    flat = {"t_et": "9:31", "kind": "entry", "entry_px": 8.0}
    legs, expansion = tlm.expand_pin_rows([flat])
    assert expansion == {"rows_in": 1, "window_rows_expanded": 0,
                         "flat_rows_passed_through": 1, "window_rows_with_no_stated_leg": 0,
                         "legs_out": 1, "by_leg": {"entry": 1}}
    assert legs[0] == flat
    kept, dropped = tlm.select_case_pins(legs, symbol="CELZ")
    assert kept == [flat] and dropped == [], "a row with no symbol has nothing to filter on"


def test_the_health_verdict_names_which_alias_family_failed():
    # Both halves of the 2026-09-04 defect, reproduced as data: a producer that renamed the
    # time key, and one that renamed the leg key.
    def _pins(raw_rows):
        legs, _ = tlm.expand_pin_rows(raw_rows)
        return [tlm.normalize_pin(r, et_date=PIN_DAY, win_start_utc=PIN_WIN_START,
                                  win_end_utc=PIN_WIN_END) for r in legs]

    renamed_time = tlm.ross_column_health(
        _pins([{"side_of_trade": "entry", "pinned_at": "2026-06-30T13:31:18"}]))
    assert renamed_time["empty"] is True
    assert renamed_time["reason"] == "no_time_alias_fired"

    renamed_kind = tlm.ross_column_health(
        _pins([{"side_of_trade": "entry", "pin_second_utc": "2026-06-30T13:31:18"}]))
    assert renamed_kind["empty"] is True
    assert renamed_kind["reason"] == "no_kind_alias_fired"
    assert renamed_kind["with_instant"] == 1, "placed, but on a line that says nothing"

    assert tlm.ross_column_health([])["empty"] is False
    assert tlm.ross_column_health([])["reason"] == "no_pins_supplied"


@requires_pinner
def test_fields_the_producer_never_carries_are_not_reported_as_drift():
    """``pin_fields_never_supplied`` is only useful if it is normally EMPTY. A
    chili.ross_event_pins.v1 pin carries no size and no PnL by its own stated constraint, so
    those two are separated out — otherwise the line is on for every case and stops being
    read, which is how the empty column survived in the first place."""
    pins, _, _ = _normalise([_real_pin_row()])
    tl = tlm.build_timeline(case_id="c", symbol="CELZ", win_start=PIN_WIN_START,
                            win_end=PIN_WIN_END, ticks=[], pins=pins, events=[], code_refs={})
    assert tl.meta["pin_fields_never_supplied"] == sorted(tlm.PIN_FIELDS_ABSENT_BY_DESIGN)
    assert tl.meta["pin_fields_never_supplied_unexpected"] == [], (
        "a real producer row supplies every field the timeline reads except the two the "
        "producer deliberately never carries"
    )
    md = tlm.render_markdown(tl)
    assert "pin fields NO pin supplied" not in md
    assert "absent by design" in md


def test_a_genuinely_missing_field_is_still_reported_as_drift():
    tl = _build(pins=[_pin(t_utc="2026-06-30T13:30:01", kind="entry", entry_px=8.0)])
    assert "pin_confidence" in tl.meta["pin_fields_never_supplied_unexpected"]
    assert "pin fields NO pin supplied" in tlm.render_markdown(tl)


def test_the_markdown_shouts_when_the_ross_column_is_empty():
    blind = tlm.normalize_pin({"mystery_key": 1}, et_date=ET_DATE, win_start_utc=WIN_START,
                              win_end_utc=WIN_END)
    tl = _build(pins=[blind])
    assert tl.meta["ross_column_health"]["empty"] is True
    md = tlm.render_markdown(tl)
    assert "THE ROSS COLUMN IS EMPTY" in md
    assert "CHILI against nothing" in md


_REAL_PINS = _ROOT / "project_ws" / "AgentOps" / "ross" / "pins.json"


@pytest.mark.skipif(not _REAL_PINS.is_file(),
                    reason=f"{_REAL_PINS} not built yet — run rossbench_pin_ross_events.py")
def test_the_real_corpus_pins_file_fills_the_ross_column():
    """THE CI HOOK. Run against the real document, a rename anywhere in the chain shows up
    here as an empty column instead of as a blank table nobody reads."""
    rows = tlm.load_pins_document(_REAL_PINS)
    legs, expansion = tlm.expand_pin_rows(rows)
    assert expansion["legs_out"] >= expansion["rows_in"], (
        "every window row states at least one leg, or is emitted as one no-leg record"
    )
    pins = []
    for leg in legs:
        day = datetime.strptime(str(leg.get("date")), "%Y-%m-%d")
        # A two-day window: this test asks whether an instant EXISTS, not where it lands, so
        # the bound is deliberately far wider than any real case window.
        pins.append(tlm.normalize_pin(leg, et_date=day.date(), win_start_utc=day,
                                      win_end_utc=day + timedelta(days=2)))
    health = tlm.ross_column_health(pins)
    assert health["empty"] is False, f"the real pins file renders NOTHING: {health}"
    assert health["with_kind"] == health["pins"], "every leg must map onto the ladder"
    assert health["with_known_confidence_vocabulary"] == health["pins"]
    assert health["with_instant"] > 0


# ─── RENDERING + ARTIFACTS ───────────────────────────────────────────────────────────────

def test_markdown_columns_are_exactly_the_specified_ones():
    assert tlm.MD_COLUMNS == ("t ET", "last", "bid/ask", "cum_vol", "Ross",
                              "CHILI (stage)", "code_ref")
    md = _md()
    assert "| t ET | last | bid/ask | cum_vol | Ross | CHILI (stage) | code_ref |" in md


def _md():
    ref = tlm.CodeRef(file="app/x.py", line=12, literal_line=14, func="_emit")
    return tlm.render_markdown(_build(
        ticks=[_tick(0, price=8.0, size=100, bid=7.99, ask=8.01)],
        pins=[_pin(t_utc="2026-06-30T13:30:02", kind="entry", entry_px=8.0, shares=1000,
                   confidence="approx")],
        events=[{"ts": "2026-06-30T13:30:04", "event_type": "live_entry_filled",
                 "payload": {"fill_price": 8.02, "filled_size": 500}}],
        refs={"live_entry_filled": ref},
    ))


def test_the_divergence_row_is_marked_in_the_table():
    md = _md()
    row = [ln for ln in md.splitlines() if "09:30:02" in ln and ln.startswith("|")][0]
    # Ross fills at 09:30:02 while CHILI is still absent, so this second is BOTH the first
    # divergence (>>) and the first one at or past ``filled`` ($$).
    assert row.startswith("| >> $$ 09:30:02 |")
    assert "**first_divergence**" in md
    assert sum(1 for ln in md.splitlines() if ln.startswith("| >> ")) == 1


def test_the_code_ref_column_carries_file_and_line():
    assert "app/x.py:12" in _md()


def test_a_pipe_in_free_text_cannot_break_the_table():
    tl = _build(pins=[_pin(t_utc="2026-06-30T13:30:01", kind="entry|weird", entry_px=8.0)])
    row = [ln for ln in tlm.render_markdown(tl).splitlines()
           if "09:30:01" in ln and ln.startswith("|")][0]
    # render_pin upper-cases the kind, so the escaped text is ENTRY\|WEIRD.
    assert r"ENTRY\|WEIRD" in row, "a raw pipe in a pin kind would split the row into cells"
    # Count only the pipes that still act as cell separators (the escaped one does not).
    assert row.replace(r"\|", "").count("|") == len(tlm.MD_COLUMNS) + 1


def test_context_seconds_narrows_only_the_markdown():
    tl = _build(pins=[_pin(t_utc="2026-06-30T13:30:05", kind="entry", entry_px=8.0)])
    narrow = tlm.render_markdown(tl, context_seconds=1)
    assert "| 09:30:00 |" not in narrow
    assert "09:30:05" in narrow
    assert "±1s around each eventful second" in narrow
    assert len(tl.rows) == 10, "the underlying grid stays dense"


def test_jsonl_is_one_object_per_second_and_carries_the_divergence_flag(tmp_path):
    tl = _build(pins=[_pin(t_utc="2026-06-30T13:30:02", kind="entry", entry_px=8.0)])
    paths = tlm.write_timeline(tl, tmp_path)
    lines = pathlib.Path(paths["jsonl"]).read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(tl.rows) == 10
    rows = [json.loads(ln) for ln in lines]
    assert {r["schema"] for r in rows} == {tlm.SCHEMA_ROW}
    assert [r["t_et"] for r in rows] == [f"09:30:0{i}" for i in range(10)]
    assert sum(1 for r in rows if r["first_divergence"]) == 1
    assert all("cum_vol" in r and "ross_stage" in r and "chili_stage" in r for r in rows)


def test_jsonl_rows_carry_no_private_keys(tmp_path):
    tl = _build(pins=[_pin(t_utc="2026-06-30T13:30:02", kind="entry", entry_px=8.0)])
    paths = tlm.write_timeline(tl, tmp_path)
    for line in pathlib.Path(paths["jsonl"]).read_text(encoding="utf-8").splitlines():
        for pin in json.loads(line)["ross"]:
            assert not any(k.startswith("_") for k in pin), pin


def test_artifacts_are_written_with_lf_not_crlf(tmp_path):
    # reference_python_write_text_crlf_windows: Windows text mode rewrites \n and changes
    # the bytes of an otherwise identical artifact, breaking byte-comparison of two runs.
    paths = tlm.write_timeline(_build(), tmp_path)
    for key in ("md", "jsonl", "meta"):
        assert b"\r\n" not in pathlib.Path(paths[key]).read_bytes(), key


def test_meta_declares_its_schema_and_its_ladder_provenance(tmp_path):
    paths = tlm.write_timeline(_build(), tmp_path)
    meta = json.loads(pathlib.Path(paths["meta"]).read_text(encoding="utf-8"))
    assert meta["schema"] == tlm.SCHEMA_META
    assert "replay_parity.py" in meta["stage_vocabulary"]["derived_from"]


# ─── INPUT CONTRACTS ─────────────────────────────────────────────────────────────────────

def test_a_foreign_schema_receipt_is_refused(tmp_path):
    path = tmp_path / "run.json"
    path.write_text(json.dumps({"schema": "something.else.v1", "events": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="chili.replay_v3_fsm_window_result.v1"):
        tlm.load_run_json(path)


def test_the_receipt_window_is_read_from_the_env_contract():
    doc = {"schema": tlm.RUN_RESULT_SCHEMA,
           "env": {"SYMBOL": "CELZ", "WIN_START": "2026-06-30T13:30:00",
                   "WIN_END": "2026-06-30T14:30:00"}}
    symbol, start, end = tlm.window_from_run(doc)
    assert symbol == "CELZ"
    assert start == datetime(2026, 6, 30, 13, 30) and end == datetime(2026, 6, 30, 14, 30)


def test_a_receipt_without_a_window_says_so():
    with pytest.raises(ValueError, match="WIN_END"):
        tlm.window_from_run({"schema": tlm.RUN_RESULT_SCHEMA,
                             "env": {"SYMBOL": "CELZ", "WIN_START": "2026-06-30T13:30:00"}})


@pytest.mark.parametrize("doc", [
    [{"kind": "entry"}],
    {"pins": [{"kind": "entry"}]},
    {"ross": {"pins": [{"kind": "entry"}]}},
])
def test_every_plausible_pins_envelope_is_accepted(tmp_path, doc):
    path = tmp_path / "pins.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert tlm.load_pins_document(path) == [{"kind": "entry"}]


def test_an_unrecognised_pins_envelope_raises_instead_of_yielding_nothing(tmp_path):
    # An empty Ross column that came from a schema mismatch is indistinguishable from a
    # case where Ross did nothing — so this must be loud.
    path = tmp_path / "pins.json"
    path.write_text(json.dumps({"ross_pins": [{"kind": "entry"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="no pin list found"):
        tlm.load_pins_document(path)


def test_tape_jsonl_accepts_the_drivers_own_column_names(tmp_path):
    # observed_at / price / size / bid / ask — replay_v3_fsm_window.py:242-270.
    path = tmp_path / "tape.jsonl"
    path.write_text(
        json.dumps({"observed_at": "2026-06-30T13:30:00", "price": 8.0, "size": 100,
                    "bid": 7.99, "ask": 8.01}) + "\n"
        + json.dumps({"ts": "2026-06-30T13:30:01", "last": 8.1, "volume": 50}) + "\n",
        encoding="utf-8")
    ticks = tlm.load_tape_jsonl(path)
    assert [t.price for t in ticks] == [8.0, 8.1]
    assert [t.size for t in ticks] == [100.0, 50.0]


def test_tape_rows_are_sorted_even_if_the_file_is_not(tmp_path):
    path = tmp_path / "tape.jsonl"
    path.write_text(
        json.dumps({"ts": "2026-06-30T13:30:05", "price": 8.5}) + "\n"
        + json.dumps({"ts": "2026-06-30T13:30:01", "price": 8.1}) + "\n", encoding="utf-8")
    assert [t.price for t in tlm.load_tape_jsonl(path)] == [8.1, 8.5]


def test_a_zero_size_print_is_kept_because_the_sentinel_rule_is_a_ledger_rule():
    tick = tlm.tape_row_to_tick({"ts": "2026-06-30T13:30:00", "price": 8.0, "size": 0})
    assert tick.size == 0.0, "0 is a real tape condition; the NULL sentinel is ledger-only"


def test_a_tape_row_without_a_timestamp_cannot_be_placed():
    assert tlm.tape_row_to_tick({"price": 8.0, "size": 10}) is None


@pytest.mark.parametrize("url", [
    "postgresql://chili:chili@localhost:5433/chili",
    "postgresql://chili:chili@localhost:5433/production",
])
def test_a_production_looking_dsn_is_refused(url):
    # Same doctrine as the _test-suffix hard-fail in tests/conftest.py (CLAUDE.md Hard
    # Rule 4). There is deliberately no override flag.
    with pytest.raises(AssertionError, match="replay sinks only"):
        tlm.assert_non_production_dsn(url)


@pytest.mark.parametrize("url", [
    "postgresql://chili:chili@localhost:5433/chili_test",
    "postgresql://chili:chili@localhost:5433/chili_replay2_test",
    "postgresql://chili:chili@localhost:5433/chili_staging",
])
def test_a_sink_dsn_is_accepted(url):
    assert tlm.assert_non_production_dsn(url)


def test_a_dsn_with_no_database_name_is_refused():
    with pytest.raises(AssertionError):
        tlm.assert_non_production_dsn("postgresql://chili:chili@localhost:5433/")


# ─── SOURCE GUARDS ───────────────────────────────────────────────────────────────────────

def test_the_module_imports_nothing_from_the_app_at_module_scope():
    # The timeline must be buildable in a bare interpreter with no DB and no settings:
    # an app-level import would drag in engine construction and make the bench's most
    # basic artifact depend on a live environment.
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    for node in tree.body:
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        for name in names:
            assert not name.startswith("app"), f"module-scope import of {name!r}"
            assert "sqlalchemy" not in name, f"module-scope import of {name!r}"


def test_the_only_sink_read_is_guarded_by_the_dsn_check():
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "load_tape_from_sink")
    calls = [n.func.id for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert "assert_non_production_dsn" in calls, (
        "load_tape_from_sink must call assert_non_production_dsn before opening an engine"
    )
    guard_line = next(n.lineno for n in ast.walk(fn)
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                      and n.func.id == "assert_non_production_dsn")
    engine_line = next(n.lineno for n in ast.walk(fn)
                       if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                       and n.func.id == "create_engine")
    assert guard_line < engine_line, "the guard must run BEFORE the engine is created"


def test_the_cli_requires_a_case_out_dir_run_and_pins():
    parser = tlm.build_parser()
    required = {a.dest for a in parser._actions if getattr(a, "required", False)}
    assert {"case_id", "run_json", "pins", "out_dir"} <= required


def test_the_cli_has_no_numeric_threshold_defaults():
    # This project's operator rejects magic numbers by doctrine: no CLI knob may carry a
    # silent numeric default that could shape a result.
    for action in tlm.build_parser()._actions:
        assert not isinstance(action.default, (int, float)) or isinstance(action.default, bool), (
            f"--{action.dest} carries a numeric default {action.default!r}"
        )


# ═══════════════════════════════════════════════════════════════════════════════════
# first_divergence is anchored at Ross's first pin (2026-09-04)
# ═══════════════════════════════════════════════════════════════════════════════════

_A_WIN_START = datetime(2026, 6, 26, 13, 5, 0)
_A_WIN_END = datetime(2026, 6, 26, 13, 30, 0)


def _a_ev(hhmmss, event_type, **payload):
    return {"ts": f"2026-06-26T{hhmmss}+00:00", "event_type": event_type, "payload": dict(payload)}


def _a_pin(**raw):
    return tlm.normalize_pin(raw, et_date="2026-06-26", win_start_utc=_A_WIN_START, win_end_utc=_A_WIN_END)


def _a_build(events, pins):
    return tlm.build_timeline(
        case_id="SDOT_2026-06-26", symbol="SDOT", win_start=_A_WIN_START, win_end=_A_WIN_END,
        ticks=[], pins=pins, events=events, code_refs={},
    )


def test_first_divergence_is_not_before_ross_first_pin():
    """CHILI watching from 13:05:01 and in the money at 13:18:12; Ross's first pin at
    13:20:30. The first SDOT timeline reported 09:05:01 ET absent/watching — noise."""
    events = [
        _a_ev("13:05:01", "live_runner_started"),
        _a_ev("13:18:12", "live_entry_filled", order_id="x"),
        _a_ev("13:18:32", "live_exit_filled", reason="bailout"),
    ]
    pins = [_a_pin(pin_second_utc="2026-06-26T13:20:30", leg="entry", ross_px=10.02,
                   pin_confidence="tape_confirmed")]
    tl = _a_build(events, pins)
    div = tl.meta["first_divergence"]
    anchor = tl.meta["first_divergence_anchor"]
    assert anchor["rule"] == "first_ross_pin"
    assert anchor["t_utc"] == "2026-06-26T13:20:30"
    assert div is not None
    assert div["t_utc"] >= "2026-06-26T13:20:30"
    # the money divergence keeps its own, earlier meaning: CHILI was filled before Ross
    money = tl.meta["first_money_divergence"]
    assert money is not None and money["t_utc"] == "2026-06-26T13:18:12"


def test_no_ross_pin_but_a_chili_fill_is_chili_ahead_at_the_fill():
    """CHILI traded and Ross did not: a real divergence (the Avoidance shape), anchored at
    CHILI's first fill — not at 'watching' on the first row."""
    events = [_a_ev("13:05:01", "live_runner_started"), _a_ev("13:18:12", "live_entry_filled")]
    tl = _a_build(events, [])
    div = tl.meta["first_divergence"]
    assert div is not None and div["direction"] == "chili_ahead"
    assert div["t_utc"] == "2026-06-26T13:18:12"
    assert tl.meta["first_divergence_anchor"]["rule"] == "chili_first_fill"


def test_no_ross_pin_and_no_chili_fill_means_no_first_divergence_and_says_why():
    events = [_a_ev("13:05:01", "live_runner_started"),
              _a_ev("13:10:00", "live_entry_candidate_detected")]
    tl = _a_build(events, [])
    assert tl.meta["first_divergence"] is None
    assert tl.meta["first_divergence_anchor"]["t_utc"] is None
    assert "CHILI never reached a fill" in tlm.render_markdown(tl)

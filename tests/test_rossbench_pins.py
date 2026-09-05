"""Unit tests for the Ross Parity Bench tape-pin tool (STEP 3).

Pure fakes: a synthetic in-memory tape, no database, no network. A live trading
lane and a shared _test database run on this machine, so nothing here may open a
connection.

What these bind is the one way a pin can be actively harmful: making the bench
look better than it is. A pin that is merely wrong is visible (the overlay does
not line up with the narration); a pin that is FLATTERING is invisible, because
it moves the grading window onto the part of the tape where CHILI happens to
look good. So the hindsight rules get the most coverage here — the window
builder cannot see the tape, a pin cannot escape the stated uncertainty,
window_basis is a closed enum with no tape-derived member, and the earliest
cluster wins even when a later one carries the better price.

The second family of tests binds the LEDGER REALITIES, all measured on the
current 187-row chili.ross_master_ledger.v1: 0 is a null sentinel, 30 rows are
not trades at all, ``account`` has three vocabularies, and video timecodes are
shaped exactly like clock ranges.

Runnable: pytest tests/test_rossbench_pins.py -v
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import build_ross_manifest as bld  # noqa: E402
from scripts import rossbench_pin_ross_events as pin  # noqa: E402
from scripts.replay_harness_invariants import assert_tie_stable_sql  # noqa: E402

# 2026-07-09 is EDT (UTC-4): 07:32 ET == 11:32 UTC. Chosen to match a real
# ledger row (VRAX 2026-07-09 entry 07:32:45 @ 6.30, _path "rows") so the
# fixtures exercise the shapes the corpus actually contains.
DAY = date(2026, 7, 9)
BASE = datetime(2026, 7, 9, 11, 32, 0)  # naive UTC, as iqfeed_trade_ticks stores it

# 2026-03-04 is EST (UTC-5) — before the 2026-03-08 DST change. Used to prove
# the ET->UTC conversion goes through zoneinfo and not a fixed offset.
EST_DAY = date(2026, 3, 4)


def tick(offset_s: float, price: float, *, bid=None, ask=None,
         source="iqfeed_lookup_hist", tid=None, size=100.0) -> dict:
    """One synthetic print, in the exact row shape ``fetch_tape_slice`` returns."""
    return {
        "observed_at": BASE + timedelta(seconds=offset_s),
        "price": float(price),
        "size": size,
        "bid": bid,
        "ask": ask,
        "source": source,
        "id": tid if tid is not None else int(offset_s * 1000),
    }


def make_event(**kw) -> pin.RossEvent:
    """A pinnable event with the fixture defaults; override what a test is about."""
    stated = kw.pop("stated", pin.parse_stated_time("07:32:45"))
    defaults = dict(
        pin_id="vid::VRAX::2026-07-09::entry::r0",
        row_index=0,
        ledger_path="rows",
        ledger_src="ledger_batch05.json",
        video_id="vid",
        day=DAY,
        date_raw="2026-07-09",
        symbol="VRAX",
        account="small",
        side="long",
        leg="entry",
        stated=stated,
        ross_px=6.30,
        setup=None,
        confidence="approx",
        frame_audited=False,
    )
    defaults.update(kw)
    return pin.RossEvent(**defaults)


def window_for(event: pin.RossEvent, halfwidth_s: float = 810.0):
    """The tool's own window builder. 810 s is not a threshold under test here —
    it is the value the current manifest's stated-range distribution produces,
    passed explicitly so no test depends on a default (there is none)."""
    return pin.build_search_window(event.day, event.stated, halfwidth_s)


# ─────────────────────────────────────────────────────────────────────────────
# HALFWIDTH — DERIVED, NEVER A LITERAL
# ─────────────────────────────────────────────────────────────────────────────

def test_halfwidth_is_the_median_stated_range_width():
    doc = {"windows": [
        {"window_et": "~09:00-09:02"},   # 120 s
        {"window_et": "~09:00-09:10"},   # 600 s
        {"window_et": "~09:00-09:15"},   # 900 s
    ]}
    assert pin.stated_range_widths_s(doc) == [120, 600, 900]
    assert pin.derive_halfwidth_s([120, 600, 900]) == 600.0
    res = pin.resolve_halfwidth_s(doc)
    assert res["value_s"] == 600.0
    assert res["basis"] == "median_stated_range_width"
    assert res["n_stated_ranges"] == 3


def test_halfwidth_has_no_default_and_fails_closed_on_an_empty_distribution():
    """The whole point of deriving it: a manifest that states no ranges must
    stop the run, not silently fall back to a number somebody liked."""
    with pytest.raises(ValueError) as exc:
        pin.derive_halfwidth_s([])
    assert "no default" in str(exc.value).lower() or "NO default" in str(exc.value)


def test_video_timecodes_are_not_stated_ranges():
    """'[00:02:24.68-00:02:40.72]' is a position in a YouTube recording. If it
    counted, the halfwidth distribution would be dominated by 16-second
    'ranges' that have nothing to do with the market."""
    doc = {"trades": [{"entry_time_et":
                       "leading gapper, not traded [00:02:24.68-00:02:40.72]"}]}
    assert pin.stated_range_widths_s(doc) == []


def test_utc_restatement_of_the_same_range_is_not_a_second_range():
    """'~09:06-09:40 ET = 13:06-13:40Z' is ONE observation stated twice."""
    doc = {"trades": [{"entry_time_et": "unknown (bounded ~09:06-09:40 ET = 13:06-13:40Z)"}]}
    assert pin.stated_range_widths_s(doc) == [2040]


def test_prose_fields_do_not_contribute_ranges():
    """A setup line says '5.40 -> 8' and a note says '9:41'; neither is a stated
    window, and only keys naming a time/window are read."""
    doc = {"trades": [{"setup": "break of 11:30 double top", "notes": "09:00-09:30 chop"}]}
    assert pin.stated_range_widths_s(doc) == []


def test_override_is_recorded_as_an_override_not_as_a_derivation():
    doc = {"windows": [{"window_et": "~09:00-09:10"}]}
    res = pin.resolve_halfwidth_s(doc, override="42")
    assert res["value_s"] == 42.0
    assert res["basis"] == "operator_override"
    # the derived distribution is still reported, so a reader can see what the
    # override displaced
    assert res["median_stated_range_s"] == 600.0


# ─────────────────────────────────────────────────────────────────────────────
# STATED-TIME PARSING (the field is narrative, not a timestamp)
# ─────────────────────────────────────────────────────────────────────────────

def test_leading_clock_is_the_stated_point():
    s = pin.parse_stated_time("09:41 (approx; 09:39-09:45)")
    # An explicitly stated RANGE is the operator's own uncertainty statement and
    # wins as the search window, but the point is retained for the record.
    assert s.kind == "range"
    assert (s.range_lo_s, s.range_hi_s) == (9 * 3600 + 39 * 60, 9 * 3600 + 45 * 60)
    assert s.point_s == 9 * 3600 + 41 * 60
    assert s.point_position == "leading"


def test_point_only_when_no_range_is_stated():
    s = pin.parse_stated_time("~08:38")
    assert s.kind == "point"
    assert s.point_s == 8 * 3600 + 38 * 60
    assert s.range_lo_s is None


def test_midsentence_clock_is_found_when_there_is_no_leading_one():
    s = pin.parse_stated_time("sold after the 09:57 halt resumption (not timed)")
    assert s.kind == "point"
    assert s.point_position == "midsentence"
    assert s.point_s == 9 * 3600 + 57 * 60


def test_no_clock_at_all_is_reported_not_guessed():
    s = pin.parse_stated_time("not stated ('ADBB hit the scanner just as VCIG pulled back')")
    assert s.kind == "none"
    assert s.point_s is None


def test_seconds_resolution_is_tracked():
    assert pin.parse_stated_time("07:32:45").point_has_seconds is True
    assert pin.parse_stated_time("07:32").point_has_seconds is False


# ─────────────────────────────────────────────────────────────────────────────
# SEARCH WINDOW
# ─────────────────────────────────────────────────────────────────────────────

def test_window_from_a_stated_range_uses_the_stated_bounds():
    ev = make_event(stated=pin.parse_stated_time("~07:30-07:40"))
    w = window_for(ev)
    assert w.basis == "stated_range"
    assert w.lo_utc == datetime(2026, 7, 9, 11, 30)
    assert w.hi_utc == datetime(2026, 7, 9, 11, 40)
    assert w.halfwidth_s is None  # a stated range needs no derived tolerance


def test_window_from_a_stated_point_is_symmetric_around_it():
    ev = make_event(stated=pin.parse_stated_time("07:32:45"))
    w = window_for(ev, halfwidth_s=600.0)
    assert w.basis == "stated_point_pm_halfwidth"
    assert w.lo_utc == BASE + timedelta(seconds=45) - timedelta(seconds=600)
    assert w.hi_utc == BASE + timedelta(seconds=45) + timedelta(seconds=600)


def test_window_is_none_when_nothing_is_stated():
    ev = make_event(stated=pin.parse_stated_time("premarket (clock time not stated)"))
    assert window_for(ev) is None


def test_et_to_utc_goes_through_zoneinfo_not_a_fixed_offset():
    """The ledger spans 2025-11 to 2026-08. A hardcoded -4 would move every
    winter symbol-day by an hour, which looks like a plausible pin."""
    assert pin.et_to_utc(DAY, 9 * 3600 + 41 * 60) == datetime(2026, 7, 9, 13, 41)      # EDT
    assert pin.et_to_utc(EST_DAY, 9 * 3600 + 41 * 60) == datetime(2026, 3, 4, 14, 41)  # EST


# ─────────────────────────────────────────────────────────────────────────────
# HINDSIGHT FENCES — the reason this file exists
# ─────────────────────────────────────────────────────────────────────────────

def test_the_window_builder_cannot_see_the_tape():
    pin.assert_window_builder_is_tape_blind()  # the real one

    def widen_until_it_looks_good(day, stated, halfwidth_s, ticks):  # pragma: no cover
        return None

    with pytest.raises(AssertionError) as exc:
        pin.assert_window_builder_is_tape_blind(widen_until_it_looks_good)
    assert "ticks" in str(exc.value)


def test_window_basis_is_a_closed_enum_with_no_tape_derived_member():
    assert set(pin.WINDOW_BASES) == {"ross_pin_minus_lead", "ross_stated_minus_lead"}
    for legal in pin.WINDOW_BASES:
        assert pin.assert_window_basis(legal) == legal
    with pytest.raises(AssertionError):
        pin.assert_window_basis("tape_peak_minus_lead")


def test_a_pin_outside_the_stated_uncertainty_is_rejected():
    ev = make_event(stated=pin.parse_stated_time("~07:30-07:40"))
    w = window_for(ev)
    with pytest.raises(AssertionError) as exc:
        pin.assert_pin_in_window(w.hi_utc + timedelta(seconds=1), w)
    assert "widened" in str(exc.value)


def test_pin_event_refuses_a_slice_wider_than_the_window():
    """The structural version of the same rule: hand pin_event prints from
    outside the stated window — the shape a 'just widen it a bit' change would
    take — and it fails loudly instead of returning a better-looking pin."""
    ev = make_event(stated=pin.parse_stated_time("~07:30-07:40"))
    w = window_for(ev)
    outside = [tick(3600, 6.30)]  # a perfect price match, 60 minutes late
    with pytest.raises(AssertionError):
        pin.pin_event(ev, outside, w)


def test_the_earliest_cluster_wins_even_when_a_later_one_is_the_better_price():
    """Two clusters, both inside the stated price tolerance. The later one is
    the better entry. Picking it would improve the bench and be a lie."""
    ev = make_event(stated=pin.parse_stated_time("07:32:45"))
    w = window_for(ev, halfwidth_s=600.0)
    rows = [tick(45, 6.30), tick(46, 6.305),          # cluster 1
            tick(300, 6.29), tick(301, 6.292)]        # cluster 2, cheaper entry
    rec = pin.pin_event(ev, rows, w)
    assert rec["pin_confidence"] == "tape_ambiguous"
    assert rec["n_clusters"] == 2
    assert rec["pin_second_utc"] == pin._iso_z(BASE + timedelta(seconds=45))
    # every candidate is listed, so the ambiguity is auditable rather than hidden
    assert [c["first_utc"] for c in rec["clusters"]] == [
        pin._iso_z(BASE + timedelta(seconds=45)),
        pin._iso_z(BASE + timedelta(seconds=300)),
    ]


def test_price_tolerance_stays_centred_on_the_stated_price():
    """A print 11% better than Ross's stated fill is a different print, not a
    better version of his."""
    ev = make_event(stated=pin.parse_stated_time("07:32:45"))
    w = window_for(ev, halfwidth_s=600.0)
    rec = pin.pin_event(ev, [tick(45, 5.60)], w)
    assert rec["pin_method"] == "stated_only"
    assert rec["pin_confidence"] == "unpinned"
    assert "no_method_matched" in rec["notes"]


def test_the_pin_never_rewrites_the_stated_price():
    ev = make_event(stated=pin.parse_stated_time("07:32:45"))
    w = window_for(ev, halfwidth_s=600.0)
    rec = pin.pin_event(ev, [tick(45, 6.3049)], w)
    assert rec["ross_px"] == 6.30           # unchanged by the tape
    assert "px" not in rec.get("tape", {})  # no tape-derived price leaks out


def test_usage_constraints_state_the_hindsight_rules_in_band():
    doc = pin.build_pins_doc([], [], provenance={})
    blob = " ".join(doc["usage_constraints"]).lower()
    assert "never a price" in blob
    assert "never widened" in blob
    assert "earliest" in blob
    assert doc["evidence_role"] == "window_placement_and_overlay_only"


# ─────────────────────────────────────────────────────────────────────────────
# PIN METHODS
# ─────────────────────────────────────────────────────────────────────────────

def test_price_match_single_cluster_is_tape_confirmed():
    ev = make_event(stated=pin.parse_stated_time("07:32:45"))
    w = window_for(ev, halfwidth_s=600.0)
    rows = [tick(0, 6.10), tick(44, 6.20), tick(45, 6.30), tick(46, 6.301), tick(200, 6.55)]
    rec = pin.pin_event(ev, rows, w)
    assert rec["pin_method"] == "price_match"
    assert rec["pin_confidence"] == "tape_confirmed"
    assert rec["window_basis"] == "ross_pin_minus_lead"
    assert rec["pin_second_utc"] == pin._iso_z(BASE + timedelta(seconds=45))
    assert rec["pin_second_et"] == "07:32:45"
    assert rec["grading_anchor_source"] == "tape_pin"


def test_price_match_widens_to_the_spread_when_the_spread_is_wider_than_a_penny():
    """max($0.01, spread_at_t): on a wide book a print 4c away IS the same
    quote, and refusing it would report a false 'the tape disagrees'."""
    wide = tick(45, 6.34, bid=6.30, ask=6.38)   # spread 0.08
    narrow = tick(46, 6.34, bid=6.335, ask=6.345)  # spread 0.01
    assert pin.price_tolerance(wide) == pytest.approx(0.08)
    assert pin.price_tolerance(narrow) == pytest.approx(0.01)
    assert pin.pin_price_match([wide], 6.30)          # inside the 8c spread
    assert not pin.pin_price_match([narrow], 6.30)    # 4c away on a 1c spread


def test_spread_is_ignored_when_the_quote_is_absent_or_crossed():
    assert pin.spread_at(tick(0, 6.3)) is None
    assert pin.spread_at(tick(0, 6.3, bid=6.40, ask=6.30)) is None  # crossed
    assert pin.spread_at(tick(0, 6.3, bid=0, ask=6.30)) is None
    assert pin.price_tolerance(tick(0, 6.3)) == pin.MIN_PRICE_TOLERANCE_USD


def test_level_cross_is_used_when_the_stated_price_is_a_null_sentinel():
    """entry_px 0 is 'absent' (67 rows), so there is no price to match; the
    setup names the level instead."""
    ev = make_event(ross_px=pin._nz(0),
                    setup="high-of-day break at 8 after a 5.40 -> 8 pop, ~500,000-share float",
                    stated=pin.parse_stated_time("07:32:45"))
    assert ev.ross_px is None
    w = window_for(ev, halfwidth_s=600.0)
    rows = [tick(40, 7.90), tick(60, 8.05), tick(80, 8.20)]
    rec = pin.pin_event(ev, rows, w)
    assert rec["pin_method"] == "level_cross"
    assert rec["pin_second_utc"] == pin._iso_z(BASE + timedelta(seconds=60))
    assert any(n.startswith("levels:") for n in rec["notes"])


def test_level_cross_works_downward_too():
    """The ledger names levels as failures as often as breakouts ('the 9.96
    rejection'), so direction is not assumed."""
    rows = [tick(0, 10.20), tick(10, 9.80)]
    clusters = pin.pin_level_cross(rows, [9.96])
    assert len(clusters) == 1
    assert clusters[0].first_utc == BASE + timedelta(seconds=10)


def test_extract_price_levels_is_bounded_by_the_slice_itself():
    """The plausibility band is the window's own price range, so float sizes,
    share counts and percentages fall out without a hand-tuned ceiling."""
    setup = ("high-of-day break at 8 after a 5.40 -> 8 pop; 5.6M float, "
             "~500,000-share float, up 109% on the day; break of 25 (whole number)")
    levels = pin.extract_price_levels(setup, price_lo=5.0, price_hi=9.0)
    assert levels == [8.0, 5.4]
    assert 25.0 not in levels     # outside the slice's price range
    assert 109 not in levels      # a percentage
    assert 5.6 not in levels      # a float size, and outside the band anyway


def test_frame_audit_stated_is_tried_first_and_confirmed_against_the_tape():
    ev = make_event(frame_audited=True,
                    stated=pin.parse_stated_time("08:49:55 (arrow #1 f0470)"),
                    ross_px=9.10)
    w = window_for(ev, halfwidth_s=600.0)
    stated_utc = pin.et_to_utc(DAY, 8 * 3600 + 49 * 60 + 55)
    off = (stated_utc - BASE).total_seconds()
    rows = [tick(off, 9.35), tick(off + 120, 9.10)]  # the price match is LATER
    rec = pin.pin_event(ev, rows, w)
    assert rec["pin_method"] == "frame_audit_stated"
    assert rec["pin_second_utc"] == pin._iso_z(stated_utc)


def test_a_frame_audit_second_absent_from_the_tape_falls_through():
    """A frame audit is evidence about Ross's screen, not proof that our tape
    covers that second."""
    ev = make_event(frame_audited=True,
                    stated=pin.parse_stated_time("07:32:45 (per frame audit)"),
                    ross_px=6.30)
    w = window_for(ev, halfwidth_s=600.0)
    rec = pin.pin_event(ev, [tick(200, 6.30)], w)
    assert rec["pin_method"] == "price_match"
    assert rec["pin_second_utc"] == pin._iso_z(BASE + timedelta(seconds=200))


def test_a_silent_tape_is_unpinned_and_falls_back_to_the_stated_anchor():
    ev = make_event(stated=pin.parse_stated_time("07:32:45"))
    w = window_for(ev, halfwidth_s=600.0)
    rec = pin.pin_event(ev, [], w)
    assert rec["pin_method"] == "stated_only"
    assert rec["pin_confidence"] == "unpinned"
    assert rec["pin_second_utc"] is None
    assert rec["window_basis"] == "ross_stated_minus_lead"
    assert rec["grading_anchor_source"] == "ross_stated"
    assert rec["grading_anchor_utc"] == pin._iso_z(BASE + timedelta(seconds=45))
    assert "no_tape_rows_in_window" in rec["notes"]


def test_an_event_with_no_stated_clock_is_reported_not_dropped():
    ev = make_event(stated=pin.parse_stated_time("not stated (last trade of the day)"))
    rec = pin.pin_event(ev, [], None)
    assert rec["search_window_utc"] is None
    assert rec["pin_confidence"] == "unpinned"
    assert rec["grading_anchor_utc"] is None
    assert "no_stated_clock" in rec["notes"]


def test_a_tape_error_is_recorded_on_the_pin_never_swallowed():
    ev = make_event(stated=pin.parse_stated_time("07:32:45"))
    w = window_for(ev, halfwidth_s=600.0)
    rec = pin.pin_event(ev, [], w, tape_error="QueryCanceled: statement timeout")
    assert rec["tape"]["error"].startswith("QueryCanceled")
    assert any(n.startswith("tape_error:") for n in rec["notes"])
    assert rec["pin_confidence"] == "unpinned"


def test_lead_is_explicitly_null_because_this_tool_does_not_choose_it():
    """An ABSENT key reads as zero to a careless consumer; an explicit null
    plus the usage_constraints line does not."""
    ev = make_event(stated=pin.parse_stated_time("07:32:45"))
    rec = pin.pin_event(ev, [], window_for(ev, halfwidth_s=600.0))
    assert "lead_s" in rec and rec["lead_s"] is None


# ─────────────────────────────────────────────────────────────────────────────
# CLUSTERING
# ─────────────────────────────────────────────────────────────────────────────

def test_prints_within_the_pin_resolution_are_one_cluster():
    """The pin is recorded to the second, so adjacent seconds are one event as
    far as anything downstream can tell."""
    hits = [(BASE, 6.30, "x"), (BASE + timedelta(seconds=1), 6.30, "x")]
    assert len(pin.cluster_hits(hits)) == 1
    hits = [(BASE, 6.30, "x"), (BASE + timedelta(seconds=3), 6.30, "x")]
    assert len(pin.cluster_hits(hits)) == 2


def test_a_multi_source_slice_is_tagged():
    """A double-hydrated symbol-day returns both providers' tapes concatenated
    (measured TMCR 2026-08-24). It cannot move a pin earlier, but it inflates
    cluster counts, so the reader is told."""
    ev = make_event(stated=pin.parse_stated_time("07:32:45"))
    w = window_for(ev, halfwidth_s=600.0)
    rows = [tick(45, 6.30, source="iqfeed_lookup_hist"),
            tick(45, 6.30, source="polygon_v3_trades", tid=99)]
    rec = pin.pin_event(ev, rows, w)
    assert "multi_source_slice" in rec["notes"]
    assert rec["tape"]["sources"] == ["iqfeed_lookup_hist", "polygon_v3_trades"]


# ─────────────────────────────────────────────────────────────────────────────
# LEDGER REALITIES
# ─────────────────────────────────────────────────────────────────────────────

def test_zero_is_a_null_sentinel_not_a_value():
    """67 entry_px / 103 exit_px / 118 shares / 30 pnl_usd zeros in the current
    ledger. A 0 pnl read as a real zero breaks Avoidance and Capture."""
    assert pin._nz(0) is None
    assert pin._nz(0.0) is None
    assert pin._nz(False) is None      # a bool is not a price
    assert pin._nz(None) is None
    assert pin._nz(2262.35) == 2262.35
    assert pin._nz(-120.0) == -120.0   # a real loss survives


def _ledger(rows):
    return {"schema": "chili.ross_master_ledger.v1", "trades": rows}


def test_non_trade_records_are_separated_by_path_not_by_shape():
    """30 of 187 rows are miss / no-trade records merged in from five
    sub-schemas. Promoting one into a graded trade fabricates a Ross trade."""
    led = _ledger([
        {"_path": "trades", "video_id": "v", "date": "2026-07-09", "symbol": "AAA",
         "entry_time_et": "07:32", "exit_time_et": "07:40", "entry_px": 6.3, "exit_px": 7.0},
        {"_path": "rows", "video_id": "v", "date": "2026-07-09", "symbol": "BBB",
         "entry_time_et": "08:00", "entry_px": 1.5},
        {"_path": "no_trade_references", "video_id": "v", "date": "2026-04-20", "symbol": "FCHL"},
        {"_path": "misses_and_no_trades", "video_id": "v", "date": "2026-07-20", "symbol": "ZYBT"},
        {"_path": "ross_no_trade_context", "video_id": "v", "date": "2026-08-17", "symbol": "UCL"},
    ])
    events, non_trades = pin.iter_ross_events(led)
    assert [e.symbol for e in events] == ["AAA", "AAA", "BBB"]   # entry + exit, then entry
    assert [e.leg for e in events] == ["entry", "exit", "entry"]
    assert {n["symbol"] for n in non_trades} == {"FCHL", "ZYBT", "UCL"}


def test_an_unrecognised_path_fails_closed():
    """A sixth sub-schema arriving later must not be pinned by default."""
    led = _ledger([{"_path": "some_new_list", "video_id": "v", "date": "2026-07-09",
                    "symbol": "CCC", "entry_time_et": "07:32", "entry_px": 3.0}])
    events, non_trades = pin.iter_ross_events(led)
    assert events == []
    assert "fail closed" in non_trades[0]["reason"]


def test_account_vocabularies_collapse_big_into_main():
    assert pin.normalize_account("big") == "main"
    assert pin.normalize_account("main") == "main"
    assert pin.normalize_account("small") == "small"   # never merged with main
    assert pin.normalize_account(None) is None
    assert pin.normalize_account("challenge") is None  # unknown, not guessed


def test_each_trade_row_yields_one_event_per_stated_leg():
    led = _ledger([{"_path": "trades", "video_id": "v", "date": "2026-07-09",
                    "symbol": "AAA", "account": "big", "side": "long",
                    "entry_time_et": "07:32", "entry_px": 0, "exit_px": 7.0}])
    events, _ = pin.iter_ross_events(led)
    assert len(events) == 1                 # exit_time_et absent -> no exit event
    assert events[0].account == "main"
    assert events[0].ross_px is None        # entry_px 0 is the sentinel


def test_the_exit_leg_matches_against_the_exit_price():
    led = _ledger([{"_path": "trades", "video_id": "v", "date": "2026-07-09",
                    "symbol": "AAA", "entry_time_et": "07:32", "exit_time_et": "07:33",
                    "entry_px": 6.3, "exit_px": 7.0}])
    events, _ = pin.iter_ross_events(led)
    entry, exit_ = events
    assert (entry.leg, entry.ross_px) == ("entry", 6.3)
    assert (exit_.leg, exit_.ross_px) == ("exit", 7.0)


# ─────────────────────────────────────────────────────────────────────────────
# DOCUMENT + SQL
# ─────────────────────────────────────────────────────────────────────────────

def test_build_pins_doc_counts_every_outcome():
    ev = make_event(stated=pin.parse_stated_time("07:32:45"))
    w = window_for(ev, halfwidth_s=600.0)
    confirmed = pin.pin_event(ev, [tick(45, 6.30)], w)
    unpinned = pin.pin_event(ev, [], w)
    doc = pin.build_pins_doc([confirmed, unpinned], [{"symbol": "FCHL"}],
                             provenance={"halfwidth": {"value_s": 600.0}})
    assert doc["schema"] == "chili.ross_event_pins.v1"
    assert doc["counts"]["pins"] == 2
    assert doc["counts"]["non_trade_records_skipped"] == 1
    assert doc["counts"]["by_pin_confidence"] == {"tape_confirmed": 1, "unpinned": 1}
    assert doc["counts"]["by_window_basis"] == {
        "ross_pin_minus_lead": 1, "ross_stated_minus_lead": 1}
    assert doc["counts"]["with_search_window"] == 2


def test_the_tape_slice_sql_is_tie_stable():
    """Equal observed_at values are common (a burst prints many rows inside one
    millisecond); without the id tiebreak they come back in physical scan order
    and the 'earliest cluster' pin would depend on heap layout. Checked with the
    replay bench's own invariant, over this module's source."""
    assert_tie_stable_sql(Path(pin.__file__).read_text(encoding="utf-8"))


def test_the_tape_slice_sql_is_bounded_by_symbol_and_time():
    flat = " ".join(pin._TAPE_SLICE_SQL.split()).lower()
    assert "from iqfeed_trade_ticks" in flat
    assert "where symbol = %s" in flat
    assert "observed_at >= %s" in flat and "observed_at < %s" in flat
    assert flat.endswith("order by observed_at asc, id asc")


def test_the_cli_exposes_the_halfwidth_override_and_offline_mode():
    args = pin.build_parser().parse_args(["--offline", "--halfwidth-s", "300"])
    assert args.offline is True
    assert args.halfwidth_s == "300"
    # no silent default: unset means "derive it", which fails closed when the
    # manifest states no ranges
    assert pin.build_parser().parse_args([]).halfwidth_s in (None, "")


# ─────────────────────────────────────────────────────────────────────────────
# THE PIN CONTRACT — one row per manifest window, and the join key on it
#
# This family exists because the previous layout emitted one row per LEG keyed
# on pin_id, and the consumer (ross_manifest_adapter) joins on manifest_id and
# reads both sides off one row. Nothing raised; the bench simply scored 0 of
# 418 cases with pin_confidence null on every one. Every test below binds one
# half of that contract so the two files cannot drift apart again silently.
# ─────────────────────────────────────────────────────────────────────────────

def _manifest(*ids_and_notes):
    """A ground-truth manifest containing exactly these (manifest_id, notes)."""
    return {"schema": "chili.ross_ground_truth_manifest.v1",
            "windows": [{"manifest_id": mid, "notes": notes}
                        for mid, notes in ids_and_notes]}


def test_the_layer4_manifest_id_matches_the_builders_own_spelling(tmp_path):
    """The join key is DERIVED here and MINTED there; one character of drift
    would emit a key matching nothing and the bench would report it as clean
    'pin_missing' evidence loss. So the two spellings are compared directly,
    against the builder's real loader rather than against a copy of its rule."""
    ledger = {
        "schema": "chili.ross_master_ledger.v1",
        "trades": [
            {"_path": "trades", "video_id": "v1", "date": "2026-07-09", "symbol": "AAA",
             "entry_time_et": "07:32", "entry_px": 6.3, "pnl_usd": 120.0},
            # same (video, symbol, date): the ordinal must advance to ml2
            {"_path": "trades", "video_id": "v1", "date": "2026-07-09", "symbol": "AAA",
             "entry_time_et": "08:10", "entry_px": 6.9, "pnl_usd": -40.0},
            # a NO-TRADE row still gets a manifest window, and still advances
            # the ordinal for its own symbol-day
            {"_path": "misses_and_no_trades", "video_id": "v1", "date": "2026-07-09",
             "symbol": "BBB"},
            {"_path": "trades", "video_id": "v1", "date": "2026-07-09", "symbol": "BBB",
             "entry_time_et": "09:05", "entry_px": 2.0, "pnl_usd": 15.0},
        ],
    }
    path = tmp_path / "ross_master_ledger.json"
    path.write_text(json.dumps(ledger), encoding="utf-8")

    built = [row["manifest_id"] for row in bld.load_master_ledger(str(path))]
    windows, non_trades = pin.iter_ross_windows(ledger)

    assert built == ["v1::AAA::2026-07-09::ml1", "v1::AAA::2026-07-09::ml2",
                     "v1::BBB::2026-07-09::ml1", "v1::BBB::2026-07-09::ml2"]
    # the pinner skips the no-trade row but must NOT reuse its ordinal
    assert [w.ledger_manifest_id for w in windows] == [
        "v1::AAA::2026-07-09::ml1", "v1::AAA::2026-07-09::ml2",
        "v1::BBB::2026-07-09::ml2"]
    assert non_trades[0]["ledger_manifest_id"] == "v1::BBB::2026-07-09::ml1"


def test_the_symbol_is_spelled_as_the_manifest_spells_it():
    """2 of the 187 ledger symbols carry a parenthetical. The manifest strips
    it, so the pinner must too — otherwise the join key is wrong AND the tape
    query asks for a ticker that does not exist."""
    assert pin.normalize_ledger_symbol("NUWE (09:30 pivot 5.34)")[0] == "NUWE"
    assert pin.normalize_ledger_symbol("VRAX") == ("VRAX", None)
    # a slash-joined symbol is NOT split by the builder, so it is not split here
    symbol, note = pin.normalize_ledger_symbol("EDBL/LGHL/BIYA")
    assert symbol == "EDBL/LGHL/BIYA" and note


def test_an_exact_manifest_id_is_reported_as_verified():
    mid = "v::AAA::2026-07-09::ml1"
    resolved, basis = pin.resolve_manifest_id(
        mid, known_ids={mid}, absorbed={}, manifest_available=True)
    assert (resolved, basis) == (mid, "layer4_exact")


def test_an_absorbed_ledger_row_takes_the_absorbing_windows_id():
    """merge_master folds a ledger row into an older layer's window, which keeps
    its OWN manifest_id and records the ledger row in its notes. Without that
    map the pin would name an id no consumer can find — measured 47 of the
    ledger's 187 rows on 2026-09-04."""
    manifest = _manifest(
        ("VID::AAA::2026-07-09::t1",
         "Merged with master-ledger row v::AAA::2026-07-09::ml1 on (symbol, date, "
         "account, kind) (existing row wins on conflict; ...)"),
    )
    absorbed = pin.manifest_absorption_map(manifest)
    assert absorbed == {"v::AAA::2026-07-09::ml1": "VID::AAA::2026-07-09::t1"}
    resolved, basis = pin.resolve_manifest_id(
        "v::AAA::2026-07-09::ml1",
        known_ids=pin.manifest_window_ids(manifest), absorbed=absorbed,
        manifest_available=True)
    assert (resolved, basis) == ("VID::AAA::2026-07-09::t1", "absorbed_into")


def test_an_unverifiable_id_is_labelled_never_dressed_up():
    """A manifest that predates layer 4 carries no id for any ledger row. The
    derived id is still what a fresh manifest WILL carry, so it is emitted —
    with a basis that says it was not checked."""
    resolved, basis = pin.resolve_manifest_id(
        "v::AAA::2026-07-09::ml1", known_ids=set(), absorbed={},
        manifest_available=False)
    assert (resolved, basis) == ("v::AAA::2026-07-09::ml1", "layer4_unverified")
    assert pin.resolve_manifest_id(None) == (None, "no_symbol")
    assert set(pin.MANIFEST_ID_BASES) == {
        "layer4_exact", "absorbed_into", "layer4_unverified", "no_symbol"}


def _window_with_both_legs():
    led = {"schema": "chili.ross_master_ledger.v1", "trades": [
        {"_path": "trades", "video_id": "vid", "date": "2026-07-09", "symbol": "VRAX",
         "account": "big", "side": "long", "entry_time_et": "07:32:45",
         "exit_time_et": "07:35:00", "entry_px": 6.30, "exit_px": 7.00,
         "pnl_usd": 260.0, "confidence": "approx"},
    ]}
    windows, _ = pin.iter_ross_windows(
        led, manifest=_manifest(("vid::VRAX::2026-07-09::ml1", None)))
    assert len(windows) == 1
    return windows[0]


def test_one_row_per_window_carries_both_sides_and_the_join_key():
    """The whole defect in one assertion: entry AND exit on ONE row, under the
    key names ross_manifest_adapter._event_pin actually reads."""
    win = _window_with_both_legs()
    entry_ev, exit_ev = win.leg("entry"), win.leg("exit")
    entry_win = pin.build_search_window(win.day, entry_ev.stated, 600.0)
    exit_win = pin.build_search_window(win.day, exit_ev.stated, 600.0)
    exit_utc = pin.et_to_utc(DAY, 7 * 3600 + 35 * 60)

    row = pin.pin_window(win, {
        "entry": pin.pin_event(entry_ev, [tick(45, 6.30)], entry_win),
        "exit": pin.pin_event(exit_ev, [{**tick(0, 7.00), "observed_at": exit_utc}], exit_win),
    })

    assert row["manifest_id"] == "vid::VRAX::2026-07-09::ml1"
    assert row["manifest_id_basis"] == "layer4_exact"
    assert row["entry_ts_utc_pinned"] == pin._iso_z(BASE + timedelta(seconds=45))
    assert row["exit_ts_utc_pinned"] == pin._iso_z(exit_utc)
    assert row["entry_pin_method"] == "price_match"
    assert row["entry_pin_confidence"] == "tape_confirmed"
    assert row["exit_pin_method"] == "price_match"
    assert row["exit_pin_confidence"] == "tape_confirmed"
    assert row["legs_stated"] == ["entry", "exit"]
    # symbol + date are the fallback join key the adapter indexes on
    assert (row["symbol"], row["date"]) == ("VRAX", "2026-07-09")


def test_the_row_level_anchor_is_the_entry_and_says_so():
    """pin_id / leg / pin_second_utc / grading_anchor_utc survive because the
    bench runner and the timeline read them (ross_replay_bench.py:238-241).
    ``leg`` names WHICH side they describe, so neither reader has to assume."""
    win = _window_with_both_legs()
    entry_ev = win.leg("entry")
    entry_win = pin.build_search_window(win.day, entry_ev.stated, 600.0)
    row = pin.pin_window(win, {"entry": pin.pin_event(entry_ev, [tick(45, 6.30)], entry_win)})

    assert row["leg"] == "entry"
    assert row["pin_id"] == "vid::VRAX::2026-07-09::entry::r0"
    assert row["pin_second_utc"] == row["entry_ts_utc_pinned"]
    assert row["grading_anchor_utc"] == row["entry_ts_utc_pinned"]
    assert row["grading_anchor_source"] == "tape_pin"
    assert row["pin_confidence"] == row["entry_pin_confidence"]
    assert row["lead_s"] is None
    # the exit leg was never pinned, and says so rather than being absent
    assert row["exit_ts_utc_pinned"] is None


def test_the_per_leg_records_are_kept_under_legs_not_under_entry():
    """A row-level ``entry`` mapping selects ross_manifest_adapter._event_pin's
    NESTED layout, which looks for ``ts_utc_pinned`` — not the
    ``entry_ts_utc_pinned`` this producer emits. Emitting both shapes would make
    the join depend on the consumer's branch order."""
    win = _window_with_both_legs()
    entry_ev = win.leg("entry")
    row = pin.pin_window(win, {"entry": pin.pin_event(
        entry_ev, [tick(45, 6.30)], pin.build_search_window(win.day, entry_ev.stated, 600.0))})

    assert not isinstance(row.get("entry"), dict)
    assert row["legs"]["entry"]["pin_method"] == "price_match"
    assert row["legs"]["entry"]["clusters"]          # full per-leg detail survives

    with pytest.raises(AssertionError) as exc:
        pin.assert_pin_row_contract({**row, "entry": {"ts_utc_pinned": "x"}})
    assert "nested layout" in str(exc.value)


def test_a_dropped_contract_key_stops_the_run():
    win = _window_with_both_legs()
    entry_ev = win.leg("entry")
    row = pin.pin_window(win, {"entry": pin.pin_event(
        entry_ev, [tick(45, 6.30)], pin.build_search_window(win.day, entry_ev.stated, 600.0))})
    for key in ("manifest_id", "entry_ts_utc_pinned", "exit_pin_confidence",
                "grading_anchor_utc"):
        broken = {k: v for k, v in row.items() if k != key}
        with pytest.raises(AssertionError) as exc:
            pin.assert_pin_row_contract(broken)
        assert key in str(exc.value)


def test_a_row_stating_no_leg_at_all_is_still_emitted():
    """The manifest has a window for it. A silently absent pin row is
    indistinguishable from a join that failed."""
    led = {"schema": "chili.ross_master_ledger.v1", "trades": [
        {"_path": "trades", "video_id": "vid", "date": "2026-07-09", "symbol": "CCC",
         "entry_px": 1.0}]}
    windows, _ = pin.iter_ross_windows(led)
    row = pin.pin_window(windows[0], {})
    assert row["leg"] is None
    assert row["pin_id"] == "vid::CCC::2026-07-09::window::r0"
    assert row["entry_ts_utc_pinned"] is None and row["exit_ts_utc_pinned"] is None
    assert row["grading_anchor_utc"] is None
    assert "no_stated_leg" in row["notes"]
    assert row["window_basis"] in pin.WINDOW_BASES


def test_the_tape_survey_is_unioned_across_the_legs():
    """ross_replay_bench.check_pin_sources compares tape.sources against what
    the driver actually read, so a window row must survey BOTH its legs."""
    win = _window_with_both_legs()
    entry_ev, exit_ev = win.leg("entry"), win.leg("exit")
    entry_win = pin.build_search_window(win.day, entry_ev.stated, 600.0)
    exit_win = pin.build_search_window(win.day, exit_ev.stated, 600.0)
    exit_utc = pin.et_to_utc(DAY, 7 * 3600 + 35 * 60)
    row = pin.pin_window(win, {
        "entry": pin.pin_event(entry_ev, [tick(45, 6.30, source="iqfeed_lookup_hist")], entry_win),
        "exit": pin.pin_event(exit_ev, [{**tick(0, 7.00, source="polygon_v3_trades"),
                                         "observed_at": exit_utc}], exit_win),
    })
    assert row["tape"]["sources"] == ["iqfeed_lookup_hist", "polygon_v3_trades"]
    assert row["tape"]["rows_in_window"] == 2
    assert row["tape"]["error"] is None


def test_iter_ross_events_is_still_the_flat_per_leg_view():
    """The LEG is still the unit the tape is searched on; only the emitted
    document is keyed by window."""
    led = {"schema": "chili.ross_master_ledger.v1", "trades": [
        {"_path": "trades", "video_id": "v", "date": "2026-07-09", "symbol": "AAA",
         "entry_time_et": "07:32", "exit_time_et": "07:40", "entry_px": 6.3, "exit_px": 7.0}]}
    events, _ = pin.iter_ross_events(led)
    windows, _ = pin.iter_ross_windows(led)
    assert [e.leg for e in events] == ["entry", "exit"]
    assert [e.pin_id for e in events] == [ev.pin_id for w in windows for ev in w.legs]


def test_the_document_counts_the_join_so_a_broken_one_is_visible():
    """A high joined_to_manifest with an all-'unpinned' entry histogram means
    the contract is fine and the TAPE is thin. Those are different failures and
    used to look identical."""
    win = _window_with_both_legs()
    entry_ev = win.leg("entry")
    row = pin.pin_window(win, {"entry": pin.pin_event(
        entry_ev, [tick(45, 6.30)], pin.build_search_window(win.day, entry_ev.stated, 600.0))})
    doc = pin.build_pins_doc([row], [], provenance={})
    assert doc["counts"]["joined_to_manifest"] == 1
    assert doc["counts"]["by_manifest_id_basis"] == {"layer4_exact": 1}
    assert doc["counts"]["by_entry_pin_confidence"] == {"tape_confirmed": 1}
    assert doc["counts"]["by_exit_pin_confidence"] == {"None": 1}


def test_the_usage_constraints_state_the_row_shape_in_band():
    blob = " ".join(pin.build_pins_doc([], [], provenance={})["usage_constraints"]).lower()
    assert "one row per manifest window" in blob
    assert "manifest_id" in blob and "entry_ts_utc_pinned" in blob


def test_an_exit_pinned_at_or_before_the_entry_is_demoted_to_unpinned():
    """MEASURED 2026-09-05: PPCB 2026-08-27 t2 carried exit_ts_utc_pinned == entry_ts_utc_pinned
    (the exit leg's level_cross landed on the entry's print), so the timeline read Ross as
    `exited` at his entry second. An exit is an exit only strictly after the entry."""
    win = _window_with_both_legs()
    entry_ev, exit_ev = win.leg("entry"), win.leg("exit")
    entry_win = pin.build_search_window(win.day, entry_ev.stated, 600.0)
    exit_win = pin.build_search_window(win.day, exit_ev.stated, 600.0)
    same_second = BASE + timedelta(seconds=45)
    row = pin.pin_window(win, {
        "entry": pin.pin_event(entry_ev, [tick(45, 6.30)], entry_win),
        "exit": pin.pin_event(exit_ev, [{**tick(45, 7.00), "observed_at": same_second}], exit_win),
    })
    assert row["entry_ts_utc_pinned"] == pin._iso_z(same_second)
    assert row["exit_ts_utc_pinned"] is None
    assert row["exit_pin_confidence"] == "unpinned"
    assert any("not_after_entry" in n for n in row["notes"])
    assert row["legs"]["exit"]["demotion"]["reason"] == "exit_not_after_entry"
    assert row["legs"]["exit"]["demotion"]["pinned_second_utc_was"] == pin._iso_z(same_second)


def test_an_exit_after_the_entry_is_untouched():
    win = _window_with_both_legs()
    entry_ev, exit_ev = win.leg("entry"), win.leg("exit")
    entry_win = pin.build_search_window(win.day, entry_ev.stated, 600.0)
    exit_win = pin.build_search_window(win.day, exit_ev.stated, 600.0)
    exit_utc = pin.et_to_utc(DAY, 7 * 3600 + 35 * 60)
    row = pin.pin_window(win, {
        "entry": pin.pin_event(entry_ev, [tick(45, 6.30)], entry_win),
        "exit": pin.pin_event(exit_ev, [{**tick(0, 7.00), "observed_at": exit_utc}], exit_win),
    })
    assert row["exit_ts_utc_pinned"] == pin._iso_z(exit_utc)
    assert row["exit_pin_confidence"] == "tape_confirmed"

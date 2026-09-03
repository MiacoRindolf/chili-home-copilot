"""Unit tests for the Phase 3 hydration fidelity checker.

Pure fakes throughout: no database, no network, no IQConnect socket.  The live
lane is running while these execute, and a test that reached for `chili` or the
lookup port would be a hazard, not a check.

What each test binds is the property that, if it broke, would make the checker
LIE about agreement -- which is worse than no checker at all.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.hydration_fidelity_check import (  # noqa: E402
    MIN_PAGE,
    PROVIDERS,
    compare_nbbo_asof,
    compare_trades,
    dedupe_by_frame,
    extremes,
    miss_rate_by_tape_speed,
    multiset_alignment,
    naive_utc,
    nbbo_structure,
    paged_scan,
    quantile,
    residual_timestamp_skew,
)

T0 = datetime(2026, 8, 21, 13, 30, 0)


def trade(offset_us: int, price: float, size: float, bid=None, ask=None,
          sha: str | None = None, run: str = "run-a", gen: int = 1):
    """(observed_at, id, price, size, bid, ask, source, basis, provider_event_at,
        source_frame_sha256, bridge_run_id, connection_generation)"""
    at = T0 + timedelta(microseconds=offset_us)
    return (at, offset_us, price, size, bid, ask, "iqfeed_l1",
            "iqfeed_selected_trade_date_timems_exact", at,
            sha if sha is not None else f"sha{offset_us}", run, gen)


def quote(offset_us: int, bid: float, ask: float, sha: str | None = None,
          pea=None):
    """(observed_at, id, bid, ask, source, basis, sha, provider_event_at)"""
    at = (T0 + timedelta(microseconds=offset_us)).replace(tzinfo=timezone.utc)
    return (at, offset_us, bid, ask, "iqfeed_l1", "basis",
            sha if sha is not None else f"q{offset_us}", pea)


# --------------------------------------------------------------------------
# timezone handling -- the landmine Phase 1 found
# --------------------------------------------------------------------------
def test_naive_utc_converts_rather_than_strips():
    """The two tapes disagree on awareness; only a CONVERSION is safe.

    momentum_nbbo_spread_tape.observed_at is TIMESTAMPTZ and
    iqfeed_trade_ticks.observed_at is TIMESTAMP WITHOUT TIME ZONE holding UTC.
    Dropping a non-UTC tzinfo instead of converting would shift every NBBO row
    by the offset and the rows would still look well-formed.
    """
    et = datetime(2026, 8, 21, 9, 30, tzinfo=ZoneInfo("America/New_York"))
    assert naive_utc(et) == datetime(2026, 8, 21, 13, 30)
    assert naive_utc(datetime(2026, 8, 21, 13, 30, tzinfo=timezone.utc)) == \
        datetime(2026, 8, 21, 13, 30)
    assert naive_utc(datetime(2026, 8, 21, 13, 30)) == datetime(2026, 8, 21, 13, 30)


def test_naive_utc_handles_the_est_edt_boundary():
    """The offset is not a constant; a fixed -4 h would be wrong in March."""
    est = datetime(2026, 3, 6, 9, 30, tzinfo=ZoneInfo("America/New_York"))
    edt = datetime(2026, 3, 9, 9, 30, tzinfo=ZoneInfo("America/New_York"))
    assert naive_utc(est) == datetime(2026, 3, 6, 14, 30)   # EST, -5
    assert naive_utc(edt) == datetime(2026, 3, 9, 13, 30)   # EDT, -4


# --------------------------------------------------------------------------
# frame-level deduplication of our own recording
# --------------------------------------------------------------------------
def test_dedupe_by_frame_drops_reingested_frames_and_names_the_cause():
    """A restart re-ingests frames we already wrote; nothing dedupes across runs."""
    rows = [
        trade(1, 10.0, 100, sha="A", run="run-a"),
        trade(2, 10.1, 200, sha="B", run="run-a"),
        trade(1, 10.0, 100, sha="A", run="run-b"),   # same frame, later run
        trade(2, 10.1, 200, sha="B", run="run-b"),
    ]
    kept, stats = dedupe_by_frame(rows, 9, 10)
    assert len(kept) == 2
    assert stats["rows_dropped"] == 2
    assert stats["dropped_frames_first_written_by_a_different_bridge_run"] == 2
    assert stats["dropped_rate"] == 0.5


def test_dedupe_by_frame_keeps_rows_that_have_no_hash():
    """Absence of provenance is not evidence of duplication."""
    rows = [trade(1, 10.0, 100, sha=""), trade(2, 10.0, 100, sha="")]
    kept, stats = dedupe_by_frame(rows, 9, 10)
    assert len(kept) == 2
    assert stats["rows_without_frame_hash"] == 2
    assert stats["rows_dropped"] == 0


def test_dedupe_within_one_run_still_counts_but_not_as_cross_run():
    rows = [trade(1, 10.0, 100, sha="A", run="r"), trade(1, 10.0, 100, sha="A", run="r")]
    _, stats = dedupe_by_frame(rows, 9, 10)
    assert stats["rows_dropped"] == 1
    assert stats["dropped_frames_first_written_by_a_different_bridge_run"] == 0


# --------------------------------------------------------------------------
# multiset alignment -- the core correctness property
# --------------------------------------------------------------------------
def test_alignment_is_a_multiset_not_a_set():
    """Two legal prints at one microsecond and price must count as two.

    Set semantics would report a perfect match here while one side is missing a
    genuine fill -- the exact way a fidelity check flatters a broken tape.
    """
    rec = [trade(5, 3.50, 100), trade(5, 3.50, 200)]
    hyd = [trade(5, 3.50, 100)]
    out = multiset_alignment(rec, hyd)
    out.pop("_groups")
    assert out["matched_rows"] == 1
    assert out["recorded_only_rows"] == 1
    assert out["hydrated_only_rows"] == 0


def test_alignment_reports_both_directions_independently():
    rec = [trade(1, 1.0, 10), trade(2, 2.0, 10)]
    hyd = [trade(2, 2.0, 10), trade(3, 3.0, 10)]
    out = multiset_alignment(rec, hyd)
    out.pop("_groups")
    assert (out["matched_rows"], out["recorded_only_rows"],
            out["hydrated_only_rows"]) == (1, 1, 1)
    assert out["recorded_match_rate"] == 0.5
    assert out["hydrated_match_rate"] == 0.5


def test_price_alignment_is_exact_at_six_decimals():
    rec = [trade(1, 3.500000, 10)]
    assert multiset_alignment(rec, [trade(1, 3.500000, 10)])["matched_rows"] == 1
    assert multiset_alignment(rec, [trade(1, 3.500001, 10)])["matched_rows"] == 0


# --------------------------------------------------------------------------
# session extremes -- what a counterfactual actually consumes
# --------------------------------------------------------------------------
def test_extremes_report_the_instant_not_just_the_level():
    rows = [trade(0, 3.0, 10), trade(10, 5.0, 10), trade(20, 2.0, 10)]
    e = extremes(rows)
    assert e["high"] == 5.0 and e["high_at"] == str(T0 + timedelta(microseconds=10))
    assert e["low"] == 2.0 and e["low_at"] == str(T0 + timedelta(microseconds=20))
    assert e["volume"] == 30.0


def test_extremes_empty_is_empty_not_an_exception():
    assert extremes([]) == {}


# --------------------------------------------------------------------------
# burst bias -- the finding that decides whether the recording is usable
# --------------------------------------------------------------------------
def test_miss_rate_by_tape_speed_separates_slow_from_fast_seconds():
    """One quiet second fully captured, one fast second half lost."""
    quiet = [trade(0, 1.0, 1)]
    base = 3_000_000  # a different wall-clock second
    fast = [trade(base + i, 1.0, 1) for i in range(60)]
    missing = fast[:30]
    buckets = {b["ticks_per_second"]: b for b in miss_rate_by_tape_speed(quiet + fast, missing)}
    assert buckets["1-1"]["hydrated_ticks"] == 1
    assert buckets["1-1"]["miss_rate"] == 0.0
    assert buckets["51-100"]["hydrated_ticks"] == 60
    assert buckets["51-100"]["miss_rate"] == 0.5


def test_miss_rate_buckets_cover_every_tick_exactly_once():
    rows = [trade(i * 1000, 1.0, 1) for i in range(200)]
    total = sum(b["hydrated_ticks"] for b in miss_rate_by_tape_speed(rows, []))
    assert total == len(rows)


# --------------------------------------------------------------------------
# residual skew -- would catch a systematic clock offset
# --------------------------------------------------------------------------
def test_residual_skew_recovers_a_planted_offset():
    rec = [trade(0, 4.0, 100), trade(1_000_000, 4.1, 200)]
    hyd = [trade(250_000, 4.0, 100), trade(1_250_000, 4.1, 200)]
    out = residual_timestamp_skew(rec, hyd)
    assert out["matched_by_price_size_within_window"] == 2
    assert out["p50"] == pytest.approx(0.25)
    assert out["abs_gt_1ms"] == 2


def test_residual_skew_ignores_matches_beyond_the_window():
    rec = [trade(0, 4.0, 100)]
    hyd = [trade(5_000_000, 4.0, 100)]
    assert residual_timestamp_skew(rec, hyd)["matched_by_price_size_within_window"] == 0


def test_residual_skew_is_empty_when_the_residual_is_genuine_absence():
    """No (price,size) partner means the tick is missing, not shifted."""
    out = residual_timestamp_skew([trade(0, 4.0, 100)], [trade(1, 9.9, 300)])
    assert out["matched_by_price_size_within_window"] == 0
    assert out["p50"] is None


# --------------------------------------------------------------------------
# NBBO as-of sampling
# --------------------------------------------------------------------------
def test_nbbo_asof_uses_the_last_quote_at_or_before_the_instant():
    rec = [quote(0, 1.00, 1.02), quote(2_000_000, 1.10, 1.12)]
    hyd = [quote(0, 1.00, 1.02), quote(2_000_000, 1.10, 1.12)]
    out = compare_nbbo_asof(rec, hyd, T0, T0 + timedelta(seconds=3), samples=10)
    assert out["comparable_instants"] == 10
    assert out["exact_rate"] == 1.0


def test_nbbo_asof_flags_a_disagreeing_tape():
    rec = [quote(0, 1.00, 1.02), quote(1_000_000, 1.00, 1.02)]
    hyd = [quote(0, 1.05, 1.02), quote(1_000_000, 1.05, 1.02)]
    out = compare_nbbo_asof(rec, hyd, T0, T0 + timedelta(seconds=2), samples=4)
    assert out["comparable_instants"] == 4
    assert out["exact_rate"] == 0.0
    assert out["ask_exact_rate"] == 1.0
    assert out["bid_diff"]["max_abs"] == pytest.approx(0.05)


def test_nbbo_asof_clips_the_sample_window_to_where_both_tapes_have_quotes():
    """Deliberate: the tail our recording never covered is the coverage gap the
    hydrator exists to fill, not a disagreement to charge against a provider."""
    rec = [quote(0, 1.0, 1.1), quote(1_000_000, 1.0, 1.1)]
    hyd = [quote(0, 1.0, 1.1), quote(9_000_000, 1.0, 1.1)]
    out = compare_nbbo_asof(rec, hyd, T0, T0 + timedelta(seconds=20), samples=10)
    assert out["hydrated_span"][1] == str(T0 + timedelta(seconds=9))
    assert out["comparable_instants"] == 10
    assert out["exact_rate"] == 1.0


def test_nbbo_asof_skips_when_the_spans_do_not_overlap():
    rec = [quote(0, 1.0, 1.1)]
    hyd = [quote(10_000_000, 1.0, 1.1)]
    out = compare_nbbo_asof(rec, hyd, T0, T0 + timedelta(seconds=1), samples=4)
    assert out.get("skipped") == "no overlapping span"


def test_nbbo_structure_exposes_a_tape_that_repeats_itself():
    """Row count flatters a tape whose rows share one borrowed timestamp."""
    rows = [quote(0, 1.0, 1.1, sha="a"), quote(0, 1.0, 1.2, sha="b"),
            quote(0, 1.0, 1.3, sha="a")]
    out = nbbo_structure(rows, recorded=True)
    assert out["rows"] == 3
    assert out["distinct_timestamps"] == 1
    assert out["max_rows_sharing_one_timestamp"] == 3
    assert out["rows_repeating_an_already_ingested_frame"] == 1
    assert out["rows_with_no_own_quote_clock"] == 3


# --------------------------------------------------------------------------
# end to end on a small fixture
# --------------------------------------------------------------------------
def test_compare_trades_confines_itself_to_the_recorded_window():
    """Hydrated coverage outside our recording is the POINT, not a disagreement."""
    rec = [trade(1_000_000, 2.0, 100), trade(2_000_000, 2.1, 100)]
    hyd = [trade(0, 1.9, 50)] + rec + [trade(3_000_000, 2.2, 50)]
    out = compare_trades(rec, hyd, "iqfeed")
    assert out["overlap_window"]["recorded_rows"] == 2
    assert out["overlap_window"]["hydrated_rows"] == 2
    assert out["multiset_ts_price"]["hydrated_only_rows"] == 0
    assert out["extremes_agree"] is True
    assert out["extreme_times_agree"] is True
    # the full-day extremes of the hydrated tape are NOT smuggled in
    assert out["hydrated_extremes"]["high"] == 2.1


def test_compare_trades_surfaces_a_missing_print_and_its_volume():
    rec = [trade(0, 2.0, 100), trade(2_000_000, 2.0, 100)]
    hyd = [trade(0, 2.0, 100), trade(1_000_000, 9.0, 777), trade(2_000_000, 2.0, 100)]
    out = compare_trades(rec, hyd, "iqfeed")
    assert out["multiset_ts_price"]["hydrated_only_rows"] == 1
    assert out["volume_missing_from_recording"] == 777.0
    # the missing print is the session high, so the extremes MUST disagree
    assert out["extremes_agree"] is False


def test_compare_trades_reports_quote_disagreement_on_matched_ticks():
    rec = [trade(0, 2.0, 100, bid=1.99, ask=2.01)]
    hyd = [trade(0, 2.0, 100, bid=1.98, ask=2.01)]
    out = compare_trades(rec, hyd, "iqfeed")
    q = out["at_trade_quote_agreement"]
    assert q["compared_pairs"] == 1
    assert q["exact_within_tol"] == 0
    assert q["bid_max_abs_diff"] == pytest.approx(0.01)


def test_compare_trades_reports_size_disagreement_without_breaking_alignment():
    """Polygon reports fractional/odd-lot sizes; that must show up as a size
    difference, not as a phantom missing tick."""
    rec = [trade(0, 2.0, 100)]
    hyd = [trade(0, 2.0, 100.5)]
    out = compare_trades(rec, hyd, "polygon")
    assert out["multiset_ts_price"]["matched_rows"] == 1
    assert out["size_agreement"]["mismatched"] == 1
    assert out["size_agreement"]["sum_signed_diff"] == pytest.approx(0.5)


def test_compare_trades_skips_cleanly_when_a_side_is_empty():
    assert compare_trades([], [trade(0, 1.0, 1)], "iqfeed")["skipped"] == "one side empty"


# --------------------------------------------------------------------------
# paging behaviour under the 30 s hard rule
# --------------------------------------------------------------------------
class _FakeQueryCanceled(Exception):
    pass


class _FakeCursor:
    def __init__(self, owner):
        self.owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if not sql.startswith("SELECT"):
            return
        limit = int(sql.rsplit("LIMIT ", 1)[1])
        self.owner.limits.append(limit)
        if limit > self.owner.survivable:
            raise _FakeQueryCanceled("canceling statement due to statement timeout")
        self.owner.rows_out = self.owner.next_batch(limit)

    def fetchall(self):
        return self.owner.rows_out


class _FakeConn:
    def __init__(self, rows, survivable):
        self.rows = rows
        self.survivable = survivable
        self.limits: list[int] = []
        self.rows_out: list = []
        self.pos = 0
        self.rollbacks = 0

    def cursor(self):
        return _FakeCursor(self)

    def rollback(self):
        self.rollbacks += 1

    def next_batch(self, limit):
        batch = self.rows[self.pos:self.pos + limit]
        self.pos += len(batch)
        return batch


def test_paged_scan_shrinks_the_page_instead_of_raising_the_timeout(monkeypatch):
    """The 30 s statement timeout on `chili` is a hard rule.

    A slow page must be answered by asking for fewer rows.  If this ever
    regressed into raising statement_timeout, the checker would be putting load
    on the live database that the rule exists to prevent.
    """
    import scripts.hydration_fidelity_check as mod

    class _Errors:
        QueryCanceled = _FakeQueryCanceled

    monkeypatch.setitem(sys.modules, "psycopg2",
                        type("m", (), {"errors": _Errors})())
    rows = [(T0 + timedelta(microseconds=i), i) for i in range(900)]
    conn = _FakeConn(rows, survivable=1250)
    got = mod.paged_scan(conn, "t", ("observed_at", "id"), "SYM",
                         T0, T0 + timedelta(days=1), None, aware=False)
    assert len(got) == 900
    assert conn.limits[0] == mod.DEFAULT_PAGE
    assert conn.limits[1] == mod.DEFAULT_PAGE // 4       # 5000, still too slow
    assert conn.limits[2] == mod.DEFAULT_PAGE // 16      # 1250, survives
    assert conn.rollbacks == 2
    assert all(limit >= MIN_PAGE for limit in conn.limits)


def test_paged_scan_gives_up_rather_than_looping_forever(monkeypatch):
    import scripts.hydration_fidelity_check as mod

    class _Errors:
        QueryCanceled = _FakeQueryCanceled

    monkeypatch.setitem(sys.modules, "psycopg2",
                        type("m", (), {"errors": _Errors})())
    conn = _FakeConn([], survivable=0)
    with pytest.raises(_FakeQueryCanceled):
        mod.paged_scan(conn, "t", ("observed_at", "id"), "SYM",
                       T0, T0 + timedelta(days=1), None, aware=False)


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------
def test_provider_sources_match_the_hydrator_constants():
    """A drifted source tag would silently compare against an empty tape and
    report a clean 'no rows' instead of a disagreement."""
    from scripts import historical_tick_hydrator as h

    assert PROVIDERS["iqfeed"] == (h.SOURCE_IQFEED_TRADES, h.SOURCE_IQFEED_NBBO)
    assert PROVIDERS["polygon"] == (h.SOURCE_POLYGON_TRADES, h.SOURCE_POLYGON_NBBO)
    assert "iqfeed_l1" not in {s for pair in PROVIDERS.values() for s in pair}


def test_quantile_edges():
    assert quantile([], 0.5) is None
    assert quantile([1.0], 0.99) == 1.0
    assert quantile([1.0, 2.0, 3.0], 0.0) == 1.0
    assert quantile([1.0, 2.0, 3.0], 1.0) == 3.0

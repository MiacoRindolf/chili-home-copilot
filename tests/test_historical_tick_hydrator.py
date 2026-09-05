"""Unit tests for the historical tick/NBBO hydrator.

These are deliberately PURE: no network, no IQConnect socket, no database. A
live trading lane and a live IQFeed bridge are running on this machine, and a
test suite is the last place that should be allowed to touch either. Everything
that talks to the outside world is exercised through a fake.

What they bind, in order of how much damage the bug would do:

  1. The TIMEZONE contract. IQFeed lookup returns ET-naive; the tick table holds
     UTC in a WITHOUT-TIME-ZONE column and the NBBO tape holds UTC in a WITH-
     TIME-ZONE column. A regression here shifts every row by four or five hours
     and leaves it looking perfectly well-formed.
  2. The PROVENANCE contract. Hydrated rows must be structurally incapable of
     passing counterfactual_replay's strict causal predicate.
  3. IQFeed TRUNCATION recovery. A capped response drops the OLDEST records, so
     a naive reader silently loses the front of every busy window.
  4. COPY TEXT encoding, which is where a stray tab or backslash turns into a
     column-count error or, worse, a silently mis-parsed row.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from scripts.historical_tick_hydrator import (
    BASIS_IQFEED,
    HYDRATED_SOURCES,
    MESSAGE_TYPE_HYDRATED,
    NBBO_COLUMNS,
    TRADE_COLUMNS,
    CopyStream,
    QuoteIndex,
    QuoteTick,
    TradeTick,
    copy_field,
    copy_line,
    et_day_bounds_utc,
    et_naive_to_utc,
    iqfeed_retention_floor,
    iter_iqfeed_window,
    naive_utc,
    nbbo_copy_row,
    parse_iqfeed_tick,
    parse_polygon_quote,
    parse_polygon_trade,
    parse_symbol_day,
    read_pairs_csv,
    resolve_dsn,
    trade_copy_row,
)
from scripts.iqfeed_lookup_client import RequestResult

UTC = timezone.utc
ET = ZoneInfo("America/New_York")


def et(*args) -> datetime:
    """An ET-AWARE datetime. ``iter_iqfeed_window`` takes zone-aware ET bounds
    because it must render them as IQFeed's local wall-clock strings AND
    compare them against UTC instants; a naive value silently does neither."""
    return datetime(*args, tzinfo=ET)


# ---------------------------------------------------------------------------
# 1. the timezone landmine
# ---------------------------------------------------------------------------
def test_et_to_utc_uses_the_zone_database_not_a_fixed_offset():
    """The EDT/EST offset differs, and BOTH sides of the transition are inside
    IQFeed's 180-day retention window. A hardcoded -4h would corrupt March."""
    edt = et_naive_to_utc("2026-09-02 09:30:00.123456")
    est = et_naive_to_utc("2026-03-06 09:30:00.123456")
    assert edt == datetime(2026, 9, 2, 13, 30, 0, 123456, tzinfo=UTC)   # -4h
    assert est == datetime(2026, 3, 6, 14, 30, 0, 123456, tzinfo=UTC)   # -5h
    assert (edt.hour - 9) != (est.hour - 9), "offset must NOT be constant"


def test_et_to_utc_accepts_a_timestamp_without_subseconds():
    assert et_naive_to_utc("2026-09-02 09:30:00") == datetime(
        2026, 9, 2, 13, 30, tzinfo=UTC
    )


def test_naive_utc_strips_the_zone_but_never_the_wall_time():
    aware = datetime(2026, 9, 2, 13, 30, 0, 123456, tzinfo=UTC)
    stripped = naive_utc(aware)
    assert stripped.tzinfo is None
    assert stripped == datetime(2026, 9, 2, 13, 30, 0, 123456)


def test_et_day_bounds_span_a_full_local_day_across_the_dst_boundary():
    edt_start, edt_end = et_day_bounds_utc(date(2026, 9, 2))
    assert edt_start == datetime(2026, 9, 2, 4, 0, tzinfo=UTC)
    assert edt_end == datetime(2026, 9, 3, 4, 0, tzinfo=UTC)

    est_start, est_end = et_day_bounds_utc(date(2026, 3, 6))
    assert est_start == datetime(2026, 3, 6, 5, 0, tzinfo=UTC)
    assert est_end == datetime(2026, 3, 7, 5, 0, tzinfo=UTC)

    # The spring-forward day is 23 hours long. Bounds computed with a fixed
    # offset would silently claim 24 and drag an hour of the next day in.
    dst_start, dst_end = et_day_bounds_utc(date(2026, 3, 8))
    assert (dst_end - dst_start).total_seconds() == 23 * 3600


def test_retention_floor_tracks_the_calendar():
    assert iqfeed_retention_floor(date(2026, 9, 2)) == date(2026, 3, 6)
    # It moves forward one day per day; a frozen constant would go stale.
    assert iqfeed_retention_floor(date(2026, 9, 3)) == date(2026, 3, 7)


# ---------------------------------------------------------------------------
# 2. the provenance contract
# ---------------------------------------------------------------------------
def _sample_tick() -> TradeTick:
    return TradeTick(
        ts_utc=datetime(2026, 9, 2, 13, 30, 0, 123456, tzinfo=UTC),
        price=4.62, size=100.0, bid=4.60, ask=4.64,
        day_volume=1_000_000.0, sequence=12345, frame_sha256="a" * 64,
    )


def test_hydrated_trade_rows_can_never_satisfy_the_strict_causal_predicate():
    """counterfactual_replay's strict gate needs, among others,
    source='iqfeed_l1' AND provider_event_at IS NULL AND available_at IS NOT NULL.
    Hydrated rows must fail it on every one of those independently, so that a
    future edit to any single field cannot quietly let hydrated data into a
    causal replay."""
    row = dict(zip(TRADE_COLUMNS, trade_copy_row(
        "CANF", _sample_tick(), source="iqfeed_lookup_hist", basis=BASIS_IQFEED,
        batch_id="b" * 36, received_at=datetime(2026, 9, 2, 23, 0, tzinfo=UTC),
    )))
    assert row["source"] != "iqfeed_l1"
    assert row["source"] in HYDRATED_SOURCES
    assert row["provider_event_at"] is not None
    assert "available_at" not in TRADE_COLUMNS       # -> stays NULL
    assert "connection_generation" not in TRADE_COLUMNS
    assert row["message_type"] == MESSAGE_TYPE_HYDRATED != "Q"


def test_hydrated_nbbo_rows_carry_the_same_three_independent_disqualifiers():
    q = QuoteTick(
        ts_utc=datetime(2026, 9, 2, 13, 30, tzinfo=UTC),
        bid=4.60, ask=4.64, day_volume=None, sequence=7, frame_sha256="c" * 64,
    )
    row = dict(zip(NBBO_COLUMNS, nbbo_copy_row(
        "CANF", q, source="iqfeed_lookup_bbo", basis=BASIS_IQFEED,
        batch_id="b" * 36, received_at=datetime(2026, 9, 2, 23, 0, tzinfo=UTC),
    )))
    assert row["source"] != "iqfeed_l1"
    assert row["provider_event_at"] is not None
    assert "available_at" not in NBBO_COLUMNS
    assert row["mid"] == pytest.approx(4.62)
    assert row["spread_bps"] == pytest.approx((0.04 / 4.62) * 10_000)


def test_the_two_tables_get_opposite_timestamp_treatment():
    """The tick table's observed_at is WITHOUT time zone; the NBBO tape's is
    WITH. Writing an aware value into the first, or a naive value into the
    second, is exactly the 4-hour-shift bug this hydrator exists to avoid."""
    tick = _sample_tick()
    trade = dict(zip(TRADE_COLUMNS, trade_copy_row(
        "CANF", tick, source="iqfeed_lookup_hist", basis=BASIS_IQFEED,
        batch_id="b" * 36, received_at=datetime(2026, 9, 2, 23, 0, tzinfo=UTC))))
    quote = dict(zip(NBBO_COLUMNS, nbbo_copy_row(
        "CANF", QuoteTick(tick.ts_utc, 4.60, 4.64, None, 1, "d" * 64),
        source="iqfeed_lookup_bbo", basis=BASIS_IQFEED, batch_id="b" * 36,
        received_at=datetime(2026, 9, 2, 23, 0, tzinfo=UTC))))
    assert trade["observed_at"].tzinfo is None
    assert quote["observed_at"].tzinfo is not None
    # ...and they still describe the same instant.
    assert trade["observed_at"] == quote["observed_at"].replace(tzinfo=None)


def test_every_hydrated_source_fits_the_varchar_24_column():
    for src in HYDRATED_SOURCES:
        assert len(src) <= 24, src
    assert len(BASIS_IQFEED) <= 48


def test_resolve_dsn_refuses_to_point_at_the_live_or_test_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5433/chili")
    monkeypatch.delenv("HYDRATED_DATABASE_URL", raising=False)
    assert resolve_dsn("chili_hydrated").endswith("/chili_hydrated")
    with pytest.raises(RuntimeError, match="refusing to hydrate"):
        resolve_dsn("chili")
    with pytest.raises(RuntimeError, match="refusing to hydrate"):
        resolve_dsn("chili_test")


# ---------------------------------------------------------------------------
# 3. IQFeed parsing and truncation recovery
# ---------------------------------------------------------------------------
REAL_LINE = (
    "LH,2026-09-02 19:24:37.716547,324.6417,1,33767501,"
    "324.6400,324.7100,453036,O,19,8717,0,2,"
)


def test_parse_iqfeed_tick_reads_a_real_protocol_62_record():
    t = parse_iqfeed_tick(REAL_LINE)
    assert t is not None
    assert t.ts_utc == datetime(2026, 9, 2, 23, 24, 37, 716547, tzinfo=UTC)
    assert t.price == pytest.approx(324.6417)
    assert t.size == pytest.approx(1.0)
    assert t.bid == pytest.approx(324.64)
    assert t.ask == pytest.approx(324.71)
    assert t.day_volume == pytest.approx(33_767_501)
    assert t.sequence == 453036
    assert len(t.frame_sha256) == 64


def test_parse_iqfeed_tick_rejects_junk_and_non_positive_prices():
    assert parse_iqfeed_tick("") is None
    assert parse_iqfeed_tick("S,CURRENT PROTOCOL,6.2") is None
    assert parse_iqfeed_tick("LH,not-a-date,1,1,1,1,1,1") is None
    assert parse_iqfeed_tick(
        "LH,2026-09-02 19:24:37.716547,0,1,1,1,1,1,O,19,0,0,2,"
    ) is None


def test_parse_iqfeed_tick_treats_a_zero_quote_as_absent_not_as_zero():
    """A 0.00 bid is 'no quote', not 'the bid is zero dollars'. Persisting it as
    0.0 would make the row pass `bid > 0` checks nowhere and skew any spread."""
    t = parse_iqfeed_tick(
        "LH,2026-09-02 08:00:00.000001,4.50,100,500,0.0000,0.0000,1,O,19,0,0,2,"
    )
    assert t is not None
    assert t.bid is None and t.ask is None


class _FakeLookupClient:
    """Replays canned HTT responses and records the windows it was asked for."""

    def __init__(self, ticks: list[tuple[str, float]], cap: int) -> None:
        self.ticks = ticks          # (ET timestamp text, price), ascending
        self.cap = cap
        self.calls: list[tuple[str, str, int]] = []
        self.reconnects = 0

    def htt(self, symbol, begin, end, max_datapoints, *, direction=1, timeout_s=None):
        self.calls.append((begin, end, max_datapoints))
        lo, hi = begin.replace(" ", ""), end.replace(" ", "")

        def key(ts: str) -> str:
            return ts[:19].replace("-", "").replace(":", "").replace(" ", "")

        window = [t for t in self.ticks if lo <= key(t[0]) <= hi]
        # IQFeed keeps the NEWEST max_datapoints records -- measured, and the
        # whole reason the hydrator continues backward.
        kept = window[-max_datapoints:]
        lines = [
            f"LH,{ts},{px},100,1000,{px - 0.01:.4f},{px + 0.01:.4f},{i},O,19,0,0,2,"
            for i, (ts, px) in enumerate(kept)
        ]
        return RequestResult(
            request_id="r", command="HTT", lines=lines,
            no_data=not lines,
        )

    def reconnect(self):
        self.reconnects += 1


def test_truncated_windows_are_continued_backward_without_loss_or_duplication():
    """The regression this guards: IQFeed drops the OLDEST records when capped,
    so a single request over a busy window silently loses the FRONT of it --
    exactly the part a momentum study cares about."""
    ticks = [
        (f"2026-09-02 09:{m:02d}:{s:02d}.000000", 10.0 + s)
        for m in range(30, 40) for s in range(60)
    ]  # 600 ticks
    fake = _FakeLookupClient(ticks, cap=600)
    got = iter_iqfeed_window(
        fake, "TEST", et(2026, 9, 2, 9, 30), et(2026, 9, 2, 9, 39, 59),
        max_datapoints=250,
    )
    assert len(got) == 600, "every tick in the window must survive the cap"
    assert len(fake.calls) >= 3, "a 600-tick window at cap 250 needs continuation"
    tss = [t.ts_utc for t in got]
    assert tss == sorted(tss), "output must be ascending for COPY id ordering"
    assert len(set(tss)) == len(tss), "the continuation seam must not duplicate"


def test_an_uncapped_window_costs_exactly_one_request():
    ticks = [(f"2026-09-02 09:30:{s:02d}.000000", 10.0) for s in range(30)]
    fake = _FakeLookupClient(ticks, cap=600)
    got = iter_iqfeed_window(
        fake, "TEST", et(2026, 9, 2, 9, 30), et(2026, 9, 2, 9, 30, 59),
        max_datapoints=250,
    )
    assert len(got) == 30
    assert len(fake.calls) == 1


def test_an_empty_window_is_not_an_error():
    fake = _FakeLookupClient([], cap=600)
    got = iter_iqfeed_window(
        fake, "TEST", et(2026, 9, 2, 9, 30), et(2026, 9, 2, 9, 30, 59),
        max_datapoints=250,
    )
    assert got == []


# ---------------------------------------------------------------------------
# 4. COPY TEXT encoding
# ---------------------------------------------------------------------------
def test_copy_field_escapes_everything_that_would_break_the_text_format():
    assert copy_field(None) == "\\N"
    assert copy_field("A\tB") == "A\\tB"
    assert copy_field("A\nB") == "A\\nB"
    assert copy_field("A\r\nB") == "A\\r\\nB"
    assert copy_field("C:\\path") == "C:\\\\path"
    assert copy_field(True) == "t"


def test_copy_field_keeps_full_precision_on_prices_and_timestamps():
    # repr() round-trips a float exactly; str() historically did not, and a
    # truncated price is a silently wrong fill in replay.
    assert float(copy_field(324.6417)) == 324.6417
    assert copy_field(datetime(2026, 9, 2, 13, 30, 0, 123456)) == "2026-09-02 13:30:00.123456"
    assert copy_field(
        datetime(2026, 9, 2, 13, 30, 0, 123456, tzinfo=UTC)
    ) == "2026-09-02 13:30:00.123456+00:00"


def test_copy_line_is_tab_delimited_and_newline_terminated():
    assert copy_line(["A", 1, None]) == "A\t1\t\\N\n"


def test_copy_stream_reassembles_exactly_under_arbitrary_chunk_sizes():
    rows = [(f"S{i}", i, None) for i in range(500)]
    expected = "".join(copy_line(r) for r in rows)
    for size in (1, 7, 4096):
        stream = CopyStream(rows)
        chunks = []
        while True:
            c = stream.read(size)
            if not c:
                break
            chunks.append(c)
        assert "".join(chunks) == expected
        assert stream.rows_written == 500


def test_copy_stream_hashes_what_actually_crossed_the_wire():
    """payload_sha256 in hydration_batches is only worth storing if it covers
    the bytes that were sent, not the rows that were intended."""
    rows = [("A", 1), ("B", 2)]
    s1, s2 = CopyStream(rows), CopyStream(list(rows))
    while s1.read(8):
        pass
    while s2.read(1024):
        pass
    assert s1.content_sha256 == s2.content_sha256
    assert s1.rows_written == s2.rows_written == 2

    s3 = CopyStream([("A", 1), ("B", 3)])
    while s3.read(1024):
        pass
    assert s3.content_sha256 != s1.content_sha256


# ---------------------------------------------------------------------------
# 5. Polygon parsing and the as-of merge
# ---------------------------------------------------------------------------
def test_polygon_trade_ns_timestamp_truncates_to_postgres_microseconds():
    rec = {"sip_timestamp": 1_756_800_000_123_456_789, "price": 4.62,
           "size": 100, "sequence_number": 42, "id": "x"}
    t = parse_polygon_trade(rec)
    assert t is not None
    assert t.ts_utc.microsecond == 123456      # truncated, not rounded
    assert t.ts_utc.tzinfo is UTC
    assert t.sequence == 42
    # A trade row carries NO bid/ask; those come from the as-of merge.
    assert t.bid is None and t.ask is None


def test_polygon_trade_falls_back_to_the_participant_clock():
    t = parse_polygon_trade({"participant_timestamp": 1_756_800_000_000_000_000,
                             "price": 1.0, "size": 1})
    assert t is not None
    assert t.ts_utc == datetime(2025, 9, 2, 8, 0, tzinfo=UTC)


def test_polygon_parsers_reject_rows_replay_would_have_to_discard_anyway():
    assert parse_polygon_trade({"price": 1.0}) is None            # no clock
    assert parse_polygon_trade({"sip_timestamp": 1, "price": 0}) is None
    assert parse_polygon_quote({"sip_timestamp": 1, "bid_price": 0.0,
                                "ask_price": 1.0}) is None        # zero bid
    assert parse_polygon_quote({"sip_timestamp": 1, "bid_price": 2.0,
                                "ask_price": 1.0}) is None        # crossed
    assert parse_polygon_quote({"sip_timestamp": 1, "bid_price": 1.0}) is None


def test_two_identical_polygon_records_hash_identically_regardless_of_key_order():
    a = {"sip_timestamp": 1, "price": 2.0, "size": 3}
    b = {"size": 3, "price": 2.0, "sip_timestamp": 1}
    assert parse_polygon_trade(a).frame_sha256 == parse_polygon_trade(b).frame_sha256


def test_quote_index_returns_the_last_nbbo_at_or_before_an_instant():
    idx = QuoteIndex()
    for sec, bid, ask in ((10, 1.0, 1.1), (20, 2.0, 2.1), (30, 3.0, 3.1)):
        idx.add(QuoteTick(datetime(2026, 9, 2, 13, 30, sec, tzinfo=UTC),
                          bid, ask, None, None, "e" * 64))
    assert len(idx) == 3
    # before the first quote there is genuinely no NBBO -- do not invent one
    assert idx.as_of(datetime(2026, 9, 2, 13, 30, 5, tzinfo=UTC)) == (None, None)
    assert idx.as_of(datetime(2026, 9, 2, 13, 30, 20, tzinfo=UTC)) == (2.0, 2.1)
    assert idx.as_of(datetime(2026, 9, 2, 13, 30, 25, tzinfo=UTC)) == (2.0, 2.1)
    assert idx.as_of(datetime(2026, 9, 2, 13, 31, tzinfo=UTC)) == (3.0, 3.1)


def test_empty_quote_index_never_raises():
    assert QuoteIndex().as_of(datetime(2026, 9, 2, tzinfo=UTC)) == (None, None)


# ---------------------------------------------------------------------------
# 6. corpus input
# ---------------------------------------------------------------------------
def test_parse_symbol_day_round_trips_and_rejects_garbage():
    assert parse_symbol_day("canf:2026-09-02") == ("CANF", date(2026, 9, 2))
    with pytest.raises(Exception):
        parse_symbol_day("CANF")


def test_read_pairs_csv_accepts_alternate_headers_and_de_duplicates(tmp_path):
    p = tmp_path / "corpus.csv"
    p.write_text(
        "ticker,session_date,note\nCANF,2026-09-02,a\ncanf,2026-09-02,dup\n"
        "SSM,2026-09-01T00:00:00Z,b\n",
        encoding="utf-8",
    )
    assert read_pairs_csv(str(p)) == [
        ("CANF", date(2026, 9, 2)),
        ("SSM", date(2026, 9, 1)),
    ]


def test_read_pairs_csv_fails_loudly_when_there_is_no_date_column(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("symbol,price\nCANF,1\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        read_pairs_csv(str(p))


# ---------------------------------------------------------------------------
# 7. the Polygon HTTP client
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self.text = json.dumps(payload) if payload is not None else ""
        self.headers = headers or {}


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        return self.responses.pop(0)


def _client(session, **kw):
    from scripts.polygon_historical_client import PolygonHistoricalClient

    creds = {
        "massive": {"key": "K1", "base": "https://m.example", "key_env": "MASSIVE_API_KEY"},
        "polygon": {"key": "K2", "base": "https://p.example", "key_env": "POLYGON_API_KEY"},
    }
    return PolygonHistoricalClient(
        credentials=creds, session=session, rps=10_000.0, **kw
    )


def test_polygon_client_follows_next_url_and_reattaches_the_key():
    """A bare next_url 401s -- the cursor carries its query string but not the
    credential. Forgetting this truncates every multi-page symbol-day to page 1."""
    session = _FakeSession([
        _FakeResponse(200, {"results": [{"a": 1}], "next_url": "https://p.example/cursor?x=1"}),
        _FakeResponse(200, {"results": [{"a": 2}]}),
    ])
    client = _client(session)
    got = list(client.iter_records("trades", "CANF", 0, 10))
    assert got == [{"a": 1}, {"a": 2}]
    assert session.calls[1][0] == "https://p.example/cursor?x=1"
    assert session.calls[1][1]["apiKey"] in ("K1", "K2")


def test_polygon_client_retries_429_then_succeeds():
    session = _FakeSession([
        _FakeResponse(429, {"error": "slow down"}, {"Retry-After": "0"}),
        _FakeResponse(200, {"results": [{"a": 1}]}),
    ])
    from scripts.polygon_historical_client import FetchStats

    client = _client(session)
    st = FetchStats()
    got = list(client.iter_records("trades", "CANF", 0, 10, stats=st))
    assert got == [{"a": 1}]
    assert st.rate_limited == 1
    assert st.retries == 1
    assert st.requests == 2


def test_polygon_client_raises_on_a_non_retryable_status_without_leaking_the_key():
    from scripts.polygon_historical_client import PolygonHTTPError

    session = _FakeSession([_FakeResponse(400, {"error": "bad Limit"})])
    client = _client(session)
    with pytest.raises(PolygonHTTPError) as exc:
        list(client.iter_records("trades", "CANF", 0, 10))
    assert exc.value.status == 400
    assert "K1" not in str(exc.value) and "K2" not in str(exc.value)


def test_polygon_client_rejects_an_unknown_dataset():
    client = _client(_FakeSession([]))
    with pytest.raises(ValueError, match="trades' or 'quotes'"):
        list(client.iter_records("aggregates", "CANF", 0, 10))


def test_polygon_client_caps_the_page_size_at_the_provider_maximum():
    from scripts.polygon_historical_client import MAX_PAGE_LIMIT

    session = _FakeSession([_FakeResponse(200, {"results": []})])
    client = _client(session)
    list(client.iter_records("trades", "CANF", 0, 10, limit=10 ** 6))
    assert session.calls[0][1]["limit"] == MAX_PAGE_LIMIT


def test_polygon_client_refuses_to_start_without_a_credential():
    from scripts.polygon_historical_client import PolygonHistoricalClient

    creds = {
        "massive": {"key": "", "base": "https://m", "key_env": "MASSIVE_API_KEY"},
        "polygon": {"key": "", "base": "https://p", "key_env": "POLYGON_API_KEY"},
    }
    with pytest.raises(RuntimeError, match="MASSIVE_API_KEY"):
        PolygonHistoricalClient(credentials=creds, session=_FakeSession([]))


def test_polygon_client_uses_half_open_bounds_that_tile_without_overlap():
    session = _FakeSession([_FakeResponse(200, {"results": []})])
    client = _client(session)
    list(client.iter_records("quotes", "CANF", 100, 200))
    params = session.calls[0][1]
    assert params["timestamp.gte"] == 100
    assert params["timestamp.lt"] == 200      # lt, not lte
    assert params["order"] == "asc"


# ---------------------------------------------------------------------------
# operational interlocks
# ---------------------------------------------------------------------------
class _AdvisoryLockPool:
    """Models ``pg_try_advisory_lock`` semantics: session-scoped, non-blocking.

    A fake rather than a real database, because the contract being bound is how
    THIS code uses the primitive -- one key, try-not-block, released with the
    session -- and that contract has to hold while the live lane is running.
    """

    def __init__(self) -> None:
        self.held: dict[int, object] = {}


class _LockCursor:
    def __init__(self, pool, owner):
        self._pool, self._owner, self._row = pool, owner, None

    def execute(self, sql, params=None):
        key = int(params[0])
        if "pg_try_advisory_lock" in sql:
            holder = self._pool.held.get(key)
            if holder is None:
                self._pool.held[key] = self._owner
                self._row = (True,)
            else:
                self._row = (holder is self._owner,)
        elif "pg_advisory_unlock" in sql:
            if self._pool.held.get(key) is self._owner:
                del self._pool.held[key]
            self._row = (True,)
        return self

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _LockConn:
    def __init__(self, pool):
        self._pool = pool
        self.closed = False

    def cursor(self):
        return _LockCursor(self._pool, self)

    def commit(self):
        pass

    def close(self):
        # A session ending releases every advisory lock it held. Modelling that
        # is the point: it is why a crashed hydrator cannot wedge the next one.
        for key, holder in list(self._pool.held.items()):
            if holder is self:
                del self._pool.held[key]
        self.closed = True


def test_a_second_hydrator_is_refused_while_the_first_holds_the_lock():
    """The failure this prevents was named by Phase 1 and left unguarded.

    IQFeed limits SIMULTANEOUS lookup connections and the behaviour past that
    limit is untested against the shared IQConnect process the live L1 bridge
    depends on. Two agent sessions running the hydrator -- this project's
    demonstrated operating mode -- would each open their own :9100 socket.
    """
    from scripts.historical_tick_hydrator import (
        HydratorAlreadyRunning,
        acquire_singleton_lock,
        acquire_singleton_lock_or_raise,
    )

    pool = _AdvisoryLockPool()
    first, second = _LockConn(pool), _LockConn(pool)

    acquire_singleton_lock_or_raise(first)          # first run wins
    assert acquire_singleton_lock(second) is False  # second sees it held
    with pytest.raises(HydratorAlreadyRunning, match="another hydrator is running"):
        acquire_singleton_lock_or_raise(second)


def test_the_lock_dies_with_the_session_so_a_crash_cannot_wedge_the_next_run():
    from scripts.historical_tick_hydrator import (
        acquire_singleton_lock_or_raise,
    )

    pool = _AdvisoryLockPool()
    crashed = _LockConn(pool)
    acquire_singleton_lock_or_raise(crashed)
    crashed.close()                                  # process died mid-run

    survivor = _LockConn(pool)
    acquire_singleton_lock_or_raise(survivor)        # must not raise


def test_singleton_lock_releases_on_the_way_out_even_on_error():
    from scripts.historical_tick_hydrator import (
        HYDRATOR_ADVISORY_LOCK_KEY,
        singleton_lock,
    )

    pool = _AdvisoryLockPool()
    conn = _LockConn(pool)
    with pytest.raises(ValueError):
        with singleton_lock(conn):
            raise ValueError("symbol-day blew up")
    assert HYDRATOR_ADVISORY_LOCK_KEY not in pool.held


def test_the_advisory_lock_key_is_a_valid_postgres_bigint():
    """A key outside int8 would make every acquisition raise, which reads as a
    broken tool rather than as a disabled interlock."""
    from scripts.historical_tick_hydrator import HYDRATOR_ADVISORY_LOCK_KEY

    assert -(2 ** 63) <= HYDRATOR_ADVISORY_LOCK_KEY < 2 ** 63


# --- the market-hours gate ---------------------------------------------------
def _utc(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def test_market_hours_gate_refuses_inside_the_session():
    """chili_hydrated is a separate DATABASE, not a separate CLUSTER.

    One postmaster, one WAL, one 4 GB shared_buffers, one bind mount, shared
    with the live 222 GB chili. Phase 4's clean run depended on 98% of its rows
    landing after the 20:00 ET close -- scheduling luck, not a property of the
    tool. This is the line that makes it a property of the tool.
    """
    from scripts.historical_tick_hydrator import (
        MarketHoursRefusal,
        assert_outside_market_hours,
    )

    # 13:35 UTC = 09:35 EDT, five minutes into the regular session.
    with pytest.raises(MarketHoursRefusal, match="04:00-20:00 ET"):
        assert_outside_market_hours(now=_utc(2026, 8, 26, 13, 35))
    # 08:00 UTC = 04:00 EDT, the first second of premarket -- inclusive.
    with pytest.raises(MarketHoursRefusal):
        assert_outside_market_hours(now=_utc(2026, 8, 26, 8, 0))


def test_market_hours_gate_allows_after_the_close():
    from scripts.historical_tick_hydrator import assert_outside_market_hours

    # 00:00 UTC = 20:00 EDT exactly -- the close, and the window is half-open.
    assert_outside_market_hours(now=_utc(2026, 8, 27, 0, 0))
    # 02:30 UTC = 22:30 EDT, when Phase 4 actually ran.
    assert_outside_market_hours(now=_utc(2026, 9, 3, 2, 30))


def test_market_hours_gate_uses_zoneinfo_not_a_fixed_offset():
    """The corpus straddles the DST boundary. A hardcoded -4 would put the gate
    an hour off for every EST date, and the refusal (or the pass) would look
    entirely reasonable."""
    from scripts.historical_tick_hydrator import (
        MarketHoursRefusal,
        assert_outside_market_hours,
    )

    # 2026-03-06 is EST (UTC-5). 09:30 UTC = 04:30 EST -> inside the session.
    with pytest.raises(MarketHoursRefusal):
        assert_outside_market_hours(now=_utc(2026, 3, 6, 9, 30))
    # ...and 08:30 UTC = 03:30 EST -> outside it. Under a fixed -4 the same two
    # instants would be read as 05:30 and 04:30 and BOTH would refuse.
    assert_outside_market_hours(now=_utc(2026, 3, 6, 8, 30))


def test_market_hours_gate_can_be_overridden_deliberately():
    from scripts.historical_tick_hydrator import assert_outside_market_hours

    assert_outside_market_hours(allow=True, now=_utc(2026, 8, 26, 13, 35))


def test_market_hours_gate_accepts_a_naive_now_as_utc():
    """A naive datetime must not be read as server-local time: on this box that
    is Pacific, which would shift the whole gate by three hours."""
    from scripts.historical_tick_hydrator import (
        MarketHoursRefusal,
        assert_outside_market_hours,
    )

    with pytest.raises(MarketHoursRefusal):
        assert_outside_market_hours(now=datetime(2026, 8, 26, 13, 35))


# ── a malformed symbol is rejected before any DB work (2026-09-05) ─────────────────

from scripts.historical_tick_hydrator import symbol_is_hydratable as _sym_ok


@pytest.mark.parametrize("sym", ["SDOT", "brk.b", "  ppcb ", "AB-C", "A"])
def test_tickers_are_hydratable(sym):
    assert _sym_ok(sym) is True


@pytest.mark.parametrize("sym", [
    "NUWE (09:30 pivot 5.34)",                       # the row that aborted the 2026-09-04 pass
    "EDBL/LGHL/BIYA", "MGRX / REPL / KUST", "UNKNOWN", "", None,
    "FCUV (4.80/break of 5; break of 8; post-11.50 halt re-entry)",
])
def test_narrative_in_the_symbol_field_is_not_hydratable(sym):
    assert _sym_ok(sym) is False


def test_the_hydratable_pattern_fits_the_jobs_column():
    """hydration_jobs.symbol is varchar(32); the pattern caps a symbol at 16 characters."""
    assert _sym_ok("A" * 16) is True and _sym_ok("A" * 17) is False

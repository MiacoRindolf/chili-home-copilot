"""A LOCKED BOOK (bid == ask) SHORT-CIRCUITS THE FEED THAT HAS THE ANSWER.

THE DEFECT, cited. ``AlpacaSpotAdapter._massive_sip_quote`` validates a
stand-in row with (alpaca_spot.py:1058-1067)::

    or bid <= 0 or ask <= 0 or mid <= 0 or ask < bid

``ask < bid`` (CROSSED) is rejected; ``ask == bid`` (LOCKED) is ACCEPTED. And
``get_execution_bbo`` returns the FIRST tier that yields a quote, so a locked
massive_ws row is handed back as execution authority while the three IQFeed
tiers 10-40 lines BELOW IT IN THE SAME FUNCTION are never consulted. Every
spread gate downstream is monotone in the spread, so 0.0 bps -- the minimum of
the domain -- passes all of them by being the most degenerate value possible.

MEASURED on ``trading_automation_events.live_entry_final_bbo``, 2026-08-17
18:17:28Z to 2026-09-02 11:19:08Z, 1,144 ok entry-submit reads:

    93 (8.13%) saw a LOCKED primary, across 34 sessions and 17 symbols
    91 of 93 had authority stand_in_massive_sip

For each, the nearest IQFeed quote to the primary's OWN provider clock was
searched in both IQFeed stores:

    ARTIFACT (other feed two-sided)  57 / 93 = 61.3%
    GENUINE  (other feed also locked) 32 / 93 = 34.4%
    NO_FEED  (no other feed)           4 / 93 =  4.3%

84 of 89 matches inside 10 ms, 88 of 89 inside 100 ms -- simultaneous, not a
smear. An independent tape-level pass over the 17 traded-entry windows agrees:
602 locked of 3,702 SIP-eligible rows, 401 ARTIFACT (66.7%) / 200 GENUINE.

THE SHARPEST ROW. AUUD session 19337: ``live_entry_spread_risk_veto`` fired 19
times from 11:06:47.973089 to 11:10:17.858038 at gate_spread 84.39-92.17 bps
against a 31.0 bps budget, with TWO independent sources in the payloads
agreeing. Then at 11:10:42.242871 the primary read 1.14/1.14, scored 0.0, and
the entry submitted at 11:10:53. IQFeed at that same instant, dt = +0.0007 s:
1.13/1.14 = 88.11 bps. The lock was the only reason AUUD ever entered.

WHAT THIS CHANGE DOES -- VERDICT ONLY, NO PRICING. It mirrors the junk-wide
pattern already in the same function (alpaca_spot.py:1251-1283): stash the
locked answer, ask the NEXT TIER THAT ANSWERS, and return the LOCKED tick
either way, carrying a verdict in ``raw``. The bid, ask, mid, spread and
``quote_authority`` are never altered, on any path. An earlier draft returned
the second feed's book on the ARTIFACT branch -- 61.3% of locked reads -- which
would have moved ``slip_ref`` and ``spread_bps_live`` at the entry-place seam.
Nothing in the four evidence sessions measures FILLS at a substituted price;
every counterfactual scored refusals. Substitution is phase 2 and needs its own
measurement.

TWO GUARDS THAT ARE NOT DECORATION, both added after review found the first
draft wrong about them:

  * ``resolve_locked`` is a SEPARATE opt-in and is NOT keyed off
    ``allow_stand_in``. That flag stopped being an entry marker at #1224/#1254:
    live_runner passes it True from four PROTECTIVE sites (:13252, :13364,
    :14153, :27583) with 900s ceilings. Exactly one caller passes
    ``resolve_locked=True`` -- the ``live_entry_final_bbo`` seam.
  * an ABSOLUTE age ceiling (default 5.0s, wall clock) sits beside the RELATIVE
    simultaneity bound, because two rows that are each fifteen minutes old can
    agree with each other perfectly.

HOW THESE TESTS BEHAVE ON origin/main, stated precisely rather than in bulk.
Verified by swapping in the pristine ``alpaca_spot.py`` / ``config.py``:
several fail there on a KeyError for a marker key or on an absent Settings
field -- main's BEHAVIOUR in those scenarios is the same as the branch's, the
tests fail because the new signal does not exist. Only the cases marked
BEHAVIOURAL below fail on main because main returns something materially
different. The cases marked INVARIANT pass on BOTH, and they are the guarantee
that nothing outside a locked entry read moves.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.services.trading.venue.alpaca_spot import AlpacaSpotAdapter, _fresh
from app.services.trading.venue.protocol import FreshnessMeta, NormalizedTicker

# The absolute ceiling is measured against the WALL CLOCK, so the fixture clock
# has to be near it. The real AUUD instant was 2026-09-01 11:10:40.883Z; the
# geometry under test is the OFFSETS between the two feeds, not the date.
_T0 = datetime.now(timezone.utc) - timedelta(milliseconds=200)


def _tick(feed: str, bid: float, ask: float, *, at: datetime = None):
    at = _T0 if at is None else at
    mid = (bid + ask) / 2.0
    meta = FreshnessMeta(
        retrieved_at_utc=at + timedelta(milliseconds=5),
        provider_time_utc=at,
        max_age_seconds=60.0,
    )
    return (
        NormalizedTicker(
            product_id="AUUD",
            bid=bid,
            ask=ask,
            mid=mid,
            spread_bps=(ask - bid) / mid * 10_000.0 if mid > 0 else 0.0,
            freshness=meta,
            raw={"feed": feed},
        ),
        meta,
    )


class _Adapter(AlpacaSpotAdapter):
    """Drive get_execution_bbo's tier chain with scripted tiers and no I/O.

    Only the four tier helpers and the direct quote are replaced. The chain
    itself -- ordering, the junk-wide branch, and the new locked handling --
    is the real code under test.
    """

    def __init__(self, *, direct=None, sip=None, l1=None, embedded=None, depth=None):
        self._direct = direct
        self._tiers = {
            "massive_sip": sip,
            "iqfeed_l1_own_clock": l1,
            "iqfeed_trade_embedded": embedded,
            "iqfeed_depth": depth,
        }
        self.calls: list[str] = []

    # the direct request
    def _alpaca_latest_quote(self, product_id):
        self.calls.append("alpaca_direct")
        return self._direct if self._direct is not None else (None, _fresh())

    def _execution_bbo_from_direct(self, tick, meta, max_age_seconds):
        return (tick, meta) if tick is not None else None

    def _mk(self, name):
        def _fn(product_id, max_age_seconds):
            self.calls.append(name)
            return self._tiers[name]
        return _fn

    def __getattribute__(self, item):
        if item in (
            "_massive_sip_execution_bbo",
            "_iqfeed_l1_own_clock_execution_bbo",
            "_iqfeed_trade_embedded_execution_bbo",
            "_iqfeed_depth_execution_bbo",
        ):
            name = {
                "_massive_sip_execution_bbo": "massive_sip",
                "_iqfeed_l1_own_clock_execution_bbo": "iqfeed_l1_own_clock",
                "_iqfeed_trade_embedded_execution_bbo": "iqfeed_trade_embedded",
                "_iqfeed_depth_execution_bbo": "iqfeed_depth",
            }[item]
            return object.__getattribute__(self, "_mk")(name)
        return object.__getattribute__(self, item)


def _get(adapter, **kw):
    """The ENTRY seam's kwargs: allow_stand_in AND the explicit resolve opt-in."""
    kw.setdefault("allow_stand_in", True)
    kw.setdefault("resolve_locked", True)
    kw.setdefault("max_age_seconds", 60.0)
    return adapter.get_execution_bbo("AUUD", **kw)


def _exit_get(adapter, **kw):
    """The EXIT seam's kwargs, copied from live_runner.py:14149-14154.

    ``allow_stand_in=True`` with a 900s stand-in ceiling and NO
    ``resolve_locked``. This is the call shape the first draft of this change
    believed could not exist.
    """
    kw.setdefault("allow_stand_in", True)
    kw.setdefault("max_age_seconds", 900.0)
    kw.setdefault("stand_in_max_age_seconds", 900.0)
    return adapter.get_execution_bbo("AUUD", **kw)


def _flags(*, enabled=True, bound=2.0, hard=5.0):
    keys = {
        "chili_alpaca_execution_bbo_locked_resolve_enabled": enabled,
        "chili_alpaca_execution_bbo_locked_resolve_max_age_seconds": bound,
        "chili_alpaca_execution_bbo_locked_resolve_hard_max_age_seconds": hard,
    }
    old = {k: getattr(settings, k, None) for k in keys}
    for k, v in keys.items():
        object.__setattr__(settings, k, v)
    return old


def _restore(old):
    for k, v in old.items():
        object.__setattr__(settings, k, v)


# ── ARTIFACT: the other feed has a real book, and it is REPORTED, not used ────

def test_artifact_lock_is_labelled_and_carries_the_real_spread():
    """BEHAVIOURAL. THE AUUD ROW, as an executable assertion.

    The SIP stand-in is locked at 1.14/1.14 and IQFeed at the same instant
    (dt = +0.0007 s) has 1.13/1.14 = 88.11 bps. origin/main returns the locked
    quote at 0.0 bps with NOTHING recorded and the entry submits. Here the same
    quote comes back -- identical bid, ask, mid, spread, authority -- but it now
    says the book was an ARTIFACT and what the real spread was, which is the
    number #1299's gate needs in order to refuse on evidence rather than on the
    session clock.
    """
    a = _Adapter(
        sip=_tick("massive_ws_universe", 1.14, 1.14),
        l1=_tick("iqfeed_l1", 1.13, 1.14, at=_T0 + timedelta(microseconds=700)),
    )
    old = _flags()
    try:
        tick, _meta = _get(a)
    finally:
        _restore(old)
    # PRICING IS UNTOUCHED. This is the whole contract of phase 1.
    assert (tick.bid, tick.ask) == (pytest.approx(1.14), pytest.approx(1.14))
    assert tick.spread_bps == pytest.approx(0.0)
    assert tick.raw["feed"] == "massive_ws_universe"
    # The verdict, and the real number behind it.
    assert tick.raw["locked_book"] is True
    assert tick.raw["locked_book_resolution"] == "artifact_resolved"
    assert tick.raw["locked_book_real_spread_bps"] == pytest.approx(88.11, abs=0.5)
    assert tick.raw["locked_book_second_feeds"] == 1
    assert tick.raw["locked_book_second_feeds_locked"] == 0
    # The tier BELOW the locked one was actually consulted -- on main the chain
    # returns at massive_sip and never reaches it -- and the walk STOPS there.
    assert a.calls == ["alpaca_direct", "massive_sip", "iqfeed_l1_own_clock"]


def test_resolution_falls_through_a_silent_l1_to_the_trade_embedded_tier():
    """BEHAVIOURAL. CANF 2026-09-02 11:00-11:20 has ZERO iqfeed_l1 rows in the
    tape (727 massive_ws_universe rows, 0 IQFeed) while iqfeed_trade_ticks
    supplied 83,288 rows for the SAME window. A tier that returns nothing is not
    an answer, so the walk continues past it; stopping at L1 would declare "no
    second feed" for a name that has one."""
    a = _Adapter(
        sip=_tick("massive_ws_universe", 1.41, 1.41),
        l1=None,
        embedded=_tick("iqfeed_trade_embedded", 1.40, 1.41,
                       at=_T0 + timedelta(microseconds=800)),
    )
    old = _flags()
    try:
        tick, _meta = _get(a)
    finally:
        _restore(old)
    assert (tick.bid, tick.ask) == (pytest.approx(1.41), pytest.approx(1.41))
    assert tick.raw["locked_book_resolution"] == "artifact_resolved"
    assert "iqfeed_trade_embedded" in a.calls
    # ... and it stops at the tier that answered, without touching depth.
    assert "iqfeed_depth" not in a.calls


def test_the_walk_stops_at_the_first_tier_that_answers():
    """THE COST BOUND, as an assertion.

    The first draft continued past every locked or out-of-bound answer, so on
    36 of 93 measured locked reads (38.7%) all three remaining tiers ran and
    none resolved -- three extra DB round-trips, one of them against the 89 GB
    ``iqfeed_trade_ticks``. A tier that ANSWERS ends the walk, whatever it says.
    """
    a = _Adapter(
        sip=_tick("massive_ws_universe", 1.42, 1.42),
        l1=_tick("iqfeed_l1", 1.42, 1.42, at=_T0 + timedelta(microseconds=800)),
        embedded=_tick("iqfeed_trade_embedded", 1.41, 1.42,
                       at=_T0 + timedelta(microseconds=900)),
    )
    old = _flags()
    try:
        tick, _meta = _get(a)
    finally:
        _restore(old)
    assert tick.raw["locked_book_resolution"] == "genuine"
    assert a.calls == ["alpaca_direct", "massive_sip", "iqfeed_l1_own_clock"]


# ── GENUINE vs NO_FEED: the verdict #1299 needs and does not have ────────────

def test_genuine_lock_is_returned_unchanged_and_labelled_genuine():
    """Both feeds agree the book is locked. The quote is returned EXACTLY as
    before -- this path must never be stricter than origin/main -- but it now
    carries the verdict, so a downstream guard can price it as a real (if
    unknown) market rather than guessing."""
    a = _Adapter(
        sip=_tick("massive_ws_universe", 1.42, 1.42),
        l1=_tick("iqfeed_l1", 1.42, 1.42, at=_T0 + timedelta(microseconds=800)),
    )
    old = _flags()
    try:
        tick, _meta = _get(a)
    finally:
        _restore(old)
    assert (tick.bid, tick.ask) == (pytest.approx(1.42), pytest.approx(1.42))
    assert tick.raw["locked_book"] is True
    assert tick.raw["locked_book_resolution"] == "genuine"
    assert tick.raw["locked_book_second_feeds"] == 1
    assert tick.raw["locked_book_second_feeds_locked"] == 1
    assert tick.raw["locked_book_real_spread_bps"] is None


def test_a_stale_second_feed_is_no_second_feed_at_all():
    """THE AMENDMENT THAT MATTERS, and the reason the session clock is the
    wrong discriminator.

    All four NO_FEED cases sit inside a hole where every feed is silent for
    15-112 seconds: SDOT's nearest IQFeed quote is 15.4 s away, AIXI's 45.3 s,
    DAIC's 68.0 s, IPST's 111.8 s. Those are LAST-KNOWN rows, not book states.
    A quote that far from the locked primary's own provider clock is not
    evidence about the book at that instant, so the verdict must be
    ``no_second_feed`` -- NOT ``genuine``, and certainly not a resolution.

    SDOT is the measured cost of getting this wrong: the one-tick substitute
    prices it at 5.78 bps against a real book of 184.03 bps, 31.8x under.
    """
    a = _Adapter(
        sip=_tick("massive_ws_universe", 17.29, 17.29),
        # 15.4 s later -- SDOT's real nearest IQFeed quote.
        l1=_tick("iqfeed_l1", 17.01, 17.18, at=_T0 + timedelta(seconds=15.4)),
    )
    old = _flags()
    try:
        tick, _meta = _get(a)
    finally:
        _restore(old)
    assert (tick.bid, tick.ask) == (pytest.approx(17.29), pytest.approx(17.29))
    assert tick.raw["locked_book_resolution"] == "no_second_feed"
    assert tick.raw["locked_book_second_feeds"] == 0


def test_no_other_feed_answers_at_all():
    a = _Adapter(sip=_tick("massive_ws_universe", 10.71, 10.71))
    old = _flags()
    try:
        tick, _meta = _get(a)
    finally:
        _restore(old)
    assert tick.raw["locked_book_resolution"] == "no_second_feed"
    # Every remaining tier was tried, because NONE of them answered.
    assert a.calls == [
        "alpaca_direct", "massive_sip", "iqfeed_l1_own_clock",
        "iqfeed_trade_embedded", "iqfeed_depth",
    ]


# ── invariants: never stricter, never wider than the measured population ─────

def test_a_two_sided_primary_returns_immediately_and_probes_nothing():
    """INVARIANT (passes on main too). The 91.87% of reads that are NOT locked
    must be byte-identical: the first answering tier wins and no extra query
    runs. This is the guarantee that the hot path is untouched."""
    a = _Adapter(sip=_tick("massive_ws_universe", 1.13, 1.14))
    old = _flags()
    try:
        tick, _meta = _get(a)
    finally:
        _restore(old)
    assert tick.bid == pytest.approx(1.13)
    assert a.calls == ["alpaca_direct", "massive_sip"]
    assert "locked_book_resolution" not in (tick.raw or {})


def test_flag_off_returns_the_locked_quote_at_the_first_tier():
    """THE KILL SWITCH, and the SHIPPED DEFAULT. Off, the locked SIP row is
    returned immediately and the IQFeed tiers are never consulted -- exactly
    origin/main. The Settings default is False for the launch window: this
    lands dark and is enabled after a soak, the shadow-first shape #1290
    shipped under."""
    from app.config import Settings

    assert Settings.model_fields[
        "chili_alpaca_execution_bbo_locked_resolve_enabled"].default is False

    a = _Adapter(
        sip=_tick("massive_ws_universe", 1.14, 1.14),
        l1=_tick("iqfeed_l1", 1.13, 1.14, at=_T0 + timedelta(microseconds=700)),
    )
    old = _flags(enabled=False)
    try:
        tick, _meta = _get(a)
    finally:
        _restore(old)
    assert (tick.bid, tick.ask) == (pytest.approx(1.14), pytest.approx(1.14))
    assert a.calls == ["alpaca_direct", "massive_sip"]


# ── THE EXIT PATH. The first draft asserted the opposite of the truth here ────

def test_the_exit_seams_real_kwargs_do_not_reach_resolution():
    """THE TEST THAT WOULD HAVE CAUGHT IT.

    An earlier draft gated resolution on ``allow_stand_in`` and asserted in
    three places -- the commit message, the Settings description, and a test
    named ``..._because_it_never_opts_into_stand_ins`` -- that the exit path
    therefore could not reach it. That was FALSE. ``allow_stand_in`` stopped
    being an entry marker at #1224/#1254 and live_runner passes it True from
    four PROTECTIVE sites, each with a 900s ceiling::

        live_runner.py:13252  _submit_live_market_exit_impl  (ext-hrs/stop-class)
        live_runner.py:13364  _submit_live_market_exit_impl  (emergency flatten)
        live_runner.py:14153  _final_literal_exit_bbo_refresh
        live_runner.py:27583  captured-paper literal exit

    The old test only ever exercised ``allow_stand_in=False``, which no exit
    site passes, so it proved nothing about the path it was named for. This one
    uses the exact kwargs of live_runner.py:14149-14154.

    WHY IT MATTERS, mechanically: the consumer at live_runner.py:14171-14186
    refuses the literal exit post when ``frozen_limit > fresh_bid``, so ANY
    movement of the returned bid changes whether a protective exit is judged
    marketable -- in both directions. A resolved bid one tick BELOW the lock
    (the AUUD shape, 1.14 -> 1.13) newly refuses an exit that main allowed; a
    resolved bid ABOVE the lock (the IPST shape, where the real bid range
    11.33-12.62 sits entirely above a 10.71 locked print) lets a non-marketable
    limit through. Neither can happen now: resolution does not run here, and it
    would not move the price if it did.
    """
    a = _Adapter(
        # A locked DIRECT quote -- the shape that reaches the exit refresh.
        direct=_tick("iex", 1.14, 1.14),
        l1=_tick("iqfeed_l1", 1.13, 1.14, at=_T0 + timedelta(microseconds=700)),
    )
    old = _flags()
    try:
        tick, _meta = _exit_get(a)
    finally:
        _restore(old)
    assert (tick.bid, tick.ask) == (pytest.approx(1.14), pytest.approx(1.14))
    assert a.calls == ["alpaca_direct"]
    assert "locked_book_resolution" not in (tick.raw or {})
    assert "locked_book" not in (tick.raw or {})


def test_the_exit_seam_is_unmoved_when_the_second_feed_bid_is_HIGHER():
    """The unsafe DIRECTION, named explicitly.

    IPST's measured row: the locked primary printed 10.71/10.71 while the whole
    real IQFeed bid range in the window was 11.33-12.62 -- ABOVE the lock. On an
    exit, a raised bid is the failure ``get_execution_bbo``'s own docstring
    exists to prevent: it "would judge an exit marketable ... above what the
    venue can actually reach". The exit's read must be untouched even in that
    configuration.
    """
    a = _Adapter(
        direct=_tick("iex", 10.71, 10.71),
        l1=_tick("iqfeed_l1", 11.33, 11.40, at=_T0 + timedelta(microseconds=700)),
    )
    old = _flags()
    try:
        tick, _meta = _exit_get(a)
    finally:
        _restore(old)
    assert tick.bid == pytest.approx(10.71)
    assert a.calls == ["alpaca_direct"]


def test_even_the_entry_seam_never_moves_the_price():
    """PHASE 1 IS VERDICT-ONLY, in the raised-bid direction too.

    The same IPST shape on the ENTRY seam, where resolution DOES run: the
    verdict is recorded, the real spread is recorded, and the returned book is
    still the locked one. Nothing in the 21-day corpus measures fills at a
    substituted price, so no price is substituted.
    """
    a = _Adapter(
        sip=_tick("massive_ws_universe", 10.71, 10.71),
        l1=_tick("iqfeed_l1", 11.33, 11.40, at=_T0 + timedelta(microseconds=700)),
    )
    old = _flags()
    try:
        tick, _meta = _get(a)
    finally:
        _restore(old)
    assert (tick.bid, tick.ask) == (pytest.approx(10.71), pytest.approx(10.71))
    assert tick.raw["locked_book_resolution"] == "artifact_resolved"
    assert tick.raw["locked_book_real_spread_bps"] == pytest.approx(61.6, abs=1.0)


def test_resolve_locked_defaults_off_so_every_other_caller_is_unchanged():
    """INVARIANT. The opt-in is explicit. A caller that passes only
    ``allow_stand_in`` -- which is 10 of the 11 sites in live_runner -- gets
    origin/main behaviour whatever the flag says."""
    a = _Adapter(
        sip=_tick("massive_ws_universe", 1.14, 1.14),
        l1=_tick("iqfeed_l1", 1.13, 1.14, at=_T0 + timedelta(microseconds=700)),
    )
    old = _flags()
    try:
        tick, _meta = a.get_execution_bbo(
            "AUUD", max_age_seconds=60.0, allow_stand_in=True)
    finally:
        _restore(old)
    assert (tick.bid, tick.ask) == (pytest.approx(1.14), pytest.approx(1.14))
    assert a.calls == ["alpaca_direct", "massive_sip"]


# ── the bounds ───────────────────────────────────────────────────────────────

def test_a_crossed_book_is_still_not_treated_as_locked():
    """INVARIANT. CROSSED (ask < bid) is a different defect with its own
    handling and is already rejected by every validity test in the adapter. The
    locked path must not soften it or claim it."""
    a = _Adapter(
        sip=_tick("massive_ws_universe", 1.15, 1.14),
        l1=_tick("iqfeed_l1", 1.13, 1.14, at=_T0 + timedelta(microseconds=700)),
    )
    old = _flags()
    try:
        tick, _meta = _get(a)
    finally:
        _restore(old)
    # Not locked -> returned at the first tier, unchanged, unlabelled.
    assert (tick.bid, tick.ask) == (pytest.approx(1.15), pytest.approx(1.14))
    assert a.calls == ["alpaca_direct", "massive_sip"]
    assert "locked_book_resolution" not in (tick.raw or {})


def test_a_second_feed_without_a_provider_clock_cannot_resolve():
    """A match I cannot TIME is not a match. Rather than resolve on a quote
    whose simultaneity is unknown, the case stays unresolved -- the same
    fail direction the dual-clock contracts in this adapter already use."""
    _t, _m = _tick("iqfeed_l1", 1.13, 1.14)
    clockless = (
        _t,
        FreshnessMeta(retrieved_at_utc=_m.retrieved_at_utc,
                      provider_time_utc=None, max_age_seconds=60.0),
    )
    a = _Adapter(sip=_tick("massive_ws_universe", 1.14, 1.14), l1=clockless)
    old = _flags()
    try:
        tick, _meta = _get(a)
    finally:
        _restore(old)
    assert (tick.bid, tick.ask) == (pytest.approx(1.14), pytest.approx(1.14))
    assert tick.raw["locked_book_resolution"] == "no_second_feed"


def test_the_bound_is_measured_against_the_primarys_provider_clock():
    """The relative bound is a SIMULTANEITY test: it is measured between the two
    provider event clocks. 88 of 89 real matches sit inside 100 ms; the default
    bound of 2.0 s is deliberately generous against that and still excludes
    every 15-112 s feed hole."""
    from app.config import Settings

    f = Settings.model_fields[
        "chili_alpaca_execution_bbo_locked_resolve_max_age_seconds"]
    assert f.default == pytest.approx(2.0)
    # Inside the bound resolves; outside it does not.
    for dt_s, expect in ((1.9, "artifact_resolved"), (2.1, "no_second_feed")):
        a = _Adapter(
            sip=_tick("massive_ws_universe", 1.14, 1.14),
            l1=_tick("iqfeed_l1", 1.13, 1.14, at=_T0 + timedelta(seconds=dt_s)),
        )
        old = _flags()
        try:
            tick, _meta = _get(a)
        finally:
            _restore(old)
        assert tick.raw["locked_book_resolution"] == expect, dt_s


def test_two_co_timed_but_ancient_rows_are_not_a_resolved_book():
    """THE ABSOLUTE CEILING, which the relative bound cannot supply.

    ``_within_lock_bound`` compares the two provider clocks to EACH OTHER. On
    its own that would call two rows fifteen minutes old an "artifact resolved"
    book as long as they agree with each other -- and a last-known row
    surviving its own freshness check is the exact failure being fixed. The
    measured evidence (84 of 89 inside 10 ms) supports a few seconds and
    supports nothing at 900 s.

    Here both feeds are 60 s old and 0.7 ms apart. The relative bound passes;
    the absolute ceiling refuses, and no verdict is claimed at all -- the quote
    comes back exactly as origin/main returns it.
    """
    _old_t0 = _T0 - timedelta(seconds=60)
    a = _Adapter(
        sip=_tick("massive_ws_universe", 1.14, 1.14, at=_old_t0),
        l1=_tick("iqfeed_l1", 1.13, 1.14,
                 at=_old_t0 + timedelta(microseconds=700)),
    )
    old = _flags()
    try:
        tick, _meta = _get(a)
    finally:
        _restore(old)
    assert (tick.bid, tick.ask) == (pytest.approx(1.14), pytest.approx(1.14))
    assert "locked_book_resolution" not in (tick.raw or {})
    # No second feed was even queried: the primary failed the ceiling first.
    assert a.calls == ["alpaca_direct", "massive_sip"]


def test_the_hard_ceiling_knob_exists_and_is_seconds_not_minutes():
    from app.config import Settings

    f = Settings.model_fields[
        "chili_alpaca_execution_bbo_locked_resolve_hard_max_age_seconds"]
    assert f.default == pytest.approx(5.0)

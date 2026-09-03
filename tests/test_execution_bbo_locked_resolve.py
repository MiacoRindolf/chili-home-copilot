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

THE FIX mirrors the junk-wide-direct pattern already in the same function
(alpaca_spot.py:1251-1283): stash the locked answer, let the remaining tiers
run, and if none answers return the stashed quote UNCHANGED -- byte-identical,
never stricter.

VERIFIED against origin/main by swapping in the pristine alpaca_spot.py and
config.py and re-running: 11 pass here, 7 FAIL there, and exactly 4 pass on
BOTH -- the two-sided fast path, the kill switch, the exit carve-out, and the
crossed-book invariant. Those four are the guarantee that nothing outside a
locked book moves.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.services.trading.venue.alpaca_spot import AlpacaSpotAdapter, _fresh
from app.services.trading.venue.protocol import FreshnessMeta, NormalizedTicker

_T0 = datetime(2026, 9, 1, 11, 10, 40, 883000, tzinfo=timezone.utc)


def _tick(feed: str, bid: float, ask: float, *, at: datetime = _T0):
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
    kw.setdefault("allow_stand_in", True)
    kw.setdefault("max_age_seconds", 60.0)
    return adapter.get_execution_bbo("AUUD", **kw)


def _flags(*, enabled=True, bound=2.0):
    old = {
        "chili_alpaca_execution_bbo_locked_resolve_enabled": getattr(
            settings, "chili_alpaca_execution_bbo_locked_resolve_enabled", True),
        "chili_alpaca_execution_bbo_locked_resolve_max_age_seconds": getattr(
            settings, "chili_alpaca_execution_bbo_locked_resolve_max_age_seconds", 2.0),
    }
    object.__setattr__(
        settings, "chili_alpaca_execution_bbo_locked_resolve_enabled", enabled)
    object.__setattr__(
        settings, "chili_alpaca_execution_bbo_locked_resolve_max_age_seconds", bound)
    return old


def _restore(old):
    for k, v in old.items():
        object.__setattr__(settings, k, v)


# ── ARTIFACT: the other feed has a real book, so use it ──────────────────────

def test_artifact_lock_is_resolved_from_the_feed_that_has_a_real_book():
    """THE AUUD ROW, as an executable assertion.

    The SIP stand-in is locked at 1.14/1.14 and IQFeed at the same instant
    (dt = +0.0007 s) has 1.13/1.14 = 88.11 bps. origin/main returns the locked
    quote at 0.0 bps and the entry submits; here the real book is returned and
    the ordinary spread budget refuses it on a real number.
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
    assert tick.bid == pytest.approx(1.13)
    assert tick.ask == pytest.approx(1.14)
    assert tick.spread_bps == pytest.approx(88.11, abs=0.5)
    assert tick.raw["locked_book_resolution"] == "artifact_resolved"
    assert tick.raw["locked_primary_bid"] == pytest.approx(1.14)
    assert tick.raw["locked_primary_feed"] == "massive_ws_universe"
    # The tiers BELOW the locked one were actually consulted -- on main the
    # chain returns at massive_sip and never reaches them.
    assert a.calls == ["alpaca_direct", "massive_sip", "iqfeed_l1_own_clock"]


def test_resolution_falls_through_l1_to_the_trade_embedded_tier():
    """CANF 2026-09-02 11:00-11:20 has ZERO iqfeed_l1 rows in the tape (727
    massive_ws_universe rows, 0 IQFeed) while iqfeed_trade_ticks supplied
    83,288 rows for the SAME window. Stopping at L1 would declare "no second
    feed" for a name that has one, so every tier must be tried."""
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
    assert tick.bid == pytest.approx(1.40)
    assert tick.raw["locked_book_resolution"] == "artifact_resolved"
    assert "iqfeed_trade_embedded" in a.calls


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
    # Every remaining tier was tried before giving up.
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
    """THE KILL SWITCH. Off, the locked SIP row is returned immediately and the
    IQFeed tiers are never consulted -- exactly origin/main."""
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


def test_exit_path_is_untouched_because_it_never_opts_into_stand_ins():
    """INVARIANT. ``allow_stand_in`` is False for the exit marketability refresh
    and the extended-hours orphan close (get_execution_bbo's own docstring says
    why: a cross-source bid is systematically permissive in the unsafe
    direction for an exit). Resolution is gated on the same opt-in, so a
    locked DIRECT quote on an exit is returned unchanged and no tier runs."""
    a = _Adapter(direct=_tick("iex", 5.00, 5.00))
    old = _flags()
    try:
        tick, _meta = _get(a, allow_stand_in=False)
    finally:
        _restore(old)
    assert (tick.bid, tick.ask) == (pytest.approx(5.00), pytest.approx(5.00))
    assert a.calls == ["alpaca_direct"]
    assert "locked_book_resolution" not in (tick.raw or {})


def test_a_crossed_book_is_still_not_treated_as_locked():
    """CROSSED (ask < bid) is a different defect with its own handling and is
    already rejected by every validity test in the adapter. The locked path
    must not soften it or claim it."""
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
    """The bound is a SIMULTANEITY test, not a freshness test: it is measured
    between the two provider event clocks, so it behaves the same whether the
    read happens now or is replayed later. 88 of 89 real matches sit inside
    100 ms; the default bound of 2.0 s is deliberately generous against that
    and still excludes every 15-112 s feed hole."""
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

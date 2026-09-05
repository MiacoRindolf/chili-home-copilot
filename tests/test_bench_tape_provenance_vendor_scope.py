"""Tape-provenance agreement is a VENDOR question, not a dataset question.

The first real Ross-bench run refused SDOT 2026-06-26 with

    MISMATCH:pin.tape.sources=['iqfeed_lookup_hist'] but the run read
             ['iqfeed_lookup_bbo', 'iqfeed_lookup_hist']

on a tape that was hydrated exactly once, from one vendor. The pinner and the driver
read different datasets BY DESIGN: pinning an entry needs a printed price at a time so
it reads trades only, while the driver also mirrors the NBBO. Comparing raw source
strings made the normal case look like contamination — and an invariant that cries wolf
on the normal case is one an operator learns to skip, which is worse than not having it.

What the guard is actually for is a symbol-day hydrated from TWO VENDORS: measured on
TMCR 2026-08-24, where a double hydration produced 33,866 ticks against a true 16,933,
and the mixed-vendor quote seam the hydrator's own check exists to find. That risk is
preserved here and pinned by the second test.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import ross_replay_bench as bench  # noqa: E402


def _receipt(**per_table):
    return {"tape_sources": {t: srcs for t, srcs in per_table.items()}}


def _pin(*sources):
    return {"tape": {"sources": list(sources)}}


def test_same_vendor_different_dataset_is_a_match():
    """trades + quotes from one vendor is one tape, not two."""
    got = bench.check_pin_sources(
        _receipt(
            iqfeed_trade_ticks={"iqfeed_lookup_hist": 155_059},
            momentum_nbbo_spread_tape={"iqfeed_lookup_bbo": 40_112},
        ),
        _pin("iqfeed_lookup_hist"),
    )
    assert got.startswith("match:")


def test_a_second_vendor_is_still_a_mismatch():
    """The TMCR double-hydration shape must still be caught, and named."""
    got = bench.check_pin_sources(
        _receipt(iqfeed_trade_ticks={"iqfeed_lookup_hist": 16_933, "polygon_v3_trades": 16_933}),
        _pin("iqfeed_lookup_hist"),
    )
    assert got.startswith("MISMATCH:")
    # The message must carry BOTH the vendor verdict and the raw sources, so the operator
    # can tell a genuine double-hydration from a naming change without re-running.
    assert "polygon" in got and "iqfeed" in got
    assert "polygon_v3_trades" in got


def test_iqfeed_trades_with_polygon_quotes_is_the_designed_august_tape():
    """Trades from one vendor and quotes from another is how every August/September
    symbol-day is hydrated (IQFeed at-trade BBO is June/July only). MEASURED 2026-09-05:
    the first winners sweep flagged PPCB 2026-08-27 as MISMATCH on exactly this pair. It
    is a match, with the NBBO vendor REPORTED so the reader knows which quotes drove it."""
    got = bench.check_pin_sources(
        _receipt(
            iqfeed_trade_ticks={"iqfeed_lookup_hist": 100},
            momentum_nbbo_spread_tape={"polygon_v3_quotes": 100},
        ),
        _pin("iqfeed_lookup_hist"),
    )
    assert got.startswith("match:")
    assert "nbbo_vendor=['polygon']" in got


def test_two_quote_vendors_in_one_table_is_still_the_seam():
    got = bench.check_pin_sources(
        _receipt(
            iqfeed_trade_ticks={"iqfeed_lookup_hist": 100},
            momentum_nbbo_spread_tape={"polygon_v3_quotes": 100, "iqfeed_lookup_bbo": 100},
        ),
        _pin("iqfeed_lookup_hist"),
    )
    assert got.startswith("MISMATCH:two vendors in one table")


def test_trades_from_a_vendor_the_pin_never_saw_is_a_contradiction():
    got = bench.check_pin_sources(
        _receipt(iqfeed_trade_ticks={"polygon_v3_trades": 100}),
        _pin("iqfeed_lookup_hist"),
    )
    assert got.startswith("MISMATCH:")
    assert "TRADES" in got


@pytest.mark.parametrize(
    "source,vendor",
    [
        ("iqfeed_lookup_hist", "iqfeed"),
        ("iqfeed_lookup_bbo", "iqfeed"),
        ("polygon_v3_trades", "polygon"),
        ("polygon_v3_quotes", "polygon"),
        ("replay_v3", "replay_v3"),
    ],
)
def test_known_sources_fold_to_their_vendor(source, vendor):
    assert bench._tape_vendor(source) == vendor


def test_an_unknown_source_is_compared_strictly():
    """A source name nobody has seen before must NOT be folded into a vendor — it is
    returned whole so it compares strictly and shows up rather than passing silently."""
    assert bench._tape_vendor("some_new_feed_v9") == "some_new_feed_v9"
    got = bench.check_pin_sources(
        _receipt(iqfeed_trade_ticks={"some_new_feed_v9": 10}),
        _pin("iqfeed_lookup_hist"),
    )
    assert got.startswith("MISMATCH:")


def test_unverifiable_pins_stay_distinguishable_from_verified():
    """"We could not check" must never read as "we checked and it matched"."""
    assert bench.check_pin_sources(_receipt(iqfeed_trade_ticks={"iqfeed_lookup_hist": 1}), None) \
        == "unverified:no_pin_row"
    assert bench.check_pin_sources(_receipt(), _pin("iqfeed_lookup_hist")) \
        == "unverified:receipt_reports_no_tape_sources"

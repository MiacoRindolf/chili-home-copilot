"""A Robinhood word was refusing every Alpaca add in extended hours (2026-09-07).

THE OPERATOR HAS ASKED FOR THIS SHAPE REPEATEDLY: sell a part into the pullback to bank the
quick profit, then buy it back when the price reclaims. Ross does it constantly. Measured
across the 158 baseline receipts:

                                  Alpaca (81 cases)   Robinhood (77 cases)
    partial sell fills                 4 (1.3%)            421 (46.1%)
    buys-while-long                    0                   147
    closed loops (partial -> re-buy)   0                   117, median 21.2 s

ZERO closed loops in 86 Alpaca symbol-days. There are three independent cuts and this file
fixes the second one, the BUY-BACK half.

Alpaca's extended-hours certification requires the LITERAL "day" alongside
`extended_hours=True`. "gfd" is Robinhood vocabulary and the adapter rejects it as
`alpaca_extended_hours_entry_not_certified`. The ENTRY path learned this on 2026-08-17 and
encodes it at live_runner.py:39670-39678, with a comment saying exactly that. The six
add/repeg sites were never updated, and the extended-hours carve-out at :1488 tests the RAW
tif -- so `gfd` never matched, and the add was refused before the normalisation at :4513
could ever map it to `day`.

Cost: 374 refusals across 36 Alpaca cases. 356 on Ross WINNERS against 18 on losers -- a
20:1 winner concentration -- and 93.5% of Alpaca entries in that corpus are extended-hours.

Runnable: pytest tests/test_alpaca_extended_hours_adds.py -v   (DB-free)
"""
from __future__ import annotations

import inspect

from app.services.trading.momentum_neural import live_runner as lr


class _Alpaca:
    execution_family = "alpaca_spot"


class _AlpacaShort:
    execution_family = "alpaca_short"


class _Robinhood:
    execution_family = "robinhood_agentic_mcp"


def test_only_an_alpaca_add_in_extended_hours_gets_the_literal_day():
    f = lr._alpaca_add_time_in_force
    assert f(_Alpaca(), True) == "day"
    assert f(_AlpacaShort(), True) == "day"
    # everything else is exactly what it was before this branch
    assert f(_Alpaca(), False) == "gfd"
    assert f(_Robinhood(), True) == "gfd"
    assert f(_Robinhood(), False) == "gfd"


def test_it_falls_back_to_the_old_value_on_anything_unreadable():
    """A TIF helper that raises would refuse the add outright, which is the failure it
    exists to remove. Every unreadable input returns the pre-2026-09-07 value."""
    f = lr._alpaca_add_time_in_force
    for sess, ext in ((None, True), (_Alpaca(), None), (object(), True),
                      (_Alpaca(), "yes"), (None, None)):
        assert f(sess, ext) in ("gfd", "day")
    assert f(None, True) == "gfd"
    assert f(_Alpaca(), None) == "gfd"


def test_no_add_or_repeg_site_still_hard_codes_gfd():
    """The whole defect was six literals. If one comes back, this fails."""
    src = inspect.getsource(lr)
    assert 'time_in_force="gfd",' not in src


def test_all_six_add_paths_route_through_the_helper():
    """Repeg, anticipation remainder, pyramid, micro-pullback re-load, pullback add and
    flag-breakout add -- these are the paths that buy back what a partial sold."""
    src = inspect.getsource(lr.tick_live_session)
    for ext_var in ("_ant_ext", "_pyr_ext", "_mpr_ext", "_pba_ext", "_fba_ext"):
        assert f"_alpaca_add_time_in_force(\n" in src
        assert ext_var in src, ext_var
    whole = inspect.getsource(lr)
    assert whole.count("_alpaca_add_time_in_force(") >= 7  # 1 def + 6 call sites


def test_the_entry_path_that_already_knew_this_is_untouched():
    """The entry path is the precedent this copies. It must still carry its own
    conditional and its own comment -- if the entry regressed, the fix is wrong."""
    src = inspect.getsource(lr)
    # anchor on the ENTRY conditional itself, not on the shared error string -- the new
    # helper's docstring quotes that string too, and matching it would find the wrong site
    i = src.find("if (\n                    _entry_extended\n")
    assert i > 0, "the entry path's own extended-hours conditional is gone"
    block = src[max(0, i - 300):i + 300]
    assert '"day"' in block and '"gfd"' in block

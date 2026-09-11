"""Two sites reused `_entry_df` without the fallback their sibling already had (2026-09-07).

ONE DEFECT, THREE SITES, ONE ALREADY FIXED. `_entry_df` is fetched once per tick at
live_runner.py:32534 and ONLY under `_live_entry_quote_gate_applies(sess, le)`. Three places
then reuse it. The site at :33914 carries a fallback fetch and its own comment names the
cause -- "kapag None ang _entry_df dahil di-applicable ang quote gate, ito ang tanging kopya
nito". The other two did not.

SITE 1 -- the front-side strength block. When the predicate is False all three frame-derived
terms go None TOGETHER: er 0.34 + vwap_dist 0.16 + range_pos 0.10 = EXACTLY 0.60 of the
score's weight, and `front_side_strength_score` renormalises over the OFI/tape remainder. The
score did not get weaker; it lost its spine, silently. MEASURED: two bench arms standing at
the SAME second on IDENTICAL tape read frontside 1.0000 vs 0.7034 and sized the JWEL
2026-08-10 winner leg 169 sh vs 87 sh. It is arm-flippable, because anything that shifts
session state or `entry_submitted` timing changes whether the ER spine is present at all.

SITE 2 -- the topping-tail runner exit. `chili_momentum_exit_topping_tail_enabled` defaults
True, and the path only runs in states where the predicate is False, so `_entry_df` was
ALWAYS None and `candles.topping_tail_from_df` always returned False on its fail-safe. A
default-True flag that cannot fire is the dark flag the doctrine forbids. Fixing it TURNS A
NEVER-FIRED EXIT ON -- arm-ready, not ship-ready.

SITE 2, 2026-09-11 [5] -- the 15m fallback frame is GONE from this site. It fired (3 live
fires after e91c18092), but the 15-minute wall-clock bucket carried prints from BEFORE the
position existed: 2 of the 3 live fires were a wick the leg never saw (WYHG 09-08 09:09:04,
bucket high 6.36 printed 09:03:35, entry fill 09:08:37). The site now reads the LEG's own
prints (`_leg_topping_tail_read` -> `entry_gates.leg_print_candle`, anchored on the G/D
verdict's entry fill, at the tick's one as-of, bounded) -- no `_entry_df`, no OHLCV fetch at
all. The front-side pins below are unchanged; the topping-tail pins now say the frame is not
read. (Review fix: these pins are AST-shaped, not verbatim lines -- renaming a local must
not break them; the BEHAVIOUR is pinned by real tick passes in
tests/test_topping_tail_leg_prints.py and tests/test_g4_grind_tick_wiring.py.)

Runnable: pytest tests/test_entry_df_fallback.py -v   (DB-free)
"""
from __future__ import annotations

import ast
import inspect

from app.services.trading.momentum_neural import live_runner as lr


def _src() -> str:
    return inspect.getsource(lr.tick_live_session)


def test_the_front_side_block_falls_back_when_the_quote_gate_did_not_fetch():
    src = _src()
    i = src.find('le["frontside_size_tilt"] = {')
    assert i > 0
    block = src[max(0, i - 6000):i]
    assert "_fs_df = _entry_df" in block
    assert '_fs_df = _replay_aware_fetch_ohlcv_df(' in block
    assert 'interval="15m", period="5d"' in block


def test_the_front_side_terms_read_the_fallback_frame_not_the_original():
    """A fallback that is fetched and then not used is worse than none -- it costs a read and
    changes nothing. Every frame-derived term must come off `_fs_df`."""
    src = _src()
    i = src.find("_fs_df = _entry_df")
    j = src.find("_fs_score = _fs_strength(", i)
    assert 0 < i < j
    block = src[i:j]
    assert "_fs_sess_df = _fs_today_frame(_fs_df)" in block
    assert "_fs_frame_bars = int(len(_fs_df))" in block
    assert "_fs_frame_stamp = str(_fs_df.index[-1])" in block
    # the ONLY surviving mention of the original is the assignment that seeds the fallback
    assert block.count("_entry_df") == 1


def _topping_tail_block() -> str:
    """The TRAILING topping-tail block: from its flag to the chandelier inputs below it."""
    src = _src()
    i = src.find('"chili_momentum_exit_topping_tail_enabled"')
    j = src.find("_atr_pct_trail = _float_or_none(", i)
    assert 0 < i < j
    return src[i:j]


def _topping_tail_if() -> ast.If:
    tick = ast.parse(inspect.getsource(lr.tick_live_session))
    hits = [
        n for n in ast.walk(tick)
        if isinstance(n, ast.If) and any(
            isinstance(c, ast.Constant) and c.value == "chili_momentum_exit_topping_tail_enabled"
            for c in ast.walk(n.test)
        )
    ]
    assert len(hits) == 1, len(hits)
    return hits[0]


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    return [
        n for n in ast.walk(node)
        if isinstance(n, ast.Call) and (
            (isinstance(n.func, ast.Name) and n.func.id == name)
            or (isinstance(n.func, ast.Attribute) and n.func.attr == name)
        )
    ]


def test_the_topping_tail_exit_is_no_longer_structurally_inert():
    """[5]: still not inert -- it has its OWN input now. The leg candle is read on every
    TRAILING pass, independent of whether the quote gate fetched `_entry_df`, at the tick's
    one as-of."""
    block = _topping_tail_if()
    [read] = _calls(block, "_leg_topping_tail_read")
    kw = {k.arg: k.value for k in read.keywords}
    assert isinstance(kw.get("as_of"), ast.Name) and kw["as_of"].id == "tick_as_of"
    assert _calls(block, "_leg_topping_tail_unavailable_once"), "an unjudgeable leg is named"
    # the frame read (and its fallback) that carried pre-entry prints is gone
    names = {n.id for n in ast.walk(block) if isinstance(n, ast.Name)}
    assert "_entry_df" not in names and "_tt_df" not in names
    assert not _calls(block, "topping_tail_from_df")
    # and the read helper is the leg-print candle, nothing else
    helper = ast.parse(inspect.getsource(lr._leg_topping_tail_read))
    assert _calls(helper, "leg_print_candle")
    assert not _calls(helper, "_replay_aware_fetch_ohlcv_df")


def test_both_fallbacks_fail_open_and_never_raise():
    """A frame fetch that throws must not take the tick down -- the pre-2026-09-07 behaviour
    (no frame, terms drop out / exit does not fire) is the correct floor. [5]: the
    topping-tail site has no frame fetch left; its read sits inside the block's own
    try/except, and `_leg_topping_tail_read` / `leg_print_candle` never raise (no candle ->
    a NAMED no-arm)."""
    src = _src()
    i = src.find("_fs_df = _replay_aware_fetch_ohlcv_df(")
    assert i > 0
    after = src[i:i + 420]
    assert "except Exception:" in after
    assert "_fs_df = None" in after
    block = _topping_tail_if()
    [tr] = [n for n in block.body if isinstance(n, ast.Try)]
    assert _calls(tr, "_leg_topping_tail_read")
    assert tr.handlers and all(
        isinstance(h.type, ast.Name) and h.type.id == "Exception" for h in tr.handlers)


def test_the_sibling_site_that_was_already_correct_is_untouched():
    """:33914 is the precedent this copies. If it regressed, the fix is wrong."""
    src = _src()
    assert "_df = _entry_df  # reuse the adaptive-spread 15m candles if present" in src
    i = src.find("_df = _entry_df  # reuse the adaptive-spread 15m candles if present")
    assert 'fetch_ohlcv_df(sess.symbol, interval="15m", period="5d")' in src[i:i + 300]


def test_the_one_fetch_per_tick_contract_is_preserved_on_the_normal_path():
    """When the quote gate DID fetch, the front-side site may not fetch again -- the frame is
    fetched once per tick by contract (`fetch them once per pre-entry tick`). [5]: the
    topping-tail site fetches NO OHLCV frame at all any more (it was a 15m fetch on every
    TRAILING tick); its one read is the leg's prints."""
    src = _src()
    i = src.find("_fs_df = _entry_df")
    assert i > 0
    # the fetch is guarded by an is-None check, so the normal path costs nothing
    assert "is None:" in src[i:i + 120]
    block = _topping_tail_block()
    assert "fetch_ohlcv_df" not in block
    assert 'interval="15m"' not in block
    assert len(_calls(_topping_tail_if(), "_leg_topping_tail_read")) == 1

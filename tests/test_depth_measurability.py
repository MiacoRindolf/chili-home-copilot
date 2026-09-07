"""The book was mirrored from a database that has no book in it (2026-09-07).

WHAT HAPPENED. `mirror_depth_streaming` connects to `PROD`, which resolves to the tape
source -- `chili_hydrated`. That database carries 41 GB of trades and 47 GB of NBBO and
`iqfeed_depth_snapshots` with **0 rows**. The live `chili` holds ~9.4M. So every baseline
receipt recorded `mirrored.depth_rows == 0`, all 180 of them, every one with FULL_MIRROR=1.

WHY THAT MATTERS MORE THAN A MISSING TABLE. Four exit mechanisms read the book, and one of
them is the literal Ross level-2 read -- its own config description quotes him, "fixated on
the level 2, specifically the ask price". Across the whole baseline:

    live_ask_side_pressure            0     never armed, never emitted
    sell_into_strength_limit_placed   0     no harvest limit ever posted
    stop_breach_chop_hold             0     never held a stop
    live_sell_into_strength      13,856     stale_or_thin 7,112 | not_deep_run 6,744

Of the 7,112 ticks that reached the book-freshness test, 7,112 -- 100% -- found no book. Not
one tick in the baseline ever evaluated a distribution threshold, so retuning one would have
changed nothing.

A no-op's A/B delta is exactly 0.00, and on the page that is indistinguishable from "we
measured this lever and it did nothing". An `exit_ladder_live` 0-vs-1 A/B on XPON 08-24
returned byte-identical fills and the identical -67.74, and was read as a result. That is
the failure mode this harness exists to prevent, and nothing failed closed on it.

Three fixes, one per section below. Measured after the fix: AEHL 2026-08-31, a bench case,
has 25,614 depth rows in live `chili` for its window -- against 0 in the corpus.

Runnable: pytest tests/test_depth_measurability.py -v   (DB-free)
"""
from __future__ import annotations

import ast
import inspect
import io
import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "scripts"))

import replay_harness_invariants as inv  # noqa: E402

DRIVER = _REPO / "scripts" / "replay_v3_fsm_window.py"


# ── 1. the book gets its own source, and unset is byte-identical ─────────────

def test_the_depth_mirror_reads_its_own_source_not_the_tape():
    src = io.open(DRIVER, encoding="utf-8").read()
    i = src.index("def mirror_depth_streaming(sim_engine):")
    j = src.index("\ndef ", i + 10)
    body = src[i:j]
    assert "psycopg2.connect(DEPTH_SOURCE)" in body
    assert "psycopg2.connect(PROD)" not in body
    # the tape and NBBO mirrors are untouched -- they were never the defect
    assert src.count("psycopg2.connect(PROD)") >= 1


def test_an_unset_override_is_byte_identical_to_the_old_behaviour():
    """The whole point of the default: every run before today is reproducible."""
    src = io.open(DRIVER, encoding="utf-8").read()
    tree = ast.parse(src)
    assign = next(
        n for n in tree.body
        if isinstance(n, ast.Assign)
        and any(getattr(t, "id", None) == "DEPTH_SOURCE" for t in n.targets)
    )
    text = ast.get_source_segment(src, assign.value) or ""
    assert "DEPTH_SOURCE_URL" in text and text.strip().endswith("PROD")


def test_the_receipt_says_which_book_it_read_and_never_leaks_the_url():
    src = io.open(DRIVER, encoding="utf-8").read()
    assert '"depth_source_db": _depth_source_db(),' in src
    assert '"depth_levers_unmeasurable": bool(int(mirrored_depth) == 0),' in src
    assert '"depth_flags_set_by_this_run": _depth_flags_in_env(),' in src
    # the helper returns the DATABASE NAME; a URL carries credentials
    i = src.index("def _depth_source_db()")
    body = src[i:src.index("\n\n", i)]
    assert 'rsplit("/", 1)[-1]' in body


def test_an_empty_book_is_announced_at_the_top_of_the_run():
    src = io.open(DRIVER, encoding="utf-8").read()
    assert "[depth] EMPTY BOOK from" in src
    assert "measures SILENCE, not the" in src


# ── 2. a blind level-2 read leaves a receipt ─────────────────────────────────

def test_a_blind_ask_side_read_is_recorded_on_change_of_reason():
    """`ask_side_pressure_lock` returns `stale_or_thin` BEFORE it sets ``armed``, and the
    existing telemetry is gated on ``armed`` -- which is why the Ross mechanism emitted
    zero rows across 180 receipts and nobody could tell blindness from silence."""
    from app.services.trading.momentum_neural import live_runner as lr

    src = inspect.getsource(lr.tick_live_session)
    i = src.find('_emit(db, sess, "live_ask_side_pressure_blind"')
    assert i > 0
    block = src[max(0, i - 2600):i]
    # it is the ELSE of the armed telemetry, so an armed tick still reports as before
    assert 'if _asp.get("armed"):' in block
    assert 'le.get("asp_blind_reason")' in block
    # ON CHANGE, never per pass
    assert 'le["asp_blind_reason"] = str(_asp.get("reason") or "")' in src


def test_the_blind_marker_is_cleared_on_recycle():
    from app.services.trading.momentum_neural import live_runner as lr

    assert "asp_blind_reason" in lr._RECYCLE_ENTRY_STATE_KEYS


def test_the_blind_branch_cannot_change_a_decision():
    """It writes one session key and emits. No stop, no order, no state transition."""
    from app.services.trading.momentum_neural import live_runner as lr

    src = inspect.getsource(lr.tick_live_session)
    i = src.index('elif str(_asp.get("reason") or "") and str(')
    j = src.index("# Action A: ratchet-only stop write", i)
    branch = src[i:j]
    for forbidden in ("pos[", "_safe_transition", "_submit_live_market_exit",
                      "place_", "stop_px ="):
        assert forbidden not in branch, forbidden


# ── 3. the invariant: a silent zero is never evidence ────────────────────────

def test_a_depth_lever_moved_against_an_empty_book_is_a_violation():
    why = inv.depth_measurability_violation(
        0, [{"CHILI_MOMENTUM_EXIT_LADDER_LIVE": "1"}, {"CHILI_MOMENTUM_EXIT_LADDER_LIVE": "0"}]
    )
    assert why and "depth_levers_unmeasurable" in why
    assert "CHILI_MOMENTUM_EXIT_LADDER_LIVE" in why
    assert "DEPTH_SOURCE_URL" in why, "it must say how to fix it"
    with pytest.raises(AssertionError):
        inv.assert_depth_measurable(0, [{"CHILI_MOMENTUM_EXIT_ASK_PRESSURE_ENABLED": "1"}])


def test_a_book_that_mirrored_rows_is_fine_and_so_is_an_arm_that_touches_no_lever():
    assert inv.depth_measurability_violation(25614, [{"CHILI_MOMENTUM_EXIT_LADDER_LIVE": "1"}]) is None
    assert inv.depth_measurability_violation(0, [{"CHILI_MOMENTUM_BREAKOUT_BAILOUT_ENABLED": "0"}]) is None
    assert inv.depth_measurability_violation(0, []) is None
    assert inv.depth_measurability_violation(0, None) is None


def test_it_fails_closed_on_an_unreadable_count():
    for bad in (None, "x", object()):
        assert inv.depth_measurability_violation(bad, []) == "depth_rows_unreadable"


def test_every_depth_dependent_flag_is_a_real_setting():
    """A guard listing a flag that does not exist protects nothing."""
    os.environ.setdefault("DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili_test")
    from app.config import Settings

    aliases = set()
    for name, field in Settings.model_fields.items():
        aliases.add(name.upper())
        va = getattr(field, "validation_alias", None)
        for choice in getattr(va, "choices", []) or []:
            aliases.add(str(choice).upper())
    missing = [f for f in inv.DEPTH_DEPENDENT_FLAGS if f.upper() not in aliases]
    assert not missing, missing


def test_the_driver_and_the_invariant_agree_on_the_flag_list():
    """Two copies of the same list drift; this is the seam that catches it."""
    src = io.open(DRIVER, encoding="utf-8").read()
    i = src.index("_DEPTH_DEPENDENT_FLAGS = (")
    driver_flags = {
        n.strip().strip('",')
        for n in src[i:src.index(")", i)].splitlines()
        if n.strip().startswith('"')
    }
    assert driver_flags == set(inv.DEPTH_DEPENDENT_FLAGS)

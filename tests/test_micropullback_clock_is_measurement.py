"""The dip buy is no longer held shut by a stopwatch.

WHAT THIS PROTECTS. The micro-pullback re-load is the operator's buy-the-dip doctrine in
code. Measured across the system's entire history to 2026-09-09:

    live_micro_pullback_reentry_blocked   571   36 sessions   2026-06-29 .. 09-08
      reason=cooldown                     547   33 sessions   ← 95.8%
      reason=flow                          14    2 sessions
      reason=midday_lull                   10    2 sessions
    micro-pullback re-entries that FILLED    0   ever

A mechanism that has never once fired is not conservative; it is absent. And 95.8% of what
held it shut was a wall clock: max(30 s, 2 x bar_seconds), re-armed every time a re-load
order cleared, and pushed to 3x by the SLOW_CHOPPER damper. Three invented multipliers with
no distribution behind any of them.

WHY THE CLOCK BECAME A MEASUREMENT RATHER THAN A DELETION. With zero fills there is no
outcome distribution from which to derive a replacement operating point — the clock
prevented the very data that would justify replacing it. So the gate opens and the same
condition is RECORDED. If the re-loads that only get through because of this turn out to
lose, the receipts name them and the bound comes back derived instead of invented.

These are source-level assertions, and that limit is deliberate: the branch lives ~46,500
lines into `tick_live_session`, behind a live position in STATE_LIVE_TRAILING, so a unit
test cannot reach it without simulating the whole session. What a source test CAN do is
guarantee the clock never silently becomes a block again and that the four non-clock guards
are still standing — which is exactly what would go wrong.

Runnable: pytest tests/test_micropullback_clock_is_measurement.py -v
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_SRC = (Path(__file__).resolve().parents[1]
        / "app/services/trading/momentum_neural/live_runner.py")


@pytest.fixture(scope="module")
def src() -> str:
    return _SRC.read_text(encoding="utf-8", errors="replace")


def test_the_clock_no_longer_emits_a_block(src: str):
    """The whole point. `live_micro_pullback_reentry_blocked` with reason "cooldown" was
    547 of 571 refusals; it must not be emitted from the re-load trigger any more."""
    assert '"reason": "cooldown", "until": _cool_raw' not in src, (
        "the cooldown block is back — the dip buy is held shut by a stopwatch again"
    )


def test_the_clock_is_recorded_instead(src: str):
    """Opening a gate without recording what it would have refused throws away the only
    evidence that could ever justify closing it again."""
    assert "live_micro_pullback_reentry_clock_observed" in src
    assert '"would_have_blocked": True' in src
    assert '"seconds_remaining"' in src, (
        "record BY HOW MUCH the clock would have blocked, not just that it would have"
    )


def test_the_four_non_clock_guards_are_still_standing(src: str):
    """The clock was never the only thing guarding this path, and none of the others are
    clocks. Removing it must not have removed them."""
    for guard, what in (
        ("chili_momentum_micropullback_reentry_max", "the re-load cap"),
        ("micropullback_last_shelf", "the shelf ratchet (hold above the PREVIOUS dip)"),
        ("min_cushion_r", "GUARD #2 cushion (a falling knife cannot bank cushion)"),
        # [1] 2026-09-10: the "flow" guard was two things wearing one name — a KNIFE
        # (_entry_flow_veto: never buy into selling) and a POSITIVE-CONFIRM on two floors
        # measured anti-selective (OFI clustered AUC 0.400; the +0.30 floor refuses 82.0%
        # of real onsets vs 74.9% of controls). All 18 all-time `reason=flow` refusals had
        # veto=false, so the knife had never once fired here. The knife stays and is now
        # NAMED; the floors became a PRINT PROOF of the micro-break.
        # [1] review fix, same day: the ladder moved OUT of the runner into the pure
        # `entry_gates.micro_pullback_reload_proof`, precisely so its order and its
        # boundaries stop being pinned by substring. What the runner must still show is
        # that it reads the knife, reads the TAPE on both sides of the comparison, and
        # routes the verdict through that one function.
        ("_entry_flow_veto(_mpr_ofi, _mpr_tf, settings)", "the named flow KNIFE"),
        ("micro_pullback_reload_proof as _mpr_ladder_fn", "the shared decision ladder"),
        ("veto=bool(_veto)", "the knife feeding the ladder's first rung"),
        ("micro_pullback_print_evidence as _mpr_evidence_fn", "the PRINT-basis micro-break reference"),
        ("reclaim_high_px=_mpr_reclaim_high", "the print proof of the micro-break"),
    ):
        assert guard in src, f"{what} is gone — that was not part of this change"


def test_the_ladder_verdicts_are_all_reachable_receipt_names():
    """[1] review fix. The receipt names now live on the pure function, so the runner and
    the tests read the SAME list instead of two copies that can drift."""
    from app.services.trading.momentum_neural.entry_gates import (
        MICRO_PULLBACK_RELOAD_VERDICTS,
        micro_pullback_reload_proof,
    )

    seen = {
        micro_pullback_reload_proof(
            veto=True, last_print=1.0, signed_tape_accel=1.0, tape_stale=False,
            break_ref_px=1.0, reclaim_high_px=2.0),
        micro_pullback_reload_proof(
            veto=False, last_print=None, signed_tape_accel=None, tape_stale=None,
            break_ref_px=1.0, reclaim_high_px=2.0),
        micro_pullback_reload_proof(
            veto=False, last_print=1.0, signed_tape_accel=1.0, tape_stale=False,
            break_ref_px=None, reclaim_high_px=2.0),
        micro_pullback_reload_proof(
            veto=False, last_print=1.0, signed_tape_accel=1.0, tape_stale=False,
            break_ref_px=2.0, reclaim_high_px=1.0),
        micro_pullback_reload_proof(
            veto=False, last_print=1.0, signed_tape_accel=-1.0, tape_stale=False,
            break_ref_px=1.0, reclaim_high_px=2.0),
        micro_pullback_reload_proof(
            veto=False, last_print=1.0, signed_tape_accel=1.0, tape_stale=False,
            break_ref_px=1.0, reclaim_high_px=2.0),
    }
    assert seen == set(MICRO_PULLBACK_RELOAD_VERDICTS), (
        "every declared verdict must be reachable, and no undeclared one may appear"
    )


def test_the_flow_guard_is_a_knife_not_an_inverted_floor(src: str):
    """[1] The replacement must not quietly re-grow a floor. Neither anti-selective
    threshold may be COMPARED against again — they are reported values only."""
    assert "_pos_confirm" not in src
    assert "_ofi_floor" not in src, "the +0.30 OFI positive-confirm floor is back"
    assert "_tf_floor" not in src, "the +0.20 trade_flow positive-confirm floor is back"


def test_the_reload_state_still_clears_on_recycle(src: str):
    """A stale cooldown surviving a recycle would describe a trade that already closed.
    This project has shipped that exact defect twice; assert the key is in the set."""
    keys = None
    for node in ast.walk(ast.parse(src)):
        target = node.target if isinstance(node, ast.AnnAssign) else (
            node.targets[0] if isinstance(node, ast.Assign) and node.targets else None)
        if isinstance(target, ast.Name) and target.id == "_RECYCLE_ENTRY_STATE_KEYS":
            keys = {e.value for e in ast.walk(node.value)
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    assert keys, "_RECYCLE_ENTRY_STATE_KEYS not found"
    assert "micropullback_reentry_cooldown_until_utc" in keys


def test_the_observation_cannot_short_circuit_the_reload(src: str):
    """The emit must not sit on an `else` that skips the re-load path — that would be the
    same block wearing a different name."""
    i = src.find("live_micro_pullback_reentry_clock_observed")
    assert i > 0
    tail = src[i:i + 1200]
    assert "if True:" in tail, (
        "the re-load branch must run unconditionally after the observation"
    )
    assert not re.search(r"clock_observed[\s\S]{0,600}?\n\s*else:\s*\n", tail), (
        "an else after the observation would re-introduce the block"
    )


def test_the_module_still_parses(src: str):
    """A 48k-line file edited by hand — parse it, cheaply, every run."""
    ast.parse(src)

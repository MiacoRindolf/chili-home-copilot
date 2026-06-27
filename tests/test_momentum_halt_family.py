"""HALT-FAMILY micro-component — adversarial branch/boundary unit tests.

This file hardens the two PURE halt-family functions that the Ross momentum lane wires
into the entry path (live_runner: halt_chain_risk_gate at sizing, halt_resume_dip_trigger
at the trigger ladder, both reading the captured per-symbol halt_level / consecutive
halt-up count). The loss-sensitive add-into-halt predicate is covered separately in
``tests/test_momentum_add_into_halt.py`` — this file deliberately does NOT duplicate it,
and the base dip-pop-reclaim geometry is covered in
``tests/test_halt_resume_dip_trigger.py``. Here we attack the parts those files leave open:

  halt_chain_risk_gate (GAP 1):
    * COUNT BOUNDARY        — exactly-at / eps-below the block count; block vs de-weight vs ok
    * DE-WEIGHT RAMP        — the linear (cnt-1)/(block-1) ramp values + monotone shrink
    * 0.5 FLOOR             — size_mult never drops to/below 0.5 on the de-weight branch
    * block_count==2 edge   — max(2, n) collapses the de-weight band; cnt==2 blocks directly
    * None / 0 / negative   — empty-count handling
    * FLAG OFF parity       — disabled BEFORE compute ⇒ (False, 1.0, "...disabled", {})
    * FAIL-OPEN             — never blocks on a bug (the gate is risk-reducing, must not veto)

  halt_resume_dip_trigger HALT-DIRECTION sub-block (GAP 2 / GAP 3):
    * RESUMPTION HIGHER     — direction='higher' + a bounded +boost size_mult (no veto)
    * RESUMPTION LOWER      — direction='lower' + a -penalty size_mult (no veto, dir-flag only)
    * RESUMPTION FLAT       — direction='flat' + size_mult==1.0
    * FALSE-HALT WEAK RESUME— lower resume under the false-halt flag ⇒ no-fire
    * MISSING halt_level    — None halt_level ⇒ block SKIPPED (byte-identical, fires)
    * EACH FLAG PARITY      — both flags OFF ⇒ no halt_level read, no annotation (byte-identical)
    * boost_frac CLAMP      — the [0,0.5] clamp on the conviction fraction

Pure-logic + no DB. These functions read the GLOBAL ``settings`` (not a settings_obj kwarg),
so tests pin flags with ``unittest.mock.patch.object`` on the imported settings — they never
mutate it outside the patched ``with`` block, so the production defaults (all OFF) stand.

Run (operator): conda run -n chili-env pytest tests/test_momentum_halt_family.py -v
with TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test
"""

from __future__ import annotations

from unittest import mock

import pandas as pd
import pytest

from app.services.trading.momentum_neural import entry_gates as eg
from app.services.trading.momentum_neural.entry_gates import (
    halt_chain_risk_gate,
    halt_resume_dip_trigger,
)


# A local settings stub: delegates to the real settings for every key EXCEPT the explicit
# overrides. halt_chain_risk_gate takes a settings_obj kwarg, so this stub is the cleanest way
# to pin only the flags a test needs without touching the global settings at all.
class _SettingsStub:
    def __init__(self, **overrides):
        self._ov = dict(overrides)

    def __getattr__(self, name):
        if name in self._ov:
            return self._ov[name]
        return getattr(eg.settings, name)


def _chain_on(**extra) -> _SettingsStub:
    base = {"chili_momentum_halt_chain_risk_gate_enabled": True}
    base.update(extra)
    return _SettingsStub(**base)


# ════════════════════════════════════════════════════════════════════════════════════════
# halt_chain_risk_gate — GAP 1
# ════════════════════════════════════════════════════════════════════════════════════════

# ── FLAG OFF parity: disabled BEFORE any compute ⇒ byte-identical (no debug populated) ─────

def test_chain_flag_off_byte_identical():
    block, mult, reason, dbg = halt_chain_risk_gate(
        consecutive_halt_up_count=9, settings_obj=_SettingsStub(
            chili_momentum_halt_chain_risk_gate_enabled=False))
    assert block is False
    assert mult == 1.0
    assert reason == "halt_chain_gate_disabled"
    assert dbg == {}  # nothing computed when disabled


def test_chain_flag_off_default_settings_disabled():
    # The PRODUCTION default (global settings) has the flag OFF ⇒ disabled for any count,
    # even an extreme blow-off chain. (settings_obj defaults to the real global settings.)
    for cnt in (0, 1, 2, 3, 9, 50):
        block, mult, reason, dbg = halt_chain_risk_gate(consecutive_halt_up_count=cnt)
        assert (block, mult, reason, dbg) == (False, 1.0, "halt_chain_gate_disabled", {})


# ── COUNT BOUNDARY (default block_count=3 ⇒ block_at=3): 1=ok / 2=deweight / 3=block ───────

def test_chain_count0_ok_full_size():
    block, mult, reason, _ = halt_chain_risk_gate(consecutive_halt_up_count=0, settings_obj=_chain_on())
    assert (block, mult, reason) == (False, 1.0, "halt_chain_ok")


def test_chain_count1_ok_full_size():
    # The FIRST halt up is not yet a chain ⇒ full size, no de-weight.
    block, mult, reason, _ = halt_chain_risk_gate(consecutive_halt_up_count=1, settings_obj=_chain_on())
    assert (block, mult, reason) == (False, 1.0, "halt_chain_ok")


def test_chain_count2_deweighted_eps_below_block():
    # eps-below the default block (3): the 2nd halt up ⇒ de-weight, NOT block.
    block, mult, reason, dbg = halt_chain_risk_gate(consecutive_halt_up_count=2, settings_obj=_chain_on())
    assert block is False
    assert reason == "halt_chain_deweighted"
    # frac=(2-1)/(3-1)=0.5 ; mult=1.0-0.5*0.5=0.75
    assert mult == pytest.approx(0.75)
    assert dbg["size_mult"] == pytest.approx(0.75)
    assert dbg["block_count"] == 3


def test_chain_count_exactly_at_block_blocks():
    # EXACTLY at the block count (3) ⇒ BLOCK (turn a would-fire into a no-fire), mult stays 1.0
    # because block already vetoes (size is irrelevant once blocked).
    block, mult, reason, dbg = halt_chain_risk_gate(consecutive_halt_up_count=3, settings_obj=_chain_on())
    assert block is True
    assert mult == 1.0
    assert reason == "halt_chain_blocked"
    assert dbg["consecutive_halt_up"] == 3
    assert dbg["block_count"] == 3


def test_chain_count_above_block_blocks():
    block, _, reason, _ = halt_chain_risk_gate(consecutive_halt_up_count=8, settings_obj=_chain_on())
    assert block is True
    assert reason == "halt_chain_blocked"


# ── DE-WEIGHT RAMP: explicit linear values across a wider band (block_count=5 ⇒ block_at=5) ─

def test_chain_deweight_ramp_block5():
    s = _chain_on(chili_momentum_halt_chain_block_count=5)
    # cnt=2 → frac=1/4=0.25 → 1-0.125=0.875
    # cnt=3 → frac=2/4=0.50 → 1-0.250=0.750
    # cnt=4 → frac=3/4=0.75 → 1-0.375=0.625
    expected = {2: 0.875, 3: 0.750, 4: 0.625}
    last = 1.01
    for cnt, exp in expected.items():
        block, mult, reason, _ = halt_chain_risk_gate(consecutive_halt_up_count=cnt, settings_obj=s)
        assert block is False, cnt
        assert reason == "halt_chain_deweighted", cnt
        assert mult == pytest.approx(exp), (cnt, mult)
        # MONOTONE: each successive halt-up shrinks the size strictly (risk-reducing ramp).
        assert mult < last, (cnt, mult, last)
        last = mult
    # cnt=5 (== block_at) ⇒ block
    block, _, reason, _ = halt_chain_risk_gate(consecutive_halt_up_count=5, settings_obj=s)
    assert block is True and reason == "halt_chain_blocked"


# ── 0.5 FLOOR invariant: the de-weight mult is ALWAYS in (0.5, 1.0]; floor never breached ──

def test_chain_deweight_mult_never_at_or_below_floor():
    # Sweep many block widths; for EVERY de-weight count the mult must be > 0.5 and <= 1.0
    # (the ramp math caps the deepest pre-block frac at (block-2)/(block-1) < 1 ⇒ mult > 0.5).
    for block_count in range(3, 20):
        s = _chain_on(chili_momentum_halt_chain_block_count=block_count)
        for cnt in range(2, block_count):  # the de-weight band [2, block_count-1]
            block, mult, reason, _ = halt_chain_risk_gate(consecutive_halt_up_count=cnt, settings_obj=s)
            assert block is False, (block_count, cnt)
            assert reason == "halt_chain_deweighted", (block_count, cnt)
            assert 0.5 < mult <= 1.0, (block_count, cnt, mult)


# ── block_count==2 edge: max(2, n) collapses the de-weight band (block_at not > 2) ─────────

def test_chain_block_count_two_no_deweight_band():
    # block_count=2 ⇒ block_at=2; the `block_at > 2` guard is False so there is NO de-weight
    # band: cnt==2 hits `cnt >= block_at` and BLOCKS directly; cnt==1 is ok.
    s = _chain_on(chili_momentum_halt_chain_block_count=2)
    block1, mult1, reason1, _ = halt_chain_risk_gate(consecutive_halt_up_count=1, settings_obj=s)
    assert (block1, mult1, reason1) == (False, 1.0, "halt_chain_ok")
    block2, _, reason2, _ = halt_chain_risk_gate(consecutive_halt_up_count=2, settings_obj=s)
    assert block2 is True and reason2 == "halt_chain_blocked"


def test_chain_block_count_below_floor_clamped_to_two():
    # A nonsensical block_count of 1 (or 0) is clamped UP to 2 by max(2, ...). So cnt=2 blocks
    # (it would be absurd to "block at 1" / never allow even a single halt-up). Proves the
    # max(2, ...) floor on the block count itself.
    for bad in (1, 0, -5):
        s = _chain_on(chili_momentum_halt_chain_block_count=bad)
        block, _, reason, dbg = halt_chain_risk_gate(consecutive_halt_up_count=2, settings_obj=s)
        assert block is True, bad
        assert reason == "halt_chain_blocked", bad
        assert dbg["block_count"] == 2, bad


# ── None / 0 / negative count handling ─────────────────────────────────────────────────────

def test_chain_none_count_treated_as_zero_ok():
    block, mult, reason, _ = halt_chain_risk_gate(consecutive_halt_up_count=None, settings_obj=_chain_on())
    assert (block, mult, reason) == (False, 1.0, "halt_chain_ok")


def test_chain_negative_count_below_block_ok():
    # A negative count (defensive / corrupt) is < block ⇒ ok, full size (never blocks/shrinks
    # on a garbage-but-low count — risk-reducing gate only ever tightens on a REAL high chain).
    block, mult, reason, _ = halt_chain_risk_gate(consecutive_halt_up_count=-3, settings_obj=_chain_on())
    assert block is False
    assert mult == 1.0
    assert reason == "halt_chain_ok"


# ── FAIL-OPEN: a bug inside must NEVER block (this gate can only ever reduce risk) ─────────

def test_chain_fail_open_on_bad_count_type():
    # int("abc") raises inside the try ⇒ the except returns (False, 1.0, "halt_chain_error"):
    # the gate must FAIL-OPEN (never veto an entry on its own bug).
    block, mult, reason, _ = halt_chain_risk_gate(
        consecutive_halt_up_count="abc", settings_obj=_chain_on())  # type: ignore[arg-type]
    assert block is False
    assert mult == 1.0
    assert reason == "halt_chain_error"


def test_chain_size_mult_invariant_never_exceeds_one():
    # ACROSS the whole reachable count range, size_mult is ALWAYS in [lo, 1.0] and NEVER > 1.0
    # (the gate is risk-reducing only — it must never BOOST size).
    s = _chain_on(chili_momentum_halt_chain_block_count=6)
    for cnt in range(0, 12):
        _, mult, _, _ = halt_chain_risk_gate(consecutive_halt_up_count=cnt, settings_obj=s)
        assert mult <= 1.0, (cnt, mult)
        assert mult >= 0.5, (cnt, mult)


# ════════════════════════════════════════════════════════════════════════════════════════
# halt_resume_dip_trigger — HALT-DIRECTION sub-block (GAP 2 / GAP 3)
# ════════════════════════════════════════════════════════════════════════════════════════

RESUME = pd.Timestamp("2026-06-10 15:13:00", tz="UTC")
NOW = pd.Timestamp("2026-06-10 15:18:00", tz="UTC")


def _frame(rows, start="2026-06-10 14:50:00", freq="1min"):
    idx = pd.date_range(start, periods=len(rows), freq=freq, tz="UTC")
    return pd.DataFrame(
        [{"Open": o, "High": h, "Low": low, "Close": c, "Volume": v}
         for o, h, low, c, v in rows],
        index=idx,
    )


def _base_rows(n=23):
    """Quiet pre-halt drift ending at the bar BEFORE the resume."""
    rows = []
    px = 1.50
    for _ in range(n):
        rows.append((px, px + 0.01, px - 0.01, px + 0.005, 60_000))
        px += 0.003
    return rows


def _firing_rows(resume_open: float):
    """A clean pop→dip→stabilize→reclaim that FIRES the base trigger, with the FIRST
    post-resume bar's OPEN pinned to ``resume_open`` (the value compared against halt_level)."""
    rows = _base_rows()
    rows += [
        (resume_open, 1.78, 1.58, 1.74, 900_000),  # 15:13 resume pop -> ref high 1.78
        (1.73, 1.74, 1.64, 1.66, 500_000),         # 15:14 the dip (1.78 -> 1.64 ~ 7.9%)
        (1.66, 1.70, 1.65, 1.69, 450_000),         # 15:15 stabilizing
        (1.69, 1.75, 1.68, 1.74, 700_000),         # 15:16 RECLAIM (holds dip low, strong close)
    ]
    return rows


def _call(df, **kw):
    return halt_resume_dip_trigger(
        df, entry_interval="1m", halt_resumed_at_utc=RESUME, now=NOW, **kw)


# ── EACH-FLAG PARITY: both halt-direction flags OFF ⇒ halt_level NEVER read (byte-identical) ─

def test_both_flags_off_halt_level_ignored_byte_identical():
    # resume_open 1.30 is WELL BELOW halt_level 1.60 — but with BOTH flags OFF (production
    # default) the whole halt-direction block is skipped: no false-halt veto, no annotation.
    df = _frame(_firing_rows(resume_open=1.30))
    with mock.patch.object(eg.settings, "chili_momentum_false_halt_avoid_enabled", False), \
         mock.patch.object(eg.settings, "chili_momentum_halt_resumption_direction_enabled", False):
        ok, reason, dbg = _call(df, halt_level=1.60)
    assert ok is True
    assert reason == "halt_resume_dip_ok"
    # NO halt-direction keys were written (block fully skipped) ⇒ byte-identical path.
    assert "halt_level" not in dbg
    assert "resumption_direction" not in dbg
    assert "resumption_size_mult" not in dbg
    assert "false_halt" not in dbg


def test_halt_level_none_skips_block_even_with_flag_on():
    # halt_level None ⇒ the block is skipped even when a flag is ON (nothing to compare).
    df = _frame(_firing_rows(resume_open=1.60))
    with mock.patch.object(eg.settings, "chili_momentum_halt_resumption_direction_enabled", True):
        ok, reason, dbg = _call(df, halt_level=None)
    assert ok is True and reason == "halt_resume_dip_ok"
    assert "resumption_direction" not in dbg
    assert "resumption_size_mult" not in dbg


# ── RESUMPTION DIRECTION (GAP 2) conviction modifier: higher / lower / flat ────────────────

def test_resumption_higher_boosts_size_no_veto():
    # resume_open 1.70 > halt_level 1.60 ⇒ 'higher' ⇒ +boost; the trigger still FIRES (the
    # modifier is size-only, it never gates).
    df = _frame(_firing_rows(resume_open=1.70))
    with mock.patch.object(eg.settings, "chili_momentum_halt_resumption_direction_enabled", True), \
         mock.patch.object(eg.settings, "chili_momentum_halt_resumption_boost_frac", 0.15):
        ok, reason, dbg = _call(df, halt_level=1.60)
    assert ok is True and reason == "halt_resume_dip_ok"
    assert dbg["resumption_direction"] == "higher"
    assert dbg["resumption_size_mult"] == pytest.approx(1.15)  # 1 + 0.15
    assert dbg["halt_level"] == pytest.approx(1.60)
    assert dbg["resumption_open"] == pytest.approx(1.70)


def test_resumption_lower_penalizes_size_no_veto_when_only_direction_flag():
    # resume_open 1.50 < halt_level 1.60 ⇒ 'lower' ⇒ -penalty. With ONLY the direction flag on
    # (false-halt flag OFF) this is a size penalty, NOT a veto — the trigger still FIRES.
    df = _frame(_firing_rows(resume_open=1.50))
    with mock.patch.object(eg.settings, "chili_momentum_halt_resumption_direction_enabled", True), \
         mock.patch.object(eg.settings, "chili_momentum_false_halt_avoid_enabled", False), \
         mock.patch.object(eg.settings, "chili_momentum_halt_resumption_boost_frac", 0.15):
        ok, reason, dbg = _call(df, halt_level=1.60)
    assert ok is True, (reason, dbg)
    assert reason == "halt_resume_dip_ok"
    assert dbg["resumption_direction"] == "lower"
    assert dbg["resumption_size_mult"] == pytest.approx(0.85)  # 1 - 0.15


def test_resumption_flat_neutral_mult():
    # resume_open == halt_level (within the 1e-9 band) ⇒ 'flat' ⇒ mult 1.0 (no change).
    df = _frame(_firing_rows(resume_open=1.60))
    with mock.patch.object(eg.settings, "chili_momentum_halt_resumption_direction_enabled", True), \
         mock.patch.object(eg.settings, "chili_momentum_halt_resumption_boost_frac", 0.15):
        ok, reason, dbg = _call(df, halt_level=1.60)
    assert ok is True and reason == "halt_resume_dip_ok"
    assert dbg["resumption_direction"] == "flat"
    assert dbg["resumption_size_mult"] == pytest.approx(1.0)


def test_boost_frac_clamped_to_half():
    # An absurd boost_frac of 5.0 must be CLAMPED to 0.5 (bounded conviction); a 'higher'
    # resume then yields mult 1.5, never an unbounded boost.
    df = _frame(_firing_rows(resume_open=1.70))
    with mock.patch.object(eg.settings, "chili_momentum_halt_resumption_direction_enabled", True), \
         mock.patch.object(eg.settings, "chili_momentum_halt_resumption_boost_frac", 5.0):
        ok, _, dbg = _call(df, halt_level=1.60)
    assert ok is True
    assert dbg["resumption_size_mult"] == pytest.approx(1.5)  # 1 + clamp(5.0)->0.5


def test_boost_frac_negative_clamped_to_zero():
    # A negative boost_frac clamps to 0.0 ⇒ a 'higher' resume yields a NEUTRAL 1.0 (the modifier
    # can never invert into a penalty-on-strength via a bad config value).
    df = _frame(_firing_rows(resume_open=1.70))
    with mock.patch.object(eg.settings, "chili_momentum_halt_resumption_direction_enabled", True), \
         mock.patch.object(eg.settings, "chili_momentum_halt_resumption_boost_frac", -0.30):
        ok, _, dbg = _call(df, halt_level=1.60)
    assert ok is True
    assert dbg["resumption_size_mult"] == pytest.approx(1.0)


# ── FALSE-HALT AVOID (GAP 3): a WEAK (lower) resume under the false-halt flag ⇒ no-fire ────

def test_false_halt_weak_resume_vetoes():
    # resume_open 1.50 < halt_level 1.60 ⇒ a limit-up halt that resumed BELOW where it halted
    # = a false halt. With the false-halt flag ON the trigger NO-FIRES (risk-reduction), and
    # it does so BEFORE the dip/reclaim structure is even evaluated.
    df = _frame(_firing_rows(resume_open=1.50))
    with mock.patch.object(eg.settings, "chili_momentum_false_halt_avoid_enabled", True):
        ok, reason, dbg = _call(df, halt_level=1.60)
    assert ok is False
    assert reason == "resume_dip_false_halt_resume_weak"
    assert dbg["false_halt"] is True
    assert dbg["resumption_direction"] == "lower"


def test_false_halt_flag_does_not_veto_a_strong_resume():
    # The false-halt flag is risk-reducing: a HIGHER (strong) resume is NOT a false halt, so it
    # must NOT be vetoed even with the flag ON (proves the veto is direction-specific, not a
    # blanket block whenever the flag is on + halt_level present).
    df = _frame(_firing_rows(resume_open=1.70))
    with mock.patch.object(eg.settings, "chili_momentum_false_halt_avoid_enabled", True):
        ok, reason, _ = _call(df, halt_level=1.60)
    assert ok is True and reason == "halt_resume_dip_ok"


def test_false_halt_flat_resume_not_vetoed():
    # An EXACTLY-flat resume (open == halt_level) is 'flat', not 'lower' ⇒ NOT a false halt ⇒
    # no veto (boundary between flat and the weak-resume veto).
    df = _frame(_firing_rows(resume_open=1.60))
    with mock.patch.object(eg.settings, "chili_momentum_false_halt_avoid_enabled", True):
        ok, reason, _ = _call(df, halt_level=1.60)
    assert ok is True and reason == "halt_resume_dip_ok"


def test_false_halt_eps_below_halt_level_vetoes():
    # eps-BELOW the halt level (1.60 - 1e-6) is strictly lower than the 1e-9 flat band ⇒ a
    # weak resume ⇒ veto. Pins the lower edge of the false-halt predicate.
    df = _frame(_firing_rows(resume_open=1.60 - 1e-6))
    with mock.patch.object(eg.settings, "chili_momentum_false_halt_avoid_enabled", True):
        ok, reason, _ = _call(df, halt_level=1.60)
    assert ok is False
    assert reason == "resume_dip_false_halt_resume_weak"


# ── HALT-DIRECTION block is fail-OPEN: a non-positive / bad halt_level annotates nothing ──

def test_nonpositive_halt_level_skips_block():
    # halt_level <= 0 ⇒ `_hl > 0` is False ⇒ the comparison block is skipped (no annotation,
    # no veto) and the base trigger fires unchanged. Proves the >0 guard.
    df = _frame(_firing_rows(resume_open=1.50))
    with mock.patch.object(eg.settings, "chili_momentum_false_halt_avoid_enabled", True), \
         mock.patch.object(eg.settings, "chili_momentum_halt_resumption_direction_enabled", True):
        ok, reason, dbg = _call(df, halt_level=0.0)
    assert ok is True and reason == "halt_resume_dip_ok"
    assert "resumption_direction" not in dbg
    assert "false_halt" not in dbg


def test_bad_halt_level_type_fails_open():
    # A non-coercible halt_level raises inside the try and is swallowed (fail-OPEN): the base
    # trigger still fires, no annotation, no spurious veto.
    df = _frame(_firing_rows(resume_open=1.70))
    with mock.patch.object(eg.settings, "chili_momentum_halt_resumption_direction_enabled", True):
        ok, reason, dbg = _call(df, halt_level="not-a-number")  # type: ignore[arg-type]
    assert ok is True and reason == "halt_resume_dip_ok"
    assert "resumption_direction" not in dbg


# ── DIRECTION-flag annotation does NOT itself gate (a 'lower' read with dir-flag still fires) ─

def test_direction_flag_lower_annotates_but_fires_without_false_halt_flag():
    # Belt-and-suspenders on the GAP2-vs-GAP3 split: 'lower' with ONLY the direction flag is a
    # size penalty (fires); the VETO requires the SEPARATE false-halt flag. If this regressed
    # to vetoing on the direction flag alone, this test fails.
    df = _frame(_firing_rows(resume_open=1.40))
    with mock.patch.object(eg.settings, "chili_momentum_halt_resumption_direction_enabled", True), \
         mock.patch.object(eg.settings, "chili_momentum_false_halt_avoid_enabled", False):
        ok, reason, dbg = _call(df, halt_level=1.60)
    assert ok is True, (reason, dbg)
    assert reason == "halt_resume_dip_ok"
    assert dbg["resumption_direction"] == "lower"
    assert dbg["resumption_size_mult"] < 1.0  # a penalty, not a veto

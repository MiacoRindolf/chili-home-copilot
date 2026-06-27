"""ADD-INTO-HALT (GAP 6, RISKIEST) — adversarial chase-guard + halt-family-context unit tests.

ADD-INTO-HALT is LOSS-SENSITIVE: you cannot exit a halted name, so a bad pyramid ADD while
the stock is HALTED LIMIT-UP is dangerous. The hardened ``add_into_halt_ok`` predicate must
therefore carry ALL of these (each fail-CLOSED), and each test below proves a guard BLOCKS a
bad add (the test FAILS if a guard regresses to letting the add through):

  * TAPE REQUIRED + fail-closed     — no add without an explicit tape confirmation
  * EXTENSION VETO / not-parabolic  — no add into an extended / blow-off top
  * NOT-BACKSIDE / above-VWAP       — no add on the backside / below VWAP
  * STRUCTURAL STOP on the add      — the favorable case returns the intact stop
  * HALT-CHAIN risk gate            — no add into an extended consecutive-halt-up blow-off
  * HALT-RESUMPTION direction       — no add when the resume is unfavorable (below halt level)
  * FALSE-HALT avoid                — no add on a weak / false-halt resume

A clean FAVORABLE case (all guards pass, in profit, tape present, front-side, chain < block,
resume strong) fires WITH a structural stop. The master flag ``chili_momentum_add_into_halt_
enabled`` default OFF ⇒ no add (byte-identical) regardless of every other input.

Pure-logic + no DB. The production default for the master flag stays OFF: these tests pass a
LOCAL settings stub with the flag ON to exercise the guard logic — they never mutate the
global ``settings`` and never enable the flag in the real config.

Run (operator): conda run -n chili-env pytest tests/test_momentum_add_into_halt.py -v
with TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test
"""

from __future__ import annotations

import pandas as pd

from app.config import settings as _real_settings
from app.services.trading.momentum_neural.entry_gates import add_into_halt_ok


class _SettingsStub:
    """Delegates to the real settings for every key EXCEPT the explicit overrides, so each
    test pins only the flags it needs while every other default matches production. Never
    mutates the global settings ⇒ the production flag stays OFF outside the test."""

    def __init__(self, **overrides):
        self._ov = dict(overrides)

    def __getattr__(self, name):  # only called on miss of an instance attr
        if name in self._ov:
            return self._ov[name]
        return getattr(_real_settings, name)


def _on(**extra) -> _SettingsStub:
    """A settings stub with the master add-into-halt flag ON (so guard legs are reachable)
    plus any extra flag overrides the test needs. The master flag is ON ONLY on the LOCAL
    stub — the global production settings.chili_momentum_add_into_halt_enabled stays OFF."""
    base = {"chili_momentum_add_into_halt_enabled": True}
    base.update(extra)
    return _SettingsStub(**base)


# ── df builders ──────────────────────────────────────────────────────────────────────────

def _frontside_df(n: int = 30, start: float = 8.0, step: float = 0.05) -> pd.DataFrame:
    """A clean RISING intraday frame: ema_9 stays above ema_20 (not backside) and the close
    holds above the session VWAP (front-side). Datetime-indexed (single session)."""
    closes = [start + step * i for i in range(n)]
    idx = pd.date_range("2026-06-24 13:30", periods=n, freq="1min", tz="UTC")
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c * 1.001 for c in closes],
            "Low": [c * 0.999 for c in closes],
            "Close": closes,
            "Volume": [1000] * n,
        },
        index=idx,
    )


def _backside_df() -> pd.DataFrame:
    """A popped-then-faded-BELOW-VWAP frame (SAGT-shape): front_side_state => is_backside."""
    closes = [10.0, 13.0, 15.0, 14.0, 12.0, 11.0, 10.5, 10.2, 10.0, 9.8, 9.6, 9.5]
    idx = pd.date_range("2026-06-24 13:30", periods=len(closes), freq="1min", tz="UTC")
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c * 1.001 for c in closes],
            "Low": [c * 0.999 for c in closes],
            "Close": closes,
            "Volume": [1000] * len(closes),
        },
        index=idx,
    )


# Shared FAVORABLE baseline: in profit by +2R, stop intact, front-side, tape lifting, a near
# break level (not extended), chain below block, resume strong (above halt level).
_FAV = dict(
    avg_entry=10.0,
    original_stop=9.0,           # R = 1.0
    current_stop=9.5,            # tightened (>= original) -> intact
    bid=12.0,                    # +2R in the green
    is_limit_up_halt=True,
    in_rth=True,
    tape_confirmed=True,
    breakout_level=11.9,         # bid 12.0 just above -> NOT extended (cap ~0.10)
    atr_pct=0.02,
    consecutive_halt_up_count=1,
    halt_level=11.0,
    resumption_open=11.5,        # >= halt_level -> favorable resume
)


# ── (1) CLEAN FAVORABLE → add FIRES with a structural stop ─────────────────────────────────

def test_favorable_case_fires_with_stop():
    ok, reason, dbg = add_into_halt_ok(df=_frontside_df(), settings_obj=_on(), **_FAV)
    assert ok is True, (reason, dbg)
    assert reason == "add_into_halt_ok"
    # the added shares carry a defined structural stop (the intact / tightened live stop).
    assert dbg.get("add_structural_stop") == 9.5


# ── (2) TAPE REQUIRED + fail-CLOSED ────────────────────────────────────────────────────────

def test_no_tape_blocks_add():
    args = dict(_FAV)
    args["tape_confirmed"] = None  # no tape read -> fail-closed
    ok, reason, _ = add_into_halt_ok(df=_frontside_df(), settings_obj=_on(), **args)
    assert ok is False
    assert reason == "add_into_halt_no_tape"


def test_tape_not_lifting_blocks_add():
    args = dict(_FAV)
    args["tape_confirmed"] = False  # tape present but NOT lifting -> no add
    ok, reason, _ = add_into_halt_ok(df=_frontside_df(), settings_obj=_on(), **args)
    assert ok is False
    assert reason == "add_into_halt_no_tape"


# ── (3) EXTENSION VETO / NOT-PARABOLIC ─────────────────────────────────────────────────────

def test_extended_parabolic_blocks_add():
    args = dict(_FAV)
    # bid 12.0 vs a FAR-below break level 7.6 (+58%) -> a blow-off top -> veto.
    args["breakout_level"] = 7.6
    ok, reason, _ = add_into_halt_ok(df=_frontside_df(), settings_obj=_on(), **args)
    assert ok is False
    assert reason == "add_into_halt_extended"


def test_missing_extension_inputs_fail_closed():
    args = dict(_FAV)
    args["breakout_level"] = None  # cannot prove not-extended -> fail-closed
    ok, reason, _ = add_into_halt_ok(df=_frontside_df(), settings_obj=_on(), **args)
    assert ok is False
    assert reason == "add_into_halt_no_extension_inputs"


# ── (4) NOT-BACKSIDE / BELOW-VWAP ──────────────────────────────────────────────────────────

def test_backside_below_vwap_blocks_add():
    # A faded-below-VWAP frame -> front_side_state.is_backside -> no add. The profit (+1.33R)
    # and extension (bid just above the break) legs are made to PASS so the backside leg is
    # the one that blocks (proves the not-backside guard, not a different leg).
    args = dict(_FAV)
    args["avg_entry"] = 9.0
    args["original_stop"] = 8.7    # R = 0.3
    args["current_stop"] = 8.8
    args["bid"] = 9.4              # +1.33R in the green (passes profit)
    args["breakout_level"] = 9.35  # bid 9.4 just above -> not extended
    ok, reason, _ = add_into_halt_ok(df=_backside_df(), settings_obj=_on(), **args)
    assert ok is False
    assert reason in ("add_into_halt_back_side", "add_into_halt_backside_lifecycle")


def test_missing_structure_df_fail_closed():
    ok, reason, _ = add_into_halt_ok(df=None, settings_obj=_on(), **_FAV)
    assert ok is False
    assert reason == "add_into_halt_no_structure"


# ── (5) HALT-CHAIN blow-off (reuse halt_chain_risk_gate) ───────────────────────────────────

def test_halt_chain_blowoff_blocks_add():
    args = dict(_FAV)
    args["consecutive_halt_up_count"] = 3  # at/above the default block_count(3)
    s = _on(chili_momentum_halt_chain_risk_gate_enabled=True)
    ok, reason, _ = add_into_halt_ok(df=_frontside_df(), settings_obj=s, **args)
    assert ok is False
    assert reason == "add_into_halt_halt_chain_blocked"


def test_halt_chain_blocks_under_master_with_subflag_off():
    # FAIL-CLOSED-UNDER-MASTER: an over-extended chain (count >= block_count) is refused even
    # when the standalone halt-chain risk-gate SUB-flag is OFF, because add-into-halt's H1 leg
    # is self-sufficient under the MASTER flag. (Was the bug: the old code reused the sub-flag-
    # gated halt_chain_risk_gate, so a sub-flag-OFF lane silently failed OPEN on the chain.)
    args = dict(_FAV)
    args["consecutive_halt_up_count"] = 5  # well above the default block_count(3)
    ok, reason, _ = add_into_halt_ok(df=_frontside_df(), settings_obj=_on(), **args)
    assert ok is False, reason
    assert reason == "add_into_halt_halt_chain_blocked"


def test_clean_chain_below_block_still_fires_under_master():
    # The chain leg only REFUSES at/above the block count — a clean below-block chain (count 1)
    # with every other leg favorable still fires, sub-flags OFF.
    args = dict(_FAV)
    args["consecutive_halt_up_count"] = 1
    ok, reason, _ = add_into_halt_ok(df=_frontside_df(), settings_obj=_on(), **args)
    assert ok is True, reason
    assert reason == "add_into_halt_ok"


# ── (6) UNFAVORABLE / FALSE-HALT RESUMPTION (reuse the halt-family direction flags) ────────

def test_unfavorable_resumption_blocks_add():
    args = dict(_FAV)
    args["resumption_open"] = 10.5   # below halt_level 11.0 -> unfavorable resume
    s = _on(chili_momentum_halt_resumption_direction_enabled=True)
    ok, reason, _ = add_into_halt_ok(df=_frontside_df(), settings_obj=s, **args)
    assert ok is False
    assert reason == "add_into_halt_unfavorable_resumption"


def test_false_halt_weak_resume_blocks_add():
    args = dict(_FAV)
    args["resumption_open"] = 9.2    # well below halt_level 11.0 -> false / weak halt
    s = _on(chili_momentum_false_halt_avoid_enabled=True)
    ok, reason, _ = add_into_halt_ok(df=_frontside_df(), settings_obj=s, **args)
    assert ok is False
    assert reason == "add_into_halt_unfavorable_resumption"


def test_missing_resumption_with_flag_on_fail_closed():
    args = dict(_FAV)
    args["resumption_open"] = None   # flag ON + halt_level present but no resume read
    s = _on(chili_momentum_false_halt_avoid_enabled=True)
    ok, reason, _ = add_into_halt_ok(df=_frontside_df(), settings_obj=s, **args)
    assert ok is False
    assert reason == "add_into_halt_no_resumption"


# ── (6b) FAIL-CLOSED UNDER MASTER — halt-family SUB-flags OFF, master ON ───────────────────
# THE FIX under test: add-into-halt is loss-sensitive, so its halt-family context (H1 chain /
# H2 resumption-direction / H3 false-halt) must self-enforce DIRECTLY on the raw signals under
# the MASTER flag, INDEPENDENT of the three Cluster-A sub-flags. With ALL three sub-flags OFF
# (their production default) but the master ON, every bad halt-context input STILL yields NO
# add. Before the fix, H1 reused the sub-flag-gated chain gate and H2/H3 were skipped entirely
# when their sub-flags were OFF — a silent fail-OPEN into a halt the lane could not exit.

_SUBFLAGS_OFF = dict(
    chili_momentum_halt_chain_risk_gate_enabled=False,
    chili_momentum_halt_resumption_direction_enabled=False,
    chili_momentum_false_halt_avoid_enabled=False,
)


def test_master_on_subflags_off_halt_chain_blowoff_still_blocks():
    args = dict(_FAV)
    args["consecutive_halt_up_count"] = 4  # >= default block_count(3)
    ok, reason, _ = add_into_halt_ok(df=_frontside_df(), settings_obj=_on(**_SUBFLAGS_OFF), **args)
    assert ok is False, reason
    assert reason == "add_into_halt_halt_chain_blocked"


def test_master_on_subflags_off_unfavorable_resumption_still_blocks():
    args = dict(_FAV)
    args["resumption_open"] = 10.5  # below halt_level 11.0 -> unfavorable resume
    ok, reason, _ = add_into_halt_ok(df=_frontside_df(), settings_obj=_on(**_SUBFLAGS_OFF), **args)
    assert ok is False, reason
    assert reason == "add_into_halt_unfavorable_resumption"


def test_master_on_subflags_off_false_halt_weak_resume_still_blocks():
    args = dict(_FAV)
    args["resumption_open"] = 9.2  # well below halt_level 11.0 -> false / weak halt
    ok, reason, _ = add_into_halt_ok(df=_frontside_df(), settings_obj=_on(**_SUBFLAGS_OFF), **args)
    assert ok is False, reason
    assert reason == "add_into_halt_unfavorable_resumption"


def test_master_on_subflags_off_missing_resumption_fail_closed():
    args = dict(_FAV)
    args["resumption_open"] = None  # halt_level present but no resume read -> fail-closed
    ok, reason, _ = add_into_halt_ok(df=_frontside_df(), settings_obj=_on(**_SUBFLAGS_OFF), **args)
    assert ok is False, reason
    assert reason == "add_into_halt_no_resumption"


def test_master_on_subflags_off_missing_halt_level_fail_closed():
    args = dict(_FAV)
    args["halt_level"] = None  # no halt signal at all -> cannot prove the resume -> fail-closed
    ok, reason, _ = add_into_halt_ok(df=_frontside_df(), settings_obj=_on(**_SUBFLAGS_OFF), **args)
    assert ok is False, reason
    assert reason == "add_into_halt_no_halt_signal"


def test_master_on_subflags_off_favorable_still_fires():
    # The fix only TIGHTENS: a fully-favorable halt context (chain below block, resume above the
    # halt level, halt signals present) STILL fires with the three sub-flags OFF.
    ok, reason, dbg = add_into_halt_ok(df=_frontside_df(), settings_obj=_on(**_SUBFLAGS_OFF), **_FAV)
    assert ok is True, (reason, dbg)
    assert reason == "add_into_halt_ok"


# ── (7) PROFIT / STRUCTURE invariants (the original GAP-6 legs still hold) ──────────────────

def test_underwater_blocks_add():
    args = dict(_FAV)
    args["bid"] = 10.0  # +0R, below the +1R min -> never add underwater/flat
    ok, reason, _ = add_into_halt_ok(df=_frontside_df(), settings_obj=_on(), **args)
    assert ok is False
    assert reason == "add_into_halt_insufficient_profit"


def test_loosened_stop_blocks_add():
    args = dict(_FAV)
    args["current_stop"] = 8.5  # below the original 9.0 -> structure changed -> refuse
    ok, reason, _ = add_into_halt_ok(df=_frontside_df(), settings_obj=_on(), **args)
    assert ok is False
    assert reason == "add_into_halt_stop_loosened"


def test_limit_down_halt_blocks_add():
    args = dict(_FAV)
    args["is_limit_up_halt"] = False  # a limit-DOWN halt is never added into
    ok, reason, _ = add_into_halt_ok(df=_frontside_df(), settings_obj=_on(), **args)
    assert ok is False
    assert reason == "add_into_halt_not_limit_up"


# ── (8) FLAG OFF → no add (byte-identical), regardless of every other input ────────────────

def test_flag_off_no_add_byte_identical():
    # The production default: master flag OFF -> disabled BEFORE any compute -> no add, even
    # on the otherwise-favorable case. (settings_obj defaults to the global production
    # settings, where chili_momentum_add_into_halt_enabled is False.)
    ok, reason, dbg = add_into_halt_ok(df=_frontside_df(), **_FAV)
    assert ok is False
    assert reason == "add_into_halt_disabled"
    assert dbg == {}


def test_flag_off_on_every_adversarial_case():
    # With the flag OFF, NONE of the adversarial inputs matter — always disabled, never an add.
    for mutate in (
        {"tape_confirmed": None},
        {"breakout_level": 7.6},
        {"consecutive_halt_up_count": 9},
        {"resumption_open": 5.0},
        {"current_stop": 1.0},
    ):
        args = dict(_FAV)
        args.update(mutate)
        ok, reason, _ = add_into_halt_ok(df=_backside_df(), **args)
        assert ok is False
        assert reason == "add_into_halt_disabled"

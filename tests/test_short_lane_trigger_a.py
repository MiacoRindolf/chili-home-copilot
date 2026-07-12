"""SHORT LANE P1 — Trigger A (parabolic-exhaustion) pure-function tests.

The detector is a confluence-AND over precomputed signals, so these tests need no
market data, no DB, and no network: they pin the gate semantics — every leg must
hold, OFI fails CLOSED on missing data, and the master/per-trigger flags make the
long-only lane byte-identical when off.
"""

from types import SimpleNamespace

from app.services.trading.momentum_neural.entry_gates import (
    parabolic_exhaustion_short_signal,
)

_ON = SimpleNamespace(
    chili_momentum_short_enabled=True,
    chili_momentum_short_trigger_parabolic_enabled=True,
)
_OFF = SimpleNamespace(
    chili_momentum_short_enabled=False,
    chili_momentum_short_trigger_parabolic_enabled=True,
)

_FULL = dict(
    extension_ok=True,
    topping_tail=True,
    macd_rollover=False,
    rolled_over=True,
    ofi_level=-1200.0,
    ofi_slope=-300.0,
)


def test_full_confluence_fires():
    ok, dbg = parabolic_exhaustion_short_signal(settings=_ON, **_FULL)
    assert ok is True
    assert dbg["reason"] == "parabolic_exhaustion_confirmed"


def test_master_gate_off_is_dark():
    ok, dbg = parabolic_exhaustion_short_signal(settings=_OFF, **_FULL)
    assert ok is False
    assert dbg["reason"] == "short_lane_disabled"


def test_trigger_flag_off_is_dark():
    s = SimpleNamespace(
        chili_momentum_short_enabled=True,
        chili_momentum_short_trigger_parabolic_enabled=False,
    )
    ok, dbg = parabolic_exhaustion_short_signal(settings=s, **_FULL)
    assert ok is False
    assert dbg["reason"] == "trigger_disabled"


def test_not_extended_never_shorts():
    ok, dbg = parabolic_exhaustion_short_signal(
        settings=_ON, **{**_FULL, "extension_ok": False}
    )
    assert ok is False
    assert dbg["reason"] == "not_extended"


def test_macd_rollover_substitutes_for_topping_tail():
    ok, dbg = parabolic_exhaustion_short_signal(
        settings=_ON, **{**_FULL, "topping_tail": False, "macd_rollover": True}
    )
    assert ok is True


def test_no_climax_print_blocks():
    ok, dbg = parabolic_exhaustion_short_signal(
        settings=_ON, **{**_FULL, "topping_tail": False, "macd_rollover": False}
    )
    assert ok is False
    assert dbg["reason"] == "no_climax_print"


def test_still_printing_hods_never_shorted():
    # The anti-chase guard: no confirmed lower-high = the squeeze that kills shorts.
    ok, dbg = parabolic_exhaustion_short_signal(
        settings=_ON, **{**_FULL, "rolled_over": False}
    )
    assert ok is False
    assert dbg["reason"] == "no_confirmed_lower_high"


def test_missing_ofi_fails_closed():
    ok, dbg = parabolic_exhaustion_short_signal(
        settings=_ON, **{**_FULL, "ofi_level": None}
    )
    assert ok is False
    assert dbg["reason"] == "ofi_unavailable_fail_closed"


def test_positive_ofi_blocks():
    ok, dbg = parabolic_exhaustion_short_signal(
        settings=_ON, **{**_FULL, "ofi_level": 500.0, "ofi_slope": -10.0}
    )
    assert ok is False
    assert dbg["reason"] == "ofi_not_flipped"


def test_decelerating_but_positive_slope_blocks():
    ok, dbg = parabolic_exhaustion_short_signal(
        settings=_ON, **{**_FULL, "ofi_level": -500.0, "ofi_slope": 10.0}
    )
    assert ok is False
    assert dbg["reason"] == "ofi_not_flipped"

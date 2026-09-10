"""[58] The near-high give-back band of ``tape_accel_reversal_exit`` is DERIVED and REPORTED.

Before this change the band was an undocumented literal (``0.35``, "the ONE new knob"). It is
now ``ACCEL_REVERSAL_GIVEBACK_BAND_R`` — the p90 of the give-back ``(H − P) / risk_dist`` at the
REAL accel rollover while above entry (n = 27 rollovers over 78 live Alpaca legs, 14 d to
2026-09-10, print-indexed on the executed tape, risk_dist in the helper's OWN unit) — and every
armed return of the helper carries the value that decided:

    giveback_r       (hwm − bid) / risk_dist          the leg's give-back at this tick
    giveback_band_r  the band in R                     what it was compared against
    binding          the named derivation, or "env override" when settings differ

These are PURE-LOGIC tests plus two SOURCE PINS: (1) the config default must equal the
constant (config.py cannot import the service layer, so the literal is pinned by test), and
(2) the live receipt ``live_tape_accel_reversal_exit`` in live_runner.py must carry the three
keys — a receipt that stops reporting the binding would fail here, not silently in prod.
"""

from __future__ import annotations

import ast
import pathlib
import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.trading.momentum_neural.paper_execution import (
    ACCEL_REVERSAL_GIVEBACK_BAND_R,
    ACCEL_REVERSAL_GIVEBACK_BINDING,
    tape_accel_reversal_exit,
)

_PE = "app.services.trading.momentum_neural.paper_execution"
_REPO = pathlib.Path(__file__).resolve().parents[1]

# Same winner geometry as test_momentum_tape_accel_reversal_exit: risk_dist = 10 * 0.012 = 0.12,
# arm_r = 1.0R, hwm 10.30 -> peak_r 2.5R. The give-back in R is set by the bid.
_ENTRY = 10.0
_ATR_PCT = 0.02
_SM = 0.60
_RISK = 0.12
_HWM = 10.30
_RR = 2.0
_CUR_STOP = 9.90
_BE = 10.0
_BAND = ACCEL_REVERSAL_GIVEBACK_BAND_R


def _bid_for_giveback_r(giveback_r: float) -> float:
    """The bid that gives back exactly ``giveback_r`` R from the high."""
    return _HWM - giveback_r * _RISK


def _settings(band: float = _BAND) -> SimpleNamespace:
    return SimpleNamespace(
        chili_momentum_exit_ofi_arm_frac=0.5,
        chili_momentum_exit_ofi_base_lock_bps=120.0,
        chili_momentum_exit_accel_reversal_giveback_frac=band,
    )


def _call(settings: SimpleNamespace | None = None, **over):
    base = dict(
        high_water_mark=_HWM,
        entry_price=_ENTRY,
        bid=_bid_for_giveback_r(0.10),
        atr_pct=_ATR_PCT,
        stop_atr_mult=_SM,
        reward_risk=_RR,
        current_stop=_CUR_STOP,
        breakeven_floor=_BE,
        signed_tape_accel=-5.0,       # turned net-negative
        prev_signed_tape_accel=20.0,  # was pushing up -> genuine TURN
        side_long=True,
    )
    base.update(over)
    with patch(f"{_PE}.settings", settings or _settings()):
        return tape_accel_reversal_exit(**base)


# ───────────────────────────── the band decides gate 3 ──────────────────────────────────

def test_constant_is_the_named_p90():
    """The constant is the p90 of the measured distribution (0.393 R, n=27) and the binding
    string names it — a change to either must be a re-derivation, not a tweak."""
    assert _BAND == pytest.approx(0.393, abs=1e-9)
    assert "p90" in ACCEL_REVERSAL_GIVEBACK_BINDING
    assert "n=27" in ACCEL_REVERSAL_GIVEBACK_BINDING
    assert "2026-09-10" in ACCEL_REVERSAL_GIVEBACK_BINDING


def test_rollover_within_band_fires():
    """A give-back inside the band (band − 0.05 R) at a genuine turn on a winner FIRES."""
    out = _call(bid=_bid_for_giveback_r(_BAND - 0.05))
    assert out["armed"] is True
    assert out["fired"] is True, out
    assert out["trigger"] == "tape_accel_reversal"
    assert out["reason"] == "fired"
    assert out["giveback_r"] == pytest.approx(_BAND - 0.05, abs=1e-3)
    assert out["giveback_band_r"] == pytest.approx(_BAND, abs=1e-9)


def test_rollover_beyond_band_is_the_trails_job():
    """A give-back beyond the band (band + 0.05 R) returns gave_back_too_much and the stop
    is untouched (Invariant A: never loosen, and no raise either)."""
    out = _call(bid=_bid_for_giveback_r(_BAND + 0.05))
    assert out["armed"] is True
    assert out["fired"] is False
    assert out["reason"] == "gave_back_too_much"
    assert out["new_stop_floor"] == _CUR_STOP
    assert out["giveback_r"] == pytest.approx(_BAND + 0.05, abs=1e-3)
    assert out["giveback_band_r"] == pytest.approx(_BAND, abs=1e-9)


def test_band_edge_is_inclusive():
    """Exactly AT the band still counts as near-high (``giveback > band`` is the refusal)."""
    out = _call(bid=_bid_for_giveback_r(_BAND))
    assert out["reason"] in ("fired", "ratchet_no_raise"), out
    assert out["reason"] != "gave_back_too_much"


# ───────────────────────── the receipt reports the binding ──────────────────────────────

@pytest.mark.parametrize(
    "over, expected_reason",
    [
        (dict(bid=_bid_for_giveback_r(0.10)), "fired"),
        (dict(bid=_bid_for_giveback_r(_BAND + 0.20)), "gave_back_too_much"),
        (dict(signed_tape_accel=30.0, prev_signed_tape_accel=10.0), "still_accelerating"),
        (dict(current_stop=10.25, breakeven_floor=10.25), "ratchet_no_raise"),
        (dict(high_water_mark=_ENTRY + 0.05, bid=_ENTRY + 0.04), "below_arm"),
    ],
)
def test_receipt_reports_binding_on_every_resolved_path(over, expected_reason):
    """Once the band is resolved (i.e. after risk_dist is known), EVERY return path carries
    giveback_r / giveback_band_r / binding — fired, refused, still accelerating, no-raise,
    and even below the arm (so the A/B receipt can be read on every tick)."""
    out = _call(**over)
    assert out["reason"] == expected_reason, out
    assert out["giveback_r"] is not None
    assert out["giveback_band_r"] == pytest.approx(_BAND, abs=1e-9)
    assert out["binding"] == ACCEL_REVERSAL_GIVEBACK_BINDING
    assert "p90" in out["binding"]


def test_fail_safe_paths_report_nothing():
    """Before risk_dist exists (no tape / short / non-finite) the band is not resolved, so
    the three keys are present but None — never a fabricated binding."""
    for over in (dict(signed_tape_accel=None), dict(side_long=False),
                 dict(high_water_mark=float("nan"))):
        out = _call(**over)
        assert out["fired"] is False
        assert out["giveback_r"] is None
        assert out["giveback_band_r"] is None
        assert out["binding"] is None


def test_env_override_is_named():
    """A settings value that differs from the constant is REPORTED as 'env override' — a
    differing band can never run as a dark literal."""
    out = _call(settings=_settings(band=0.25))
    assert out["giveback_band_r"] == pytest.approx(0.25, abs=1e-9)
    assert out["binding"] == "env override"
    # and it actually binds: 0.30 R is refused under a 0.25 band, accepted under the constant.
    refused = _call(settings=_settings(band=0.25), bid=_bid_for_giveback_r(0.30))
    assert refused["reason"] == "gave_back_too_much"
    accepted = _call(bid=_bid_for_giveback_r(0.30))
    assert accepted["reason"] == "fired"


def test_zero_band_is_an_honest_override_not_the_default():
    """An env value of 0.0 must NOT silently collapse to the derived default (the old
    ``or 0.35`` did exactly that): it is reported as 'env override' with band 0 and refuses
    every give-back > 0 — visible in the receipt, never a dark fallback."""
    out = _call(settings=_settings(band=0.0), bid=_bid_for_giveback_r(0.01))
    assert out["giveback_band_r"] == 0.0
    assert out["binding"] == "env override"
    assert out["reason"] == "gave_back_too_much"
    # a settings object with NO attribute at all falls back to the derived constant
    bare = SimpleNamespace(chili_momentum_exit_ofi_arm_frac=0.5,
                           chili_momentum_exit_ofi_base_lock_bps=120.0)
    out2 = _call(settings=bare)
    assert out2["giveback_band_r"] == pytest.approx(_BAND, abs=1e-9)
    assert out2["binding"] == ACCEL_REVERSAL_GIVEBACK_BINDING


# ──────────────────────────────── source pins ────────────────────────────────────────────

def test_config_default_equals_the_constant():
    """app/config.py cannot import the service layer, so its literal default is pinned here:
    the pydantic default of chili_momentum_exit_accel_reversal_giveback_frac == the constant."""
    from app.config import Settings

    field = Settings.model_fields["chili_momentum_exit_accel_reversal_giveback_frac"]
    assert field.default == pytest.approx(_BAND, abs=1e-9)
    assert "DERIVED" in (field.description or "")
    assert "ONE new knob" not in (field.description or "")


def test_live_receipt_carries_the_three_keys():
    """The live emit of ``live_tape_accel_reversal_exit`` must report giveback_r,
    giveback_band_r and binding (AST-pinned so a refactor that drops them fails here)."""
    src = (_REPO / "app" / "services" / "trading" / "momentum_neural" / "live_runner.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(src)
    found = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_emit"
            and len(node.args) >= 4
            and isinstance(node.args[2], ast.Constant)
            and node.args[2].value == "live_tape_accel_reversal_exit"
            and isinstance(node.args[3], ast.Dict)
        ):
            found = node.args[3]
            break
    assert found is not None, "live_tape_accel_reversal_exit emit not found"
    keys = {k.value for k in found.keys if isinstance(k, ast.Constant)}
    assert {"giveback_r", "giveback_band_r", "binding"} <= keys, keys


def test_no_stray_035_literal_remains_in_the_helper():
    """The old undocumented literal must not survive as a fallback anywhere in the helper."""
    src = (_REPO / "app" / "services" / "trading" / "momentum_neural" / "paper_execution.py").read_text(
        encoding="utf-8"
    )
    start = src.index("def tape_accel_reversal_exit(")
    end = src.index("\ndef ", start + 1)
    body = src[start:end]
    assert not re.search(r"\b0\.35\b", body), "0.35 literal still present in tape_accel_reversal_exit"

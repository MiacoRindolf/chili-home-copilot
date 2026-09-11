"""[37] ANG PLANO'NG R:R AY MAY IISANG PINAGMUMULAN — at ang fallback ay ang PANALO ng A/B.

ANG DEPEKTO. ``chili_momentum_risk_reward_risk_ratio`` = 2.5 ay ang panalo ng interleaved A/B
#1271 (10 window x 3 arm, 2026-09-01):

        RR 2.0 = +160.47      RR 2.5 = +185.21      RR 3.0 = +151.91      (peak, hindi ramp)

Na-dokumento na ito sa config ng #1393 (c800c6271). Pero ang mga GUMAGAMIT nito ay may WALONG
hubad na ``2.0`` na fallback — pito sa ``paper_execution.py`` (stop_target_prices,
class_aware_reward_risk, cushion_adaptive_trail_stop, ofi_exhaustion_lock,
tape_accel_reversal_exit, ask_side_pressure_lock, sell_into_strength_ladder) at isa sa
``counterfactual_replay.py``. Ang 2.0 ay ang braso na TUMALO. At dahil ``ge=0.0`` ang config,
ang ``CHILI_MOMENTUM_RISK_REWARD_RISK_RATIO=0`` ay PUMAPASA sa validation at TAHIMIK na
nagiging 2.0 sa bawat gumagamit — isang geometry na natalo na, walang resibo.

ANG AYOS. Iisang pinagmumulan: ``paper_execution.plan_reward_risk()`` ay bumabalik sa
``Settings.model_fields[...].default`` (hindi literal). Ang config ay ``gt=0.0`` +
``allow_inf_nan=False``: ang 0 / NaN / inf ay tinatanggihan sa LOAD. At ang pinagmulan ay
iniuulat (``plan_rr_source`` sa ``momentum_mfe_target_applied``; ``counterfactual_reward_risk:``
sa confidence_reasons ng counterfactual).

WALANG LIVE BEHAVIOUR CHANGE: ``momentum_mfe_target_applied`` 2026-09-11 = ``plan_rr 2.5`` sa
24/24 at ``live_tape_accel_reversal_exit`` ``arm_r = 1.25`` (= 0.5 x 2.5) sa 7/7 — ang mga
fallback ay hindi pumuputok ngayon. Ito ay pagsasara ng isang tahimik na daan, hindi pagbabago
ng antas.

Runnable: pytest tests/test_plan_reward_risk_single_source.py -v
"""
from __future__ import annotations

import ast
import math
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import app.services.trading.momentum_neural.paper_execution as pe
from app.config import Settings
from app.config import settings as real_settings
from app.services.trading.momentum_neural.counterfactual_replay import (
    counterfactual_reward_risk,
)
from app.services.trading.momentum_neural.paper_execution import (
    ask_side_pressure_lock,
    class_aware_reward_risk,
    cushion_adaptive_trail_stop,
    ofi_exhaustion_lock,
    plan_reward_risk,
    plan_reward_risk_source,
    plan_reward_risk_with_source,
    stop_target_prices,
    tape_accel_reversal_exit,
)

_FIELD = "chili_momentum_risk_reward_risk_ratio"
_AB_WINNER = 2.5
_AB_LOSER = 2.0
_SRC_DEFAULT = "ab_1271_interleaved_10x3"
_SRC_FALLBACK = "declared_default_fallback_unreadable_setting"
_SRC_OVERRIDE = "env_override:CHILI_MOMENTUM_RISK_REWARD_RISK_RATIO"
_SRC_CRYPTO = "crypto_class_reward_risk"
_PKG = Path(__file__).resolve().parents[1] / "app/services/trading/momentum_neural"


class _NoPlanRR:
    """Settings stand-in na WALANG plan R:R attribute (ang 'nawawalang attribute' na kaso)."""


_MISSING = object()


def _stand_in(value=_MISSING, **extra):
    if value is _MISSING:
        s = _NoPlanRR()
    else:
        s = SimpleNamespace(**{_FIELD: value})
    for k, v in extra.items():
        setattr(s, k, v)
    return s


# ── ang pinagmumulan ────────────────────────────────────────────────────────────
def test_the_fallback_is_the_declared_field_default_which_is_the_ab_winner():
    declared = Settings.model_fields[_FIELD].default
    assert declared == _AB_WINNER
    assert pe._PLAN_REWARD_RISK_DEFAULT == declared
    # ang tunay na settings (walang env key) ay ang idineklarang default, at sinasabi iyon
    assert plan_reward_risk_with_source() == (_AB_WINNER, _SRC_DEFAULT)
    assert plan_reward_risk() == _AB_WINNER
    assert class_aware_reward_risk("AAPL") == _AB_WINNER
    assert plan_reward_risk_source("AAPL") == _SRC_DEFAULT


@pytest.mark.parametrize(
    "bad",
    [_MISSING, None, 0, 0.0, -1.0, float("nan"), float("inf"), float("-inf"), "abc", ""],
    ids=["missing", "none", "zero_int", "zero", "negative", "nan", "inf", "neg_inf", "str", "empty"],
)
def test_an_unreadable_plan_falls_back_to_the_ab_winner_not_the_loser(monkeypatch, bad):
    monkeypatch.setattr(pe, "settings", _stand_in(bad))
    assert plan_reward_risk() == _AB_WINNER
    assert plan_reward_risk() != _AB_LOSER
    assert plan_reward_risk_with_source() == (_AB_WINNER, _SRC_FALLBACK)
    # class_aware_reward_risk is the equity/crypto accessor every plan consumer reads
    assert class_aware_reward_risk("AAPL") == _AB_WINNER
    assert class_aware_reward_risk(None) == _AB_WINNER
    # ...and the receipt NAMES the fallback instead of reporting it as the A/B value
    assert plan_reward_risk_source("AAPL") == _SRC_FALLBACK


def test_a_real_override_is_kept_and_named(monkeypatch):
    monkeypatch.setattr(pe, "settings", _stand_in(3.0))
    assert plan_reward_risk_with_source() == (3.0, _SRC_OVERRIDE)
    assert class_aware_reward_risk("AAPL") == 3.0
    assert plan_reward_risk_source("AAPL") == _SRC_OVERRIDE


def test_the_crypto_class_is_named_only_when_it_decides(monkeypatch):
    monkeypatch.setattr(
        pe, "settings", _stand_in(_AB_WINNER, chili_momentum_crypto_reward_risk_ratio=3.0)
    )
    assert class_aware_reward_risk("ORCA-USD") == 3.0
    assert plan_reward_risk_source("ORCA-USD") == _SRC_CRYPTO
    assert class_aware_reward_risk("AAPL") == _AB_WINNER
    assert plan_reward_risk_source("AAPL") == _SRC_DEFAULT
    # a crypto override BELOW the plan is clamped up — the global decided, and says so
    monkeypatch.setattr(
        pe, "settings", _stand_in(_AB_WINNER, chili_momentum_crypto_reward_risk_ratio=1.2)
    )
    assert class_aware_reward_risk("ETH-USD") == _AB_WINNER
    assert plan_reward_risk_source("ETH-USD") == _SRC_DEFAULT
    # a cleared crypto override falls back to the global
    monkeypatch.setattr(
        pe, "settings", _stand_in(_AB_WINNER, chili_momentum_crypto_reward_risk_ratio=None)
    )
    assert class_aware_reward_risk("ETH-USD") == _AB_WINNER
    assert plan_reward_risk_source("ETH-USD") == _SRC_DEFAULT


# ── stop_target_prices ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("rr", [None, 0.0, -1.0, float("nan"), float("inf"), "x"])
def test_stop_target_prices_default_and_invalid_rr_is_the_plan(rr):
    """reward_risk=None ⇒ the PLAN. partial_capable=False so the round-number snap is out."""
    entry = 100.0
    stop, target = stop_target_prices(
        entry, atr_pct=0.02, stop_atr_mult=0.6, reward_risk=rr, partial_capable=False,
    )
    assert entry - stop > 0
    assert (target - entry) / (entry - stop) == pytest.approx(_AB_WINNER)


def test_stop_target_prices_short_side_default_is_the_plan():
    entry = 100.0
    stop, target = stop_target_prices(entry, atr_pct=0.02, stop_atr_mult=0.6, side_long=False)
    assert (entry - target) / (stop - entry) == pytest.approx(_AB_WINNER)


def test_stop_target_prices_missing_setting_is_the_plan_not_2(monkeypatch):
    monkeypatch.setattr(pe, "settings", _stand_in())
    entry = 100.0
    stop, target = stop_target_prices(entry, atr_pct=0.02, stop_atr_mult=0.6, partial_capable=False)
    assert (target - entry) / (entry - stop) == pytest.approx(_AB_WINNER)


def test_an_explicit_valid_rr_is_untouched():
    entry = 100.0
    stop, target = stop_target_prices(
        entry, atr_pct=0.02, stop_atr_mult=0.6, reward_risk=0.7, partial_capable=False,
    )
    assert (target - entry) / (entry - stop) == pytest.approx(0.7)


# ── trail patience ──────────────────────────────────────────────────────────────
def _trail(**kw):
    base = dict(
        high_water_mark=10.0 + 1.5 * 0.12,   # +1.5R (risk_dist = 10 x 0.02 x 0.6 = 0.12)
        entry_price=10.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        day_realized_usd=0.0,
        position_risk_usd=100.0,
        breakeven_floor=9.0,                 # below the trailed candidate, so it is visible
        current_stop=9.0,
        side_long=True,
        ema_5m=None,
    )
    base.update(kw)
    return cushion_adaptive_trail_stop(**base)


def test_trail_patience_divides_by_the_plan(monkeypatch):
    hwm = 10.0 + 1.5 * 0.12
    # stand-ins carry no band knobs, so the band is the documented 500/1000 bps defaults
    monkeypatch.setattr(pe, "settings", _stand_in(_AB_WINNER))
    at_plan = _trail()
    expected = hwm * (1.0 - (500.0 + 500.0 * min(1.0, 1.5 / _AB_WINNER)) / 10_000.0)
    assert at_plan == pytest.approx(expected)
    for bad in (_MISSING, 0.0, None, float("nan")):
        monkeypatch.setattr(pe, "settings", _stand_in(bad))
        assert _trail() == pytest.approx(at_plan), bad
    # the test is sensitive: the losing arm (2.0) gives a DIFFERENT (looser) trail
    monkeypatch.setattr(pe, "settings", _stand_in(_AB_LOSER))
    assert _trail() != pytest.approx(at_plan)


# ── the exit ratchets' arm level ────────────────────────────────────────────────
def _arm_frac() -> float:
    return min(max(float(getattr(real_settings, "chili_momentum_exit_ofi_arm_frac", 0.5) or 0.5), 0.0), 1.0)


@pytest.mark.parametrize("bad", [float("nan"), 0.0, -1.0, None, "x", float("inf")])
def test_tape_accel_reversal_invalid_rr_arms_at_the_plan(bad):
    out = tape_accel_reversal_exit(
        high_water_mark=10.3, entry_price=10.0, bid=10.3, atr_pct=0.02, stop_atr_mult=0.6,
        reward_risk=bad, current_stop=9.8, breakeven_floor=10.0, signed_tape_accel=-0.1,
    )
    assert out["reward_risk"] == pytest.approx(_AB_WINNER)
    assert out["arm_r"] == pytest.approx(max(0.5, out["arm_frac"] * _AB_WINNER))


def test_ofi_exhaustion_lock_invalid_rr_arms_at_the_plan_not_2():
    af = _arm_frac()
    rd = 1.0 * max(0.003, 0.02 * 0.6)
    peak_r = af * 0.5 * (_AB_LOSER + _AB_WINNER)       # between the two arms
    assert peak_r >= 0.5
    hwm = 1.0 + peak_r * rd

    def _call(rr):
        return ofi_exhaustion_lock(
            high_water_mark=hwm, entry_price=1.0, bid=hwm - 0.6 * rd, atr_pct=0.02,
            stop_atr_mult=0.6, ofi=-0.8, micro_edge=-30.0, hidden_seller=None,
            reward_risk=rr, current_stop=0.99, breakeven_floor=1.0, current_band_bps=800.0,
            side_long=True,
        )

    assert _call(_AB_LOSER)["armed"] is True          # the old fallback WOULD have armed
    assert _call(_AB_WINNER)["armed"] is False
    for bad in (float("nan"), 0.0, -1.0, float("inf")):
        assert _call(bad)["armed"] is False, bad


def test_ask_side_pressure_lock_invalid_rr_arms_at_the_plan_not_2():
    af = _arm_frac()
    rd = 1.0 * max(0.003, 0.02 * 0.6)
    peak_r = af * 0.5 * (_AB_LOSER + _AB_WINNER)
    hwm = 1.0 + peak_r * rd
    ladder = SimpleNamespace(
        n_snaps=5, snapshot_age_s=0.0, ask_build=None, bid_refill=None,
        micro_edge=None, depth_imbal=None,
    )

    def _call(rr):
        return ask_side_pressure_lock(
            high_water_mark=hwm, entry_price=1.0, bid=hwm, atr_pct=0.02, stop_atr_mult=0.6,
            reward_risk=rr, current_stop=0.99, breakeven_floor=1.0, current_band_bps=800.0,
            ladder=ladder,
        )

    assert _call(_AB_LOSER)["armed"] is True
    assert _call(_AB_WINNER)["reason"] == "below_arm"
    for bad in (float("nan"), 0.0, -1.0, None):
        out = _call(bad)
        assert out["armed"] is False and out["reason"] == "below_arm", bad


# ── counterfactual replay ───────────────────────────────────────────────────────
def test_counterfactual_default_rr_is_the_plan(monkeypatch):
    assert counterfactual_reward_risk(None) == (_AB_WINNER, _SRC_DEFAULT)
    # an A/B arm's explicit value is kept EXACTLY and named as the caller's
    assert counterfactual_reward_risk(3.0) == (3.0, "caller")
    assert counterfactual_reward_risk(2.0) == (2.0, "caller")
    monkeypatch.setattr(pe, "settings", _stand_in())
    assert counterfactual_reward_risk(None) == (_AB_WINNER, _SRC_FALLBACK)


def test_counterfactual_run_reports_its_rr_binding():
    src = (_PKG / "counterfactual_replay.py").read_text(encoding="utf-8")
    fn = src[src.index("def run_counterfactual_symbol_replay("):]
    fn = fn[: fn.index("\ndef ", 1)]
    assert "rr, rr_source = counterfactual_reward_risk(reward_risk)" in fn
    assert 'f"counterfactual_reward_risk:{round(rr, 4)}_source:{rr_source}"' in fn


# ── config: 0 / NaN / inf rejected at LOAD, not silently remapped ───────────────
@pytest.mark.parametrize(
    "bad", [0, 0.0, -1.0, "0", "-2.5", "nan", "inf", float("nan"), float("inf")],
)
def test_config_rejects_a_non_positive_or_non_finite_plan(bad):
    with pytest.raises(ValidationError):
        Settings(CHILI_MOMENTUM_RISK_REWARD_RISK_RATIO=bad)


def test_config_rejects_zero_from_the_environment(monkeypatch):
    monkeypatch.setenv("CHILI_MOMENTUM_RISK_REWARD_RISK_RATIO", "0")
    with pytest.raises(ValidationError):
        Settings()


def test_config_still_accepts_the_plan_and_an_override():
    assert Settings().chili_momentum_risk_reward_risk_ratio == _AB_WINNER
    assert Settings(CHILI_MOMENTUM_RISK_REWARD_RISK_RATIO=3.0).chili_momentum_risk_reward_risk_ratio == 3.0
    assert Settings(CHILI_MOMENTUM_RISK_REWARD_RISK_RATIO="2.5").chili_momentum_risk_reward_risk_ratio == 2.5


def test_the_field_description_names_the_derivation_and_the_caveat():
    d = Settings.model_fields[_FIELD].description or ""
    for needle in ("#1271", "+185.21", "+160.47", "+151.91", "CELU", "first_partial_target_r"):
        assert needle in d, needle
    # [37]: the selector ranks on the first partial since c800c6271 — the description must not
    # list it as a plan consumer any more
    assert "setup-selector R:R ranking," not in d


# ── source guard: no literal fallback for the plan R:R anywhere in the package ──
def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_no_module_reads_the_plan_with_a_literal_default():
    """``getattr(settings, "chili_momentum_risk_reward_risk_ratio", <number>)`` is the old
    shape. The ONE reader (``plan_reward_risk_with_source``) uses ``None`` and falls back to the
    declared field default."""
    offenders: list[str] = []
    for path in sorted(_PKG.glob("*.py")):
        for node in ast.walk(_parse(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 3
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == _FIELD
                and not (isinstance(node.args[2], ast.Constant) and node.args[2].value is None)
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], offenders


def test_no_reward_risk_helper_falls_back_to_a_bare_number():
    tree = _parse(_PKG / "paper_execution.py")
    offenders: list[str] = []
    for node in ast.walk(tree):
        # `... if <reward_risk ok> else 2.0`
        if (
            isinstance(node, ast.IfExp)
            and "reward_risk" in ast.unparse(node.test)
            and isinstance(node.orelse, ast.Constant)
            and isinstance(node.orelse.value, (int, float))
        ):
            offenders.append(f"IfExp:{node.lineno}")
        # `rr = 2.0` / `g = 2.0`
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in ("rr", "g")
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, (int, float))
        ):
            offenders.append(f"Assign:{node.lineno}")
    assert offenders == [], offenders
    must_use = {
        "stop_target_prices", "ofi_exhaustion_lock", "tape_accel_reversal_exit",
        "ask_side_pressure_lock", "sell_into_strength_ladder",
    }
    seen = set()
    for fn in tree.body:
        if isinstance(fn, ast.FunctionDef) and fn.name in must_use:
            calls = {
                n.func.id for n in ast.walk(fn)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            }
            assert "_reward_risk_or_plan" in calls, fn.name
            seen.add(fn.name)
    assert seen == must_use


def test_the_live_receipt_reports_where_the_plan_came_from():
    src = (_PKG / "live_runner.py").read_text(encoding="utf-8", errors="replace")
    i = src.index('_emit(db, sess, "momentum_mfe_target_applied", {')
    block = src[i : i + 4000]
    assert '"plan_rr": round(_plan_rr, 3),' in block
    assert '"plan_rr_source": plan_reward_risk_source(sess.symbol),' in block


def test_nothing_here_is_nan_by_accident():
    assert math.isfinite(pe._PLAN_REWARD_RISK_DEFAULT) and pe._PLAN_REWARD_RISK_DEFAULT > 0

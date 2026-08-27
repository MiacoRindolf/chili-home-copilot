"""Ang live wiring ng protected partial OCO sa Alpaca (2026-08-27).

ANG KASAYSAYAN: sinuppress ng lane ang BAWAT partial sa Alpaca
("alpaca_scale_out_suppressed_for_deadman", 5 sa 5 live fill mula 08-21) dahil
sa komentong "Alpaca has no OCO contract here" — totoo sa kodigo, mali sa API.
Nasukat na halaga sa DAIC 08-26: parehong rung ay na-touch na may 622k/283k
shares na nagpi-print; ang gumaganang partial ay ~+$19 sa halip na −$11.58.

ANG DISENYO: sa fill tick ang scale order ay nauuna sa deadman, kaya OCO(f)
muna — may SARILING stop ang tranche mula sa sandaling umiral — tapos ang
deadman ay R = Q − f. R + f = Q: walang overlap, walang unprotected window,
walang resize ng frozen request. Ang Alpaca OCO parent MISMO ang TP limit at
ang stop ay legs[0], kaya ang buong scale_limit_* lifecycle (poll / adopt /
oversell clamp) ay muling ginagamit.

Runnable: pytest tests/test_tranche_oco_wiring.py -v
"""
from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace

from app.config import settings
from app.services.trading.momentum_neural import live_runner as LR

_SRC = pathlib.Path(LR.__file__)
_VENUE_SRC = pathlib.Path(LR.__file__).parents[1] / "venue" / "alpaca_spot.py"


def _fn(name: str) -> ast.FunctionDef:
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    return next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == name
    )


# ── Ang flag ─────────────────────────────────────────────────────────────────


def test_the_flag_ships_ON_with_the_incident_recorded():
    assert settings.chili_momentum_alpaca_protected_partial_enabled is True
    desc = str(type(settings).model_fields[
        "chili_momentum_alpaca_protected_partial_enabled"].description or "")
    assert "2026-08-27" in desc
    assert "DAIC" in desc, "ang nasukat na counterfactual ang katwiran"
    assert "R = Q - f" in desc or "R + f = Q" in desc


# ── B: placement ─────────────────────────────────────────────────────────────


def test_the_oco_attempt_comes_before_the_legacy_suppression():
    """ANG PANGUNAHING KASO: ang alpaca branch ay dapat SUMUBOK ng OCO bago
    bumagsak sa suppression — hindi na unconditional ang suppression."""
    src = ast.unparse(_fn("_place_scale_out_limit"))
    i_oco = src.index("place_protected_partial_oco")
    i_sup = src.index("alpaca_scale_out_suppressed_for_deadman")
    assert i_oco < i_sup, "OCO muna, suppression bilang fallback"


def test_every_shortfall_falls_back_to_suppression_not_a_bare_limit():
    """⚠️ FAIL-SAFE: kapag walang stop px / hindi split / tumanggi ang broker /
    walang method — ang lumang suppression ANG landas, hindi isang hubad na
    limit na mag-iiwan sa tranche nang walang stop."""
    src = ast.unparse(_fn("_place_scale_out_limit"))
    i_oco = src.index("place_protected_partial_oco")
    tail = src[i_oco:]
    assert "alpaca_scale_out_suppressed_for_deadman" in tail
    assert "tranche_oco_place_failed" in tail, "obserbable dapat ang pagtanggi"
    # ang tp>stop na tseke ay nasa placement gate
    head = src[:src.index("tranche_oco_placed")]
    assert "_oco_stop" in head and "> float(_oco_stop)" in head


def test_success_stores_the_brokers_quantized_prices_not_the_requested_ones():
    """⚠️ Ang adapter ay nag-quantize ng TP; ang strict identity gate sa clamp
    ay naghahambing ng broker limit_price laban sa scale_limit_px. Ang pag-tala
    ng HINILING na presyo ay magba-block ng BAWAT exit sa identity mismatch."""
    src = ast.unparse(_fn("_place_scale_out_limit"))
    assert "take_profit_price" in src
    i = src.index("scale_limit_px'] = ")
    region = src[i:i + 200]
    assert "take_profit_price" in region, (
        "scale_limit_px ay dapat ang quantized na presyo ng broker"
    )


def test_success_marks_the_order_as_oco_and_resets_adoption():
    src = ast.unparse(_fn("_place_scale_out_limit"))
    for key in (
        "scale_limit_is_oco",
        "scale_limit_oco_stop",
        "scale_limit_oco_legs",
        "scale_limit_adopted_qty",
        "scale_limit_client_order_id",
    ):
        assert key in src, f"kulang: {key}"


def test_the_flag_off_path_reaches_the_legacy_suppression_unconditionally():
    """OFF ⇒ byte-identical na suppression — ang OCO block ay nasa likod ng
    flag at ang suppression ay nasa labas nito."""
    src = ast.unparse(_fn("_place_scale_out_limit"))
    i_flag = src.index("chili_momentum_alpaca_protected_partial_enabled")
    i_sup = src.index("alpaca_scale_out_suppressed_for_deadman")
    assert i_flag < i_sup


# ── C: ang deadman ay sumasakop LAMANG sa runner ─────────────────────────────


def test_the_deadman_subtracts_the_tranche_for_an_oco_scale_order():
    """R = Q − f. Ang guard ay nasa ulo ng function kaya BAWAT landas (unang
    lagay + re-arm pagkatapos ng terminal) ay dumadaan dito."""
    src = ast.unparse(_fn("_ensure_alpaca_deadman_stop"))
    assert "scale_limit_is_oco" in src
    i = src.index("scale_limit_is_oco")
    region = src[i:i + 900]
    assert "quantity" in region and "- _tr_qty" in region


def test_invalid_tranche_arithmetic_fails_closed():
    """⚠️ Kapag tranche ≥ posisyon o ≤ 0, ang aritmetika ay sira — full close,
    hindi hula."""
    src = ast.unparse(_fn("_ensure_alpaca_deadman_stop"))
    assert "tranche_oco_split_arithmetic_invalid" in src


def test_a_non_oco_scale_order_still_forces_the_legacy_full_close():
    """Ang lumang (hindi-OCO) scale order + deadman ay overlap pa rin ⇒ ang
    dating conflict close ay nananatili."""
    src = ast.unparse(_fn("_ensure_alpaca_deadman_stop"))
    assert "alpaca_legacy_scale_order_conflicts_with_deadman" in src


# ── A/D: ang stop-leg fill ay nakikita at nabu-book sa presyo ng leg ─────────


def _mk_order(filled=0.0, avg=0.0, legs=None):
    return SimpleNamespace(
        filled_size=filled,
        average_filled_price=avg,
        raw={"legs": legs if legs is not None else []},
    )


def test_a_parent_fill_wins_and_reports_the_parent_price():
    q, px, src = LR._scale_order_total_fill(
        _mk_order(filled=47.0, avg=6.37), {"scale_limit_is_oco": True})
    assert (q, px, src) == (47.0, 6.37, "parent")


def test_a_stop_leg_fill_is_detected_at_the_legs_own_price():
    """ANG PANGUNAHING KASO. Kapag ang STOP leg ang nag-fill, ang parent ay
    canceled na may zero fill — ang lumang pagbasa ay magbu-book ng WALA
    habang ang shares ay naibenta na."""
    q, px, src = LR._scale_order_total_fill(
        _mk_order(filled=0.0, legs=[
            {"filled_qty": 47.0, "filled_avg_price": 6.01, "stop_price": 6.02},
        ]),
        {"scale_limit_is_oco": True},
    )
    assert (q, src) == (47.0, "stop_leg")
    assert px == 6.01, "presyo ng LEG, hindi ang scale_limit_px"


def test_a_stop_leg_fill_without_avg_price_falls_back_to_the_stop():
    q, px, src = LR._scale_order_total_fill(
        _mk_order(filled=0.0, legs=[
            {"filled_qty": 20.0, "filled_avg_price": None, "stop_price": 6.02},
        ]),
        {"scale_limit_is_oco": True},
    )
    assert (q, px, src) == (20.0, 6.02, "stop_leg")


def test_a_non_oco_scale_order_never_reads_legs():
    """Ang legacy scale limit ay walang legs contract — huwag mag-imbento."""
    q, px, src = LR._scale_order_total_fill(
        _mk_order(filled=0.0, legs=[
            {"filled_qty": 20.0, "filled_avg_price": 6.02},
        ]),
        {},
    )
    assert (q, px, src) == (0.0, 0.0, "none")


def test_unfilled_everything_returns_none():
    q, px, src = LR._scale_order_total_fill(
        _mk_order(), {"scale_limit_is_oco": True})
    assert (q, px, src) == (0.0, 0.0, "none")


def test_both_adopt_branches_use_the_helper_and_the_oco_reason():
    src = ast.unparse(_fn("_cancel_scale_limit_and_clamp"))
    assert src.count("_scale_order_total_fill(") >= 2, (
        "parehong adopt branch (strict at generic) ay dapat gumagamit ng helper"
    )
    assert src.count("tranche_oco_stop_fill") >= 2


# ── nested=True: kung wala nito ang stop leg ay invisible ────────────────────


def test_single_order_truth_reads_are_nested():
    """⚠️ Ang get_order_by_id na walang nested=True ay nagbabalik ng OCO parent
    na WALANG legs — ang stop-leg fill ay hindi makikita kailanman."""
    vsrc = _VENUE_SRC.read_text(encoding="utf-8")
    tree = ast.parse(vsrc)
    for name in ("get_order", "get_order_truth"):
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == name
        )
        fsrc = ast.unparse(fn)
        assert "GetOrderByIdRequest(nested=True)" in fsrc, (
            f"{name} ay dapat nested=True"
        )

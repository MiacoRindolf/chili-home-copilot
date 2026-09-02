"""Guards for the MARKETABLE partial shape (PATH B addendum, 2026-09-02).

Pure, DB-free. These bind the three properties the addendum's argument rests on:

1. the trigger is the **bid**, not the mid (the mid trigger is what costs
   −0.53 R on the f shares);
2. the posted price is **never above the bid** — a limit above the bid is a
   resting offer again, which is the entire D1 exposure; and
3. every helper is **total** — unreadable input returns a verdict, never a
   raise (revision-4 defect #10; ``broker_qty=None`` is a live-real state).

There is also a tripwire asserting the module has ZERO production callers, the
same shape as ``test_partial_exit_path_b_unwired.py`` — and unlike revision 1's
version of that test, this one asserts it actually scanned files.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.services.trading.momentum_neural.path_b_marketable import (
    DEFAULT_NOTIONAL_GUARD_BPS,
    EXTENDED_CROSS_MULTIPLE,
    PARTIAL_SHAPE_MARKETABLE,
    marketable_left_behind,
    marketable_partial_limit_price,
    partial_post_request,
    partial_trigger_ready,
)

_MODULE_NAME = "path_b_marketable"


# ---------------------------------------------------------------- trigger ---

def test_trigger_is_the_bid_not_the_mid():
    # SSM 19315: target 4.05, bid 4.05 at the touch instant.
    assert partial_trigger_ready(bid=4.05, target_price=4.05)["ready"] is True
    # A mid of 4.05 with a bid of 4.02 must NOT trigger: selling there hits a
    # bid that has not arrived, which is the -0.53R case.
    assert partial_trigger_ready(bid=4.02, target_price=4.05)["ready"] is False


def test_trigger_gap_bps_is_signed_from_the_target():
    v = partial_trigger_ready(bid=4.10, target_price=4.05)
    assert v["ready"] is True
    assert v["gap_bps"] == pytest.approx((4.10 - 4.05) / 4.05 * 10_000.0)


@pytest.mark.parametrize("bad", [None, "", "abc", 0, -1.0, float("nan"), True])
def test_trigger_is_total_on_unreadable_input(bad):
    v = partial_trigger_ready(bid=bad, target_price=4.05)
    assert v["valid"] is False and v["ready"] is False
    v2 = partial_trigger_ready(bid=4.05, target_price=bad)
    assert v2["valid"] is False and v2["ready"] is False


# ------------------------------------------------------------------ price ---

def test_price_is_never_above_the_bid():
    """The whole point. A limit above the bid is a resting offer = D1 again."""
    for bid in (0.1234, 1.0, 4.28, 4.6349, 17.01, 355.0):
        for ext in (False, True):
            out = marketable_partial_limit_price(bid=bid, extended_hours=ext)
            assert out["ok"] is True
            assert out["limit_price"] <= bid, (bid, ext, out)
            assert out["marketable"] is True


def test_extended_hours_crosses_eight_times_the_guard():
    """LR:15660-15694 — 25 bps guard x8 = 200 bps under the bid, extended only."""
    ext = marketable_partial_limit_price(bid=100.0, extended_hours=True)
    rth = marketable_partial_limit_price(bid=100.0, extended_hours=False)
    assert ext["cross_bps"] == pytest.approx(
        DEFAULT_NOTIONAL_GUARD_BPS * EXTENDED_CROSS_MULTIPLE
    )
    assert ext["cross_bps"] == pytest.approx(200.0)
    assert rth["cross_bps"] == pytest.approx(25.0)
    assert ext["limit_price"] < rth["limit_price"] <= 100.0


def test_price_fails_closed_without_a_bid():
    out = marketable_partial_limit_price(bid=None, extended_hours=True)
    assert out["ok"] is False
    assert out["limit_price"] is None
    assert out["reason"] == "marketable_partial_requires_a_bid"


def test_sub_dollar_price_rounds_down_not_up():
    # AUUD-shaped: 1.05 bid, penny tick. Rounding UP would post above the bid.
    out = marketable_partial_limit_price(bid=1.05, extended_hours=True, tick=0.01)
    assert out["limit_price"] <= 1.05


# ------------------------------------------------------------------- POST ---

def test_post_request_is_the_chokepoints_own_shape():
    out = partial_post_request(
        product_id="CANF", quantity=106, bid=4.28, extended_hours=True,
        client_order_id="cid-1",
    )
    assert out["ok"] is True
    assert out["shape"] == PARTIAL_SHAPE_MARKETABLE
    req = out["request"]
    assert req["order_type"] == "limit"
    assert req["time_in_force"] == "day"
    assert req["position_intent"] == "sell_to_close"
    assert req["extended_hours"] is True
    assert req["side"] == "sell"
    assert req["limit_price"] <= 4.28


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(product_id="", quantity=106, bid=4.28, client_order_id="c"),
        dict(product_id="CANF", quantity=0, bid=4.28, client_order_id="c"),
        dict(product_id="CANF", quantity=106, bid=None, client_order_id="c"),
        dict(product_id="CANF", quantity=106, bid=4.28, client_order_id=""),
    ],
)
def test_post_request_refuses_rather_than_raises(kwargs):
    out = partial_post_request(extended_hours=True, **kwargs)
    assert out["ok"] is False
    assert out["request"] is None


# ----------------------------------------------------------- left behind ---

def test_left_behind_detects_the_residual_tail():
    priced = marketable_partial_limit_price(bid=4.28, extended_hours=True)
    px = priced["limit_price"]
    assert marketable_left_behind(bid_now=4.28, limit_price=px)["left_behind"] is False
    # Bid falls more than the 200 bps crossing inside the round trip.
    assert marketable_left_behind(bid_now=px - 0.05, limit_price=px)["left_behind"] is True


def test_left_behind_is_total():
    v = marketable_left_behind(bid_now=None, limit_price=4.0)
    assert v["valid"] is False and v["left_behind"] is None


# -------------------------------------------------------------- tripwire ---

def test_module_has_zero_production_callers():
    """Additive-only: nothing under app/ may import this yet (S1/S2 pending)."""
    app_root = Path(__file__).resolve().parents[1] / "app"
    assert app_root.is_dir(), app_root
    scanned = 0
    offenders: list[str] = []
    for py in app_root.rglob("*.py"):
        if py.name == f"{_MODULE_NAME}.py":
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        scanned += 1
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and _MODULE_NAME in (node.module or ""):
                offenders.append(str(py))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if _MODULE_NAME in alias.name:
                        offenders.append(str(py))
    assert scanned > 100, f"tripwire scanned only {scanned} files — it asserts nothing"
    assert offenders == [], offenders

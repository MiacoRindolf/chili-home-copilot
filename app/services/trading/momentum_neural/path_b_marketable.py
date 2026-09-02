"""PATH B — MARKETABLE partial shape. Pure, no I/O, zero production callers.

Sinasagot ng module na ito ang D1 blocker ng
``docs/DESIGN/PARTIAL_EXIT_PATH_B.md``: hindi ang PATCH ang gumagawa ng naked
window, kundi ang **hugis ng benta**. Ang isang RESTING sell limit sa itaas ng
merkado ay nabubuhay hangga't buhay ang posisyon (sinukat: Alpaca p50 459.2 s,
p75 3,265 s, max 7,167 s), kaya ang ``f`` shares ay walang downside stop sa
buong panahong iyon. Ang isang MARKETABLE sell — kapareho ng hugis na
ipinapadala na ngayon ng buong-posisyong exit sa chokepoint — ay nabubuhay lang
sa habang ng round trip nito (sinukat: ``place_rtt_s`` p50 0.109 s, p95 0.452 s,
max 0.860 s).

Ito rin ang mekaniko ni Ross: *"I sell into the spike"* — tinatamaan niya ang
bid; hindi siya nag-po-post ng offer sa itaas at naghihintay na buhatin.

Bawat function dito ay TOTAL: hindi ito nag-raise sa hindi mabasang input,
nagbabalik ito ng sarili nitong ``invalid`` verdict (revision-4 defect #10 ng
design). Walang I/O, walang settings read, walang clock.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = [
    "PARTIAL_SHAPE_MARKETABLE",
    "PARTIAL_SHAPE_RESTING",
    "DEFAULT_NOTIONAL_GUARD_BPS",
    "RTH_CROSS_MULTIPLE",
    "EXTENDED_CROSS_MULTIPLE",
    "partial_trigger_ready",
    "marketable_partial_limit_price",
    "partial_post_request",
    "marketable_left_behind",
]

#: Ang dalawang hugis. ``PARTIAL_SHAPE_RESTING`` ay ang orihinal na §3.5 —
#: iniingatan dito para may pangalan ang inihahambing, HINDI para gamitin.
PARTIAL_SHAPE_RESTING: Final[str] = "resting_limit"
PARTIAL_SHAPE_MARKETABLE: Final[str] = "marketable_limit"

#: ``chili_momentum_order_notional_guard_bps`` default (LR:19814-19820).
DEFAULT_NOTIONAL_GUARD_BPS: Final[float] = 25.0

#: Ang mga multiple na ginagamit na ng chokepoint sa buong-posisyong exit
#: (LR:15660-15694): ×1 sa unang attempt sa RTH, ×8 kapag extended-hours (kung
#: saan tinatanggihan nang tuwiran ang market order). 25 bps × 8 = **200 bps**
#: na pagtawid sa ilalim ng bid. Ito ay LIMIT PRICE — ang pinakamasamang presyo
#: na tatanggapin — hindi ang inaasahang fill: ang isang marketable sell ay
#: napupunan sa pinakamahusay na bid na nakatayo, hindi sa limit nito.
RTH_CROSS_MULTIPLE: Final[float] = 1.0
EXTENDED_CROSS_MULTIPLE: Final[float] = 8.0


def _as_float(value: Any) -> float | None:
    """Total float coercion. ``None`` kapag hindi mabasa — kailanman hindi raise."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return out


def partial_trigger_ready(
    *,
    bid: Any,
    target_price: Any,
    tolerance_bps: Any = 0.0,
) -> dict[str, Any]:
    """Ang partial ba ay dapat nang ipadala NGAYON?

    Ang trigger ay ``bid >= target``, HINDI ``mid >= target`` at hindi
    ``ask >= target``. Mahalaga ito: sa bid-touch trigger ang marketable na
    benta ay napupunan sa presyong ``>= target`` **by construction**, kaya ang
    fill certainty nito ay eksaktong kapareho ng resting limit (sinukat: 0 sa 13
    na kaso ang hindi nagkasundo) samantalang ang presyo ay hindi nagbabago.
    Ang mid-touch trigger ay nagbebenta sa isang bid na hindi pa dumarating at
    nagkakahalaga ng −0.53 R sa ``f`` shares sa nag-iisang kasong may naitalang
    ``stop_distance`` (SSM 19315).

    Nagbabalik ng verdict dict; hindi kailanman nag-raise.
    """
    b = _as_float(bid)
    t = _as_float(target_price)
    tol_bps = _as_float(tolerance_bps)
    if tol_bps is None or tol_bps < 0.0:
        tol_bps = 0.0
    if b is None or t is None or b <= 0.0 or t <= 0.0:
        return {
            "ready": False,
            "valid": False,
            "reason": "unreadable_bid_or_target",
            "bid": b,
            "target_price": t,
        }
    threshold = t * (1.0 - tol_bps / 10_000.0)
    ready = b >= threshold
    return {
        "ready": bool(ready),
        "valid": True,
        "reason": "bid_at_or_through_target" if ready else "bid_below_target",
        "bid": b,
        "target_price": t,
        "threshold": threshold,
        "gap_bps": (b - t) / t * 10_000.0,
    }


def marketable_partial_limit_price(
    *,
    bid: Any,
    extended_hours: bool,
    guard_bps: Any = DEFAULT_NOTIONAL_GUARD_BPS,
    tick: Any = 0.01,
) -> dict[str, Any]:
    """Presyo ng marketable sell limit — pareho sa buong-posisyong exit.

    ``limit = bid × (1 − guard_bps/10000 × multiple)``, binababa sa tick para
    HINDI ito kailanman tumaas nang lampas sa bid. Ang huling kondisyon ang
    buong punto: ang isang limit na nasa ITAAS ng bid ay isang resting offer
    muli, at ibinabalik nito ang buong D1 exposure.

    Fail-CLOSED: kapag walang mababasang bid, ``ok=False`` — walang order.
    """
    b = _as_float(bid)
    g = _as_float(guard_bps)
    tk = _as_float(tick)
    if g is None or g < 0.0:
        g = DEFAULT_NOTIONAL_GUARD_BPS
    if tk is None or tk <= 0.0:
        tk = 0.01
    if b is None or b <= 0.0:
        return {
            "ok": False,
            "reason": "marketable_partial_requires_a_bid",
            "limit_price": None,
            "cross_bps": None,
        }
    multiple = EXTENDED_CROSS_MULTIPLE if extended_hours else RTH_CROSS_MULTIPLE
    cross_bps = g * multiple
    raw = b * (1.0 - cross_bps / 10_000.0)
    # Ibaba sa tick: ang pag-round pataas ay maaaring ilagay ang limit sa itaas
    # ng bid sa mga presyong sub-dollar, na siyang mismong bitag na iniiwasan.
    steps = int(raw / tk)
    px = steps * tk
    if px <= 0.0 or px > b:
        px = min(b, raw)
    if px <= 0.0:
        return {
            "ok": False,
            "reason": "marketable_partial_price_underflow",
            "limit_price": None,
            "cross_bps": cross_bps,
        }
    return {
        "ok": True,
        "reason": "marketable",
        "limit_price": round(px, 6),
        "cross_bps": cross_bps,
        "bid": b,
        "marketable": px <= b,
    }


def partial_post_request(
    *,
    product_id: Any,
    quantity: Any,
    bid: Any,
    extended_hours: bool,
    client_order_id: Any,
    guard_bps: Any = DEFAULT_NOTIONAL_GUARD_BPS,
    tick: Any = 0.01,
) -> dict[str, Any]:
    """Buoin ang eksaktong POST body para sa P4 (§3.5), marketable na hugis.

    Hindi ito nagpapadala ng kahit ano. Ito ay nandito para HINDI na muling
    imbentuhin ng wiring ang hugis ng order — ang parehong pagkakaiba-iba ng
    hugis ang nagdala ng ``tranche_oco_skipped_extended_hours`` at ng
    inert-premarket na containment close (R5).
    """
    qty = _as_float(quantity)
    sym = str(product_id or "").strip()
    cid = str(client_order_id or "").strip()
    if not sym or qty is None or qty <= 0.0 or not cid:
        return {"ok": False, "reason": "unreadable_post_inputs", "request": None}
    priced = marketable_partial_limit_price(
        bid=bid, extended_hours=extended_hours, guard_bps=guard_bps, tick=tick
    )
    if not priced["ok"]:
        return {"ok": False, "reason": priced["reason"], "request": None}
    return {
        "ok": True,
        "reason": "marketable",
        "shape": PARTIAL_SHAPE_MARKETABLE,
        "request": {
            "product_id": sym,
            "side": "sell",
            "order_type": "limit",
            "quantity": qty,
            "limit_price": priced["limit_price"],
            "time_in_force": "day",
            "position_intent": "sell_to_close",
            "extended_hours": bool(extended_hours),
            "client_order_id": cid,
        },
        "cross_bps": priced["cross_bps"],
    }


def marketable_left_behind(
    *,
    bid_now: Any,
    limit_price: Any,
) -> dict[str, Any]:
    """Naiwan na ba ang naipadalang marketable sell?

    Ito ang nananatiling buntot: kung bumagsak ang bid nang lampas sa
    crossing fraction sa loob ng round trip, ang order ay nakapahinga na muli at
    ang phase ``partial_posted`` — kasama ang buong makinarya nito — ay bumabalik.
    Ginagawa itong BIHIRA ng marketable na hugis, hindi imposible, kaya HINDI
    tinatanggal ang remedy path; ang tinatanggal ay ang pagiging normal na estado
    nito.
    """
    b = _as_float(bid_now)
    px = _as_float(limit_price)
    if b is None or px is None or b <= 0.0 or px <= 0.0:
        return {"left_behind": None, "valid": False, "reason": "unreadable_quote"}
    left = b < px
    return {
        "left_behind": bool(left),
        "valid": True,
        "reason": "bid_below_limit" if left else "still_marketable",
        "shortfall_bps": (px - b) / px * 10_000.0 if left else 0.0,
    }

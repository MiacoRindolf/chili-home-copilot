"""SEAM GUARD: kaya bang patunayan ng PRODUCTION lineage matcher ang isang
edge na NAGPAPALIIT ng qty?

BAKIT ITO ANG PINAKAMAHALAGANG TEST NG PR NA ITO. Ang buong PATH B ay
nakasalalay sa isang bagay: matapos ang matagumpay na PATCH (Q -> R), dapat
ma-CERTIFY ng `_dispatch_alpaca_replaced_deadman_successor` ang successor.
Hindi niya kaya ngayon. Ginagawa niya ang inaasahang envelope sa pamamagitan
ng pagkopya sa predecessor at pagpalit LAMANG ng cid (LR:10101-10104)::

    successor_request = {**predecessor_request, "client_order_id": successor_cid}

kaya nananatiling Q ang `base_size`, samantalang ang successor ay nakaupo para
sa R = Q - f. Hinihingi ng `_owner_transport_order_matches` (LR:9108-9130) ang
eksaktong pagkakapantay ng qty, kaya ang tugma ay babagsak nang eksaktong f —
bawat pulse, magpakailanman (`replacement_deadman_successor_lineage_unproven`).
Ang ORDINARYONG, MATAGUMPAY na PATCH ay hindi kailanman maka-ce-certify.

Ito ang hugis ng aral ng #1283: ang helper ay berde, ang SEAM ang sira. Kaya
tinatawag ng test na ito ang TUNAY na production predicate — hindi isang
kopya nito — gamit ang fake na order object lamang. Walang DB, walang broker.

Kapag naikabit na ng isang PR ang marker-aware na envelope, babagsak ang
`test_..._is_still_unreachable_today`; iyon ang senyas na puwede nang alisin
ang §7.1 sa `docs/DESIGN/PARTIAL_EXIT_PATH_B.md`.

Runnable: pytest tests/test_partial_exit_path_b_lineage_seam.py -v
"""
from __future__ import annotations

import inspect

from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural import path_b_partial as pb

_PREDECESSOR_OID = "alpaca-oid-predecessor"
_SUCCESSOR_OID = "alpaca-oid-successor"
_SUCCESSOR_CID = "chili-deadman-canf-gen8"

_Q = 355.0
_F = 106.0
_R = 249.0
_STOP_PX = 3.91

_PREDECESSOR_REQUEST = {
    "product_id": "CANF",
    "side": "sell",
    "order_type": "stop",
    "base_size": _Q,
    "stop_price": _STOP_PX,
    "time_in_force": "gtc",
    "position_intent": "sell_to_close",
    "extended_hours": False,
    "client_order_id": "chili-deadman-canf-gen7",
}


class _FakeOrder:
    """Ang eksaktong surface na binabasa ng dalawang production predicate."""

    def __init__(self, *, qty: float, filled: float = 0.0) -> None:
        self.order_id = _SUCCESSOR_OID
        self.client_order_id = _SUCCESSOR_CID
        self.product_id = "CANF"
        self.side = "sell"
        self.order_type = "stop"
        self.filled_size = filled
        self.raw = {
            "qty": qty,
            "stop_price": _STOP_PX,
            "time_in_force": "gtc",
            "position_intent": "sell_to_close",
            "extended_hours": False,
            "replaces": _PREDECESSOR_OID,
            "alpaca_status": "new",
        }


def _matches(request: dict) -> bool:
    return lr._alpaca_replacement_successor_order_matches(
        _FakeOrder(qty=_R),
        predecessor_broker_order_id=_PREDECESSOR_OID,
        successor_broker_order_id=_SUCCESSOR_OID,
        successor_order_request=request,
    )


def _todays_envelope() -> dict:
    """Ang eksaktong ginagawa ng LR:10101-10104 ngayon."""
    return {**_PREDECESSOR_REQUEST, "client_order_id": _SUCCESSOR_CID}


def test_a_shrinking_edge_is_unprovable_with_todays_envelope():
    """S1. Ito ang blocker. Ang successor na nakaupo para sa R laban sa isang
    envelope na may base_size Q ay TINATANGGIHAN — kaya `pending` ang dispatch
    magpakailanman at hindi kailanman umuusad ang marker sa
    `successor_certified`."""
    assert _matches(_todays_envelope()) is False


def test_the_same_successor_is_provable_with_the_marker_envelope():
    """At ito ang lunas: ang envelope mula sa MARKER (base_size R, lahat ng
    iba ay pareho) ay tinatanggap ng parehong production predicate."""
    envelope = pb.marker_successor_envelope(
        predecessor_order_request=_PREDECESSOR_REQUEST,
        successor_client_order_id=_SUCCESSOR_CID,
        successor_qty=_R,
    )
    assert envelope is not None
    assert _matches(envelope) is True


def test_the_marker_envelope_does_not_weaken_any_other_field():
    """Ang amendment ay nagpapaluwag LAMANG ng qty. Ang bawat ibang field ay
    dapat manatiling mahigpit — kung hindi, ang isang dayuhang order ay
    puwedeng maampon bilang ating stop."""
    base = pb.marker_successor_envelope(
        predecessor_order_request=_PREDECESSOR_REQUEST,
        successor_client_order_id=_SUCCESSOR_CID,
        successor_qty=_R,
    )
    assert base is not None
    for key, wrong in (
        ("client_order_id", "some-other-cid"),
        ("product_id", "NOTCANF"),
        ("order_type", "limit"),
        ("time_in_force", "day"),
        ("stop_price", 9.99),
        ("extended_hours", True),
    ):
        assert _matches({**base, key: wrong}) is False, key


def test_a_successor_resting_for_the_wrong_size_is_still_rejected():
    """Ang marker envelope ay hindi isang blangkong tseke: ang successor na
    nakaupo para sa maling bilang ay tinatanggihan pa rin."""
    envelope = pb.marker_successor_envelope(
        predecessor_order_request=_PREDECESSOR_REQUEST,
        successor_client_order_id=_SUCCESSOR_CID,
        successor_qty=_R - 1.0,
    )
    assert envelope is not None
    assert _matches(envelope) is False


def test_the_dispatch_now_builds_its_envelope_through_the_shared_helper():
    """RE-POINTED 2026-09-08. THE TRIPWIRE FIRED AND NOBODY READ IT.

    This test used to assert `'**predecessor_request' in src` with a docstring
    saying "when this fails, someone has changed the envelope source". It has been
    RED since 2026-09-02, when 89cb0eb64 (#1292) replaced the inline copy with the
    pure `_alpaca_replacement_successor_envelope`. It worked exactly as designed.
    The failure simply went unread for six days, while the design document was
    revised the following day still listing that work as outstanding (§4/D0).

    A source guard is only useful if a red run is read. When this one fails again,
    the question to ask is not "how do I make it green" but "did the envelope stop
    coming from a value WE intend, and start coming from one the BROKER echoed?" —
    because a broker-echoed size makes the whole lineage check a tautology.
    """
    src = inspect.getsource(lr._dispatch_alpaca_replaced_deadman_successor)
    assert "_alpaca_replacement_successor_envelope(" in src
    assert "expected_successor_quantity=expected_successor_quantity" in src
    # The inline copy is gone; if it ever comes back the shrink becomes
    # unprovable again and PATH B is blocked without anyone being told.
    assert "**predecessor_request" not in src


def test_the_envelope_helper_licenses_a_shrink_and_only_a_shrink():
    """The other half of what #1292 landed: the helper permits R <= Q and refuses
    a successor LARGER than the order it replaces, which would arm a stop for
    shares the position does not hold."""
    src = inspect.getsource(lr._alpaca_replacement_successor_envelope)
    assert "expected_successor_quantity" in src
    assert "successor_qty <= predecessor_qty" in src


def test_the_conservation_gate_now_anchors_on_covered_quantity():
    """Amendment 2, re-pointed. The gate the first design missed compared
    `local_qty` against the PREDECESSOR's base_size (Q), so a partial fill of k
    made it `replacement_deadman_successor_quantity_generation_mismatch` forever.

    It now lives in its own pure frame and anchors on `covered_qty` — the
    successor plus whatever is reserved outside it — which is the quantity the
    POSITION as a whole is expected to carry. That is the form PATH B needs: a
    successor of Q-f with nothing reserved, against a local position already at
    Q-f, conserves exactly.
    """
    src = inspect.getsource(lr._alpaca_replacement_quantity_frame)
    assert "covered_qty = successor_qty + reserved_qty" in src
    # And the old form is gone from the dispatcher.
    dispatch_src = inspect.getsource(lr._dispatch_alpaca_replaced_deadman_successor)
    assert "abs(local_qty - requested_qty) <= tol" not in dispatch_src
    assert pb.conservation_holds(
        broker_qty=_Q - 40.0, successor_qty=_R,
        partial_qty=_F, partial_cum_filled=40.0,
    ) is True
    # at ito ang tamang anyo na dapat pumalit dito:
    assert pb.conservation_holds(
        broker_qty=_Q - 40.0, successor_qty=_R,
        partial_qty=_F, partial_cum_filled=40.0,
    ) is True


def test_partially_filled_is_still_a_certifiably_active_lifecycle():
    """Amendment 10. Dahil dito, ang PATCH sa isang bahagyang napunang stop ay
    hindi mahuhuli ng lifecycle gate — kailangan itong hulihin sa planner."""
    assert "partially_filled" in lr._ACTIVE_ALPACA_PROTECTIVE_LIFECYCLES
    assert pb.plan_replacement_edge(
        total_qty=_Q, partial_qty=_F, predecessor_filled_size=40.0
    ).ok is False


def test_the_clamp_is_still_a_pass_through_noop_without_a_resolvable_sibling():
    """S2, RE-POINTED 2026-09-08 — same defect, wider door than when this was
    written.

    The clamp still returns the full requested quantity when no sibling sell can
    be resolved, so the oversell invariant its own docstring promises does not run
    on that path. What changed is where the id comes from: the ledger read
    `le.get("scale_limit_order_id")` has been joined by a
    `sibling_order_id_resolver`, so a sibling living only in a claim can now be
    asserted by the caller instead of being invisible. That makes S2 SMALLER — the
    resolver is the seam a wiring can feed — but it does not close it: with no
    resolver and no ledger id, the pass-through is unchanged.
    """
    src = inspect.getsource(lr._cancel_scale_limit_and_clamp)
    assert "sibling_order_id_resolver" in src
    assert "if not oid:" in src
    assert "return float(requested_qty)" in src


def test_the_head_guard_still_subtracts_the_original_partial_size():
    """S3. Ang head guard ay nagbabawas ng `scale_limit_qty` — ang ORIHINAL na
    f, na hindi kailanman binabawasan kapag bahagyang napunan ang sibling.
    Matapos ang k ay mag-mi-mint ito ng stop para sa (Q-k)-f at mag-iiwan ng
    f-k na hubad, nang walang error at walang event."""
    src = inspect.getsource(lr._ensure_alpaca_deadman_stop)
    assert '_tr_qty = _float_or_none(le.get("scale_limit_qty")) or 0.0' in src
    assert "alpaca_legacy_scale_order_conflicts_with_deadman" in src
    assert "scale_limit_open_qty" not in src


def test_the_containment_cannot_accept_a_reverted_predecessor():
    """L4. Ang post-cancel na two-sided proof ay nag-ce-certify LAMANG ng
    predecessor na `replaced` na may terminal na successor. Ang resulta na
    hinihingi ng §3.9 — predecessor na bumalik sa `new` — ay ang mismong
    tinatanggihan nito, kaya kailangan ng hiwalay na service function."""
    src = inspect.getsource(lr._service_deadman_replacement_containment)
    assert "replacement_containment_identity_invalid" in src
    assert "replacement_containment_post_cancel_two_sided_truth_unproven" in src
    assert '_alpaca_protective_order_lifecycle(predecessor_after) == "replaced"' in src
    # FORWARD TRIPWIRE, hindi isang tseke ng kasalukuyang gawi: ang
    # `close_shape` ay ang PANUKALANG extension ng §3.9(b), at wala pa ito
    # kahit saan sa puno. Kapag idinagdag ito ng isang PR ay babagsak ang linya
    # na ito at iyon ang senyas na naikabit na ang sanga (b) ng stuck-replace
    # escape. Ang POSITIBONG binding ng signature na ito ay nasa
    # `test_the_two_seam_signatures_are_pinned_by_inspect_signature` sa ibaba —
    # kung wala iyon ay isang tripwire lamang ito at hindi isang gabay.
    assert "close_shape" not in inspect.signature(
        lr._service_deadman_replacement_containment
    ).parameters


# --------------------------------------------------------------------------
# Ang binding ng SIGNATURE — ang aral ng #1283, sa dalawang seam function
# --------------------------------------------------------------------------

def _keyword_only_names(fn) -> set[str]:
    return {
        name
        for name, param in inspect.signature(fn).parameters.items()
        if param.kind is inspect.Parameter.KEYWORD_ONLY
    }


def test_the_two_seam_signatures_are_pinned_by_inspect_signature():
    """ANG ARAL NG #1283, INILAPAT SA LAHAT NG TATLONG SEAM FUNCTION.

    Ang `_matches()` sa itaas ay TUNAY na tumatawag sa
    `_alpaca_replacement_successor_order_matches` gamit ang lahat ng tatlong
    kw-only na pangalan nito, kaya ang isang rename doon ay agad na bumabasag ng
    suite. Pero ang DALAWANG function na kailangang pakainin ng wiring ng
    marker envelope — ang dispatch at ang containment — ay may source-substring
    na assert LAMANG. Ang isang refactor na magre-rename, mag-aayos muli, o
    magdaragdag ng kinakailangang kw-only na parameter sa alinman sa kanila ay
    tahimik na magpapabulok sa call plan ng disenyo habang berde ang buong
    suite: iyon ang eksaktong hugis ng #1283 (berdeng helper, patay na seam).

    Ang test na ito ay hindi tumatawag sa dalawa (kailangan nila ng DB at ng
    adapter); ini-pin nito ang KONTRATA na binabanggit ng disenyo.
    """
    dispatch = _keyword_only_names(lr._dispatch_alpaca_replaced_deadman_successor)
    # RE-POINTED 2026-09-08. #1292 added the two parameters PATH B needs, both
    # keyword-only and both defaulted so every existing caller is byte-identical.
    # Their presence is now part of the contract: losing them would take the
    # remedy away again, which is what this pin exists to catch.
    assert dispatch == {
        "le", "product_id", "predecessor_transport", "predecessor_order",
        "avg_entry_price", "software_stop_price", "rearm_after_terminal",
        "expected_successor_quantity", "quantity_reserved_outside_successor",
    }, sorted(dispatch)

    containment = _keyword_only_names(lr._service_deadman_replacement_containment)
    assert containment == {
        "le", "product_id", "predecessor_transport", "predecessor_order",
        "successor_order", "successor_order_request",
        "avg_entry_price", "software_stop_price", "prepared",
    }, sorted(containment)

    # Ang `successor_order_request` ay ang MISMONG parameter na dapat tanggapin
    # ang envelope ng marker (§3.4a). Kung mawala o mapalitan ang pangalan nito
    # ay wala nang mapagpapasahan ang lunas ng S1.
    assert "successor_order_request" in containment


def test_the_matcher_signature_is_pinned_too():
    """Ang predicate na tinatawag ng `_matches()`: ang tatlong kw-only na
    pangalan ay bahagi ng kontrata, hindi detalye ng pagpapatupad."""
    assert _keyword_only_names(
        lr._alpaca_replacement_successor_order_matches
    ) == {
        "predecessor_broker_order_id",
        "successor_broker_order_id",
        "successor_order_request",
    }

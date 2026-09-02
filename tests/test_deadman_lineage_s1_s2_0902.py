"""Deadman protection lineage: two latent defects on the per-tick Alpaca path.

Both live in the protection path that runs for EVERY Alpaca position on EVERY
tick, and both are latent today -- which is exactly why they are worth pinning
before something makes them live.

S1 -- the replaced-successor lineage could not certify a changed quantity.
``_dispatch_alpaca_replaced_deadman_successor`` built the expected successor
envelope as ``{**predecessor_request, "client_order_id": cid}``, so ``base_size``
stayed the PREDECESSOR's Q while ``_owner_transport_order_matches`` demands exact
quantity equality against the order actually resting at the broker.  A deadman
replace that CHANGES the quantity therefore fails by exactly the delta -- and the
failure is ``pending``, not terminal, so the tick re-enters and fails identically
forever.

Building the envelope from the successor's own size is only HALF of that: the
conservation arithmetic downstream was anchored to the predecessor's Q too.  With
a shrunk successor the coverage bound wanted ``broker == successor`` while the
delta wanted ``broker == predecessor`` -- mutually exclusive, so every non-default
call was rejected by construction and the deadlock simply moved one gate later.
``_alpaca_replacement_quantity_frame`` is the whole frame, anchored to COVERAGE,
and it is pure so these tests can prove it without a broker or a DB.

SCOPE, stated honestly: only the QUANTITY is substituted.  ``stop_price`` and
``time_in_force`` are still inherited from the predecessor verbatim, so an
operator raising the resting stop in the Alpaca UI still fails the lineage
conjunction and still returns ``..._lineage_unproven``.  That refusal is the
correct fail-closed answer to a silent external change -- but it means this fix
does not make that route work, and nothing here claims it does.

S2 -- the scale-limit clamp was a silent pass-through.
``_cancel_scale_limit_and_clamp`` opened with ``if not oid: return requested_qty``,
so a live sibling sell whose id the ledger never recorded would be invisible and
the exit would release the FULL requested quantity against a broker position that
has shares reserved by that sibling.

The live-reachable source of such a sibling is not exotic: BOTH placement paths
(``_place_scale_out_limit``'s legacy GTC limit and the Alpaca tranche OCO) write
``scale_limit_order_id`` only inside ``if res["ok"] and res["order_id"]``.  A lost
response -- which the venue layer already classifies as ``indeterminate``, because
the broker may have committed the order before the response path failed -- leaves
the sell resting and the ledger empty.  The fix records the client_order_id
DURABLY BEFORE the submit and resolves it against broker truth, which is the one
handle that survives a lost response.

Where today's behaviour is right it is unchanged: a genuinely absent sibling
still passes the full quantity through, and a determinate outcome (broker
rejection, pre-transport block, or an adapter that does not classify at all)
retires the marker and touches nothing.  Each defect therefore gets BOTH a
byte-identity control and a negative control that fails against the unfixed code.

DB-free and network-free: pure helpers, duck-typed order/session doubles, and AST
guards over the source text.  ``_commit_le`` explicitly tolerates a
``SimpleNamespace`` session double, and every branch exercised here returns before
any adapter or DB round-trip that would need one.
"""

from __future__ import annotations

import ast
import inspect
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import live_runner


# --------------------------------------------------------------------------
# doubles
# --------------------------------------------------------------------------


class _FakeOrder:
    """Duck-typed broker order: only the attributes the matchers actually read."""

    def __init__(
        self,
        *,
        order_id: str,
        client_order_id: str,
        product_id: str,
        order_type: str,
        filled_size: float,
        raw: dict,
        side: str = "sell",
        status: str = "new",
    ) -> None:
        self.order_id = order_id
        self.client_order_id = client_order_id
        self.product_id = product_id
        self.side = side
        self.order_type = order_type
        self.filled_size = filled_size
        self.status = status
        self.average_filled_price = None
        self.raw = raw


_PREDECESSOR_OID = "pred-oid"
_SUCCESSOR_OID = "succ-oid"
_SUCCESSOR_CID = "succ-cid"
_Q = 355.0


def _predecessor_request(qty: str = "355") -> dict:
    """The immutable long-close stop envelope the deadman outbox recorded."""
    return {
        "product_id": "BIAF",
        "order_type": "stop",
        "base_size": qty,
        "stop_price": "1.23",
        "time_in_force": "day",
        "extended_hours": False,
        "client_order_id": "pred-cid",
    }


def _successor_order(qty: str) -> _FakeOrder:
    """A stop resting at the broker for ``qty`` shares, linked to the predecessor."""
    return _FakeOrder(
        order_id=_SUCCESSOR_OID,
        client_order_id=_SUCCESSOR_CID,
        product_id="BIAF",
        order_type="stop",
        filled_size=0.0,
        raw={
            "qty": qty,
            "time_in_force": "day",
            "position_intent": "sell_to_close",
            "stop_price": "1.23",
            "extended_hours": False,
            "replaces": _PREDECESSOR_OID,
        },
    )


def _matches(order: _FakeOrder, envelope: dict) -> bool:
    return live_runner._alpaca_replacement_successor_order_matches(
        order,
        predecessor_broker_order_id=_PREDECESSOR_OID,
        successor_broker_order_id=_SUCCESSOR_OID,
        successor_order_request=envelope,
    )


def _envelope(**kwargs):
    builder = getattr(live_runner, "_alpaca_replacement_successor_envelope", None)
    assert builder is not None, (
        "S1 UNFIXED: the successor envelope is still built inline in "
        "_dispatch_alpaca_replaced_deadman_successor; there is no seam to test."
    )
    return builder(**kwargs)


# --------------------------------------------------------------------------
# S1 -- byte-identity controls
# --------------------------------------------------------------------------


def test_default_envelope_is_the_literal_predecessor_spread():
    """No quantity supplied -> EXACTLY the historical inline dict.

    ``base_size`` must be the SAME OBJECT, not merely an equal one: re-formatting
    it through a float round-trip would hand the matcher a different string to
    parse, which is the whole failure mode this seam must not introduce.
    """
    pred = _predecessor_request()
    env = _envelope(
        predecessor_request=pred,
        successor_client_order_id=_SUCCESSOR_CID,
    )
    assert env == {**pred, "client_order_id": _SUCCESSOR_CID}
    assert env["base_size"] is pred["base_size"]


def test_default_envelope_still_certifies_a_same_qty_successor():
    """The ordinary carry-the-quantity replacement still proves its lineage."""
    env = _envelope(
        predecessor_request=_predecessor_request(),
        successor_client_order_id=_SUCCESSOR_CID,
    )
    assert _matches(_successor_order("355"), env) is True


def test_a_blank_successor_cid_still_yields_the_historical_envelope():
    """A CID-less successor must still reach the caller's own lineage conjunction.

    Rejecting it inside the builder would swap the caller's
    ``..._lineage_unproven`` error for a different one on a reachable path.
    """
    pred = _predecessor_request()
    env = _envelope(predecessor_request=pred, successor_client_order_id="")
    assert env == {**pred, "client_order_id": ""}


# --------------------------------------------------------------------------
# S1 -- negative controls (these fail against the unfixed code)
# --------------------------------------------------------------------------


def test_dispatcher_accepts_an_explicit_successor_quantity():
    """The dispatcher must expose the seam, keyword-only, defaulting to today."""
    params = inspect.signature(
        live_runner._dispatch_alpaca_replaced_deadman_successor
    ).parameters
    expected = getattr(params.get("expected_successor_quantity"), "default", "MISSING")
    reserved = getattr(
        params.get("quantity_reserved_outside_successor"), "default", "MISSING"
    )
    assert expected is None, "S1 UNFIXED: no expected_successor_quantity parameter"
    assert reserved == 0.0, "S1 UNFIXED: no quantity_reserved_outside_successor parameter"
    assert (
        params["expected_successor_quantity"].kind is inspect.Parameter.KEYWORD_ONLY
    )
    assert (
        params["quantity_reserved_outside_successor"].kind
        is inspect.Parameter.KEYWORD_ONLY
    )


def test_a_shrunk_successor_certifies_against_a_shrunk_envelope():
    """THE DEFECT, INVERTED: a 249-share successor of a 355-share predecessor
    proves its lineage once the envelope is allowed to carry the real size."""
    env = _envelope(
        predecessor_request=_predecessor_request(),
        successor_client_order_id=_SUCCESSOR_CID,
        expected_successor_quantity=249.0,
    )
    assert env["base_size"] == "249"
    assert _matches(_successor_order("249"), env) is True


@pytest.mark.parametrize(
    "bad",
    [0.0, -1.0, float("nan"), float("inf"), _Q + 1.0, "not-a-number"],
)
def test_a_growing_or_unreadable_successor_quantity_is_refused(bad):
    """Fail closed.  A deadman successor may CARRY or SHRINK the protected
    quantity, never grow it -- growth would protect shares this lineage was
    never granted authority over."""
    assert (
        _envelope(
            predecessor_request=_predecessor_request(),
            successor_client_order_id=_SUCCESSOR_CID,
            expected_successor_quantity=bad,
        )
        is None
    )


# --------------------------------------------------------------------------
# S1 -- characterisation: the deadlock still holds for a default envelope
# --------------------------------------------------------------------------


def test_a_shrunk_successor_cannot_certify_against_the_predecessor_envelope():
    """This is the deadlock itself: the predecessor-sized envelope can never
    match a resized successor, and the caller's failure is ``pending``, so the
    tick re-enters and fails identically forever."""
    env = _envelope(
        predecessor_request=_predecessor_request(),
        successor_client_order_id=_SUCCESSOR_CID,
    )
    assert _matches(_successor_order("249"), env) is False


# --------------------------------------------------------------------------
# S2 -- doubles
# --------------------------------------------------------------------------


def _sess(execution_family: str = "robinhood_spot") -> SimpleNamespace:
    """``_commit_le`` documents that a SimpleNamespace double is supported."""
    return SimpleNamespace(
        id=1,
        symbol="BIAF",
        execution_family=execution_family,
        risk_snapshot_json={},
    )


class _SpyAdapter:
    """Records which order id the clamp actually went after.

    ``get_order_truth`` / ``get_order_by_client_order_id_truth`` are the two
    broker-truth primitives the recovery path uses; supply them per test so a
    test can pin "the broker cannot be read" as distinctly as "the broker
    answered".
    """

    def __init__(
        self,
        le: dict,
        *,
        truth: dict | None = None,
        cid_truth: dict | None = None,
    ) -> None:
        self._le = le
        self._truth = truth
        self._cid_truth = cid_truth
        self.cancelled: list[str] = []
        self.ledger_at_cancel: list[object] = []
        self.cid_lookups: list[str] = []

    def cancel_order(self, oid: str) -> None:
        self.cancelled.append(oid)
        self.ledger_at_cancel.append(self._le.get("scale_limit_order_id"))

    def get_order(self, oid: str):
        return None, None

    def __getattr__(self, name: str):
        # Only expose the truth primitives a test actually configured, so an
        # adapter WITHOUT them is a faithful double for a venue that cannot
        # answer -- which must fail closed, not fall through.
        if name == "get_order_truth" and self._truth is not None:
            return lambda oid: self._truth
        if name == "get_order_by_client_order_id_truth" and self._cid_truth is not None:

            def _lookup(cid: str):
                self.cid_lookups.append(cid)
                return self._cid_truth

            return _lookup
        raise AttributeError(name)


def _sibling_order(order_id: str, cid: str, *, qty: str = "40", px: str = "5.25"):
    return _FakeOrder(
        client_order_id=cid,
        order_id=order_id,
        product_id="BIAF",
        side="sell",
        order_type="limit",
        raw={
            "qty": qty,
            "limit_price": px,
            "position_intent": "sell_to_close",
        },
        filled_size=0.0,
    )


@pytest.fixture
def emitted(monkeypatch) -> list[tuple[str, dict]]:
    """Capture ``_emit`` instead of reaching the DB.

    Only the adoption path emits, and it does so AFTER deciding; every other
    branch exercised here returns before any DB round-trip.
    """
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        live_runner,
        "_emit",
        lambda db, sess, event_type, payload: seen.append((event_type, payload)),
    )
    return seen


def _clamp(**kwargs):
    return live_runner._cancel_scale_limit_and_clamp(**kwargs)


# --------------------------------------------------------------------------
# S2 -- byte-identity controls
# --------------------------------------------------------------------------


def test_no_sibling_still_passes_the_full_quantity_through():
    """The pass-through is CORRECT when there is genuinely no sibling sell, and
    that is the overwhelmingly common case.  Nothing may be written to the
    ledger on the way out."""
    le: dict = {}
    sess = _sess()
    assert _clamp(
        db=None,
        sess=sess,
        adapter=None,
        le=le,
        requested_qty=355.0,
        reason="stop",
    ) == 355.0
    assert le == {}
    assert sess.risk_snapshot_json == {}


def test_a_present_ledger_id_never_consults_the_resolver():
    """The ledger is authoritative.  A caller that offers a resolver cannot
    perturb the tracked path."""
    calls: list[int] = []
    le: dict = {"scale_limit_order_id": "abc", "position": {"quantity": 100.0}}
    adapter = _SpyAdapter(le)
    released = _clamp(
        db=None,
        sess=_sess(),
        adapter=adapter,
        le=le,
        requested_qty=355.0,
        reason="stop",
        sibling_order_id_resolver=(
            lambda: calls.append(1) or ("should-not-be-used", False)
        ),
    )
    assert calls == []
    assert adapter.cancelled == ["abc"]
    assert released == 100.0


# --------------------------------------------------------------------------
# S2 -- negative controls (these fail against the unfixed code)
# --------------------------------------------------------------------------


def test_an_unreadable_sibling_resolver_fails_closed():
    """A sibling was asserted but could not be read.  Releasing the full
    requested quantity here is precisely the oversell this function exists to
    prevent, so the answer must be None -- never a quantity."""

    def _boom():
        raise RuntimeError("broker unreadable")

    le: dict = {}
    sess = _sess("alpaca_spot")
    assert (
        _clamp(
            db=None,
            sess=sess,
            adapter=None,
            le=le,
            requested_qty=355.0,
            reason="stop",
            sibling_order_id_resolver=_boom,
        )
        is None
    )
    assert (
        le["alpaca_scale_limit_release_block"]["reason"]
        == "scale_limit_sibling_id_unreadable"
    )


@pytest.mark.parametrize(
    "answer",
    [
        None,               # the idiomatic "I could not read it" return
        ("sib-1",),         # malformed
        "sib-1",            # a bare id: no readability claim at all
        (None, True),       # explicitly unreadable
        ("   ", False),     # junk
        ("sib-1", "yes"),   # non-bool flag
    ],
)
def test_only_an_explicit_readable_answer_can_avoid_failing_closed(answer):
    """The resolver's tri-state lives in its RETURN TYPE.

    A bare ``None`` is what every Python lookup returns when it could not find
    OR could not read the thing, and ``str | None`` cannot tell those apart.  A
    resolver written as ``try: ... except: return None`` -- the idiom used
    throughout this very module -- would therefore have been read as "there is
    no sibling" and released the full quantity against a live resting sell.
    Anything that is not an explicit ``(id_or_None, False)`` fails closed.
    """
    le: dict = {}
    assert (
        _clamp(
            db=None,
            sess=_sess("alpaca_spot"),
            adapter=None,
            le=le,
            requested_qty=355.0,
            reason="stop",
            sibling_order_id_resolver=lambda: answer,
        )
        is None
    )


def test_an_explicitly_absent_sibling_keeps_the_pass_through():
    """A resolver is allowed to say "there is no sibling", and that answer is
    the same correct pass-through as having no resolver at all."""
    le: dict = {}
    assert (
        _clamp(
            db=None,
            sess=_sess("alpaca_spot"),
            adapter=None,
            le=le,
            requested_qty=355.0,
            reason="stop",
            sibling_order_id_resolver=lambda: (None, False),
        )
        == 355.0
    )
    assert le == {}


def test_a_non_dict_ledger_is_unreadable_not_a_full_release():
    """The clamp reads the ledger FIRST.  If it cannot read that, it cannot
    claim there is no sibling -- and must not release the full quantity against
    a ledger whose ``scale_limit_order_id`` it never saw."""
    assert (
        live_runner._resolve_scale_limit_sibling_order_id(["not", "a", "dict"])
        == (None, True)
    )
    assert live_runner._resolve_scale_limit_sibling_order_id("") == (None, True)


def test_an_unprovable_sibling_id_writes_nothing_durable():
    """FAIL CLOSED, LEAVE NO WRECKAGE.

    A bare id adopted into the ledger before it is proven is unrecoverable: the
    strict identity gate needs ``scale_limit_qty``/``scale_limit_px``, which a
    bare id never carries, so every later tick would block the exit forever --
    and the id ALONE trips the deadman's head guard, standing the position down
    from protection.  Refusing costs one blocked attempt and nothing durable.
    """
    le: dict = {"position": {"quantity": 100.0}}
    adapter = _SpyAdapter(le)  # no truth primitives => nothing is provable
    released = _clamp(
        db=None,
        sess=_sess("alpaca_spot"),
        adapter=adapter,
        le=le,
        requested_qty=355.0,
        reason="stop",
        sibling_order_id_resolver=lambda: ("sib-1", False),
    )
    assert released is None
    assert adapter.cancelled == []
    assert "scale_limit_order_id" not in le
    assert "scale_limit_qty" not in le
    assert (
        le["alpaca_scale_limit_release_block"]["reason"]
        == "scale_limit_sibling_identity_unprovable"
    )


def test_a_broker_proven_sibling_is_adopted_with_a_broker_read_identity(emitted):
    """Adoption must IMPLY provability: the identity written durably is read
    from broker truth, never asserted, and the order must be provably OURS."""
    le: dict = {"position": {"quantity": 100.0}}
    order = _sibling_order("sib-1", "chili_ml_sol_1_deadbeef")
    adapter = _SpyAdapter(
        le,
        truth={"readable": True, "found": True, "order": order},
    )
    released = _clamp(
        db=None,
        sess=_sess(),
        adapter=adapter,
        le=le,
        requested_qty=355.0,
        reason="stop",
        sibling_order_id_resolver=lambda: ("sib-1", False),
    )
    assert adapter.cancelled == ["sib-1"]
    assert adapter.ledger_at_cancel == ["sib-1"]
    assert le["scale_limit_qty"] == 40.0
    assert le["scale_limit_px"] == 5.25
    assert le["scale_limit_adopted_qty"] == 0.0
    assert released == 100.0
    assert "scale_limit_sibling_recovered_from_lost_placement" in [
        e for e, _ in emitted
    ]


def test_a_sibling_belonging_to_someone_else_is_never_adopted():
    """A foreign or stale id is not ours to cancel or to make durable."""
    le: dict = {"position": {"quantity": 100.0}}
    order = _sibling_order("sib-1", "someone-elses-order")
    adapter = _SpyAdapter(
        le,
        truth={"readable": True, "found": True, "order": order},
    )
    assert (
        _clamp(
            db=None,
            sess=_sess(),
            adapter=adapter,
            le=le,
            requested_qty=355.0,
            reason="stop",
            sibling_order_id_resolver=lambda: ("sib-1", False),
        )
        is None
    )
    assert adapter.cancelled == []
    assert "scale_limit_order_id" not in le


# --------------------------------------------------------------------------
# S2 -- the LIVE-REACHABLE source of an invisible sibling: a lost response
# --------------------------------------------------------------------------


def _intent(cid: str = "chili_ml_toco_1_deadbeef") -> dict:
    return {
        "client_order_id": cid,
        "qty": 40.0,
        "limit_price": 5.25,
        "kind": "oco",
        "recorded_at_utc": "2026-09-02T00:00:00+00:00",
    }


def test_a_lost_placement_is_resolved_by_our_own_client_order_id(emitted):
    """``scale_limit_order_id`` can only be written from a response, so a lost
    response leaves a live resting sell invisible and the exit releases the full
    position against it.  The client id is minted BEFORE the submit, so it
    survives the lost response and is what broker truth is asked about."""
    cid = "chili_ml_toco_1_deadbeef"
    le: dict = {"position": {"quantity": 100.0}, "scale_limit_place_intent": _intent(cid)}
    order = _sibling_order("sib-1", cid)
    adapter = _SpyAdapter(
        le,
        truth={"readable": True, "found": True, "order": order},
        cid_truth={"readable": True, "found": True, "order": order},
    )
    released = _clamp(
        db=None,
        sess=_sess(),
        adapter=adapter,
        le=le,
        requested_qty=355.0,
        reason="stop",
    )
    assert adapter.cid_lookups == [cid]
    assert adapter.cancelled == ["sib-1"]
    assert le.get("scale_limit_is_oco") is True
    assert "scale_limit_place_intent" not in le
    assert released == 100.0


def test_a_lost_placement_the_broker_says_never_landed_is_retired():
    """An explicit readable "no such order" is the ONLY thing that clears the
    marker, and it restores the correct full-quantity pass-through."""
    le: dict = {"scale_limit_place_intent": _intent()}
    adapter = _SpyAdapter(le, cid_truth={"readable": True, "found": False})
    assert (
        _clamp(
            db=None,
            sess=_sess(),
            adapter=adapter,
            le=le,
            requested_qty=355.0,
            reason="stop",
        )
        == 355.0
    )
    assert "scale_limit_place_intent" not in le


@pytest.mark.parametrize(
    "cid_truth",
    [
        None,                                   # adapter cannot look up by client id
        {"readable": False},                    # broker unreadable
        {"readable": True},                     # answered nothing useful
        {"readable": True, "found": True, "order": None},
    ],
)
def test_a_lost_placement_that_cannot_be_read_fails_closed(cid_truth):
    """"Cannot tell" must never collapse into "no sibling"."""
    le: dict = {"scale_limit_place_intent": _intent()}
    adapter = _SpyAdapter(le, cid_truth=cid_truth)
    assert (
        _clamp(
            db=None,
            sess=_sess(),
            adapter=adapter,
            le=le,
            requested_qty=355.0,
            reason="stop",
        )
        is None
    )
    assert le["scale_limit_place_intent"]["client_order_id"]


def test_the_place_intent_survives_only_an_indeterminate_submit():
    """The venue layer already classifies a failed submit.  Only
    ``indeterminate`` means the broker may have committed the order before the
    response path failed; a definitive rejection, a pre-transport block and an
    adapter that does not classify at all keep today's behaviour exactly."""
    for response, survives in (
        ({"ok": False, "submit_outcome": "indeterminate"}, True),
        ({"ok": False, "submit_outcome": "broker_rejected"}, False),
        ({"ok": False, "submit_outcome": "pre_transport_blocked"}, False),
        ({"ok": False, "error": "no classification"}, False),
        ({}, False),
        (None, False),
    ):
        le: dict = {"scale_limit_place_intent": _intent()}
        live_runner._clear_scale_limit_place_intent_if_determinate(
            _sess(), le, response
        )
        assert ("scale_limit_place_intent" in le) is survives, response


def test_the_clamp_signature_carries_the_seam():
    params = inspect.signature(live_runner._cancel_scale_limit_and_clamp).parameters
    resolver = params.get("sibling_order_id_resolver")
    assert resolver is not None, "S2 UNFIXED: no sibling_order_id_resolver parameter"
    assert resolver.default is None
    assert resolver.kind is inspect.Parameter.KEYWORD_ONLY


# --------------------------------------------------------------------------
# AST guards -- structure, not text.  Regex over Python source is how #1283
# shipped a green helper suite against a seam that was never wired in.
# --------------------------------------------------------------------------


def _function_def(name: str) -> ast.FunctionDef:
    tree = ast.parse(Path(live_runner.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in live_runner")


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            if sub.func.id == name:
                out.append(sub)
    return out


def test_the_successor_envelope_is_not_built_inline_any_more():
    """S1 structural guard: the dispatcher must delegate envelope construction,
    and must no longer contain a ``{**<something>_request, "client_order_id": ...}``
    literal that would silently reintroduce the predecessor's quantity."""
    fn = _function_def("_dispatch_alpaca_replaced_deadman_successor")
    assert _calls(fn, "_alpaca_replacement_successor_envelope"), (
        "S1 UNFIXED: the dispatcher does not call the envelope builder"
    )
    for sub in ast.walk(fn):
        if not isinstance(sub, ast.Dict):
            continue
        spread_request = any(
            key is None
            and isinstance(val, ast.Name)
            and val.id.endswith("_request")
            for key, val in zip(sub.keys, sub.values)
        )
        names_cid = any(
            isinstance(key, ast.Constant) and key.value == "client_order_id"
            for key in sub.keys
        )
        assert not (spread_request and names_cid), (
            "S1 UNFIXED: an inline successor envelope literal survives at line "
            f"{sub.lineno}"
        )


def test_the_scale_limit_pass_through_is_no_longer_the_first_statement():
    """S2 structural guard: the sibling id must be RESOLVED before any return can
    hand back a quantity."""
    fn = _function_def("_cancel_scale_limit_and_clamp")
    resolves = _calls(fn, "_resolve_scale_limit_sibling_order_id")
    assert resolves, "S2 UNFIXED: the clamp does not resolve the sibling id"
    first_return = min(
        sub.lineno for sub in ast.walk(fn) if isinstance(sub, ast.Return)
    )
    assert min(call.lineno for call in resolves) < first_return


def test_the_ast_guards_can_actually_find_something():
    """Positive control for the walkers themselves.

    A structural guard that silently scans nothing is worse than no guard: it
    reports green forever.  These two calls are known to exist in those bodies
    at HEAD, so if the walker stops finding them the guards above are inert.
    """
    assert _calls(
        _function_def("_dispatch_alpaca_replaced_deadman_successor"),
        "_alpaca_replacement_successor_order_matches",
    )
    assert _calls(
        _function_def("_cancel_scale_limit_and_clamp"),
        "_scale_order_total_fill",
    )


def test_the_dispatcher_delegates_the_quantity_frame():
    """S1 structural guard: the conservation arithmetic must live in the pure
    helper the tests above can actually prove, not inline where only a full
    broker+DB round trip could reach it."""
    fn = _function_def("_dispatch_alpaca_replaced_deadman_successor")
    assert _calls(fn, "_alpaca_replacement_quantity_frame"), (
        "S1 UNFIXED: the dispatcher does not delegate the quantity frame"
    )
    assert _calls(fn, "_alpaca_deadman_reserved_tranche_quantity"), (
        "S1 UNFIXED: the asserted reserve is not bound to the ledger"
    )


def _attr_calls(node: ast.AST, attr: str) -> list[ast.Call]:
    return [
        sub
        for sub in ast.walk(node)
        if isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Attribute)
        and sub.func.attr == attr
    ]


def test_every_scale_out_placement_records_its_intent_first():
    """S2 wired-in guard (the #1283 lesson: a green helper suite against a seam
    nothing calls).  The durable "a sibling MAY now rest" marker is worthless
    unless it is written BEFORE the submit that can lose its response."""
    fn = _function_def("_place_scale_out_limit")
    records = _calls(fn, "_record_scale_limit_place_intent")
    assert len(records) >= 2, (
        "S2 UNFIXED: not every placement in _place_scale_out_limit records an "
        f"intent (found {len(records)})"
    )
    first_record = min(call.lineno for call in records)
    placements = _attr_calls(fn, "place_limit_order_gtc") + _calls(fn, "_oco_place")
    assert placements, "guard is inert: no placement call found"
    for call in placements:
        assert first_record < call.lineno, (
            f"S2 UNFIXED: placement at line {call.lineno} precedes any intent record"
        )
    assert _calls(fn, "_clear_scale_limit_place_intent_if_determinate"), (
        "S2 UNFIXED: the intent is never retired on a determinate outcome"
    )


def test_the_clamp_never_writes_a_bare_sibling_id():
    """The one line that made a resolver-supplied id unrecoverable was a direct
    ``le["scale_limit_order_id"] = ...`` assignment ahead of any identity proof.
    Adoption must go through the broker-truth helper instead."""
    fn = _function_def("_cancel_scale_limit_and_clamp")
    for sub in ast.walk(fn):
        if not isinstance(sub, ast.Assign):
            continue
        for target in sub.targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.slice, ast.Constant)
                and target.slice.value == "scale_limit_order_id"
            ):
                raise AssertionError(
                    "S2 UNFIXED: a bare sibling id is written durably at line "
                    f"{sub.lineno}, before anything proves it"
                )
    assert _calls(fn, "_adopt_recovered_scale_limit_sibling")


# --------------------------------------------------------------------------
# S1 -- the quantity frame (pure arithmetic, provable without a broker)
# --------------------------------------------------------------------------


def _frame(**kwargs):
    base = dict(
        requested_qty=_Q,
        successor_qty=_Q,
        reserved_qty=0.0,
        broker_qty=_Q,
        local_qty=_Q,
        predecessor_fill=0.0,
        successor_fill=0.0,
        ledger_reserved_qty=0.0,
    )
    base.update(kwargs)
    return live_runner._alpaca_replacement_quantity_frame(**base)


def test_the_default_frame_is_the_historical_arithmetic():
    """Byte-identity control: with no successor quantity and no reserve, every
    number collapses onto the predecessor's and the delta is the historical
    ``requested_qty - broker_qty``."""
    frame, error = _frame(broker_qty=300.0)
    assert error is None
    assert frame["covered_qty"] == _Q
    assert frame["quantity_delta"] == _Q - 300.0
    assert frame["tol"] == max(1e-9, _Q * 1e-8)


def test_a_plain_shrink_no_longer_deadlocks():
    """106 shares genuinely left the position and the replacement resized the
    resting stop to match.  The old code anchored the delta to the PREDECESSOR's
    355, so ``conserved`` demanded broker==355 while the coverage bound demanded
    broker==249 -- mutually exclusive, ``pending``, and therefore forever."""
    frame, error = _frame(successor_qty=249.0, broker_qty=249.0, local_qty=249.0)
    assert error is None
    assert frame["covered_qty"] == 249.0
    assert frame["quantity_delta"] == 0.0


def test_a_reserved_tranche_frame_is_admitted():
    """The deadman protects the runner R while an OCO tranche f rests; the
    POSITION carries R+f.  The old bound compared the broker's R+f against the
    predecessor's R and failed by exactly f."""
    frame, error = _frame(
        requested_qty=249.0,
        successor_qty=249.0,
        reserved_qty=106.0,
        broker_qty=355.0,
        local_qty=355.0,
        ledger_reserved_qty=106.0,
    )
    assert error is None
    assert frame["covered_qty"] == 355.0
    assert frame["quantity_delta"] == 0.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"successor_qty": 400.0, "local_qty": 400.0},           # grows
        {"successor_qty": 0.0},                                  # non-positive
        {"successor_qty": float("nan")},                         # unreadable
        {"broker_qty": _Q + 1.0},                                # exceeds coverage
        {"local_qty": None},                                     # ledger unreadable
        {"local_qty": 300.0},                                    # ledger disagrees
        {"predecessor_fill": -1.0},
    ],
)
def test_the_frame_still_fails_closed(kwargs):
    _, error = _frame(**kwargs)
    assert error == "replacement_deadman_successor_quantity_generation_mismatch"


def test_a_negative_reserve_can_never_shrink_the_re_armed_stop():
    """``covered_qty`` is handed straight to the re-arm.  A sign error in a
    future reservation calculation would otherwise arm a stop smaller than the
    position it must protect."""
    _, error = _frame(
        successor_qty=300.0, reserved_qty=-50.0, broker_qty=250.0, local_qty=250.0
    )
    assert error == "replacement_deadman_successor_quantity_generation_mismatch"


@pytest.mark.parametrize("reserved,ledger", [(5.0, 2.0), (2.0, 5.0), (5.0, 0.0)])
def test_the_reserve_must_be_the_one_the_rearm_will_subtract(reserved, ledger):
    """The reserve decides how many shares this lineage claims authority over.
    Over-state it and a protection gap is recorded as certified; under-state it
    and the re-armed stop is smaller than the shares it must cover.  It is bound
    to ``le["scale_limit_qty"]`` -- the only quantity the re-arm subtracts."""
    _, error = _frame(
        requested_qty=350.0,
        successor_qty=350.0,
        reserved_qty=reserved,
        broker_qty=350.0 + reserved,
        local_qty=350.0 + reserved,
        ledger_reserved_qty=ledger,
    )
    assert error == "replacement_deadman_reserved_quantity_not_ledger_backed"


def test_the_ledger_reserve_mirrors_the_rearm_head_guard(monkeypatch):
    """It must return exactly what ``_ensure_alpaca_deadman_stop`` subtracts --
    which is nothing at all unless a tracked OCO tranche is in force."""
    assert live_runner._alpaca_deadman_reserved_tranche_quantity({}) == 0.0
    assert live_runner._alpaca_deadman_reserved_tranche_quantity(None) == 0.0
    legacy = {"scale_limit_order_id": "x", "scale_limit_qty": 40.0}
    # A legacy (non-OCO) sibling makes the re-arm FULL CLOSE, not split, so a
    # reserve asserted there would be a protection gap.
    assert live_runner._alpaca_deadman_reserved_tranche_quantity(legacy) == 0.0
    oco = {**legacy, "scale_limit_is_oco": True}
    monkeypatch.setattr(
        live_runner.settings,
        "chili_momentum_alpaca_protected_partial_enabled",
        True,
        raising=False,
    )
    assert live_runner._alpaca_deadman_reserved_tranche_quantity(oco) == 40.0
    monkeypatch.setattr(
        live_runner.settings,
        "chili_momentum_alpaca_protected_partial_enabled",
        False,
        raising=False,
    )
    assert live_runner._alpaca_deadman_reserved_tranche_quantity(oco) == 0.0


def test_the_successor_envelope_builder_is_pure():
    """The builder must not reach for a session, a db handle or an adapter --
    it is a value transform and is called before any lineage is proven."""
    fn = _function_def("_alpaca_replacement_successor_envelope")
    forbidden = {"_commit_le", "_emit", "_utcnow"}
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            assert sub.func.id not in forbidden
    assert not math.isnan(0.0)  # sanity: module imported cleanly

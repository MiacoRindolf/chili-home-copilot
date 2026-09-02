"""Deadman protection lineage: two latent defects on the per-tick Alpaca path.

Both live in the protection path that runs for EVERY Alpaca position on EVERY
tick, and both are latent today -- which is exactly why they are worth pinning
before something makes them live.

S1 -- the replaced-successor envelope kept the predecessor's quantity.
``_dispatch_alpaca_replaced_deadman_successor`` built the expected successor
envelope as ``{**predecessor_request, "client_order_id": cid}``, so ``base_size``
stayed the PREDECESSOR's Q while ``_owner_transport_order_matches`` demands exact
quantity equality against the order actually resting at the broker.  A deadman
replace that CHANGES the quantity therefore fails by exactly the delta -- and the
failure is ``pending``, not terminal, so the tick re-enters and fails identically
forever.  Nothing in production replaces a deadman stop with a different quantity
(``replace_order_qty`` has zero production callers, and no ``replaced`` lifecycle
has ever been observed), so this cannot fire today; an operator replacing the
resting stop by hand, or an Alpaca corporate-action adjustment re-issuing open
orders at an adjusted size, would deadlock the lineage permanently.

S2 -- the scale-limit clamp was a silent pass-through.
``_cancel_scale_limit_and_clamp`` opened with ``if not oid: return requested_qty``,
so a live sibling sell whose id the ledger never recorded would be invisible and
the exit would release the FULL requested quantity against a broker position that
has shares reserved by that sibling.  Today the ledger is the only possible
source of that id, so the pass-through is only ever taken when there genuinely is
no sibling -- where returning the full quantity is CORRECT.  The tests below pin
that correct case as much as they pin the seam.

The mandate for both fixes is byte-identity: where today's behaviour is right, it
must be unchanged.  Each defect therefore gets BOTH a byte-identity control (the
default path still computes exactly what it computed before) and a negative
control that fails against the unfixed code.

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
    """Records which order id the clamp actually went after."""

    def __init__(self, le: dict) -> None:
        self._le = le
        self.cancelled: list[str] = []
        self.ledger_at_cancel: list[object] = []

    def cancel_order(self, oid: str) -> None:
        self.cancelled.append(oid)
        self.ledger_at_cancel.append(self._le.get("scale_limit_order_id"))

    def get_order(self, oid: str):
        return None, None


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
        sibling_order_id_resolver=lambda: calls.append(1) or "should-not-be-used",
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

    def _boom() -> str:
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


def test_a_blank_resolved_sibling_id_is_unreadable_not_absent():
    """A resolver that answers with junk has not answered "no sibling"."""
    le: dict = {}
    assert (
        _clamp(
            db=None,
            sess=_sess("alpaca_spot"),
            adapter=None,
            le=le,
            requested_qty=355.0,
            reason="stop",
            sibling_order_id_resolver=lambda: "   ",
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
            sibling_order_id_resolver=lambda: None,
        )
        == 355.0
    )
    assert le == {}


def test_a_resolved_sibling_id_is_adopted_and_reaches_the_cancel_path():
    """The resolved id must be durably adopted into the ledger BEFORE the strict
    identity path runs, so this tick and every later one work against the same
    order rather than a value that lives only on this stack."""
    le: dict = {"position": {"quantity": 100.0}}
    adapter = _SpyAdapter(le)
    released = _clamp(
        db=None,
        sess=_sess(),
        adapter=adapter,
        le=le,
        requested_qty=355.0,
        reason="stop",
        sibling_order_id_resolver=lambda: "sib-1",
    )
    assert adapter.cancelled == ["sib-1"]
    assert adapter.ledger_at_cancel == ["sib-1"]
    assert released == 100.0


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


def test_the_successor_envelope_builder_is_pure():
    """The builder must not reach for a session, a db handle or an adapter --
    it is a value transform and is called before any lineage is proven."""
    fn = _function_def("_alpaca_replacement_successor_envelope")
    forbidden = {"_commit_le", "_emit", "_utcnow"}
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            assert sub.func.id not in forbidden
    assert not math.isnan(0.0)  # sanity: module imported cleanly

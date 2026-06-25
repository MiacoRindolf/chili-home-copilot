"""Agentic BROKER-QTY CLAMP — held-quantity resolution for the exit path.

The exit clamp in ``momentum_neural/live_runner._submit_live_market_exit`` asks the
venue adapter ``get_position_quantity(product_id)`` for the shares the BROKER truly
holds, then sells only that (killing the "Not enough shares to sell" -> 8-retry ->
naked-long storm, e.g. PLSM 2026-06-24 agentic sessions 8613/8616).

These tests pin the *agentic* adapter's new ``get_position_quantity`` to the same
fail-safe contract the spot path uses:

  * a partial held qty (< requested) resolves to the real number so the clamp
    sells only what is held;
  * a confirmed-absent symbol resolves to ``0.0`` (confirmed flat -> ``broker_zero``);
  * an API/parse error resolves to ``None`` (unknown -> clamp leaves the request
    UNCHANGED, never treats unknown as flat).

DB-free + network-free: ``get_agentic_open_positions`` is stubbed on the instance,
so no MCP transport, no broker, no DB.
"""

from __future__ import annotations

from app.services.trading.venue.robinhood_mcp import RobinhoodAgenticMcpAdapter


def _bare_adapter() -> RobinhoodAgenticMcpAdapter:
    """Adapter without running ``__init__`` — we only exercise the pure read/parse."""
    return RobinhoodAgenticMcpAdapter.__new__(RobinhoodAgenticMcpAdapter)


def test_partial_held_qty_resolves_for_clamp():
    """Broker holds LESS than the tracked size -> the real held qty is returned so the
    clamp can sell only what exists (the strand-preventing case)."""
    a = _bare_adapter()
    a.get_agentic_open_positions = lambda: [
        {"symbol": "OTHER", "quantity": 10.0},
        {"symbol": "PLSM", "quantity": 3.0},
    ]
    # Session 'remembers' selling more (e.g. 5) but broker holds 3.
    assert a.get_position_quantity("PLSM-USD") == 3.0
    # Plain ticker form (agentic equities) also resolves.
    assert a.get_position_quantity("PLSM") == 3.0


def test_absent_symbol_resolves_zero_confirmed_flat():
    """A SUCCESSFUL fetch with the symbol absent -> 0.0 (confirmed flat -> broker_zero),
    NOT None. This lets the clamp reconcile the session to exited instead of stranding
    a phantom sell."""
    a = _bare_adapter()
    a.get_agentic_open_positions = lambda: [{"symbol": "OTHER", "quantity": 10.0}]
    assert a.get_position_quantity("PLSM") == 0.0


def test_empty_book_resolves_zero():
    a = _bare_adapter()
    a.get_agentic_open_positions = lambda: []
    assert a.get_position_quantity("PLSM") == 0.0


def test_read_error_resolves_none_fail_safe():
    """An API/re-auth error -> None (unknown). The caller MUST fail safe and leave the
    requested exit quantity unchanged — never treat unknown as flat."""
    a = _bare_adapter()

    def _boom():
        raise RuntimeError("re-auth required")

    a.get_agentic_open_positions = _boom
    assert a.get_position_quantity("PLSM") is None


def test_alt_field_keys_and_abs_short():
    """Field-key flexibility (shares/size/...) + a negative/short qty is abs()'d, matching
    the spot helper's shape."""
    a = _bare_adapter()
    a.get_agentic_open_positions = lambda: [{"ticker": "PLSM", "shares": -4.0}]
    assert a.get_position_quantity("PLSM") == 4.0


def test_empty_product_id_resolves_none():
    a = _bare_adapter()
    a.get_agentic_open_positions = lambda: [{"symbol": "PLSM", "quantity": 3.0}]
    assert a.get_position_quantity("") is None


def test_clamp_arithmetic_uses_resolved_qty():
    """End-to-end-ish: the resolved held qty drives the clamp decision the live_runner
    makes — broker_qty < requested clamps to broker_qty; >= leaves it alone."""
    a = _bare_adapter()
    a.get_agentic_open_positions = lambda: [{"symbol": "PLSM", "quantity": 3.0}]
    broker_qty = a.get_position_quantity("PLSM")
    requested = 5.0
    # Mirror of live_runner's clamp predicate.
    clamped = (
        broker_qty
        if (broker_qty is not None and broker_qty >= 0 and broker_qty < requested - 1e-9)
        else requested
    )
    assert clamped == 3.0

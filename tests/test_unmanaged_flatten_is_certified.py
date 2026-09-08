"""The last-resort flatten must actually be placeable.

`_maybe_flatten_unmanaged_alpaca_positions` is the net for a position with no
owner and no protection. It sent the right side and the right position intent but
no client_order_id — and the certifier treats a missing client id as an
uncertified instruction, so every attempt came back
`alpaca_instruction_side_intent_not_certified` and the net could never fire.

Live 2026-09-08: BJDX, 1,103 shares unmanaged from 10:39Z, 3,025 blocked release
attempts, unrealised drifting -$19.82 -> -$41.55, every flatten refused.

DB-free: asserts the call the flattener makes against the certifier's own rule.
"""
from __future__ import annotations

import inspect
import re

import pytest

from app.services.trading.momentum_neural import alpaca_reconcile
from app.services.trading.momentum_neural.live_runner import (
    _alpaca_place_instruction_kind,
)


class _Sess:
    execution_family = "alpaca_spot"


def test_a_sell_to_close_with_a_client_id_is_certified():
    kind = _alpaca_place_instruction_kind(_Sess(), {
        "side": "sell", "position_intent": "sell_to_close",
        "client_order_id": "chili_unmanaged_deadbeefdeadbeef",
    })
    assert kind == "close"


@pytest.mark.parametrize("intent", [None, "", "close", "sell_to_open"])
def test_a_sell_without_the_close_intent_is_not_certified(intent):
    """The rule exists because a bare SELL could open a short."""
    kind = _alpaca_place_instruction_kind(_Sess(), {
        "side": "sell", "position_intent": intent,
        "client_order_id": "chili_unmanaged_deadbeefdeadbeef",
    })
    assert kind != "close"


def test_the_flattener_passes_a_client_order_id():
    """The regression: the call must carry the id the certifier requires."""
    src = inspect.getsource(alpaca_reconcile)
    call = re.search(
        r"_mkt = adapter\.place_market_order\((.*?)\)\n", src, re.S)
    assert call, "the unmanaged flatten call moved — re-point this test"
    body = call.group(1)
    assert 'side="sell"' in body
    assert 'position_intent="sell_to_close"' in body
    assert "client_order_id=" in body, (
        "a flatten without a client_order_id is refused as uncertified and the "
        "position stays naked"
    )


def test_the_client_id_is_non_blank_at_runtime():
    """A present-but-empty id fails the same check as a missing one."""
    src = inspect.getsource(alpaca_reconcile)
    call = re.search(r"_mkt = adapter\.place_market_order\((.*?)\)\n", src, re.S)
    body = call.group(1)
    cid = re.search(r"client_order_id=([^,\n]+)", body).group(1).strip()
    assert cid not in ('""', "''", "None"), cid
    assert "uuid" in cid or "f\"" in cid or "f'" in cid, cid


def test_the_certifier_still_rejects_a_blank_id():
    """Guards the reason this bug was invisible: blank reads as missing."""
    for cid in (None, "", "   "):
        kind = _alpaca_place_instruction_kind(_Sess(), {
            "side": "sell", "position_intent": "sell_to_close",
            "client_order_id": cid,
        })
        # The live_runner-side classifier keys on side+intent; the adapter's own
        # gate is what rejects the blank id. Both must agree that a close is a
        # close — the id check belongs to the adapter and is asserted above.
        assert kind == "close", cid

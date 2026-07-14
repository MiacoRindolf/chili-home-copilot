"""Dup-Reference 409 reconcile detector (BJDX sid 10484 orphan, 2026-07-06).

The agentic RH rail passes our deterministic entry cid as the order Reference ID, so
an idempotent re-submit of the SAME logical entry (ack-timeout re-place / parallel
watcher) gets ``409 {"detail":"Reference ID must be unique."}``. That 409 CONFIRMS the
first submit is already live at the venue — it must NOT terminalize the session to
live_error (which orphans the filled position, unmanaged). ``_is_dup_reference_reject``
is the narrow detector that routes such a reject into the reconcile-and-adopt branch.
"""

from unittest.mock import MagicMock

from app.services.trading.momentum_neural.live_runner import (
    _bind_recovered_entry_order,
    _is_dup_reference_reject,
    _recover_entry_order_by_client_id,
)
from app.services.trading.venue.protocol import NormalizedOrder


# The EXACT error string that stranded BJDX session 10484 (from
# trading_automation_events.payload_json->result->error, 2026-07-06 10:38:37 ET).
BJDX_409 = (
    "MCP tool 'place_equity_order' returned isError: API error 409: "
    '{"detail":"Reference ID must be unique."}'
)


def test_bjdx_live_incident_string_is_detected():
    assert _is_dup_reference_reject(BJDX_409) is True


def test_detects_phrase_variants():
    assert _is_dup_reference_reject("Reference ID must be unique.") is True
    assert _is_dup_reference_reject("reference id must be unique") is True
    assert _is_dup_reference_reject("API error 409: reference must be unique") is True


def test_ignores_unrelated_rejects():
    # A generic 409 without the reference-uniqueness phrase must NOT trip it — those
    # ARE real place failures that should still terminalize (no silent orphan-adopt).
    assert _is_dup_reference_reject("API error 409: rate limited") is False
    assert _is_dup_reference_reject("EQUITY_SUITABILITY: not tradable") is False
    assert _is_dup_reference_reject("401 not available for agentic trading") is False
    # Alpaca's exact idempotency confirmation (40010001) is the same condition.
    assert _is_dup_reference_reject("client_order_id must be unique") is True


def test_empty_and_none_are_false():
    assert _is_dup_reference_reject(None) is False
    assert _is_dup_reference_reject("") is False


def test_client_id_recovery_requires_exact_broker_order_id():
    adapter = MagicMock()
    recovered = NormalizedOrder(
        order_id="alpaca-98143",
        client_order_id="chili_ml_e_actu",
        product_id="ACTU",
        side="buy",
        status="filled",
        order_type="limit",
        filled_size=17991.0,
        average_filled_price=1.48,
    )
    adapter.get_order_by_client_order_id.return_value = (recovered, None)

    assert _recover_entry_order_by_client_id(adapter, "chili_ml_e_actu") is recovered
    adapter.get_order_by_client_order_id.assert_called_once_with("chili_ml_e_actu")

    adapter.get_order_by_client_order_id.return_value = (None, None)
    assert _recover_entry_order_by_client_id(adapter, "chili_ml_e_missing") is None


def test_binding_recovered_order_clears_reconcile_marker_and_tracks_history():
    le = {
        "entry_submitted": True,
        "entry_client_order_id": "chili_ml_e_actu",
        "entry_reconcile_pending_client_order_id": "chili_ml_e_actu",
        "entry_reconcile_pending_since_utc": "2026-07-13T16:04:29",
    }
    recovered = NormalizedOrder(
        order_id="alpaca-98143",
        client_order_id="chili_ml_e_actu",
        product_id="ACTU",
        side="buy",
        status="open",
        order_type="limit",
        filled_size=0.0,
        average_filled_price=None,
    )

    oid = _bind_recovered_entry_order(le, recovered)

    assert oid == "alpaca-98143"
    assert le["entry_order_id"] == "alpaca-98143"
    assert le["entry_order_ids_all"] == ["alpaca-98143"]
    assert le["entry_submitted"] is True
    assert "entry_reconcile_pending_client_order_id" not in le
    assert "entry_reconcile_pending_since_utc" not in le

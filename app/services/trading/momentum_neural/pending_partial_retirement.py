"""Identity and zero-fill proof for retiring an old fractional exit request.

This module performs no I/O and never infers prior fill application from missing
history. Positive fills need a separate accounting recovery contract.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any


KEY = "pending_partial_retirement"
HISTORY_KEY = "pending_partial_retirement_history"
CONTRACT = "pending_partial_terminal_zero_v1"


def number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def binding(sess: Any, le: dict) -> dict:
    """Freeze ownership/accounting, excluding changing quote/stop observations."""
    snapshot = dict(sess.risk_snapshot_json or {})
    snapshot.pop("momentum_live_execution", None)
    pos = le.get("position") or {}
    return deepcopy({
        "session": {key: getattr(sess, key, None) for key in (
            "id", "user_id", "mode", "execution_family", "symbol", "state",
            "correlation_id", "variant_id",
        )},
        "snapshot": snapshot,
        "entry": {key: value for key, value in le.items() if key.startswith("entry_")},
        "pending": {key: value for key, value in le.items() if (
            key.startswith("pending_exit_") or key in {
                "exit_order_id", "exit_client_order_id",
                "alpaca_active_exit_owner_transport", "exit_submit_transport_identities",
            }
        )},
        "position": {key: pos.get(key) for key in (
            "product_id", "side", "side_long", "quantity", "original_quantity",
            "avg_entry_price", "opened_at_utc",
        )},
        "side_long": le.get("side_long"),
        "protective_orders": {
            "deadman": {key: (le.get("deadman_stop") or {}).get(key)
                        for key in ("order_id", "client_order_id")},
            "scale": {"order_id": le.get("scale_limit_order_id"),
                      "client_order_id": le.get("scale_limit_client_order_id")},
        },
        "whole_decision": deepcopy((le.get("exit_verdict") or {}).get("exit")),
        "verdict_entry_at": (le.get("exit_verdict") or {}).get("entry_at"),
        "accounting": {key: le.get(key) for key in (
            "alpaca_exit_applied_fill_watermarks", "last_exit_broker_truth",
            "realized_pnl_usd", "fees_usd_total", "last_partial_exit_qty",
            "last_partial_exit_at_utc", "last_partial_exit_price",
        )},
    })


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def binding_error(value: dict) -> str | None:
    pending = value["pending"]
    decision = value["whole_decision"]
    qty = number(pending.get("pending_exit_quantity"))
    held = number(value["position"].get("quantity"))
    if not (
        pending.get("pending_exit_is_scale_out") is True
        and pending.get("pending_exit_reason")
        and pending.get("exit_order_id") and pending.get("exit_client_order_id")
        and qty is not None and qty > 0 and held is not None and held > 0
        and isinstance(decision, dict) and decision.get("reason")
        and decision.get("decided_as_of") and number(decision.get("exit_fraction")) == 1
        and value.get("verdict_entry_at")
    ):
        return "pending_partial_identity_unproven"
    oid = str(pending["exit_order_id"])
    cid = str(pending["exit_client_order_id"])
    if any(
        (row.get("order_id") and str(row["order_id"]) == oid)
        or (row.get("client_order_id") and str(row["client_order_id"]) == cid)
        for row in value.get("protective_orders", {}).values()
    ):
        return "pending_partial_protective_identity_conflict"
    transport = pending.get("alpaca_active_exit_owner_transport")
    if transport is not None:
        request = transport.get("order_request") if isinstance(transport, dict) else None
        if not (
            isinstance(request, dict) and transport.get("transport_kind") == "ordinary_exit"
            and transport.get("client_order_id") == cid
            and transport.get("broker_order_id") == oid
            and request.get("client_order_id") == cid
            and request.get("side") == "sell" and request.get("position_intent") == "sell_to_close"
            and str(request.get("product_id") or "").upper() == str(value["session"]["symbol"]).upper()
            and number(request.get("base_size")) == qty
            and request.get("account_scope") == value["snapshot"].get("alpaca_account_scope")
            and request.get("alpaca_account_id") == value["snapshot"].get("alpaca_account_id")
        ):
            return "pending_partial_owner_transport_mismatch"
        for key in ("filled_size", "provider_cumulative_quantity"):
            if key in transport and number(transport[key]) != 0:
                return "pending_partial_prior_accounting_unproven"
    accounting = value["accounting"]
    prior = accounting.get("last_exit_broker_truth")
    if isinstance(prior, dict) and str(prior.get("broker_order_id") or "") == oid:
        observed = number(prior.get("filled_size"))
        if observed is None or observed != 0:
            return "pending_partial_prior_accounting_unproven"
    for row in accounting.get("alpaca_exit_applied_fill_watermarks") or []:
        if not isinstance(row, dict):
            return "pending_partial_prior_accounting_unproven"
        if (str(row.get("broker_order_id") or "") == oid
                or str(row.get("client_order_id") or "") == cid):
            applied = number(row.get("applied_filled_size"))
            if applied is None or applied != 0:
                return "pending_partial_prior_accounting_unproven"
    return None


def order_error(order: Any, value: dict) -> str | None:
    """Require exact broker echoes; caller separately verifies strict readability."""
    pending = value["pending"]
    raw = getattr(order, "raw", None)
    raw = raw if isinstance(raw, dict) else {}
    expected = {
        "order_id": pending["exit_order_id"],
        "client_order_id": pending["exit_client_order_id"],
        "product_id": value["session"]["symbol"], "side": "sell",
    }
    for key, wanted in expected.items():
        actual = str(getattr(order, key, "") or "").strip()
        wanted = str(wanted).strip()
        if key in {"product_id", "side"}:
            actual, wanted = actual.lower(), wanted.lower()
        if actual != wanted:
            return "pending_partial_order_identity_mismatch"
    echoed_identity = {
        "id": expected["order_id"], "client_order_id": expected["client_order_id"],
        "symbol": expected["product_id"], "side": "sell",
        "account_id": value["snapshot"].get("alpaca_account_id"),
        "position_intent": "sell_to_close",
        "broker_account_id_echo": value["snapshot"].get("alpaca_account_id"),
        "broker_order_id_echo": expected["order_id"],
        "broker_client_order_id_echo": expected["client_order_id"],
        "broker_symbol_echo": expected["product_id"], "broker_side_echo": "sell",
        "broker_position_intent_echo": "sell_to_close", "broker_asset_class_echo": "us_equity",
        "broker_order_type_echo": getattr(order, "order_type", None),
        "broker_time_in_force_echo": raw.get("time_in_force"),
    }
    for key, wanted in echoed_identity.items():
        if raw.get(key) is None:
            continue
        actual, wanted = str(raw[key]).strip(), str(wanted).strip()
        if key not in {"id", "client_order_id", "account_id", "broker_account_id_echo",
                       "broker_order_id_echo", "broker_client_order_id_echo"}:
            actual, wanted = actual.lower(), wanted.lower()
        if actual != wanted:
            return "pending_partial_order_identity_mismatch"
    requested = number(pending["pending_exit_quantity"])
    broker_qty = number(raw.get("qty"))
    cumulative = number(getattr(order, "filled_size", None))
    if broker_qty != requested or cumulative is None or not 0 <= cumulative <= requested:
        return "pending_partial_quantity_unproven"
    if raw.get("broker_quantity_echo") is not None and number(raw["broker_quantity_echo"]) != requested:
        return "pending_partial_quantity_unproven"
    for key in ("filled_qty", "filled_size", "alpaca_filled_qty", "broker_filled_quantity_echo"):
        if key in raw and number(raw[key]) != cumulative:
            return "pending_partial_quantity_unproven"
    if "fill_truth_readable" in raw and raw["fill_truth_readable"] is not True:
        return "pending_partial_quantity_unproven"
    if (raw.get("broker_limit_price_echo") is not None
            and (number(raw["broker_limit_price_echo"]) is None
                 or number(raw["broker_limit_price_echo"]) != number(raw.get("limit_price")))):
        return "pending_partial_order_identity_mismatch"
    if (raw.get("broker_extended_hours_echo") is not None
            and (type(raw["broker_extended_hours_echo"]) is not bool
                 or raw["broker_extended_hours_echo"] is not raw.get("extended_hours"))):
        return "pending_partial_order_identity_mismatch"
    if cumulative != 0:
        return "pending_partial_prior_accounting_unproven"
    status = str(getattr(order, "status", "") or "").strip().lower()
    raw_status = str(raw.get("alpaca_status") or raw.get("status") or status).strip().lower()
    echo_status = raw.get("broker_order_status_echo")
    if (echo_status is not None and str(echo_status).strip().lower().replace("cancelled", "canceled")
            != raw_status.replace("cancelled", "canceled")):
        return "pending_partial_terminal_truth_unproven"
    # Filled-zero for a positive order and replacement descendants are not zero
    # cancellation proof, even when convenience normalization says terminal.
    if status == "filled" or raw_status in {"filled", "replaced", "pending_replace"}:
        return "pending_partial_terminal_truth_unproven"
    if raw.get("replaced_by") or raw.get("legs"):
        return "pending_partial_linked_order_unresolved"
    terminal = {"cancelled", "canceled", "expired", "failed", "rejected", "voided", "done", "closed"}
    if status in terminal and raw_status not in terminal:
        return "pending_partial_terminal_truth_unproven"
    return None


def financial_observation(order: Any) -> dict:
    """Retain unknown finances as unknown; zero quantity is not a fee receipt."""
    def safe(value: Any) -> Any:
        if value is None or isinstance(value, (str, bool, int)):
            return value
        if isinstance(value, float):
            return value if number(value) is not None else str(value)
        if isinstance(value, dict):
            return {str(k): safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [safe(v) for v in value]
        return {"unreadable_type": type(value).__name__}
    raw = getattr(order, "raw", None)
    raw = raw if isinstance(raw, dict) else {}
    return {
        "status": "unresolved_financial_observation",
        "average_filled_price": safe(getattr(order, "average_filled_price", None)),
        "raw": {key: safe(raw[key]) for key in (
            "total_fees", "fees", "fee", "commission", "commissions", "regulatory_fees",
        ) if key in raw},
    }

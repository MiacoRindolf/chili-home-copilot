"""New ordinary entry clocks: broker order evidence, never first-execution inference.

Pure selection only. A refusal preserves the caller's existing adoption-stamp
fallback and cannot refuse adoption/protection of shares that already exist.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Any


CONTRACT = "entry_order_fill_clock_v1"
_CLOCK = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.(\d+))?(?:Z|[+-]\d{2}:\d{2})$")
_RAW_KEYS = (
    "filled_at", "submitted_at", "qty", "alpaca_status", "filled_size",
    "alpaca_filled_qty", "filled_qty", "fill_truth_readable", "canceled_at",
    "broker_account_id_echo", "broker_order_id_echo", "broker_client_order_id_echo",
    "broker_symbol_echo", "broker_side_echo", "broker_order_type_echo",
    "broker_quantity_echo", "broker_filled_quantity_echo", "broker_order_status_echo",
    "broker_position_intent_echo", "broker_asset_class_echo", "time_in_force",
    "extended_hours", "position_intent", "limit_price", "replaces", "replaced_by", "legs",
    "broker_time_in_force_echo", "broker_extended_hours_echo", "broker_limit_price_echo",
)


def _number(value: Any) -> Decimal | None:
    if type(value) not in (str, int, float, Decimal):
        return None
    try:
        n = Decimal(str(value))
        return n if n.is_finite() else None
    except InvalidOperation:
        return None


def _clock(value: Any) -> tuple[datetime, Decimal] | None:
    """UTC microsecond projection plus sub-microsecond residue for comparisons."""
    if type(value) is datetime:
        value = value.isoformat()
    if type(value) is not str or not (match := _CLOCK.fullmatch(value)):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        fraction = match.group(1) or ""
        residue = Decimal("0." + fraction[6:]) if len(fraction) > 6 else Decimal(0)
        return parsed.astimezone(timezone.utc), residue
    except (TypeError, ValueError, OverflowError, InvalidOperation):
        return None


def observe(order: Any, *, at: datetime) -> dict[str, Any]:
    """Copy only the selected order evidence; no references to mutable SDK state."""
    raw = getattr(order, "raw", None)
    raw = raw if isinstance(raw, dict) else {}
    return {
        "observed_at_utc": at.isoformat(),
        **{k: deepcopy(getattr(order, k, None)) for k in (
            "order_id", "client_order_id", "product_id", "side", "order_type",
            "status", "filled_size", "created_time",
        )},
        "raw": {k: deepcopy(raw[k]) for k in _RAW_KEYS if k in raw},
    }


def select(observation: dict[str, Any], *, binding: dict[str, Any],
           authority: dict[str, Any], recorded_at: datetime, mode: str) -> dict[str, Any]:
    """Freeze one new leg's canonical clock, with an explicit local fallback."""
    raw = observation.get("raw") or {}
    raw_fill = raw.get("filled_at")
    result = {
        "contract": CONTRACT, "source": "local_adoption_stamp",
        "entry_filled_at_utc": recorded_at.isoformat(),
        "recorded_at_utc": recorded_at.isoformat(),
        "observed_at_utc": observation.get("observed_at_utc"),
        "broker_filled_at_raw": raw_fill if type(raw_fill) is str else None,
        "broker_clock_value_type": type(raw_fill).__name__,
        "first_execution_at_utc": None,
        "first_execution_time_known": False,
        "binding": deepcopy(binding), "fallback_reason": None,
    }

    def refuse(reason: str) -> dict[str, Any]:
        result["fallback_reason"] = reason
        return result

    if mode != "ordinary_alpaca":
        result["source"] = "replay_local_adoption_stamp" if mode == "replay" else "local_adoption_stamp"
        return refuse(mode + "_clock_contract_preserved")
    request = authority.get("order_request")
    if not isinstance(request, dict) or authority.get("account_verified") is not True:
        return refuse("entry_account_request_authority_unavailable")
    for key in ("session_id", "account_scope", "account_id", "claim_token"):
        if binding.get(key) in (None, "") or binding.get(key) != authority.get(key):
            return refuse("entry_clock_authority_mismatch")
    if binding["account_scope"] != "alpaca:paper":
        return refuse("entry_clock_account_scope_unsupported")
    if authority.get("claim_account_id") is not None and authority["claim_account_id"] != binding["account_id"]:
        return refuse("entry_clock_claim_account_mismatch")
    if authority.get("order_role", "primary") not in {"primary", "repeg"}:
        return refuse("entry_clock_nonprimary_order")
    for key, field in (("order_id", "order_id"), ("client_order_id", "client_order_id"),
                       ("symbol", "product_id"), ("side", "side")):
        wanted = binding.get(key)
        actual = observation.get(field)
        if key in {"symbol", "side"} and isinstance(actual, str):
            actual, wanted = actual.lower(), str(wanted).lower()
        if not wanted or not isinstance(actual, str) or actual.strip() != str(wanted).strip():
            return refuse("entry_clock_order_identity_mismatch")
    if (authority.get("client_order_id") != binding["client_order_id"]
            or (authority.get("order_id") and authority["order_id"] != binding["order_id"])):
        return refuse("entry_clock_claim_order_mismatch")
    expected = {
        "product_id": binding["symbol"], "side": binding["side"],
        "client_order_id": binding["client_order_id"],
    }
    for key, wanted in expected.items():
        actual = request.get(key)
        if key != "client_order_id":
            actual, wanted = str(actual).lower(), str(wanted).lower()
        if actual != wanted:
            return refuse("entry_clock_request_identity_mismatch")
    if request.get("position_intent") != ("buy_to_open" if binding["side"] == "buy" else "sell_to_open"):
        return refuse("entry_clock_request_intent_mismatch")
    for key, wanted in (("account_scope", binding["account_scope"]),
                        ("alpaca_account_id", binding["account_id"])):
        if request.get(key) is not None and request[key] != wanted:
            return refuse("entry_clock_request_account_mismatch")
    echoes = {
        "broker_account_id_echo": binding["account_id"],
        "broker_order_id_echo": binding["order_id"],
        "broker_client_order_id_echo": binding["client_order_id"],
        "broker_symbol_echo": binding["symbol"], "broker_side_echo": binding["side"],
        "broker_asset_class_echo": "us_equity",
        "broker_position_intent_echo": request.get("position_intent"),
        "broker_order_type_echo": request.get("order_type"),
        "broker_time_in_force_echo": request.get("time_in_force"),
        "position_intent": request.get("position_intent"),
        "time_in_force": request.get("time_in_force"),
    }
    for key, wanted in echoes.items():
        actual = raw.get(key)
        if key not in {"broker_account_id_echo", "broker_order_id_echo", "broker_client_order_id_echo"}:
            actual, wanted = (str(actual).lower() if actual is not None else None), str(wanted).lower()
        if actual is not None and str(actual) != str(wanted):
            return refuse("entry_clock_broker_echo_mismatch")
    if str(observation.get("order_type") or "").lower() != str(request.get("order_type") or "").lower():
        return refuse("entry_clock_request_type_mismatch")
    for key in ("extended_hours", "broker_extended_hours_echo"):
        if raw.get(key) is not None and (type(raw[key]) is not bool or raw[key] is not request.get("extended_hours")):
            return refuse("entry_clock_request_extended_hours_mismatch")
    for key in ("limit_price", "broker_limit_price_echo"):
        if raw.get(key) is not None and (_number(raw[key]) is None or _number(raw[key]) != _number(request.get("limit_price"))):
            return refuse("entry_clock_request_limit_mismatch")
    cumulative, requested = _number(observation.get("filled_size")), _number(request.get("base_size"))
    if (cumulative is None or requested is None or not 0 < cumulative <= requested
            or cumulative != _number(binding.get("adopted_quantity")) or _number(raw.get("qty")) != requested):
        return refuse("entry_clock_quantity_unproven")
    for key in ("filled_size", "alpaca_filled_qty", "filled_qty", "broker_filled_quantity_echo"):
        if key in raw and _number(raw[key]) != cumulative:
            return refuse("entry_clock_cumulative_echo_mismatch")
    if raw.get("broker_quantity_echo") is not None and _number(raw["broker_quantity_echo"]) != requested:
        return refuse("entry_clock_request_quantity_mismatch")
    if "fill_truth_readable" in raw and raw["fill_truth_readable"] is not True:
        return refuse("entry_clock_fill_truth_unreadable")
    status = str(observation.get("status") or "").lower().replace("cancelled", "canceled")
    broker_status = str(raw.get("alpaca_status") or "").lower().replace("cancelled", "canceled")
    if status != broker_status or status not in {"filled", "canceled", "expired"}:
        return refuse("entry_clock_terminal_quantity_unproven")
    if raw.get("broker_order_status_echo") is not None and str(raw["broker_order_status_echo"]).lower().replace("cancelled", "canceled") != broker_status:
        return refuse("entry_clock_status_echo_mismatch")
    if status == "filled" and cumulative != requested:
        return refuse("entry_clock_completed_quantity_mismatch")
    if raw.get("replaces") or raw.get("replaced_by") or raw.get("legs"):
        return refuse("entry_clock_linked_order_unsupported")
    fill, observed = _clock(raw_fill), _clock(observation.get("observed_at_utc"))
    if fill is None:
        return refuse("broker_fill_clock_missing_or_invalid")
    if observed is None or fill > observed or observed > _clock(recorded_at):
        return refuse("broker_fill_clock_after_observation")
    for key, value in (("submitted_at", raw.get("submitted_at")), ("created_time", observation.get("created_time"))):
        if value not in (None, ""):
            lower = _clock(value)
            if lower is None or fill < lower:
                return refuse("broker_fill_clock_before_or_invalid_" + key)
    if raw.get("canceled_at") is not None:
        canceled = _clock(raw["canceled_at"])
        if canceled is None or not fill <= canceled <= observed:
            return refuse("broker_cancel_clock_order_invalid")
    result.update(
        source="broker_order_reported_fill_at", fallback_reason=None,
        entry_filled_at_utc=fill[0].isoformat(),
        timestamp_projection="utc_microseconds_truncated_raw_precision_retained",
        quantity_kind="completed_request" if cumulative == requested else "terminal_partial_quantity",
        broker_order_status=broker_status,
    )
    return result

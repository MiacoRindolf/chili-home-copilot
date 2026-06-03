"""Live vs research attribution: closed trades linked to scan patterns vs pattern OOS stats."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from .return_math import (
    paper_trade_realized_pnl,
    paper_trade_return_pct,
    trade_realized_pnl,
    trade_return_pct,
)
from .execution_cost_builder import _usable_tca_bps


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def _json_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _trade_tca_cost_pct(trade: Any) -> float | None:
    """Return entry+exit TCA cost in percent points, or None if incomplete."""
    entry_bps = _usable_tca_bps(trade, "tca_entry_slippage_bps")
    exit_bps = _usable_tca_bps(trade, "tca_exit_slippage_bps")
    if entry_bps is None or exit_bps is None:
        return None
    return (entry_bps + exit_bps) / 100.0


def _trade_tca_bps(trade: Any, attr: str) -> float | None:
    return _usable_tca_bps(trade, attr)


def _paper_realized_pnl_with_raw_fallback(pt: Any) -> float | None:
    pnl = paper_trade_realized_pnl(pt)
    if pnl is not None:
        return pnl
    return _finite_float(getattr(pt, "pnl", None))


def _trade_realized_pnl_with_raw_fallback(trade: Any) -> float | None:
    pnl = trade_realized_pnl(trade)
    if pnl is not None:
        return pnl
    return _finite_float(getattr(trade, "pnl", None))


def _paper_directional_outcome(pt: Any) -> float | None:
    """Win/loss source for paper attribution, preferring complete realized return."""
    ret = paper_trade_return_pct(pt)
    if ret is not None:
        return ret
    pnl = _paper_realized_pnl_with_raw_fallback(pt)
    if pnl is not None:
        return pnl
    return None


def _trade_directional_outcome(trade: Any) -> float | None:
    """Win/loss source for live attribution, preferring complete realized return."""
    ret = trade_return_pct(trade)
    if ret is not None:
        return ret
    pnl = _trade_realized_pnl_with_raw_fallback(trade)
    if pnl is not None:
        return pnl
    return None


def _normalized_exit_reason(trade: Any) -> str:
    reason = str(getattr(trade, "exit_reason", "") or "").strip().lower()
    return reason or "missing"


def _exit_reason_family(reason: str) -> str:
    reason_l = str(reason or "").strip().lower()
    if not reason_l or reason_l == "missing":
        return "unknown"
    if (
        "reconcile" in reason_l
        or "sync_gone" in reason_l
        or "position_gone" in reason_l
        or "position_absent" in reason_l
        or reason_l in {"sync_duplicate", "sync_duplicate_cross_user"}
    ):
        return "reconciler_or_broker_cleanup"
    if "take_profit" in reason_l or "target" in reason_l:
        return "planned_profit_capture"
    if "stop" in reason_l:
        return "planned_risk_stop"
    if reason_l == "pattern_exit_now":
        return "dynamic_pattern_exit"
    if "expired" in reason_l or "time_decay" in reason_l or "time_stop" in reason_l:
        return "time_or_expiry"
    if (
        reason_l.startswith("emergency_")
        or reason_l.startswith("kill_switch")
        or reason_l.startswith("desk_")
        or reason_l.startswith("manual")
        or reason_l.startswith("portfolio_close")
    ):
        return "operator_or_risk_override"
    return "other"


def _is_low_confidence_exit_family(family: str) -> bool:
    return family in {"unknown", "reconciler_or_broker_cleanup"}


def _exit_reason_quality_rows(trades: list[Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from collections import defaultdict

    groups: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "exit_family": "",
            "exit_reason": "",
            "trades": 0,
            "pnl_sample_n": 0,
            "total_pnl": 0.0,
            "win_sample_n": 0,
            "wins": 0,
        }
    )
    total = len(trades)
    low_confidence_count = 0
    planned_count = 0
    dynamic_count = 0
    missing_count = 0
    reconciler_count = 0
    low_confidence_pnls: list[float] = []

    for trade in trades:
        reason = _normalized_exit_reason(trade)
        family = _exit_reason_family(reason)
        row = groups[(family, reason)]
        row["exit_family"] = family
        row["exit_reason"] = reason
        row["trades"] += 1
        pnl = _trade_realized_pnl_with_raw_fallback(trade)
        if pnl is not None:
            row["pnl_sample_n"] += 1
            row["total_pnl"] += float(pnl)
            if _is_low_confidence_exit_family(family):
                low_confidence_pnls.append(float(pnl))
        outcome = _trade_directional_outcome(trade)
        if outcome is not None:
            row["win_sample_n"] += 1
            if outcome > 0:
                row["wins"] += 1

        if family in {"planned_profit_capture", "planned_risk_stop"}:
            planned_count += 1
        if family == "dynamic_pattern_exit":
            dynamic_count += 1
        if family == "unknown":
            missing_count += 1
        if family == "reconciler_or_broker_cleanup":
            reconciler_count += 1
        if _is_low_confidence_exit_family(family):
            low_confidence_count += 1

    rows: list[dict[str, Any]] = []
    for row in groups.values():
        pnl_n = int(row["pnl_sample_n"] or 0)
        win_n = int(row["win_sample_n"] or 0)
        trades_n = int(row["trades"] or 0)
        rows.append({
            "exit_family": row["exit_family"],
            "exit_reason": row["exit_reason"],
            "trades": trades_n,
            "trade_rate_pct": (
                round((trades_n / total) * 100.0, 2) if total > 0 else None
            ),
            "pnl_sample_n": pnl_n,
            "total_pnl": round(float(row["total_pnl"] or 0.0), 2) if pnl_n else None,
            "win_sample_n": win_n,
            "win_rate_pct": (
                round((int(row["wins"] or 0) / win_n) * 100.0, 1)
                if win_n
                else None
            ),
            "low_confidence_attribution": _is_low_confidence_exit_family(
                str(row["exit_family"] or "")
            ),
        })
    rows.sort(
        key=lambda r: (
            r["trades"],
            abs(float(r["total_pnl"] or 0.0)),
            r["exit_family"],
            r["exit_reason"],
        ),
        reverse=True,
    )

    summary = {
        "total_closed_trades": total,
        "planned_exit_count": planned_count,
        "dynamic_pattern_exit_count": dynamic_count,
        "reconciler_exit_count": reconciler_count,
        "missing_exit_reason_count": missing_count,
        "low_confidence_exit_count": low_confidence_count,
        "planned_exit_rate_pct": (
            round((planned_count / total) * 100.0, 2) if total > 0 else None
        ),
        "low_confidence_exit_rate_pct": (
            round((low_confidence_count / total) * 100.0, 2) if total > 0 else None
        ),
        "low_confidence_pnl_sample_n": len(low_confidence_pnls),
        "low_confidence_total_pnl": (
            round(sum(low_confidence_pnls), 2) if low_confidence_pnls else None
        ),
    }
    return rows, summary


def _scan_patterns_by_id(db: Session, pattern_ids: set[int]) -> dict[int, Any]:
    ids = sorted({int(pid) for pid in pattern_ids if int(pid) > 0})
    if not ids:
        return {}

    from ...models.trading import ScanPattern

    rows = db.query(ScanPattern).filter(ScanPattern.id.in_(ids)).all()
    return {int(row.id): row for row in rows if row.id is not None}


def _expected_net_pct_from_payload(payload: dict[str, Any]) -> float | None:
    edge = _json_dict(payload.get("entry_edge"))
    expected = _finite_float(edge.get("expected_net_pct"))
    if expected is not None:
        return expected
    expected = _finite_float(payload.get("entry_edge_expected_net_pct"))
    if expected is not None:
        return expected
    entry_execution = _json_dict(payload.get("entry_execution"))
    return _finite_float(entry_execution.get("entry_edge_expected_net_pct"))


def _execution_asset_class(row: Any, payload: dict[str, Any]) -> str:
    values = [
        payload.get("asset_class"),
        payload.get("asset_type"),
        payload.get("asset_kind"),
    ]
    if bool(payload.get("options_path")) and isinstance(payload.get("option_meta"), dict):
        values.append("options")
    values.append(getattr(row, "asset_type", None))
    for raw in values:
        value = str(raw or "").strip().lower()
        if value in {"crypto", "coin", "coinbase_spot"}:
            return "crypto"
        if value in {"option", "options", "robinhood_options"}:
            return "options"
        if value in {"stock", "stocks", "equity", "equities", "robinhood"}:
            return "stock"
    ticker = str(getattr(row, "ticker", "") or "").strip().upper()
    return "crypto" if ticker.endswith("-USD") else "stock"


def _execution_venue(row: Any, payload: dict[str, Any]) -> str:
    venue = str(payload.get("broker_reject_venue") or "").strip().lower()
    if venue:
        return venue
    reason = str(getattr(row, "reason", "") or "").strip().lower()
    parts = reason.split(":")
    if reason.startswith("venue_") and len(parts) >= 2 and parts[1]:
        return parts[1]
    if bool(payload.get("options_path")) and isinstance(payload.get("option_meta"), dict):
        return "robinhood_options"
    ticker = str(getattr(row, "ticker", "") or "").strip().upper()
    return "coinbase" if ticker.endswith("-USD") else "robinhood"


def _execution_order_hint(payload: dict[str, Any]) -> str:
    hint = str(payload.get("broker_reject_order_hint") or "").strip().lower()
    if hint:
        return hint
    entry_execution = _json_dict(payload.get("entry_execution"))
    hint = str(
        entry_execution.get("active_order_type")
        or entry_execution.get("order_type")
        or ""
    ).strip().lower()
    if hint:
        return hint
    if bool(payload.get("options_path")) and isinstance(payload.get("option_meta"), dict):
        legs = payload.get("option_meta", {}).get("legs")
        return "option_spread" if isinstance(legs, list) and len(legs) > 1 else "option_limit"
    return "unknown"


def _trade_execution_payload(trade: Any) -> dict[str, Any]:
    return _json_dict(getattr(trade, "indicator_snapshot", None))


def _trade_execution_venue(trade: Any, payload: dict[str, Any]) -> str:
    entry_execution = _json_dict(payload.get("entry_execution"))
    for raw in (
        getattr(trade, "broker_source", None),
        entry_execution.get("broker_source"),
        payload.get("_chili_broker_source"),
        payload.get("broker_source"),
    ):
        venue = str(raw or "").strip().lower()
        if venue:
            return venue
    if bool(payload.get("options_path")) and isinstance(payload.get("option_meta"), dict):
        return "robinhood_options"
    ticker = str(getattr(trade, "ticker", "") or "").strip().upper()
    return "coinbase" if ticker.endswith("-USD") else "robinhood"


def _trade_execution_order_hint(payload: dict[str, Any]) -> str:
    entry_execution = _json_dict(payload.get("entry_execution"))
    hint = str(
        entry_execution.get("active_order_type")
        or entry_execution.get("order_type")
        or payload.get("order_type")
        or ""
    ).strip().lower()
    return hint or "unknown"


def _execution_edge_cost_rows(trades: list[Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    groups: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "broker_venue": "",
            "order_hint": "",
            "live_trades": 0,
            "_expected_net_pct_values": [],
            "_tca_cost_pct_values": [],
            "_tca_adjusted_expected_net_pct_values": [],
            "_entry_slippage_bps_values": [],
            "_exit_slippage_bps_values": [],
            "_tca_cost_to_expected_net_ratios": [],
            "positive_expected_edge_events": 0,
            "tca_consumed_expected_edge_events": 0,
        }
    )
    summary = {
        "expected_edge_sample_n": 0,
        "tca_cost_sample_n": 0,
        "tca_adjusted_expected_edge_sample_n": 0,
        "positive_expected_edge_events": 0,
        "tca_consumed_expected_edge_events": 0,
    }

    for trade in trades:
        payload = _trade_execution_payload(trade)
        key = (
            _trade_execution_venue(trade, payload),
            _trade_execution_order_hint(payload),
        )
        group = groups[key]
        group["broker_venue"] = key[0]
        group["order_hint"] = key[1]
        group["live_trades"] += 1

        expected_net_pct = _expected_net_pct_from_payload(payload)
        tca_cost_pct = _trade_tca_cost_pct(trade)
        entry_bps = _trade_tca_bps(trade, "tca_entry_slippage_bps")
        exit_bps = _trade_tca_bps(trade, "tca_exit_slippage_bps")

        if expected_net_pct is not None:
            group["_expected_net_pct_values"].append(expected_net_pct)
            summary["expected_edge_sample_n"] += 1
            if expected_net_pct > 0.0:
                group["positive_expected_edge_events"] += 1
                summary["positive_expected_edge_events"] += 1
        if tca_cost_pct is not None:
            group["_tca_cost_pct_values"].append(tca_cost_pct)
            summary["tca_cost_sample_n"] += 1
        if entry_bps is not None:
            group["_entry_slippage_bps_values"].append(entry_bps)
        if exit_bps is not None:
            group["_exit_slippage_bps_values"].append(exit_bps)
        if expected_net_pct is None or tca_cost_pct is None:
            continue

        adjusted = expected_net_pct - tca_cost_pct
        group["_tca_adjusted_expected_net_pct_values"].append(adjusted)
        summary["tca_adjusted_expected_edge_sample_n"] += 1
        if expected_net_pct > 0.0:
            group["_tca_cost_to_expected_net_ratios"].append(
                tca_cost_pct / expected_net_pct
            )
            if tca_cost_pct >= expected_net_pct:
                group["tca_consumed_expected_edge_events"] += 1
                summary["tca_consumed_expected_edge_events"] += 1

    rows: list[dict[str, Any]] = []
    for group in groups.values():
        expected_values = group.pop("_expected_net_pct_values")
        tca_cost_values = group.pop("_tca_cost_pct_values")
        adjusted_values = group.pop("_tca_adjusted_expected_net_pct_values")
        entry_values = group.pop("_entry_slippage_bps_values")
        exit_values = group.pop("_exit_slippage_bps_values")
        ratios = group.pop("_tca_cost_to_expected_net_ratios")
        positive_n = int(group.get("positive_expected_edge_events") or 0)
        consumed_n = int(group.get("tca_consumed_expected_edge_events") or 0)
        group.update(
            {
                "expected_edge_sample_n": len(expected_values),
                "avg_expected_net_pct": (
                    round(sum(expected_values) / len(expected_values), 4)
                    if expected_values
                    else None
                ),
                "tca_cost_sample_n": len(tca_cost_values),
                "avg_tca_cost_pct": (
                    round(sum(tca_cost_values) / len(tca_cost_values), 4)
                    if tca_cost_values
                    else None
                ),
                "avg_tca_adjusted_expected_net_pct": (
                    round(sum(adjusted_values) / len(adjusted_values), 4)
                    if adjusted_values
                    else None
                ),
                "avg_entry_slippage_bps": (
                    round(sum(entry_values) / len(entry_values), 2)
                    if entry_values
                    else None
                ),
                "avg_exit_slippage_bps": (
                    round(sum(exit_values) / len(exit_values), 2)
                    if exit_values
                    else None
                ),
                "avg_tca_cost_to_expected_net_ratio": (
                    round(sum(ratios) / len(ratios), 4) if ratios else None
                ),
                "tca_consumed_expected_edge_rate_pct": (
                    round((consumed_n / positive_n) * 100.0, 2)
                    if positive_n > 0
                    else None
                ),
            }
        )
        rows.append(group)

    rows.sort(
        key=lambda r: (
            r["tca_consumed_expected_edge_events"],
            r["positive_expected_edge_events"],
            r["live_trades"],
            r["avg_tca_cost_pct"] or 0.0,
        ),
        reverse=True,
    )
    positive_n = int(summary["positive_expected_edge_events"] or 0)
    consumed_n = int(summary["tca_consumed_expected_edge_events"] or 0)
    summary["tca_consumed_expected_edge_rate_pct"] = (
        round((consumed_n / positive_n) * 100.0, 2) if positive_n > 0 else None
    )
    return rows, summary


def _execution_reason_family(row: Any, payload: dict[str, Any] | None = None) -> str:
    payload = payload or {}
    reason = str(getattr(row, "reason", "") or "").strip().lower()
    decision = str(getattr(row, "decision", "") or "").strip().lower()
    if reason.startswith("broker:option_entry_no_fill"):
        return "option_entry_no_fill"
    if reason == "broker:place_no_order_id" or payload.get("broker_reject_missing_order_id"):
        return "place_no_order_id"
    if reason.startswith("broker_reject_suppressed:"):
        return "broker_reject_suppressed"
    if reason.startswith("broker:"):
        broker_reason = reason.split(":", 1)[1].split(":", 1)[0].strip()
        return f"broker_{broker_reason or 'reject'}"
    if reason.startswith("venue_") or reason.startswith("venue:"):
        return "venue_health"
    if reason.startswith("cost_gate:"):
        return "cost_gate"
    if reason.startswith("coinbase_cap"):
        return "coinbase_cap"
    if decision:
        return decision
    return "unknown"


def _execution_order_status(row: Any, payload: dict[str, Any]) -> str:
    if payload.get("broker_reject_missing_order_id"):
        return "no_order_id"
    terminal = str(payload.get("option_entry_terminal_state") or "").strip().lower()
    if terminal:
        return "no_fill"
    reason_family = _execution_reason_family(row, payload)
    if reason_family == "venue_health":
        return "venue_blocked"
    if reason_family in {"cost_gate", "coinbase_cap"}:
        return "pre_broker_blocked"
    if str(getattr(row, "reason", "") or "").startswith("broker"):
        return "broker_rejected"
    return str(getattr(row, "decision", "") or "unknown").strip().lower() or "unknown"


def _is_execution_drag_audit(row: Any) -> bool:
    payload = _json_dict(getattr(row, "rule_snapshot", None))
    reason_family = _execution_reason_family(row, payload)
    if reason_family in {
        "option_entry_no_fill",
        "place_no_order_id",
        "broker_reject_suppressed",
        "venue_health",
        "cost_gate",
        "coinbase_cap",
    }:
        return True
    if reason_family.startswith("broker_"):
        return True
    return bool(
        payload.get("broker_reject_fingerprint")
        or payload.get("option_entry_terminal_state")
    )


def _paper_shadow_alert_id(pt: Any) -> int | None:
    raw = getattr(pt, "paper_shadow_of_alert_id", None)
    if raw is None:
        raw = _json_dict(getattr(pt, "signal_json", None)).get("shadow_of_alert_id")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _shadow_reason_family(pt: Any) -> str | None:
    sig = _json_dict(getattr(pt, "signal_json", None))
    decision = str(sig.get("shadow_decision") or "").strip().lower()
    if decision == "blocked_option_entry_no_fill":
        return "option_entry_no_fill"
    if decision == "blocked_no_order_id":
        return "place_no_order_id"
    if decision == "blocked_venue_health":
        return "venue_health"
    if decision.startswith("blocked_"):
        return decision.removeprefix("blocked_")
    if decision.startswith("skipped_"):
        return decision.removeprefix("skipped_")
    return decision or None


def _execution_drag_evidence_fingerprint(group: dict[str, Any]) -> str:
    body = {
        "asset_class": group.get("asset_class"),
        "scan_pattern_id": group.get("scan_pattern_id"),
        "broker_venue": group.get("broker_venue"),
        "order_hint": group.get("order_hint"),
        "reason_family": group.get("reason_family"),
        "order_status": group.get("order_status"),
    }
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(body, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:24]


def _execution_drag_recommendation(group: dict[str, Any]) -> dict[str, Any]:
    positive_events = int(group.get("positive_edge_events") or 0)
    if positive_events <= 0:
        return {
            "recommended_work_event": None,
            "recommended_next_action": "monitor_non_positive_edge_execution_drag",
        }
    if group.get("scan_pattern_id") is None:
        return {
            "recommended_work_event": "provenance_backfill",
            "recommended_next_action": "recover_pattern_lineage_before_execution_repair",
        }
    reason_family = str(group.get("reason_family") or "").strip().lower()
    if reason_family == "venue_health":
        action = "refresh_edge_reliability_and_review_venue_policy"
    elif reason_family in {"option_entry_no_fill", "place_no_order_id"} or reason_family.startswith("broker_"):
        action = "refresh_edge_reliability_and_review_entry_execution_geometry"
    elif reason_family in {"cost_gate", "coinbase_cap"}:
        action = "refresh_edge_reliability_and_review_capital_or_cost_constraints"
    else:
        action = "refresh_edge_reliability_after_execution_drag"
    return {
        "recommended_work_event": "edge_reliability_refresh",
        "recommended_next_action": action,
    }


def _execution_drag_report_from_rows(
    audits: list[Any],
    paper_trades: list[Any],
    *,
    days: int,
    limit: int,
) -> dict[str, Any]:
    shadows_by_alert: dict[int, list[Any]] = defaultdict(list)
    for pt in paper_trades:
        alert_id = _paper_shadow_alert_id(pt)
        if alert_id is not None:
            shadows_by_alert[alert_id].append(pt)

    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in audits:
        if not _is_execution_drag_audit(row):
            continue
        payload = _json_dict(getattr(row, "rule_snapshot", None))
        pattern_id = getattr(row, "scan_pattern_id", None)
        try:
            pattern_id = int(pattern_id) if pattern_id is not None else None
        except (TypeError, ValueError):
            pattern_id = None
        reason_family = _execution_reason_family(row, payload)
        key = (
            _execution_asset_class(row, payload),
            pattern_id,
            _execution_venue(row, payload),
            _execution_order_hint(payload),
            reason_family,
            _execution_order_status(row, payload),
        )
        group = groups.setdefault(
            key,
            {
                "asset_class": key[0],
                "scan_pattern_id": key[1],
                "broker_venue": key[2],
                "order_hint": key[3],
                "reason_family": key[4],
                "order_status": key[5],
                "events": 0,
                "expected_edge_sample_n": 0,
                "positive_edge_events": 0,
                "penalized_positive_drag_events": 0,
                "paper_shadow_sample_n": 0,
                "paper_shadow_confirmed_missed_alpha_events": 0,
                "paper_shadow_spared_loss_events": 0,
                "paper_shadow_unknown_outcome_events": 0,
                "unobserved_positive_drag_events": 0,
                "_expected_net_pct_values": [],
                "_shadow_adjusted_expected_net_pct_values": [],
                "_paper_shadow_ids": set(),
                "_paper_shadow_returns": [],
                "_paper_shadow_pnls": [],
                "_paper_shadow_outcomes": [],
            },
        )
        group["events"] += 1
        alert_id = getattr(row, "breakout_alert_id", None)
        try:
            alert_key = int(alert_id)
        except (TypeError, ValueError):
            alert_key = 0
        matching_shadows = []
        for pt in shadows_by_alert.get(alert_key, []):
            shadow_family = _shadow_reason_family(pt)
            if shadow_family and shadow_family != reason_family:
                continue
            matching_shadows.append(pt)

        expected = _expected_net_pct_from_payload(payload)
        if expected is not None:
            group["expected_edge_sample_n"] += 1
            group["_expected_net_pct_values"].append(expected)
            if expected > 0.0:
                group["positive_edge_events"] += 1
                outcomes = []
                if not matching_shadows:
                    group["unobserved_positive_drag_events"] += 1
                    group["penalized_positive_drag_events"] += 1
                    group["_shadow_adjusted_expected_net_pct_values"].append(expected)
                else:
                    group["paper_shadow_sample_n"] += len(matching_shadows)
                    for pt in matching_shadows:
                        outcome = paper_trade_return_pct(pt)
                        if outcome is None:
                            outcome = _paper_realized_pnl_with_raw_fallback(pt)
                        if outcome is not None:
                            outcomes.append(outcome)
                    if not outcomes:
                        group["paper_shadow_unknown_outcome_events"] += 1
                        group["penalized_positive_drag_events"] += 1
                        group["_shadow_adjusted_expected_net_pct_values"].append(expected)
                    elif any(outcome > 0.0 for outcome in outcomes):
                        group["paper_shadow_confirmed_missed_alpha_events"] += 1
                        group["penalized_positive_drag_events"] += 1
                        group["_shadow_adjusted_expected_net_pct_values"].append(expected)
                    else:
                        group["paper_shadow_spared_loss_events"] += 1

        for pt in matching_shadows:
            shadow_id = getattr(pt, "id", None)
            if shadow_id in group["_paper_shadow_ids"]:
                continue
            group["_paper_shadow_ids"].add(shadow_id)
            paper_return = paper_trade_return_pct(pt)
            if paper_return is not None:
                group["_paper_shadow_returns"].append(paper_return)
                group["_paper_shadow_outcomes"].append(paper_return)
            paper_pnl = _paper_realized_pnl_with_raw_fallback(pt)
            if paper_pnl is not None:
                group["_paper_shadow_pnls"].append(paper_pnl)
                if paper_return is None:
                    group["_paper_shadow_outcomes"].append(paper_pnl)

    rows: list[dict[str, Any]] = []
    for group in groups.values():
        expected_values = group.pop("_expected_net_pct_values")
        adjusted_expected_values = group.pop("_shadow_adjusted_expected_net_pct_values")
        shadow_ids = group.pop("_paper_shadow_ids")
        shadow_returns = group.pop("_paper_shadow_returns")
        shadow_pnls = group.pop("_paper_shadow_pnls")
        shadow_outcomes = group.pop("_paper_shadow_outcomes")
        wins = sum(1 for outcome in shadow_outcomes if outcome > 0)
        events = int(group.get("events") or 0)
        penalized = int(group.get("penalized_positive_drag_events") or 0)
        adjusted_avg = (
            sum(adjusted_expected_values) / len(adjusted_expected_values)
            if adjusted_expected_values
            else None
        )
        execution_drag_cost_fraction = (
            max(0.0, (penalized / events) * (adjusted_avg / 100.0))
            if events > 0 and adjusted_avg is not None
            else 0.0
        )
        group.update(
            {
                "avg_expected_net_pct": (
                    round(sum(expected_values) / len(expected_values), 4)
                    if expected_values
                    else None
                ),
                "total_expected_net_pct": (
                    round(sum(expected_values), 4) if expected_values else None
                ),
                "shadow_adjusted_avg_expected_net_pct": (
                    round(adjusted_avg, 4) if adjusted_avg is not None else None
                ),
                "shadow_adjusted_total_expected_net_pct": (
                    round(sum(adjusted_expected_values), 4)
                    if adjusted_expected_values
                    else None
                ),
                "raw_positive_drag_rate_pct": (
                    round(
                        (int(group.get("positive_edge_events") or 0) / events)
                        * 100.0,
                        2,
                    )
                    if events > 0
                    else None
                ),
                "shadow_adjusted_positive_drag_rate_pct": (
                    round((penalized / events) * 100.0, 2)
                    if events > 0
                    else None
                ),
                "net_edge_execution_drag_cost_fraction_estimate": (
                    round(execution_drag_cost_fraction, 6)
                ),
                "net_edge_execution_drag_cost_pct_estimate": (
                    round(execution_drag_cost_fraction * 100.0, 4)
                ),
                "paper_shadow_count": len(shadow_ids),
                "paper_shadow_return_sample_n": len(shadow_returns),
                "paper_shadow_avg_return_pct": (
                    round(sum(shadow_returns) / len(shadow_returns), 4)
                    if shadow_returns
                    else None
                ),
                "paper_shadow_pnl_sample_n": len(shadow_pnls),
                "paper_shadow_avg_pnl": (
                    round(sum(shadow_pnls) / len(shadow_pnls), 4)
                    if shadow_pnls
                    else None
                ),
                "paper_shadow_win_rate_pct": (
                    round(wins / len(shadow_outcomes) * 100.0, 2)
                    if shadow_outcomes
                    else None
                ),
            }
        )
        group["evidence_fingerprint"] = _execution_drag_evidence_fingerprint(group)
        group.update(_execution_drag_recommendation(group))
        rows.append(group)

    rows.sort(
        key=lambda r: (
            r["positive_edge_events"],
            r["events"],
            r["avg_expected_net_pct"] or -999.0,
            r["paper_shadow_count"],
        ),
        reverse=True,
    )
    safe_limit = max(1, min(200, int(limit)))
    return {
        "ok": True,
        "window_days": days,
        "summary": {
            "execution_drag_events": sum(row["events"] for row in rows),
            "positive_edge_events": sum(row["positive_edge_events"] for row in rows),
            "penalized_positive_drag_events": sum(
                int(row.get("penalized_positive_drag_events") or 0) for row in rows
            ),
            "paper_shadow_confirmed_missed_alpha_events": sum(
                int(row.get("paper_shadow_confirmed_missed_alpha_events") or 0)
                for row in rows
            ),
            "paper_shadow_spared_loss_events": sum(
                int(row.get("paper_shadow_spared_loss_events") or 0) for row in rows
            ),
            "groups": len(rows),
            "paper_shadow_count": sum(row["paper_shadow_count"] for row in rows),
        },
        "groups": rows[:safe_limit],
    }


def execution_alpha_drag_followup_candidates(
    report: dict[str, Any],
    *,
    min_positive_edge_events: int = 1,
) -> list[dict[str, Any]]:
    """Return safe followup work candidates from an execution-drag report."""
    min_events = max(1, int(min_positive_edge_events))
    candidates: list[dict[str, Any]] = []
    for group in report.get("groups") or []:
        if not isinstance(group, dict):
            continue
        event_type = str(group.get("recommended_work_event") or "").strip()
        if not event_type:
            continue
        positive_events = int(group.get("positive_edge_events") or 0)
        if positive_events < min_events:
            continue
        scan_pattern_id = group.get("scan_pattern_id")
        if scan_pattern_id is None and event_type != "provenance_backfill":
            continue
        candidates.append(
            {
                "event_type": event_type,
                "scan_pattern_id": scan_pattern_id,
                "asset_class": group.get("asset_class"),
                "evidence_fingerprint": group.get("evidence_fingerprint"),
                "expected_evidence_value": group.get("total_expected_net_pct"),
                "positive_edge_events": positive_events,
                "events": group.get("events"),
                "reason_family": group.get("reason_family"),
                "order_status": group.get("order_status"),
                "broker_venue": group.get("broker_venue"),
                "order_hint": group.get("order_hint"),
                "recommended_next_action": group.get("recommended_next_action"),
            }
        )
    return candidates


def execution_alpha_drag_report(
    db: Session,
    user_id: int | None,
    *,
    days: int = 7,
    limit: int = 50,
) -> dict[str, Any]:
    """Summarize execution failures separately from thesis outcomes.

    Uses AutoTraderRun audit rows for broker/venue execution drag and matches
    paper-shadow rows by breakout alert when available.
    """
    from ...models.trading import AutoTraderRun, PaperTrade

    if user_id is None:
        return {
            "ok": True,
            "window_days": days,
            "summary": {
                "execution_drag_events": 0,
                "positive_edge_events": 0,
                "penalized_positive_drag_events": 0,
                "paper_shadow_confirmed_missed_alpha_events": 0,
                "paper_shadow_spared_loss_events": 0,
                "groups": 0,
                "paper_shadow_count": 0,
            },
            "groups": [],
        }

    safe_days = max(1, int(days))
    since = datetime.utcnow() - timedelta(days=safe_days)
    audits = (
        db.query(AutoTraderRun)
        .filter(
            AutoTraderRun.user_id == user_id,
            AutoTraderRun.created_at >= since,
        )
        .all()
    )
    paper_trades = (
        db.query(PaperTrade)
        .filter(
            PaperTrade.user_id == user_id,
            PaperTrade.paper_shadow_of_alert_id.isnot(None),
            PaperTrade.entry_date >= since,
        )
        .all()
    )
    return _execution_drag_report_from_rows(
        audits,
        paper_trades,
        days=safe_days,
        limit=limit,
    )


def queue_execution_alpha_drag_followups(
    db: Session,
    user_id: int | None,
    *,
    days: int = 7,
    limit: int = 50,
    min_positive_edge_events: int = 1,
) -> dict[str, Any]:
    """Queue conservative followups for positive-edge execution drag groups."""
    report = execution_alpha_drag_report(db, user_id, days=days, limit=limit)
    candidates = execution_alpha_drag_followup_candidates(
        report,
        min_positive_edge_events=min_positive_edge_events,
    )
    created: list[int] = []
    skipped = 0
    for candidate in candidates:
        event_type = str(candidate.get("event_type") or "")
        event_id: int | None = None
        if event_type == "edge_reliability_refresh":
            scan_pattern_id = candidate.get("scan_pattern_id")
            if scan_pattern_id is None:
                skipped += 1
                continue
            from .edge_reliability import emit_edge_reliability_refresh_requested

            event_id = emit_edge_reliability_refresh_requested(
                db,
                int(scan_pattern_id),
                source="execution_alpha_drag_report",
                asset_class=candidate.get("asset_class"),
                window_days=max(1, int(days)),
                evidence_fingerprint=str(candidate.get("evidence_fingerprint") or ""),
            )
        elif event_type == "provenance_backfill":
            from .edge_reliability import PROVENANCE_BACKFILL, emit_targeted_profitability_work

            event_id = emit_targeted_profitability_work(
                db,
                event_type=PROVENANCE_BACKFILL,
                scan_pattern_id=None,
                source="execution_alpha_drag_report",
                asset_class=candidate.get("asset_class"),
                evidence_fingerprint=str(candidate.get("evidence_fingerprint") or ""),
                payload=dict(candidate),
            )
        else:
            skipped += 1
            continue
        if event_id is None:
            skipped += 1
            continue
        created.append(int(event_id))
    return {
        "ok": True,
        "window_days": max(1, int(days)),
        "considered": len(candidates),
        "created": len(created),
        "skipped": skipped,
        "event_ids": created,
        "report_summary": report.get("summary") or {},
        "candidates": candidates,
    }


def live_vs_research_by_pattern(
    db: Session,
    user_id: int | None,
    *,
    days: int = 90,
    limit: int = 50,
    include_phase5b_compare: bool = False,
) -> dict[str, Any]:
    """Aggregate closed trades with ``scan_pattern_id`` vs ``ScanPattern`` research fields."""
    from ...models.trading import PaperTrade, Trade

    from .backtest_metrics import backtest_win_rate_db_to_display_pct

    if user_id is None:
        out: dict[str, Any] = {"ok": True, "window_days": days, "patterns": []}
        if include_phase5b_compare:
            out["phase5b_compare"] = {
                "enabled": False,
                "reason": "missing_user_id",
            }
        return out

    since = datetime.utcnow() - timedelta(days=max(1, int(days)))
    safe_days = max(1, int(days))
    safe_limit = max(1, min(200, int(limit)))
    trades = (
        db.query(Trade)
        .filter(
            Trade.user_id == user_id,
            Trade.status == "closed",
            Trade.scan_pattern_id.isnot(None),
            Trade.exit_date.isnot(None),
            Trade.exit_date >= since,
        )
        .all()
    )
    by_pid: dict[int, list[Trade]] = defaultdict(list)
    for t in trades:
        by_pid[int(t.scan_pattern_id or 0)].append(t)
    paper_trades = (
        db.query(PaperTrade)
        .filter(
            PaperTrade.user_id == user_id,
            PaperTrade.status == "closed",
            PaperTrade.scan_pattern_id.isnot(None),
            PaperTrade.exit_date.isnot(None),
            PaperTrade.exit_date >= since,
            or_(
                PaperTrade.paper_shadow_of_alert_id.isnot(None),
                PaperTrade.signal_json.contains({"auto_trader_v1": True}),
                PaperTrade.signal_json.contains({"paper_shadow": True}),
            ),
        )
        .all()
    )
    paper_by_pid: dict[int, list[PaperTrade]] = defaultdict(list)
    for pt in paper_trades:
        paper_by_pid[int(pt.scan_pattern_id or 0)].append(pt)

    rows: list[dict[str, Any]] = []
    pattern_ids = set(by_pid) | set(paper_by_pid)
    patterns_by_id = _scan_patterns_by_id(db, pattern_ids)
    for pid in sorted(pattern_ids):
        if pid <= 0:
            continue
        tlist = by_pid.get(pid, [])
        ptlist = paper_by_pid.get(pid, [])
        pat = patterns_by_id.get(pid)
        pnls = [
            pnl
            for pnl in (_trade_realized_pnl_with_raw_fallback(t) for t in tlist)
            if pnl is not None
        ]
        live_directional_outcomes = [
            outcome
            for outcome in (_trade_directional_outcome(t) for t in tlist)
            if outcome is not None
        ]
        wins = sum(1 for outcome in live_directional_outcomes if outcome > 0)
        n = len(tlist)
        live_returns = [
            ret for ret in (trade_return_pct(t) for t in tlist) if ret is not None
        ]
        live_net_returns: list[float] = []
        tca_costs_pct: list[float] = []
        for t in tlist:
            ret = trade_return_pct(t)
            cost_pct = _trade_tca_cost_pct(t)
            if cost_pct is not None:
                tca_costs_pct.append(cost_pct)
            if ret is not None and cost_pct is not None:
                live_net_returns.append(ret - cost_pct)
        entry_slips = [
            v
            for t in tlist
            if (v := _trade_tca_bps(t, "tca_entry_slippage_bps")) is not None
        ]
        exit_slips = [
            v
            for t in tlist
            if (v := _trade_tca_bps(t, "tca_exit_slippage_bps")) is not None
        ]
        execution_edge_cost_rows, execution_edge_cost_summary = (
            _execution_edge_cost_rows(tlist)
        )
        paper_pnls = [
            p
            for p in (_paper_realized_pnl_with_raw_fallback(pt) for pt in ptlist)
            if p is not None
        ]
        paper_directional_outcomes = [
            outcome
            for outcome in (_paper_directional_outcome(pt) for pt in ptlist)
            if outcome is not None
        ]
        paper_wins = sum(1 for outcome in paper_directional_outcomes if outcome > 0)
        paper_returns = [
            ret
            for ret in (paper_trade_return_pct(pt) for pt in ptlist)
            if ret is not None
        ]
        paper_tca_costs_pct: list[float] = []
        paper_net_returns: list[float] = []
        for pt in ptlist:
            ret = paper_trade_return_pct(pt)
            cost_pct = _trade_tca_cost_pct(pt)
            if cost_pct is not None:
                paper_tca_costs_pct.append(cost_pct)
            if ret is not None and cost_pct is not None:
                paper_net_returns.append(ret - cost_pct)
        rows.append(
            {
                "scan_pattern_id": pid,
                "pattern_name": pat.name if pat else None,
                "promotion_status": pat.promotion_status if pat else None,
                "research_win_rate_pct": (
                    round(float(backtest_win_rate_db_to_display_pct(pat.win_rate)), 2)
                    if pat and pat.win_rate is not None
                    else None
                ),
                "research_oos_win_rate_pct": (
                    round(float(backtest_win_rate_db_to_display_pct(pat.oos_win_rate)), 2)
                    if pat and pat.oos_win_rate is not None
                    else None
                ),
                "research_oos_avg_return_pct": round(float(pat.oos_avg_return_pct), 3)
                if pat and pat.oos_avg_return_pct is not None
                else None,
                "live_closed_trades": n,
                "live_win_sample_n": len(live_directional_outcomes),
                "live_win_rate_pct": (
                    round(wins / len(live_directional_outcomes) * 100.0, 1)
                    if live_directional_outcomes
                    else None
                ),
                "live_pnl_sample_n": len(pnls),
                "live_total_pnl": round(sum(pnls), 2) if pnls else None,
                "live_avg_pnl": round(sum(pnls) / len(pnls), 2)
                if pnls
                else None,
                "live_return_sample_n": len(live_returns),
                "live_avg_return_pct": (
                    round(sum(live_returns) / len(live_returns), 3)
                    if live_returns
                    else None
                ),
                "live_avg_tca_cost_pct": (
                    round(sum(tca_costs_pct) / len(tca_costs_pct), 4)
                    if tca_costs_pct
                    else None
                ),
                "live_avg_net_return_pct": (
                    round(sum(live_net_returns) / len(live_net_returns), 3)
                    if live_net_returns
                    else None
                ),
                "live_avg_entry_slippage_bps": round(sum(entry_slips) / len(entry_slips), 2)
                if entry_slips
                else None,
                "live_avg_exit_slippage_bps": round(sum(exit_slips) / len(exit_slips), 2)
                if exit_slips
                else None,
                "live_execution_edge_cost_summary": execution_edge_cost_summary,
                "live_execution_edge_cost_by_venue": execution_edge_cost_rows,
                "paper_closed_trades": len(ptlist),
                "paper_win_sample_n": len(paper_directional_outcomes),
                "paper_win_rate_pct": (
                    round(paper_wins / len(paper_directional_outcomes) * 100.0, 1)
                    if paper_directional_outcomes
                    else None
                ),
                "paper_total_pnl": round(sum(paper_pnls), 2)
                if paper_pnls
                else None,
                "paper_avg_pnl": round(sum(paper_pnls) / len(paper_pnls), 2)
                if paper_pnls
                else None,
                "paper_return_sample_n": len(paper_returns),
                "paper_avg_return_pct": (
                    round(sum(paper_returns) / len(paper_returns), 3)
                    if paper_returns
                    else None
                ),
                "paper_avg_tca_cost_pct": (
                    round(sum(paper_tca_costs_pct) / len(paper_tca_costs_pct), 4)
                    if paper_tca_costs_pct
                    else None
                ),
                "paper_avg_net_return_pct": (
                    round(sum(paper_net_returns) / len(paper_net_returns), 3)
                    if paper_net_returns
                    else None
                ),
            }
        )

    rows.sort(
        key=lambda r: (
            r["live_closed_trades"],
            r["paper_closed_trades"],
        ),
        reverse=True,
    )
    rows = rows[:safe_limit]

    out = {"ok": True, "window_days": days, "patterns": rows}
    if include_phase5b_compare:
        out["phase5b_compare"] = _phase5b_pattern_attribution_compare(
            db,
            user_id=int(user_id),
            days=safe_days,
            limit=safe_limit,
        )
    return out


def _phase5b_pattern_attribution_compare(
    db: Session,
    *,
    user_id: int,
    days: int,
    limit: int,
) -> dict[str, Any]:
    """Compare legacy envelope-pattern attribution with Phase 5B decisions."""
    params = {"user_id": user_id, "days": days, "limit": limit}
    grouped_rows = db.execute(text("""
        WITH closed AS (
            SELECT
                decision_scan_pattern_id,
                envelope_scan_pattern_id,
                COALESCE(envelope_pnl, 0)::double precision AS pnl
              FROM trading_phase5b_decision_envelope_position
             WHERE envelope_user_id = :user_id
               AND envelope_status = 'closed'
               AND envelope_exit_date IS NOT NULL
               AND envelope_exit_date >= (NOW() - (:days * INTERVAL '1 day'))
        ),
        attributed AS (
            SELECT
                'envelope'::text AS attribution_source,
                envelope_scan_pattern_id AS scan_pattern_id,
                pnl
              FROM closed
            UNION ALL
            SELECT
                'decision'::text AS attribution_source,
                decision_scan_pattern_id AS scan_pattern_id,
                pnl
              FROM closed
        )
        SELECT
            attribution_source,
            scan_pattern_id,
            COUNT(*)::bigint AS closed_envelopes,
            ROUND(SUM(pnl)::numeric, 4) AS total_pnl,
            ROUND(AVG(pnl)::numeric, 4) AS avg_pnl
          FROM attributed
         GROUP BY attribution_source, scan_pattern_id
         ORDER BY attribution_source, closed_envelopes DESC, total_pnl DESC
    """), params).mappings().all()

    mismatch_rows = db.execute(text("""
        SELECT
            decision_scan_pattern_id,
            envelope_scan_pattern_id,
            COUNT(*)::bigint AS closed_envelopes,
            ROUND(SUM(COALESCE(envelope_pnl, 0))::numeric, 4) AS total_pnl
          FROM trading_phase5b_decision_envelope_position
         WHERE envelope_user_id = :user_id
           AND envelope_status = 'closed'
           AND envelope_exit_date IS NOT NULL
           AND envelope_exit_date >= (NOW() - (:days * INTERVAL '1 day'))
           AND decision_scan_pattern_id IS DISTINCT FROM envelope_scan_pattern_id
         GROUP BY decision_scan_pattern_id, envelope_scan_pattern_id
         ORDER BY ABS(SUM(COALESCE(envelope_pnl, 0))) DESC, closed_envelopes DESC
         LIMIT :limit
    """), params).mappings().all()

    by_source: dict[str, list[dict[str, Any]]] = {"envelope": [], "decision": []}
    source_totals: dict[str, dict[int | None, float]] = {"envelope": {}, "decision": {}}
    source_counts: dict[str, int] = {"envelope": 0, "decision": 0}
    for row in grouped_rows:
        source = str(row["attribution_source"])
        pid = row["scan_pattern_id"]
        pid_key = int(pid) if pid is not None else None
        closed = int(row["closed_envelopes"] or 0)
        total_pnl = round(float(row["total_pnl"] or 0.0), 4)
        payload = {
            "scan_pattern_id": pid_key,
            "closed_envelopes": closed,
            "total_pnl": total_pnl,
            "avg_pnl": round(float(row["avg_pnl"] or 0.0), 4),
        }
        if source in by_source:
            by_source[source].append(payload)
            source_totals[source][pid_key] = total_pnl
            source_counts[source] += closed

    envelope_keys = set(source_totals["envelope"])
    decision_keys = set(source_totals["decision"])
    diff_keys = envelope_keys | decision_keys
    pnl_delta_abs = sum(
        abs(source_totals["envelope"].get(key, 0.0) - source_totals["decision"].get(key, 0.0))
        for key in diff_keys
    )

    mismatches = [
        {
            "decision_scan_pattern_id": (
                int(row["decision_scan_pattern_id"])
                if row["decision_scan_pattern_id"] is not None
                else None
            ),
            "envelope_scan_pattern_id": (
                int(row["envelope_scan_pattern_id"])
                if row["envelope_scan_pattern_id"] is not None
                else None
            ),
            "closed_envelopes": int(row["closed_envelopes"] or 0),
            "total_pnl": round(float(row["total_pnl"] or 0.0), 4),
        }
        for row in mismatch_rows
    ]

    return {
        "enabled": True,
        "source_view": "trading_phase5b_decision_envelope_position",
        "window_days": days,
        "legacy_attribution": "envelope_scan_pattern_id",
        "phase5b_attribution": "decision_scan_pattern_id",
        "summary": {
            "envelope_pattern_groups": len(envelope_keys),
            "decision_pattern_groups": len(decision_keys),
            "envelope_closed_envelopes": source_counts["envelope"],
            "decision_closed_envelopes": source_counts["decision"],
            "mismatched_pattern_groups": len(mismatches),
            "mismatched_closed_envelopes": sum(m["closed_envelopes"] for m in mismatches),
            "absolute_group_pnl_delta": round(pnl_delta_abs, 4),
            "null_decision_pattern_envelopes": sum(
                m["closed_envelopes"]
                for m in mismatches
                if m["decision_scan_pattern_id"] is None
            ),
        },
        "by_envelope_pattern": by_source["envelope"][:limit],
        "by_decision_pattern": by_source["decision"][:limit],
        "attribution_mismatches": mismatches,
    }


# ── Post-trade review loop ──────────────────────────────────────────────────

def post_trade_review(
    db: Session,
    user_id: int | None,
    *,
    days: int = 30,
) -> dict[str, Any]:
    """Produce a structured "what worked, what failed, and why" review.

    Aggregates closed trades over the last *days* and returns:
    - Top-performing patterns (live win-rate vs research expectation)
    - Underperforming patterns (where live results lagged research)
    - Slippage outliers (high TCA entry/exit cost)
    - Consecutive-loss streaks (execution timing issues)
    - Key takeaways for the learning loop
    - Pattern feedback signals (which patterns should be up/down-weighted)

    The returned dict is additive and does not mutate DB state.
    """
    from datetime import datetime, timedelta
    from ...models.trading import Trade
    from .backtest_metrics import backtest_win_rate_db_to_display_pct

    if user_id is None:
        return {"ok": True, "window_days": days, "review": {}, "feedback_signals": []}

    since = datetime.utcnow() - timedelta(days=max(1, int(days)))

    closed = (
        db.query(Trade)
        .filter(
            Trade.user_id == user_id,
            Trade.status == "closed",
            Trade.exit_date >= since,
        )
        .order_by(Trade.exit_date.asc())
        .all()
    )

    if not closed:
        return {
            "ok": True,
            "window_days": days,
            "review": {"total_trades": 0},
            "feedback_signals": [],
        }

    pnls = [
        pnl
        for pnl in (_trade_realized_pnl_with_raw_fallback(t) for t in closed)
        if pnl is not None
    ]
    directional_outcomes = [
        outcome
        for outcome in (_trade_directional_outcome(t) for t in closed)
        if outcome is not None
    ]
    wins = sum(1 for outcome in directional_outcomes if outcome > 0)
    n = len(closed)
    directional_n = len(directional_outcomes)
    pnl_n = len(pnls)
    total_pnl = round(sum(pnls), 2) if pnls else None
    avg_pnl = round(sum(pnls) / pnl_n, 2) if pnls else None
    live_win_rate = round(wins / directional_n * 100, 1) if directional_n else None
    exit_reason_rows, exit_quality = _exit_reason_quality_rows(closed)

    # --- Consecutive losses ---
    max_consec_losses = 0
    cur_streak = 0
    for outcome in (_trade_directional_outcome(t) for t in closed):
        if outcome is None:
            cur_streak = 0
        elif outcome < 0:
            cur_streak += 1
            max_consec_losses = max(max_consec_losses, cur_streak)
        else:
            cur_streak = 0

    # --- Slippage outliers ---
    high_slip_trades = []
    for t in closed:
        entry_slip = _trade_tca_bps(t, "tca_entry_slippage_bps") or 0.0
        exit_slip = _trade_tca_bps(t, "tca_exit_slippage_bps") or 0.0
        total_slip = entry_slip + exit_slip
        if total_slip > 50:  # >50 bps total is notable
            high_slip_trades.append({
                "ticker": t.ticker,
                "entry_slippage_bps": round(entry_slip, 1),
                "exit_slippage_bps": round(exit_slip, 1),
                "total_slippage_bps": round(total_slip, 1),
                "pnl": _trade_realized_pnl_with_raw_fallback(t),
            })
    high_slip_trades.sort(key=lambda x: x["total_slippage_bps"], reverse=True)

    # --- Pattern performance ---
    from collections import defaultdict
    by_pid: dict[int, list[Trade]] = defaultdict(list)
    for t in closed:
        if t.scan_pattern_id:
            by_pid[int(t.scan_pattern_id)].append(t)

    outperformers: list[dict[str, Any]] = []
    underperformers: list[dict[str, Any]] = []
    feedback_signals: list[dict[str, Any]] = []
    patterns_by_id = _scan_patterns_by_id(db, set(by_pid))

    for pid, trades in by_pid.items():
        if pid <= 0:
            continue
        pat = patterns_by_id.get(pid)
        trade_pnls = [
            pnl
            for pnl in (_trade_realized_pnl_with_raw_fallback(t) for t in trades)
            if pnl is not None
        ]
        trade_directional_outcomes = [
            outcome
            for outcome in (_trade_directional_outcome(t) for t in trades)
            if outcome is not None
        ]
        t_wins = sum(1 for outcome in trade_directional_outcomes if outcome > 0)
        t_n = len(trades)
        t_directional_n = len(trade_directional_outcomes)
        live_wr = round(t_wins / t_directional_n * 100, 1) if t_directional_n else None

        research_wr = None
        if pat and pat.oos_win_rate is not None:
            research_wr = round(float(backtest_win_rate_db_to_display_pct(pat.oos_win_rate) or 0), 1)
        elif pat and pat.win_rate is not None:
            research_wr = round(float(backtest_win_rate_db_to_display_pct(pat.win_rate) or 0), 1)

        delta = (
            round(live_wr - research_wr, 1)
            if live_wr is not None and research_wr is not None
            else None
        )

        row = {
            "scan_pattern_id": pid,
            "pattern_name": pat.name if pat else None,
            "live_trades": t_n,
            "live_win_sample_n": t_directional_n,
            "live_win_rate_pct": live_wr,
            "research_win_rate_pct": research_wr,
            "delta_pct": delta,
            "live_pnl_sample_n": len(trade_pnls),
            "live_total_pnl": round(sum(trade_pnls), 2) if trade_pnls else None,
        }
        _, pattern_exit_quality = _exit_reason_quality_rows(trades)
        row["exit_quality"] = pattern_exit_quality

        if delta is not None and t_directional_n >= 3:
            low_confidence_exit_rate = pattern_exit_quality.get(
                "low_confidence_exit_rate_pct"
            )
            low_confidence_feedback = (
                low_confidence_exit_rate is not None
                and float(low_confidence_exit_rate) >= 50.0
            )
            if delta >= 5:
                outperformers.append(row)
                if low_confidence_feedback:
                    feedback_signals.append({
                        "pattern_id": pid,
                        "pattern_name": pat.name if pat else None,
                        "signal": "collect_exit_evidence",
                        "suppressed_signal": "upweight",
                        "exit_quality": pattern_exit_quality,
                        "reason": (
                            f"Live win rate beat research by {delta}pp, but "
                            f"{low_confidence_exit_rate}% of exits were reconciler/unknown; "
                            "collect cleaner thesis-exit evidence before upweighting."
                        ),
                    })
                else:
                    feedback_signals.append({
                        "pattern_id": pid,
                        "pattern_name": pat.name if pat else None,
                        "signal": "upweight",
                        "exit_quality": pattern_exit_quality,
                        "reason": (
                            f"Live win rate {live_wr}% exceeded research {research_wr}% "
                            f"by {delta}pp over {t_directional_n} directional outcomes"
                        ),
                    })
            elif delta <= -10:
                underperformers.append(row)
                if low_confidence_feedback:
                    feedback_signals.append({
                        "pattern_id": pid,
                        "pattern_name": pat.name if pat else None,
                        "signal": "collect_exit_evidence",
                        "suppressed_signal": "downweight",
                        "exit_quality": pattern_exit_quality,
                        "reason": (
                            f"Live win rate lagged research by {abs(delta)}pp, but "
                            f"{low_confidence_exit_rate}% of exits were reconciler/unknown; "
                            "collect cleaner thesis-exit evidence before downweighting."
                        ),
                    })
                else:
                    feedback_signals.append({
                        "pattern_id": pid,
                        "pattern_name": pat.name if pat else None,
                        "signal": "downweight",
                        "exit_quality": pattern_exit_quality,
                        "reason": (
                            f"Live win rate {live_wr}% lagged research {research_wr}% "
                            f"by {abs(delta)}pp over {t_directional_n} directional outcomes"
                        ),
                    })

    outperformers.sort(key=lambda r: r["delta_pct"] or 0, reverse=True)
    underperformers.sort(key=lambda r: r["delta_pct"] or 0)

    # --- Takeaways ---
    takeaways: list[str] = []
    if live_win_rate is not None and live_win_rate >= 60:
        takeaways.append(
            f"Strong period: {live_win_rate}% win rate across "
            f"{directional_n} directional outcomes."
        )
    elif live_win_rate is not None and live_win_rate < 40:
        takeaways.append(f"Challenging period: {live_win_rate}% win rate — review entry criteria.")
    if max_consec_losses >= 4:
        takeaways.append(
            f"Max consecutive loss streak was {max_consec_losses} — consider pausing after {max_consec_losses - 1} losses."
        )
    if high_slip_trades:
        avg_slip = round(
            sum(t["total_slippage_bps"] for t in high_slip_trades) / len(high_slip_trades), 1
        )
        takeaways.append(
            f"{len(high_slip_trades)} trades had high slippage (avg {avg_slip} bps) — review order type/timing."
        )
    low_confidence_rate = exit_quality.get("low_confidence_exit_rate_pct")
    if (
        low_confidence_rate is not None
        and float(low_confidence_rate) >= 25.0
    ):
        takeaways.append(
            f"{exit_quality['low_confidence_exit_count']} of {n} exits were reconciler/unknown "
            f"({low_confidence_rate}%) — treat pattern P&L attribution as low-confidence until exit provenance improves."
        )
    if outperformers:
        takeaways.append(
            f"{len(outperformers)} pattern(s) beat research expectations — consider increasing allocation."
        )
    if underperformers:
        takeaways.append(
            f"{len(underperformers)} pattern(s) underperformed research — review for market-regime mismatch."
        )

    return {
        "ok": True,
        "window_days": days,
        "review": {
            "total_trades": n,
            "win_sample_n": directional_n,
            "wins": wins,
            "losses": directional_n - wins,
            "live_win_rate_pct": live_win_rate,
            "pnl_sample_n": pnl_n,
            "total_pnl": total_pnl,
            "avg_pnl": avg_pnl,
            "exit_quality": exit_quality,
            "exit_reason_summary": exit_reason_rows[:10],
            "max_consecutive_losses": max_consec_losses,
            "high_slippage_trades": high_slip_trades[:5],
            "outperforming_patterns": outperformers[:5],
            "underperforming_patterns": underperformers[:5],
            "takeaways": takeaways,
        },
        "feedback_signals": feedback_signals,
    }

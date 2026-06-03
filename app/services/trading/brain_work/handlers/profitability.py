"""Profitability evidence handlers for pilot-first candidate improvement.

Handlers here are deliberately conservative:
* edge reliability writes aggregate outcome snapshots only;
* recert rescue recomputes existing quality evidence but never clears recert;
* exit variant refresh delegates to existing ScanPattern evolution gates;
* provenance backfill writes diagnostics plus unambiguous closed-trade
  exit-reason repairs only.
"""
from __future__ import annotations

import logging
import math
import json
from datetime import UTC, datetime, timedelta
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
LOG_PREFIX = "[brain_work:profitability]"

_RECERT_RESCUE_MIN_EDGE_EVALS = 5
_RECERT_RESCUE_MIN_POSITIVE_EV_PCT = 0.0
_TIME_DECAY_EDGE_MISS_SOURCE = "paper_time_decay_edge_miss"
_TIME_DECAY_EDGE_MISS_REASONS = frozenset({
    "exit_engine_time_decay",
    "time_decay",
    "exit_time_decay",
})
_TIME_DECAY_EDGE_MISS_DEFAULT_MIN_LOSSES = 2
_TIME_DECAY_EDGE_MISS_FETCH_LIMIT = 200
_REPAIRABLE_LOW_CONFIDENCE_EXIT_REASONS = frozenset({
    "",
    "missing",
    "broker_reconcile_close",
    "broker_reconcile_position_gone",
})
_UNUSABLE_EXIT_REPAIR_REASONS = frozenset({
    "",
    "missing",
    "unknown",
    "pending_exit",
})
_TERMINAL_PENDING_EXIT_STATUSES = frozenset({
    "filled",
    "complete",
    "completed",
    "closed",
    "executed",
})


def _payload(ev: Any) -> dict[str, Any]:
    raw = getattr(ev, "payload", None)
    return raw if isinstance(raw, dict) else {}


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _pattern_id(ev: Any) -> int | None:
    raw = _payload(ev).get("scan_pattern_id")
    try:
        pid = int(raw)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _window_days(ev: Any) -> int:
    try:
        return max(1, int(_payload(ev).get("window_days") or 30))
    except (TypeError, ValueError):
        return 30


def _positive_int_payload(payload: dict[str, Any], key: str) -> int:
    return _positive_int_value(payload.get(key))


def _positive_int_value(value: Any) -> int:
    try:
        out = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, out)


def _needs_exit_provenance_backfill(payload: dict[str, Any]) -> bool:
    category = str(payload.get("cash_deployment_category") or "").strip().lower()
    if category in {"needs_exit_provenance", "low_confidence_exit_attribution"}:
        return True
    if str(payload.get("exit_provenance_blocker") or "").strip():
        return True
    return _positive_int_payload(payload, "live_low_confidence_exit_count") > 0


def _normalized_exit_reason_value(value: Any) -> str:
    return str(value or "").strip().lower()


def _is_low_confidence_exit_reason_value(value: Any) -> bool:
    reason = _normalized_exit_reason_value(value)
    if reason in _UNUSABLE_EXIT_REPAIR_REASONS:
        return True
    from app.services.trading.realized_pnl_sql import (
        LIVE_PATTERN_EV_LOW_CONFIDENCE_EXIT_REASONS,
        LIVE_PATTERN_EV_LOW_CONFIDENCE_EXIT_TOKENS,
    )

    return reason in LIVE_PATTERN_EV_LOW_CONFIDENCE_EXIT_REASONS or any(
        token in reason for token in LIVE_PATTERN_EV_LOW_CONFIDENCE_EXIT_TOKENS
    )


def _repairable_current_exit_reason(value: Any) -> bool:
    reason = _normalized_exit_reason_value(value)
    if reason in _REPAIRABLE_LOW_CONFIDENCE_EXIT_REASONS:
        return True
    if reason.startswith("broker_reconcile_") and "no_exit_price" not in reason:
        return True
    return False


def _usable_exit_repair_reason(value: Any) -> str | None:
    reason = _normalized_exit_reason_value(value)
    if reason in _UNUSABLE_EXIT_REPAIR_REASONS:
        return None
    if _is_low_confidence_exit_reason_value(reason):
        return None
    return reason[:50]


def _trade_has_realized_exit_basis(trade: Any) -> bool:
    if _safe_float(getattr(trade, "pnl", None)) is not None:
        return True
    entry = _safe_float(getattr(trade, "entry_price", None))
    exit_px = _safe_float(getattr(trade, "exit_price", None))
    qty = _safe_float(getattr(trade, "quantity", None))
    return bool(entry and entry > 0.0 and exit_px and exit_px > 0.0 and qty and qty > 0.0)


def _add_exit_repair_candidate(
    candidates: dict[str, set[str]],
    *,
    reason: Any,
    source: str,
) -> None:
    clean = _usable_exit_repair_reason(reason)
    if not clean:
        return
    candidates.setdefault(clean, set()).add(source)


def _exit_reason_repair_candidates(db: "Session", trade: Any) -> dict[str, set[str]]:
    from app.models.trading import EconomicLedgerEvent, TradingExecutionEvent

    candidates: dict[str, set[str]] = {}
    pending_status = _normalized_exit_reason_value(
        getattr(trade, "pending_exit_status", None)
    )
    if pending_status in _TERMINAL_PENDING_EXIT_STATUSES:
        _add_exit_repair_candidate(
            candidates,
            reason=getattr(trade, "pending_exit_reason", None),
            source="trade.pending_exit_reason",
        )

    events = (
        db.query(TradingExecutionEvent)
        .filter(TradingExecutionEvent.trade_id == int(getattr(trade, "id", 0) or 0))
        .order_by(TradingExecutionEvent.recorded_at.desc(), TradingExecutionEvent.id.desc())
        .limit(20)
        .all()
    )
    for event in events:
        payload = getattr(event, "payload_json", None)
        if not isinstance(payload, dict):
            continue
        event_type = _normalized_exit_reason_value(getattr(event, "event_type", None))
        if event_type not in {"exit_fill", "stop_engine_auto_sell", "coinbase_dust_close"}:
            continue
        for key in ("exit_reason", "pending_exit_reason", "reason"):
            _add_exit_repair_candidate(
                candidates,
                reason=payload.get(key),
                source=f"execution_event.{event_type}.{key}",
            )

    ledger_rows = (
        db.query(EconomicLedgerEvent)
        .filter(EconomicLedgerEvent.trade_id == int(getattr(trade, "id", 0) or 0))
        .filter(EconomicLedgerEvent.event_type == "exit_fill")
        .order_by(EconomicLedgerEvent.created_at.desc(), EconomicLedgerEvent.id.desc())
        .limit(10)
        .all()
    )
    for row in ledger_rows:
        provenance = getattr(row, "provenance_json", None)
        if not isinstance(provenance, dict):
            continue
        _add_exit_repair_candidate(
            candidates,
            reason=provenance.get("exit_reason"),
            source="economic_ledger.exit_fill.exit_reason",
        )
    return candidates


def _repair_low_confidence_exit_provenance(
    db: "Session",
    rows: list[dict[str, Any]],
    *,
    user_id: int | None,
) -> dict[str, Any]:
    from app.models.trading import Trade

    trade_ids = sorted({
        int(tid)
        for row in rows
        for tid in (row.get("low_confidence_trade_ids") or [])
        if _positive_int_value(tid) > 0
    })
    summary: dict[str, Any] = {
        "considered_trade_ids": trade_ids,
        "considered_count": len(trade_ids),
        "repaired_count": 0,
        "repaired": [],
        "skipped": [],
    }
    if not trade_ids:
        return summary

    q = (
        db.query(Trade)
        .filter(Trade.id.in_(trade_ids))
        .filter(Trade.status == "closed")
    )
    if user_id is not None:
        q = q.filter(Trade.user_id == user_id)

    trades = {int(trade.id): trade for trade in q.all()}
    for trade_id in trade_ids:
        trade = trades.get(trade_id)
        if trade is None:
            summary["skipped"].append({"trade_id": trade_id, "reason": "not_closed_or_not_found"})
            continue
        old_reason = _normalized_exit_reason_value(getattr(trade, "exit_reason", None)) or "missing"
        if not _is_low_confidence_exit_reason_value(old_reason):
            summary["skipped"].append({"trade_id": trade_id, "reason": "already_high_confidence"})
            continue
        if not _repairable_current_exit_reason(old_reason):
            summary["skipped"].append({
                "trade_id": trade_id,
                "reason": "unrepairable_current_exit_reason",
                "exit_reason": old_reason,
            })
            continue
        if not _trade_has_realized_exit_basis(trade):
            summary["skipped"].append({"trade_id": trade_id, "reason": "missing_realized_exit_basis"})
            continue
        candidates = _exit_reason_repair_candidates(db, trade)
        if len(candidates) != 1:
            summary["skipped"].append({
                "trade_id": trade_id,
                "reason": "ambiguous_or_missing_repair_reason",
                "candidate_reasons": sorted(candidates),
            })
            continue
        repaired_reason, sources = next(iter(candidates.items()))
        trade.exit_reason = repaired_reason[:50]
        db.add(trade)
        repaired = {
            "trade_id": trade_id,
            "old_exit_reason": old_reason,
            "new_exit_reason": trade.exit_reason,
            "sources": sorted(sources),
        }
        summary["repaired"].append(repaired)
    summary["repaired_count"] = len(summary["repaired"])
    return summary


def _queue_exit_repair_edge_refresh(
    db: "Session",
    *,
    scan_pattern_id: int | None,
    asset_class: Any,
    window_days: int,
    evidence_key: str,
    repair_summary: dict[str, Any],
) -> dict[str, Any]:
    repaired_count = _positive_int_payload(repair_summary, "repaired_count")
    if repaired_count <= 0:
        return {
            "queued": False,
            "event_id": None,
            "reason": "no_repairs",
        }
    if scan_pattern_id is None:
        return {
            "queued": False,
            "event_id": None,
            "reason": "missing_scan_pattern_id",
        }

    from app.services.trading.edge_reliability import (
        EDGE_RELIABILITY_REFRESH,
        emit_edge_reliability_refresh_requested,
    )

    fp_basis = str(evidence_key or "latest").strip()[:24] or "latest"
    event_id = emit_edge_reliability_refresh_requested(
        db,
        int(scan_pattern_id),
        source="provenance_backfill_exit_repair",
        asset_class=str(asset_class or "") or None,
        window_days=window_days,
        evidence_fingerprint=f"exit_repair:{fp_basis}:{repaired_count}",
    )
    return {
        "queued": event_id is not None,
        "event_id": event_id,
        "event_type": EDGE_RELIABILITY_REFRESH,
        "reason": "queued" if event_id is not None else "deduped_or_recently_completed",
        "scan_pattern_id": int(scan_pattern_id),
        "asset_class": asset_class,
        "window_days": int(window_days),
        "repaired_count": repaired_count,
    }


def _recert_reason_set(raw: Any) -> set[str]:
    if isinstance(raw, str):
        return {part.strip() for part in raw.split(",") if part.strip()}
    if isinstance(raw, (list, tuple, set)):
        return {str(part).strip() for part in raw if str(part).strip()}
    return set()


def _recert_reason_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(raw, (list, tuple, set)):
        return [str(part).strip() for part in raw if str(part).strip()]
    return []


def _safe_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _mean_float(values: list[float]) -> float | None:
    vals = [
        out
        for out in (_safe_float(v) for v in values)
        if out is not None
    ]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _counter_add(counter: dict[str, int], value: Any, *, fallback: str = "unknown") -> None:
    key = str(value or fallback).strip().lower() or fallback
    counter[key] = int(counter.get(key, 0) or 0) + 1


def _utc_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def _hard_reason_still_unresolved(pattern: Any, reason: str, config: Any) -> bool:
    reason = str(reason or "").strip()
    if reason == "negative_oos_recert":
        oos_avg = _safe_float(getattr(pattern, "oos_avg_return_pct", None))
        if oos_avg is None:
            return True
        return oos_avg < float(getattr(config, "min_oos_avg_return_pct", 0.0) or 0.0)
    if reason == "negative_realized_ev":
        raw_avg = _safe_float(getattr(pattern, "raw_realized_avg_return_pct", None))
        if raw_avg is None:
            return True
        raw_n = _safe_int(getattr(pattern, "raw_realized_trade_count", None)) or 0
        payoff = _safe_float(getattr(pattern, "payoff_ratio", None))
        payoff_n = _safe_int(getattr(pattern, "payoff_ratio_n", None)) or 0
        payoff_protected = (
            payoff is not None
            and payoff >= 1.5
            and payoff_n >= int(getattr(config, "min_realized_trades", 5) or 5)
        )
        return raw_avg < 0.0 and not payoff_protected and raw_n > 0
    if reason in {
        "promotion_gate_not_currently_passed",
        "promotion_gate_not_passed",
        "promotion_gate_failed",
        "cpcv_promotion_gate_failed",
    }:
        return getattr(pattern, "promotion_gate_passed", None) is not True
    return True


def _refresh_pattern_recert_state(
    pattern: Any,
    hard_recert_reasons: set[str],
) -> dict[str, Any]:
    """Reconcile stale persisted recert flags with current evidence, without bypassing hard debt."""
    from app.config import settings
    from app.services.trading.alpha_portfolio_gate import (
        config_from_settings,
        recert_reasons_for_pattern,
    )

    config = config_from_settings(settings)
    previous = _recert_reason_list(getattr(pattern, "recert_reason", None))
    previous_set = set(previous)
    current = recert_reasons_for_pattern(pattern, config=config)
    current_set = set(current)

    if previous:
        refreshed: list[str] = []
        for reason in previous:
            if reason in current_set:
                refreshed.append(reason)
                continue
            if (
                reason in hard_recert_reasons
                and _hard_reason_still_unresolved(pattern, reason, config)
            ):
                refreshed.append(reason)

        for reason in current:
            if reason in hard_recert_reasons and reason not in refreshed:
                refreshed.append(reason)
        if not refreshed and current:
            refreshed = list(current)
    else:
        refreshed = list(current)

    changed = previous != refreshed
    pattern.recert_required = bool(refreshed)
    pattern.recert_reason = ",".join(refreshed) if refreshed else None
    return {
        "previous_recert_reasons": previous,
        "current_recert_reasons": current,
        "persisted_recert_reasons": refreshed,
        "cleared_recert_reasons": [r for r in previous if r not in set(refreshed)],
        "added_recert_reasons": [r for r in refreshed if r not in previous_set],
        "changed": changed,
    }


def _recert_rescue_diagnostic_status(
    *,
    recert_required: bool,
    recert_reason: Any,
    graduation_blocker: Any,
    hard_recert_reasons: set[str],
) -> tuple[str, str]:
    if not recert_required:
        return "not_recert_required", "no_recert_action_needed"

    blocker = str(graduation_blocker or "").strip().lower()
    reasons = _recert_reason_set(recert_reason)
    if reasons & hard_recert_reasons or blocker == "hard_recert_blocked":
        return "hard_blocked", "keep_live_blocked_until_hard_recert_clears"
    if blocker == "recert_blocked":
        return "soft_blocked", "complete_oos_recert_and_quality_refresh"
    return "needs_review", "inspect_recert_diagnostic"


def _recert_rescue_backtest_refresh(
    db: "Session",
    *,
    scan_pattern_id: int,
    reliability: dict[str, Any],
    hard_reasons: list[str],
    soft_reasons: list[str],
    parent_event_id: int,
) -> dict[str, Any]:
    """Queue a targeted backtest refresh when recert debt has positive edge supply."""
    edge_eval_count = _safe_int(reliability.get("edge_eval_count")) or 0
    calibrated_ev = _safe_float(reliability.get("calibrated_ev_pct"))
    expected_ev = _safe_float(reliability.get("expected_ev_pct"))
    ev_for_gate = calibrated_ev if calibrated_ev is not None else expected_ev
    asset_class = str(
        reliability.get("slice_asset_class")
        or reliability.get("asset_class")
        or "all"
    ).strip() or "all"
    fingerprint = str(reliability.get("evidence_fingerprint") or "none").strip() or "none"

    out = {
        "requested": False,
        "event_id": None,
        "reason": None,
        "asset_class": asset_class,
        "edge_eval_count": edge_eval_count,
        "calibrated_ev_pct": calibrated_ev,
        "expected_ev_pct": expected_ev,
        "evidence_fingerprint": fingerprint,
    }
    if edge_eval_count < _RECERT_RESCUE_MIN_EDGE_EVALS:
        out["reason"] = "insufficient_positive_edge_evaluations"
        return out
    if ev_for_gate is None or ev_for_gate <= _RECERT_RESCUE_MIN_POSITIVE_EV_PCT:
        out["reason"] = "non_positive_recent_edge"
        return out

    if "negative_oos_recert" in set(hard_reasons):
        refresh_reason = "positive_edge_supply_needs_asset_sliced_oos_refresh"
    elif soft_reasons:
        refresh_reason = "soft_recert_needs_oos_quality_refresh"
    else:
        out["reason"] = "no_recert_refresh_needed"
        return out

    from app.models.trading import BrainWorkEvent
    from app.services.trading.brain_work.ledger import enqueue_work_event

    open_refresh = (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.event_type == "backtest_requested")
        .filter(BrainWorkEvent.status.in_(("pending", "processing", "retry_wait")))
        .filter(BrainWorkEvent.payload["scan_pattern_id"].astext == str(int(scan_pattern_id)))
        .filter(BrainWorkEvent.payload["source"].astext == "recert_rescue_refresh")
        .filter(BrainWorkEvent.payload["asset_class"].astext == asset_class)
        .order_by(BrainWorkEvent.updated_at.desc().nullslast(), BrainWorkEvent.id.desc())
        .first()
    )
    if open_refresh is not None:
        out["event_id"] = int(open_refresh.id)
        out["reason"] = "recert_backtest_refresh_already_open"
        return out

    event_id = enqueue_work_event(
        db,
        event_type="backtest_requested",
        dedupe_key=(
            f"bt_req:recert_rescue:p{int(scan_pattern_id)}:"
            f"{asset_class}:{fingerprint}"
        ),
        payload={
            "scan_pattern_id": int(scan_pattern_id),
            "source": "recert_rescue_refresh",
            "asset_class": asset_class,
            "recert_refresh_reason": refresh_reason,
            "evidence_fingerprint": fingerprint,
            "parent_event_id": int(parent_event_id or 0),
        },
        parent_event_id=int(parent_event_id or 0),
        lease_scope="backtest",
    )
    out["requested"] = event_id is not None
    out["event_id"] = event_id
    out["reason"] = refresh_reason
    return out


def _recert_rescue_parent_payload(
    db: "Session",
    ev: Any,
) -> tuple[dict[str, Any], int | None]:
    parent_id = (
        _safe_int(_payload(ev).get("parent_work_event_id"))
        or _safe_int(getattr(ev, "parent_event_id", None))
    )
    if parent_id is None:
        return {}, None
    try:
        from app.models.trading import BrainWorkEvent

        parent = db.get(BrainWorkEvent, parent_id)
    except Exception:
        logger.debug(
            "%s recert post-backtest parent lookup failed id=%s",
            LOG_PREFIX,
            parent_id,
            exc_info=True,
        )
        return {}, parent_id
    parent_payload = getattr(parent, "payload", None) if parent is not None else None
    return parent_payload if isinstance(parent_payload, dict) else {}, parent_id


def handle_recert_rescue_post_backtest(
    db: "Session",
    ev: Any,
    user_id: int | None,
) -> bool:
    """Reconcile recert flags after a recert-rescue backtest completes.

    This is intentionally a reconciliation-only leg. If fresh OOS/quality
    evidence clears stale recert debt, the pattern can move forward through the
    existing gates; if hard debt remains, live stays blocked. It never queues
    another backtest, which avoids a rescue loop.
    """
    from app.models.trading import ScanPattern
    from app.services.trading.brain_work.ledger import enqueue_outcome_event
    from app.services.trading.edge_reliability import (
        HARD_RECERT_REASONS,
        RECERT_RESCUE_DIAGNOSTIC,
        compute_pattern_edge_reliability,
    )

    payload = _payload(ev)
    parent_payload, parent_id = _recert_rescue_parent_payload(db, ev)
    source = str(payload.get("source") or parent_payload.get("source") or "").strip()
    refresh_reason = str(
        payload.get("recert_refresh_reason")
        or parent_payload.get("recert_refresh_reason")
        or ""
    ).strip()
    if source != "recert_rescue_refresh" and not refresh_reason:
        return False

    pid = _pattern_id(ev) or _safe_int(parent_payload.get("scan_pattern_id"))
    if pid is None:
        raise ValueError("recert rescue post-backtest missing scan_pattern_id")
    pattern = db.get(ScanPattern, pid)
    if pattern is None:
        raise ValueError(f"scan_pattern_id={pid} not found")

    recert_reconcile = _refresh_pattern_recert_state(pattern, set(HARD_RECERT_REASONS))
    db.flush()

    asset_class = payload.get("asset_class") or parent_payload.get("asset_class")
    window_days = _safe_int(payload.get("window_days") or parent_payload.get("window_days")) or _window_days(ev)
    reliability = compute_pattern_edge_reliability(
        db,
        pid,
        asset_class=asset_class,
        window_days=window_days,
    )
    reasons = reliability.get("recert_reason")
    reason_set = _recert_reason_set(reasons)
    hard_reasons = sorted(reason_set & HARD_RECERT_REASONS)
    soft_reasons = sorted(reason_set - set(hard_reasons))
    recert_required = bool(getattr(pattern, "recert_required", False))
    rescue_status, next_action = _recert_rescue_diagnostic_status(
        recert_required=recert_required,
        recert_reason=reasons,
        graduation_blocker=reliability.get("graduation_blocker"),
        hard_recert_reasons=set(HARD_RECERT_REASONS),
    )
    payload_out = {
        "scan_pattern_id": pid,
        "source": "recert_rescue_post_backtest",
        "recert_required": recert_required,
        "recert_reason": reasons,
        "recert_rescue_status": rescue_status,
        "hard_recert_reasons": hard_reasons,
        "soft_recert_reasons": soft_reasons,
        "recommended_next_action": next_action,
        "graduation_blocker": reliability.get("graduation_blocker"),
        "recert_reconcile": recert_reconcile,
        "recert_backtest_refresh": {
            "requested": False,
            "event_id": None,
            "reason": "post_backtest_reconcile_only",
            "asset_class": reliability.get("slice_asset_class") or asset_class,
            "evidence_fingerprint": reliability.get("evidence_fingerprint"),
        },
        "parent_backtest_event_id": int(getattr(ev, "id", 0) or 0),
        "parent_recert_request_event_id": parent_id,
        "recert_refresh_reason": refresh_reason or None,
        "safe_to_bypass_live": False,
        "uses_existing_probation_only": True,
        "calibrated_ev_pct": reliability.get("calibrated_ev_pct"),
        "realized_ev_pct": reliability.get("realized_ev_pct"),
        "observed_at": _utc_iso(),
    }
    enqueue_outcome_event(
        db,
        event_type=RECERT_RESCUE_DIAGNOSTIC,
        dedupe_key=(
            f"{RECERT_RESCUE_DIAGNOSTIC}:post_bt:p{pid}:"
            f"ev{int(getattr(ev, 'id', 0) or 0)}:"
            f"{reliability.get('evidence_fingerprint')}"
        ),
        payload=payload_out,
        parent_event_id=int(getattr(ev, "id", 0) or 0),
        claimable=False,
    )
    return True


def handle_edge_reliability_refresh(
    db: "Session",
    ev: Any,
    user_id: int | None,
) -> None:
    """Persist a rolling reliability snapshot and enqueue the next safe work item."""
    from app.services.trading.edge_reliability import (
        EDGE_RELIABILITY_REFRESH,
        emit_targeted_profitability_work,
        persist_edge_reliability_snapshot,
    )

    pid = _pattern_id(ev)
    if pid is None:
        raise ValueError("edge_reliability_refresh missing scan_pattern_id")
    row = persist_edge_reliability_snapshot(
        db,
        pid,
        asset_class=_payload(ev).get("asset_class"),
        window_days=_window_days(ev),
        source=str(_payload(ev).get("source") or EDGE_RELIABILITY_REFRESH),
        parent_event_id=int(getattr(ev, "id", 0) or 0),
    )
    recommended = str(row.get("recommended_work_event") or "")
    if recommended and recommended != EDGE_RELIABILITY_REFRESH:
        calibrated_ev = row.get("calibrated_ev_pct")
        try:
            evidence_value = max(0.0, float(calibrated_ev or 0.0)) * math.log1p(
                int(row.get("edge_eval_count") or 0)
                + int(row.get("closed_evidence_count") or 0)
            )
        except (TypeError, ValueError):
            evidence_value = 0.0
        emit_targeted_profitability_work(
            db,
            event_type=recommended,
            scan_pattern_id=pid,
            source="edge_reliability_snapshot",
            asset_class=row.get("slice_asset_class"),
            evidence_fingerprint=str(row.get("evidence_fingerprint") or ""),
            payload={
                "edge_snapshot_event_id": row.get("snapshot_event_id"),
                "asset_class": row.get("slice_asset_class"),
                "graduation_blocker": row.get("graduation_blocker"),
                "calibrated_ev_pct": row.get("calibrated_ev_pct"),
                "expected_evidence_value": round(evidence_value, 6),
            },
        )
    logger.info(
        "%s edge_reliability_refresh ev_id=%s pattern_id=%s blocker=%s",
        LOG_PREFIX,
        getattr(ev, "id", None),
        pid,
        row.get("graduation_blocker"),
    )


def handle_recert_rescue_refresh(
    db: "Session",
    ev: Any,
    user_id: int | None,
) -> None:
    """Refresh quality evidence for a recert-blocked pattern; never bypass recert."""
    from app.models.trading import ScanPattern
    from app.services.trading.edge_reliability import (
        HARD_RECERT_REASONS,
        RECERT_RESCUE_DIAGNOSTIC,
        compute_pattern_edge_reliability,
    )
    from app.services.trading.brain_work.ledger import enqueue_outcome_event

    pid = _pattern_id(ev)
    if pid is None:
        raise ValueError("recert_rescue_refresh missing scan_pattern_id")
    pattern = db.get(ScanPattern, pid)
    if pattern is None:
        raise ValueError(f"scan_pattern_id={pid} not found")

    quality_recomputed = False
    try:
        from app.services.trading.brain_work.handlers.quality_score import (
            _recompute_for_pattern,
        )

        quality_recomputed = _recompute_for_pattern(
            db,
            pid,
            source="recert_rescue_refresh",
            ev=ev,
        ) is not None
    except Exception:
        logger.debug("%s quality recompute skipped pattern_id=%s", LOG_PREFIX, pid, exc_info=True)

    recert_reconcile = _refresh_pattern_recert_state(pattern, set(HARD_RECERT_REASONS))
    db.flush()

    reliability = compute_pattern_edge_reliability(
        db,
        pid,
        asset_class=_payload(ev).get("asset_class"),
        window_days=_window_days(ev),
    )
    reasons = reliability.get("recert_reason")
    reason_set = _recert_reason_set(reasons)
    hard_reasons = sorted(reason_set & HARD_RECERT_REASONS)
    soft_reasons = sorted(reason_set - set(hard_reasons))
    recert_required = bool(getattr(pattern, "recert_required", False))
    rescue_status, next_action = _recert_rescue_diagnostic_status(
        recert_required=recert_required,
        recert_reason=reasons,
        graduation_blocker=reliability.get("graduation_blocker"),
        hard_recert_reasons=set(HARD_RECERT_REASONS),
    )
    backtest_refresh = _recert_rescue_backtest_refresh(
        db,
        scan_pattern_id=pid,
        reliability=reliability,
        hard_reasons=hard_reasons,
        soft_reasons=soft_reasons,
        parent_event_id=int(getattr(ev, "id", 0) or 0),
    )
    if backtest_refresh.get("requested"):
        next_action = "run_recert_backtest_refresh_keep_live_blocked"
    payload = {
        "scan_pattern_id": pid,
        "source": "recert_rescue_refresh",
        "recert_required": recert_required,
        "recert_reason": reasons,
        "recert_rescue_status": rescue_status,
        "hard_recert_reasons": hard_reasons,
        "soft_recert_reasons": soft_reasons,
        "recommended_next_action": next_action,
        "graduation_blocker": reliability.get("graduation_blocker"),
        "quality_recomputed": quality_recomputed,
        "recert_reconcile": recert_reconcile,
        "recert_backtest_refresh": backtest_refresh,
        "safe_to_bypass_live": False,
        "uses_existing_probation_only": True,
        "calibrated_ev_pct": reliability.get("calibrated_ev_pct"),
        "realized_ev_pct": reliability.get("realized_ev_pct"),
        "observed_at": _utc_iso(),
    }
    enqueue_outcome_event(
        db,
        event_type=RECERT_RESCUE_DIAGNOSTIC,
        dedupe_key=f"{RECERT_RESCUE_DIAGNOSTIC}:p{pid}:{reliability.get('evidence_fingerprint')}",
        payload=payload,
        parent_event_id=int(getattr(ev, "id", 0) or 0),
        claimable=False,
    )


def _exit_variant_fast_skip_reason(payload: dict[str, Any]) -> str | None:
    """Return a safe skip reason when an exit refresh has no positive evidence to mine."""
    expected_value = _safe_float(payload.get("expected_evidence_value"))
    if expected_value is not None and expected_value > 0.0:
        return None

    category = str(payload.get("cash_deployment_category") or "").strip().lower()
    if category == "negative_ev":
        return "negative_ev_no_exit_variant_birth"

    edge_values = [
        _safe_float(payload.get("calibrated_ev_after_cost_pct")),
        _safe_float(payload.get("calibrated_ev_pct")),
        _safe_float(payload.get("expected_net_pct")),
    ]
    non_positive_edges = [
        value for value in edge_values if value is not None and value <= 0.0
    ]
    if not non_positive_edges:
        return None

    blocker = str(payload.get("graduation_blocker") or "").strip().lower()
    if blocker in {"quality_blocked", "negative_ev", "non_positive_expected_edge"}:
        return "non_positive_quality_evidence_no_exit_variant_birth"
    return None


def _time_decay_payload_sample(payload: dict[str, Any]) -> dict[str, Any] | None:
    expected = _safe_float(payload.get("expected_net_pct"))
    if expected is None or expected <= 0.0:
        return None
    realized = _safe_float(payload.get("realized_return_pct"))
    pnl = _safe_float(payload.get("pnl"))
    if (realized is None or realized >= 0.0) and (pnl is None or pnl >= 0.0):
        return None
    static_reward = _safe_float(
        payload.get("target_reward_fraction")
        or payload.get("reward_fraction")
        or payload.get("target_fraction")
    )
    static_loss = _safe_float(
        payload.get("stop_loss_fraction")
        or payload.get("hard_stop_loss_fraction")
        or payload.get("loss_fraction")
    )
    return {
        "paper_trade_id": _safe_int(payload.get("paper_trade_id")),
        "ticker": str(payload.get("ticker") or "").strip().upper(),
        "asset_class": payload.get("asset_class"),
        "expected_net_pct": expected,
        "realized_return_pct": realized,
        "pnl": pnl,
        "exit_reason": str(payload.get("exit_reason") or "exit_engine_time_decay").strip().lower(),
        "static_reward_fraction": (
            static_reward if static_reward is not None and static_reward > 0.0 else None
        ),
        "static_stop_loss_fraction": (
            static_loss if static_loss is not None and static_loss > 0.0 else None
        ),
    }


def _time_decay_row_sample(row: Any, fallback_asset_class: Any) -> dict[str, Any] | None:
    sig = _json_dict(getattr(row, "signal_json", None))
    if not (
        getattr(row, "paper_shadow_of_alert_id", None)
        or sig.get("paper_shadow")
        or sig.get("shadow_of_alert_id")
        or sig.get("auto_trader_v1")
    ):
        return None
    edge = _json_dict(sig.get("entry_edge"))
    expected = _safe_float(edge.get("expected_net_pct"))
    if expected is None:
        expected = _safe_float(sig.get("entry_edge_expected_net_pct"))
    if expected is None or expected <= 0.0:
        return None

    pnl = _safe_float(getattr(row, "pnl", None))
    realized = _safe_float(getattr(row, "pnl_pct", None))
    if realized is None:
        entry = _safe_float(getattr(row, "entry_price", None))
        exit_price = _safe_float(getattr(row, "exit_price", None))
        qty = _safe_float(getattr(row, "quantity", None)) or 0.0
        direction = str(getattr(row, "direction", "") or "").strip().lower()
        if entry is not None and entry > 0.0 and exit_price is not None and qty > 0.0:
            raw = ((exit_price - entry) / entry) * 100.0
            realized = -raw if direction == "short" else raw
    if (realized is None or realized >= 0.0) and (pnl is None or pnl >= 0.0):
        return None

    paper_meta = _json_dict(sig.get("_paper_meta"))
    exit_config = _json_dict(paper_meta.get("exit_config"))
    static_reward = _safe_float(
        exit_config.get("target_reward_fraction")
        or exit_config.get("reward_fraction")
        or exit_config.get("target_fraction")
    )
    static_loss = _safe_float(
        exit_config.get("hard_stop_loss_fraction")
        or exit_config.get("stop_loss_fraction")
        or exit_config.get("loss_fraction")
    )
    asset_class = (
        sig.get("asset_class")
        or sig.get("asset_type")
        or fallback_asset_class
        or ("crypto" if str(getattr(row, "ticker", "") or "").upper().endswith("-USD") else None)
    )
    return {
        "paper_trade_id": _safe_int(getattr(row, "id", None)),
        "ticker": str(getattr(row, "ticker", "") or "").strip().upper(),
        "asset_class": asset_class,
        "expected_net_pct": expected,
        "realized_return_pct": realized,
        "pnl": pnl,
        "exit_reason": str(getattr(row, "exit_reason", "") or "").strip().lower(),
        "static_reward_fraction": static_reward if static_reward and static_reward > 0.0 else None,
        "static_stop_loss_fraction": static_loss if static_loss and static_loss > 0.0 else None,
    }


def _paper_time_decay_edge_miss_report(
    db: "Session",
    *,
    pattern_id: int,
    payload: dict[str, Any],
    window_days: int,
) -> dict[str, Any] | None:
    """Build a learned-exit report from positive-edge paper time-decay losses."""
    source = str(payload.get("source") or "").strip().lower()
    blocker = str(payload.get("graduation_blocker") or "").strip().lower()
    exit_reason = str(payload.get("exit_reason") or "").strip().lower()
    category = str(payload.get("cash_deployment_category") or "").strip().lower()
    is_time_decay_mismatch = (
        blocker == "exit_thesis_mismatch"
        and (
            exit_reason in _TIME_DECAY_EDGE_MISS_REASONS
            or "time_decay" in category
        )
    )
    if source != _TIME_DECAY_EDGE_MISS_SOURCE and not is_time_decay_mismatch:
        return None

    from app.config import settings
    from app.models.trading import PaperTrade
    from app.services.trading.learning import EDGE_EXIT_CONFIG_SOURCE

    rows: list[Any] = []
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        days=max(1, int(window_days))
    )
    try:
        rows = (
            db.query(PaperTrade)
            .filter(PaperTrade.status == "closed")
            .filter(PaperTrade.scan_pattern_id == int(pattern_id))
            .filter(PaperTrade.exit_reason.in_(tuple(_TIME_DECAY_EDGE_MISS_REASONS)))
            .filter(PaperTrade.exit_date >= cutoff)
            .filter(PaperTrade.pnl < 0)
            .order_by(PaperTrade.exit_date.desc(), PaperTrade.id.desc())
            .limit(_TIME_DECAY_EDGE_MISS_FETCH_LIMIT)
            .all()
        )
    except Exception:
        logger.debug(
            "%s time_decay edge-miss report query failed pattern_id=%s",
            LOG_PREFIX,
            pattern_id,
            exc_info=True,
        )

    samples: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    fallback_asset_class = payload.get("asset_class")
    for row in rows:
        sample = _time_decay_row_sample(row, fallback_asset_class)
        if not sample:
            continue
        paper_trade_id = sample.get("paper_trade_id")
        if paper_trade_id is not None:
            seen_ids.add(int(paper_trade_id))
        samples.append(sample)

    payload_sample = _time_decay_payload_sample(payload)
    if payload_sample:
        payload_id = payload_sample.get("paper_trade_id")
        if payload_id is None or int(payload_id) not in seen_ids:
            samples.append(payload_sample)

    if not samples:
        return None

    try:
        min_losses = max(
            1,
            int(
                getattr(
                    settings,
                    "brain_work_time_decay_exit_variant_min_losses",
                    _TIME_DECAY_EDGE_MISS_DEFAULT_MIN_LOSSES,
                )
            ),
        )
    except (TypeError, ValueError):
        min_losses = _TIME_DECAY_EDGE_MISS_DEFAULT_MIN_LOSSES

    expected_vals = [
        float(s["expected_net_pct"])
        for s in samples
        if _safe_float(s.get("expected_net_pct")) is not None
    ]
    realized_vals = [
        float(s["realized_return_pct"])
        for s in samples
        if _safe_float(s.get("realized_return_pct")) is not None
    ]
    pnl_vals = [
        float(s["pnl"])
        for s in samples
        if _safe_float(s.get("pnl")) is not None
    ]
    static_reward_vals = [
        float(s["static_reward_fraction"])
        for s in samples
        if _safe_float(s.get("static_reward_fraction")) is not None
    ]
    static_loss_vals = [
        float(s["static_stop_loss_fraction"])
        for s in samples
        if _safe_float(s.get("static_stop_loss_fraction")) is not None
    ]
    tickers: dict[str, int] = {}
    asset_types: dict[str, int] = {}
    reject_reasons: dict[str, int] = {}
    for sample in samples:
        _counter_add(tickers, sample.get("ticker"), fallback="unknown")
        _counter_add(asset_types, sample.get("asset_class"), fallback="unknown")
        _counter_add(reject_reasons, sample.get("exit_reason"), fallback="exit_engine_time_decay")

    total = len(samples)
    return {
        "source": EDGE_EXIT_CONFIG_SOURCE,
        "original_source": _TIME_DECAY_EDGE_MISS_SOURCE,
        "paper_time_decay_edge_miss": True,
        "scan_pattern_id": int(pattern_id),
        "total_rejects": total,
        "min_rejects_for_variant": min_losses,
        "thin_sample": bool(total < min_losses),
        "avg_expected_net_pct": round(_mean_float(expected_vals) or 0.0, 6),
        "min_expected_net_pct": round(min(expected_vals), 6) if expected_vals else None,
        "max_expected_net_pct": round(max(expected_vals), 6) if expected_vals else None,
        "avg_realized_return_pct": (
            round(_mean_float(realized_vals), 6) if realized_vals else None
        ),
        "total_pnl": round(sum(pnl_vals), 6) if pnl_vals else None,
        "avg_static_reward_fraction": (
            round(_mean_float(static_reward_vals), 8) if static_reward_vals else None
        ),
        "avg_static_stop_loss_fraction": (
            round(_mean_float(static_loss_vals), 8) if static_loss_vals else None
        ),
        "tickers": tickers,
        "asset_types": asset_types,
        "signal_lanes": {"paper_shadow_or_autotrader": total},
        "managed_geometry_reasons": {"time_decay_exit_thesis_mismatch": total},
        "probability_sources": {"paper_entry_edge": total},
        "reject_reasons": reject_reasons,
        "paper_trade_ids": [
            int(s["paper_trade_id"])
            for s in samples
            if _safe_int(s.get("paper_trade_id")) is not None
        ][:50],
        "root_cause": "paper_time_decay_exit_thesis_mismatch",
    }


def handle_exit_variant_refresh(
    db: "Session",
    ev: Any,
    user_id: int | None,
) -> None:
    """Ask existing edge-aware ScanPattern evolution for learned exit children."""
    from app.services.trading.brain_work.ledger import enqueue_outcome_event
    from app.services.trading.edge_reliability import EXIT_VARIANT_DIAGNOSTIC

    pid = _pattern_id(ev)
    if pid is None:
        raise ValueError("exit_variant_refresh missing scan_pattern_id")
    payload_in = _payload(ev)
    fast_skip_reason = _exit_variant_fast_skip_reason(payload_in)
    if fast_skip_reason:
        enqueue_outcome_event(
            db,
            event_type=EXIT_VARIANT_DIAGNOSTIC,
            dedupe_key=(
                f"{EXIT_VARIANT_DIAGNOSTIC}:p{pid}:fast_skip:"
                f"{fast_skip_reason}:{payload_in.get('evidence_fingerprint') or 'none'}"
            ),
            payload={
                "scan_pattern_id": pid,
                "source": "exit_variant_refresh",
                "asset_class": payload_in.get("asset_class"),
                "cash_deployment_category": payload_in.get("cash_deployment_category"),
                "evidence_fingerprint": payload_in.get("evidence_fingerprint"),
                "graduation_blocker": payload_in.get("graduation_blocker"),
                "skip_reason": fast_skip_reason,
                "created_child_ids": [],
                "created_count": 0,
                "loss_report": None,
                "fast_skipped": True,
                "shadow_only": True,
                "observed_at": _utc_iso(),
            },
            parent_event_id=int(getattr(ev, "id", 0) or 0),
            claimable=False,
        )
        logger.info(
            "%s exit_variant_refresh ev_id=%s pattern_id=%s fast_skip=%s",
            LOG_PREFIX,
            getattr(ev, "id", None),
            pid,
            fast_skip_reason,
        )
        return

    from app.services.trading.learning import (
        _edge_debt_loss_reports,
        fork_edge_learned_exit_variants,
    )

    window_days = _window_days(ev)
    report = _paper_time_decay_edge_miss_report(
        db,
        pattern_id=pid,
        payload=payload_in,
        window_days=window_days,
    )
    if report is None:
        report = _edge_debt_loss_reports(db, lookback_days=window_days).get(pid)
    variant_diag: dict[str, Any] = {}
    created_ids = fork_edge_learned_exit_variants(
        db,
        pid,
        edge_loss_report=report,
        diagnostics=variant_diag,
    )
    skip_reason = variant_diag.get("skip_reason")
    payload = {
        "scan_pattern_id": pid,
        "source": "exit_variant_refresh",
        "asset_class": payload_in.get("asset_class"),
        "cash_deployment_category": payload_in.get("cash_deployment_category"),
        "evidence_fingerprint": payload_in.get("evidence_fingerprint"),
        "graduation_blocker": payload_in.get("graduation_blocker"),
        "skip_reason": skip_reason,
        "variant_label": variant_diag.get("variant_label"),
        "existing_child_id": variant_diag.get("existing_child_id"),
        "refreshed_existing_child_id": variant_diag.get(
            "refreshed_existing_child_id"
        ),
        "refreshed_count": int(variant_diag.get("refreshed_count") or 0),
        "active_child_count": variant_diag.get("active_child_count"),
        "created_child_ids": created_ids,
        "created_count": len(created_ids),
        "loss_report": report,
        "variant_diagnostics": dict(variant_diag),
        "shadow_only": True,
        "observed_at": _utc_iso(),
    }
    key_basis = (report or {}).get("avg_expected_net_pct")
    action_key = skip_reason or ("created" if created_ids else "none")
    child_key = (
        variant_diag.get("refreshed_existing_child_id")
        or variant_diag.get("existing_child_id")
        or ",".join(str(x) for x in created_ids)
        or "none"
    )
    enqueue_outcome_event(
        db,
        event_type=EXIT_VARIANT_DIAGNOSTIC,
        dedupe_key=(
            f"{EXIT_VARIANT_DIAGNOSTIC}:p{pid}:{key_basis}:"
            f"{action_key}:{child_key}"
        ),
        payload=payload,
        parent_event_id=int(getattr(ev, "id", 0) or 0),
        claimable=False,
    )


def handle_provenance_backfill(
    db: "Session",
    ev: Any,
    user_id: int | None,
) -> None:
    """Record scoped provenance debt diagnostics for later repair work."""
    from app.services.trading.brain_work.ledger import enqueue_outcome_event
    from app.services.trading.cash_deployment import low_confidence_exit_attribution_rollup
    from app.services.trading.edge_reliability import (
        PROVENANCE_BACKFILL_DIAGNOSTIC,
        null_lineage_short_paper_candidates,
    )

    payload_in = _payload(ev)
    pid = _pattern_id(ev)
    asset_class = payload_in.get("asset_class")
    window_days = _window_days(ev)
    candidates = null_lineage_short_paper_candidates(
        db,
        window_days=window_days,
    )
    low_confidence_exit_attribution = {
        "window_days": window_days,
        "total_groups": 0,
        "returned_groups": 0,
        "rows": [],
    }
    if _needs_exit_provenance_backfill(payload_in):
        low_confidence_exit_attribution = low_confidence_exit_attribution_rollup(
            db,
            user_id=user_id,
            pattern_ids=[pid] if pid is not None else None,
            asset_class=str(asset_class or "") or None,
            window_days=window_days,
            limit=10,
        )
    exit_rows = list(low_confidence_exit_attribution.get("rows") or [])
    exit_summary = {
        k: v for k, v in low_confidence_exit_attribution.items() if k != "rows"
    }
    exit_repair_summary = _repair_low_confidence_exit_provenance(
        db,
        exit_rows,
        user_id=user_id,
    ) if exit_rows else {
        "considered_trade_ids": [],
        "considered_count": 0,
        "repaired_count": 0,
        "repaired": [],
        "skipped": [],
    }
    key = str(payload_in.get("evidence_fingerprint") or "").strip()
    if not key and exit_rows:
        key = str(exit_rows[0].get("evidence_fingerprint") or "none")
    if not key and candidates:
        key = str(candidates[0].get("evidence_fingerprint") or "none")
    if not key:
        key = "none"
    edge_refresh_after_repair = _queue_exit_repair_edge_refresh(
        db,
        scan_pattern_id=pid,
        asset_class=asset_class,
        window_days=window_days,
        evidence_key=key,
        repair_summary=exit_repair_summary,
    )
    enqueue_outcome_event(
        db,
        event_type=PROVENANCE_BACKFILL_DIAGNOSTIC,
        dedupe_key=f"{PROVENANCE_BACKFILL_DIAGNOSTIC}:{key[:96]}",
        payload={
            "source": "provenance_backfill",
            "scan_pattern_id": pid,
            "asset_class": asset_class,
            "cash_deployment_category": payload_in.get("cash_deployment_category"),
            "exit_provenance_blocker": payload_in.get("exit_provenance_blocker"),
            "evidence_fingerprint": payload_in.get("evidence_fingerprint"),
            "candidate_count": len(candidates),
            "candidates": candidates,
            "exit_attribution_debt_count": len(exit_rows),
            "low_confidence_exit_attribution": exit_rows,
            "low_confidence_exit_attribution_summary": exit_summary,
            "exit_provenance_repair_summary": exit_repair_summary,
            "edge_reliability_refresh_after_repair": edge_refresh_after_repair,
            "repair_applied": bool(exit_repair_summary.get("repaired_count")),
            "research_only": not bool(exit_repair_summary.get("repaired_count")),
            "observed_at": _utc_iso(),
        },
        parent_event_id=int(getattr(ev, "id", 0) or 0),
        claimable=False,
    )

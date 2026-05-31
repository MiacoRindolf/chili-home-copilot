"""Profitability evidence handlers for pilot-first candidate improvement.

Handlers here are deliberately conservative:
* edge reliability writes aggregate outcome snapshots only;
* recert rescue recomputes existing quality evidence but never clears recert;
* exit variant refresh delegates to existing ScanPattern evolution gates;
* provenance backfill writes diagnostics only.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, TYPE_CHECKING

from app.services.trading.recert_rescue_policy import (
    recert_rescue_diagnostic_matches_asset,
    recert_rescue_diagnostic_blocks_refresh,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
LOG_PREFIX = "[brain_work:profitability]"

_RECERT_RESCUE_MIN_EDGE_EVALS = 5
_RECERT_RESCUE_MIN_POSITIVE_EV_PCT = 0.0


def _payload(ev: Any) -> dict[str, Any]:
    raw = getattr(ev, "payload", None)
    return raw if isinstance(raw, dict) else {}


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


def _append_priority_ticker(out: list[str], seen: set[str], value: Any) -> None:
    ticker = str(value or "").strip().upper()
    if not ticker or ticker in seen:
        return
    out.append(ticker)
    seen.add(ticker)


def _coerce_priority_tickers(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, dict):
        values = [
            key for key, _count in sorted(
                value.items(),
                key=lambda item: _safe_float(item[1]) or 0.0,
                reverse=True,
            )
        ]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = [value]
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        _append_priority_ticker(out, seen, item)
    return out


def _recert_rescue_priority_tickers(
    *,
    payload: dict[str, Any],
    reliability: dict[str, Any],
) -> list[str]:
    """Return signal-first tickers for recert backtests.

    Recert rescue is only useful if it retests the asset slice that is actively
    blocked. Pattern-level queues can otherwise spend their whole budget on an
    unrelated broad universe and leave the live blocker unchanged.
    """
    out: list[str] = []
    seen: set[str] = set()
    for key in ("signal_ticker", "ticker", "primary_symbol"):
        _append_priority_ticker(out, seen, payload.get(key))
    for key in ("priority_tickers", "top_tickers"):
        for ticker in _coerce_priority_tickers(payload.get(key)):
            _append_priority_ticker(out, seen, ticker)
    _append_priority_ticker(out, seen, reliability.get("primary_symbol"))
    for ticker in _coerce_priority_tickers(reliability.get("tickers")):
        _append_priority_ticker(out, seen, ticker)
    return out


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


def _recert_rescue_recent_backtest_cooldown(
    db: "Session",
    *,
    scan_pattern_id: int,
) -> dict[str, Any] | None:
    try:
        from app.config import settings
        from app.models.trading import ScanPattern

        if not bool(getattr(settings, "brain_queue_recert_cooldown_enabled", True)):
            return None
        cooldown_minutes = int(
            getattr(settings, "brain_queue_recert_cooldown_minutes", 360) or 0
        )
        if cooldown_minutes <= 0:
            return None
        pattern = db.get(ScanPattern, int(scan_pattern_id))
    except Exception:
        logger.debug(
            "%s recert cooldown lookup skipped pattern_id=%s",
            LOG_PREFIX,
            scan_pattern_id,
            exc_info=True,
        )
        return None
    last_backtest_at = getattr(pattern, "last_backtest_at", None) if pattern else None
    if not isinstance(last_backtest_at, datetime):
        return None
    last_bt = last_backtest_at
    if last_bt.tzinfo is not None:
        last_bt = last_bt.astimezone(timezone.utc).replace(tzinfo=None)
    cooldown_until = last_bt + timedelta(minutes=cooldown_minutes)
    now_utc = datetime.utcnow()
    if now_utc >= cooldown_until:
        return None
    return {
        "reason": "recent_recert_backtest_cooldown",
        "cooldown_active": True,
        "last_backtest_at": last_bt.isoformat(),
        "cooldown_until": cooldown_until.isoformat(),
        "cooldown_minutes": cooldown_minutes,
    }


def _recert_rescue_backtest_refresh(
    db: "Session",
    *,
    scan_pattern_id: int,
    reliability: dict[str, Any],
    request_payload: dict[str, Any],
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
    priority_tickers = _recert_rescue_priority_tickers(
        payload=request_payload,
        reliability=reliability,
    )

    out = {
        "requested": False,
        "event_id": None,
        "reason": None,
        "asset_class": asset_class,
        "priority_tickers": priority_tickers,
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
        existing_payload = (
            dict(open_refresh.payload)
            if isinstance(open_refresh.payload, dict)
            else {}
        )
        existing_priority = _coerce_priority_tickers(
            existing_payload.get("priority_tickers")
        )
        merged_priority: list[str] = []
        seen_priority: set[str] = set()
        for ticker in priority_tickers + existing_priority:
            _append_priority_ticker(merged_priority, seen_priority, ticker)
        updated_open_request = False
        if merged_priority and merged_priority != existing_priority:
            existing_payload["priority_tickers"] = merged_priority
            existing_payload["signal_ticker"] = merged_priority[0]
            open_refresh.payload = existing_payload
            db.add(open_refresh)
            db.flush()
            updated_open_request = True
        out["event_id"] = int(open_refresh.id)
        out["reason"] = "recert_backtest_refresh_already_open"
        out["updated_open_request_priority_tickers"] = updated_open_request
        out["priority_tickers"] = merged_priority or priority_tickers
        return out

    cooldown = _recert_rescue_recent_backtest_cooldown(
        db,
        scan_pattern_id=scan_pattern_id,
    )
    if cooldown is not None:
        out.update(cooldown)
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
            "signal_ticker": priority_tickers[0] if priority_tickers else None,
            "priority_tickers": priority_tickers,
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
) -> tuple[dict[str, Any], int | None, datetime | None]:
    parent_id = (
        _safe_int(_payload(ev).get("parent_work_event_id"))
        or _safe_int(getattr(ev, "parent_event_id", None))
    )
    if parent_id is None:
        return {}, None, None
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
        return {}, parent_id, None
    parent_payload = getattr(parent, "payload", None) if parent is not None else None
    parent_created_at = getattr(parent, "created_at", None) if parent is not None else None
    return (
        parent_payload if isinstance(parent_payload, dict) else {},
        parent_id,
        parent_created_at if isinstance(parent_created_at, datetime) else None,
    )


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
    parent_payload, parent_id, parent_created_at = _recert_rescue_parent_payload(db, ev)
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

    from app.services.trading.recert_queue_service import (
        stamp_oos_recert_from_backtests,
    )

    oos_recert_stamp = stamp_oos_recert_from_backtests(
        db,
        scan_pattern_id=pid,
        since=parent_created_at,
        certifier="recert_rescue_post_backtest",
    )
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
    if (
        not bool(oos_recert_stamp.get("ok"))
        and oos_recert_stamp.get("reason") == "cert_failed_no_oos_evidence"
        and rescue_status != "not_recert_required"
    ):
        next_action = "inspect_recert_backtest_no_oos_evidence_keep_live_blocked"
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
        "oos_recert_stamp": oos_recert_stamp,
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
        "observed_at": datetime.utcnow().isoformat(),
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


def _recent_blocked_recert_rescue_diagnostic(
    db: "Session",
    *,
    scan_pattern_id: int,
    asset_class: str | None = None,
) -> bool:
    """True when a recent diagnostic says another rescue refresh would churn."""
    return _recent_blocked_recert_rescue_diagnostic_payload(
        db,
        scan_pattern_id=scan_pattern_id,
        asset_class=asset_class,
    ) is not None


def _recent_blocked_recert_rescue_diagnostic_payload(
    db: "Session",
    *,
    scan_pattern_id: int,
    asset_class: str | None = None,
) -> dict[str, Any] | None:
    """Return the latest recent diagnostic that makes a rescue refresh redundant."""
    from app.config import settings
    from app.models.trading import BrainWorkEvent
    from app.services.trading.edge_reliability import RECERT_RESCUE_DIAGNOSTIC

    minutes = _safe_int(
        getattr(settings, "brain_work_cash_deployment_noop_cooldown_minutes", 360)
    )
    if minutes is None or minutes <= 0:
        return None
    cutoff = datetime.utcnow() - timedelta(minutes=minutes)
    try:
        rows = (
            db.query(BrainWorkEvent)
            .filter(BrainWorkEvent.event_kind == "outcome")
            .filter(BrainWorkEvent.event_type == RECERT_RESCUE_DIAGNOSTIC)
            .filter(BrainWorkEvent.created_at >= cutoff)
            .filter(
                BrainWorkEvent.payload["scan_pattern_id"].astext
                == str(int(scan_pattern_id))
            )
            .order_by(BrainWorkEvent.created_at.desc(), BrainWorkEvent.id.desc())
            .limit(20)
            .all()
        )
    except Exception:
        logger.debug(
            "%s recert blocker lookup failed pattern_id=%s",
            LOG_PREFIX,
            scan_pattern_id,
            exc_info=True,
        )
        return None
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        if recert_rescue_diagnostic_blocks_refresh(
            payload
        ) and recert_rescue_diagnostic_matches_asset(
            payload,
            asset_class=asset_class,
        ):
            return {
                "event_id": int(getattr(row, "id", 0) or 0),
                "payload": payload,
            }
    return None


def handle_edge_reliability_refresh(
    db: "Session",
    ev: Any,
    user_id: int | None,
) -> None:
    """Persist a rolling reliability snapshot and enqueue the next safe work item."""
    from app.services.trading.edge_reliability import (
        EDGE_RELIABILITY_REFRESH,
        RECERT_RESCUE_REFRESH,
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
        if (
            recommended == RECERT_RESCUE_REFRESH
            and _recent_blocked_recert_rescue_diagnostic(
                db,
                scan_pattern_id=pid,
                asset_class=row.get("slice_asset_class"),
            )
        ):
            logger.info(
                "%s edge_reliability_refresh ev_id=%s pattern_id=%s suppressed=%s",
                LOG_PREFIX,
                getattr(ev, "id", None),
                pid,
                "recent_recert_blocker_diagnostic",
            )
            return
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
    recent_blocker = _recent_blocked_recert_rescue_diagnostic_payload(
        db,
        scan_pattern_id=pid,
        asset_class=_payload(ev).get("asset_class"),
    )
    if recent_blocker is not None:
        blocker_payload = recent_blocker["payload"]
        refresh = blocker_payload.get("recert_backtest_refresh")
        refresh_payload = refresh if isinstance(refresh, dict) else {}
        enqueue_outcome_event(
            db,
            event_type=RECERT_RESCUE_DIAGNOSTIC,
            dedupe_key=(
                f"{RECERT_RESCUE_DIAGNOSTIC}:p{pid}:fast_skip:"
                f"recent_blocker:{int(getattr(ev, 'id', 0) or 0)}"
            ),
            payload={
                "scan_pattern_id": pid,
                "source": "recert_rescue_refresh",
                "recert_rescue_status": blocker_payload.get("recert_rescue_status"),
                "recommended_next_action": "live_blocked_recert_debt_no_refresh",
                "skip_reason": "recent_recert_rescue_blocker_diagnostic",
                "blocker_event_id": recent_blocker["event_id"],
                "blocker_source": blocker_payload.get("source"),
                "blocker_next_action": blocker_payload.get("recommended_next_action"),
                "blocker_refresh_reason": refresh_payload.get("reason"),
                "quality_recomputed": False,
                "recert_backtest_refresh": {
                    "requested": False,
                    "event_id": None,
                    "reason": "recent_recert_rescue_blocker_diagnostic",
                    "asset_class": _payload(ev).get("asset_class"),
                    "evidence_fingerprint": _payload(ev).get("evidence_fingerprint"),
                },
                "fast_skipped": True,
                "safe_to_bypass_live": False,
                "uses_existing_probation_only": True,
                "observed_at": datetime.utcnow().isoformat(),
            },
            parent_event_id=int(getattr(ev, "id", 0) or 0),
            claimable=False,
        )
        logger.info(
            "%s recert_rescue_refresh ev_id=%s pattern_id=%s fast_skip=%s",
            LOG_PREFIX,
            getattr(ev, "id", None),
            pid,
            "recent_recert_rescue_blocker_diagnostic",
        )
        return
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
        request_payload=_payload(ev),
        hard_reasons=hard_reasons,
        soft_reasons=soft_reasons,
        parent_event_id=int(getattr(ev, "id", 0) or 0),
    )
    if backtest_refresh.get("requested"):
        next_action = "run_recert_backtest_refresh_keep_live_blocked"
    elif backtest_refresh.get("reason") == "recent_recert_backtest_cooldown":
        next_action = "wait_for_recert_backtest_cooldown_keep_live_blocked"
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
        "observed_at": datetime.utcnow().isoformat(),
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
                "observed_at": datetime.utcnow().isoformat(),
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

    report = _edge_debt_loss_reports(db, lookback_days=_window_days(ev)).get(pid)
    variant_diag: dict[str, Any] = {}
    created_ids = fork_edge_learned_exit_variants(
        db,
        pid,
        edge_loss_report=report,
        diagnostics=variant_diag,
    )
    payload = {
        "scan_pattern_id": pid,
        "source": "exit_variant_refresh",
        "asset_class": payload_in.get("asset_class"),
        "cash_deployment_category": payload_in.get("cash_deployment_category"),
        "evidence_fingerprint": payload_in.get("evidence_fingerprint"),
        "graduation_blocker": payload_in.get("graduation_blocker"),
        "skip_reason": variant_diag.get("skip_reason"),
        "variant_label": variant_diag.get("variant_label"),
        "created_child_ids": created_ids,
        "created_count": len(created_ids),
        "loss_report": report,
        "shadow_only": True,
        "observed_at": datetime.utcnow().isoformat(),
    }
    key_basis = (report or {}).get("avg_expected_net_pct")
    enqueue_outcome_event(
        db,
        event_type=EXIT_VARIANT_DIAGNOSTIC,
        dedupe_key=f"{EXIT_VARIANT_DIAGNOSTIC}:p{pid}:{key_basis}:{len(created_ids)}",
        payload=payload,
        parent_event_id=int(getattr(ev, "id", 0) or 0),
        claimable=False,
    )


def handle_provenance_backfill(
    db: "Session",
    ev: Any,
    user_id: int | None,
) -> None:
    """Record null-lineage short paper candidates for later provenance work."""
    from app.services.trading.brain_work.ledger import enqueue_outcome_event
    from app.services.trading.edge_reliability import (
        PROVENANCE_BACKFILL_DIAGNOSTIC,
        null_lineage_short_paper_candidates,
    )

    candidates = null_lineage_short_paper_candidates(
        db,
        window_days=_window_days(ev),
    )
    key = "none"
    if candidates:
        key = str(candidates[0].get("evidence_fingerprint") or "none")
    enqueue_outcome_event(
        db,
        event_type=PROVENANCE_BACKFILL_DIAGNOSTIC,
        dedupe_key=f"{PROVENANCE_BACKFILL_DIAGNOSTIC}:{key}",
        payload={
            "source": "provenance_backfill",
            "candidate_count": len(candidates),
            "candidates": candidates,
            "research_only": True,
            "observed_at": datetime.utcnow().isoformat(),
        },
        parent_event_id=int(getattr(ev, "id", 0) or 0),
        claimable=False,
    )

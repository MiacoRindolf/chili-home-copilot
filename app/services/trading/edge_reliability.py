"""Pattern-level edge reliability and profitability supply diagnostics.

This module deliberately stores aggregate metrics only: it reads existing
AutoTrader, paper, live, and alert evidence, then writes pattern-level and
asset-sliced snapshots to the ``brain_work_events`` outcome ledger. It does
not promote patterns or relax live-trading gates.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import false, or_
from sqlalchemy.orm import Session

from ...models.trading import (
    AutoTraderRun,
    BrainWorkEvent,
    BreakoutAlert,
    PaperTrade,
    ScanPattern,
    Trade,
)
from .brain_work.ledger import enqueue_outcome_event, enqueue_work_event

EDGE_RELIABILITY_REFRESH = "edge_reliability_refresh"
RECERT_RESCUE_REFRESH = "recert_rescue_refresh"
EXIT_VARIANT_REFRESH = "exit_variant_refresh"
PROVENANCE_BACKFILL = "provenance_backfill"
EDGE_RELIABILITY_SNAPSHOT = "edge_reliability_snapshot"
RECERT_RESCUE_DIAGNOSTIC = "recert_rescue_diagnostic"
EXIT_VARIANT_DIAGNOSTIC = "exit_variant_diagnostic"
PROVENANCE_BACKFILL_DIAGNOSTIC = "provenance_backfill_diagnostic"
EDGE_RELIABILITY_GRANULARITY_PATTERN = "pattern"
EDGE_RELIABILITY_GRANULARITY_ASSET_SLICE = "asset_slice"

DEFAULT_WINDOW_DAYS = 30
DEFAULT_MIN_CLOSED_EVIDENCE = 5
DEFAULT_TOP_LIMIT = 25
RunSliceRecord = tuple[AutoTraderRun, BreakoutAlert | None, dict[str, Any], dict[str, str]]

HARD_RECERT_REASONS = frozenset({
    "negative_oos_recert",
    "negative_realized_ev",
    "promotion_gate_not_currently_passed",
    "promotion_gate_not_passed",
    "promotion_gate_failed",
    "cpcv_promotion_gate_failed",
})

SHADOW_REASONS = frozenset({
    "selector:shadow_observation_signal_lane",
    "selector:shadow_promoted_pattern_eval",
})


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


def _canonical_asset_class(value: Any) -> str | None:
    raw = str(value or "").strip().lower()
    if raw in {"stock", "stocks", "equity", "equities", "us_equity", "us_eq"}:
        return "stock"
    if raw in {"crypto", "cryptocurrency", "coin", "coinbase_spot"}:
        return "crypto"
    if raw in {"option", "options"}:
        return "options"
    if raw in {"", "all", "unknown", "none"}:
        return None
    return raw


def _asset_class_for_evidence(
    *,
    ticker: Any = None,
    alert: BreakoutAlert | None = None,
    pattern: ScanPattern | None = None,
    trade: Trade | None = None,
    paper: PaperTrade | None = None,
) -> str:
    direct = None
    if alert is not None:
        direct = _canonical_asset_class(getattr(alert, "asset_type", None))
    if direct is None and trade is not None:
        direct = _canonical_asset_class(getattr(trade, "asset_kind", None))
    if direct is None and paper is not None:
        signal = _json_dict(getattr(paper, "signal_json", None))
        direct = _canonical_asset_class(signal.get("asset_type") or signal.get("asset_class"))
    if direct is None and pattern is not None:
        direct = _canonical_asset_class(getattr(pattern, "asset_class", None))
    symbol = str(
        ticker
        or getattr(alert, "ticker", None)
        or getattr(trade, "ticker", None)
        or getattr(paper, "ticker", None)
        or ""
    ).strip().upper()
    if direct is None and symbol.endswith("-USD"):
        direct = "crypto"
    return direct or "stock"


def _probability_source_for(edge: dict[str, Any]) -> str:
    return str(edge.get("probability_source") or "unknown").strip().lower() or "unknown"


def _execution_lane_for(run: AutoTraderRun) -> str:
    snap = _json_dict(getattr(run, "rule_snapshot", None))
    for key in ("execution_lane", "autotrader_execution_lane", "broker_lane"):
        lane = str(snap.get(key) or "").strip().lower()
        if lane:
            return lane
    reason = str(getattr(run, "reason", "") or "").strip().lower()
    decision = str(getattr(run, "decision", "") or "").strip().lower()
    if decision in {"placed", "scaled_in"}:
        return "live"
    if reason in SHADOW_REASONS or snap.get("paper_observation_signal_lane"):
        return "shadow_observation"
    if reason == "non_positive_expected_edge":
        return "ev_gate"
    if reason == "missed_entry_slippage":
        return "slippage_gate"
    if reason == "pattern_recert_required":
        return "recert_gate"
    if reason.startswith("broker:") or reason.startswith("broker_reject_suppressed:") or reason.startswith("venue_"):
        return "broker_reject"
    return decision or "unknown"


def _mean(values: list[float]) -> float | None:
    vals = [float(v) for v in values if math.isfinite(float(v))]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _round(value: float | None, digits: int = 6) -> float | None:
    return round(float(value), digits) if value is not None else None


def _event_time(row: Any) -> datetime | None:
    for name in ("created_at", "entry_date", "exit_date", "submitted_at", "filled_at"):
        value = getattr(row, name, None)
        if isinstance(value, datetime):
            return value
    return None


def _entry_edge_snapshot(run: AutoTraderRun) -> dict[str, Any]:
    snap = _json_dict(getattr(run, "rule_snapshot", None))
    edge = snap.get("entry_edge")
    return edge if isinstance(edge, dict) else {}


def _signal_lane_for(alert: BreakoutAlert | None, run: AutoTraderRun | None) -> str:
    if run is not None:
        snap = _json_dict(getattr(run, "rule_snapshot", None))
        lane = str(snap.get("paper_observation_signal_lane") or "").strip().lower()
        if lane:
            return lane
    if alert is not None:
        ind = _json_dict(getattr(alert, "indicator_snapshot", None))
        scorecard = ind.get("imminent_scorecard")
        if isinstance(scorecard, dict):
            lane = str(scorecard.get("signal_lane") or "").strip().lower()
            if lane:
                return lane
    return "standard"


def _reason_bucket(reason: str) -> str:
    r = str(reason or "").strip().lower()
    if r == "non_positive_expected_edge":
        return "negative_expected_edge"
    if r == "missed_entry_slippage":
        return "missed_entry_slippage"
    if r.startswith("broker:") or "adapter" in r or r.startswith("venue_"):
        return "broker_execution_reject"
    if r in SHADOW_REASONS:
        return "shadow_observation"
    if r == "pattern_recert_required":
        return "recert_required"
    return r or "unknown"


def _recert_reasons(pattern: ScanPattern | None) -> set[str]:
    raw = getattr(pattern, "recert_reason", None) if pattern is not None else None
    if isinstance(raw, str):
        return {part.strip() for part in raw.split(",") if part.strip()}
    if isinstance(raw, (list, tuple, set)):
        return {str(part).strip() for part in raw if str(part).strip()}
    return set()


def _paper_return_pct(row: PaperTrade) -> float | None:
    pct = _safe_float(getattr(row, "pnl_pct", None))
    if pct is not None:
        return pct
    pnl = _safe_float(getattr(row, "pnl", None))
    entry = _safe_float(getattr(row, "entry_price", None))
    qty = _safe_float(getattr(row, "quantity", None))
    notional = abs((entry or 0.0) * (qty or 0.0))
    if pnl is None or notional <= 0.0:
        return None
    return (pnl / notional) * 100.0


def _live_return_pct(row: Trade) -> float | None:
    pnl = _safe_float(getattr(row, "pnl", None))
    entry = (
        _safe_float(getattr(row, "avg_fill_price", None))
        or _safe_float(getattr(row, "entry_price", None))
    )
    qty = (
        _safe_float(getattr(row, "filled_quantity", None))
        or _safe_float(getattr(row, "quantity", None))
    )
    notional = abs((entry or 0.0) * (qty or 0.0))
    if pnl is None or notional <= 0.0:
        return None
    return (pnl / notional) * 100.0


def _outcome_label(pnl: Any) -> int | None:
    val = _safe_float(pnl)
    if val is None:
        return None
    return 1 if val > 0.0 else 0


def _calibrated_ev(
    expected_ev_pct: float | None,
    realized_ev_pct: float | None,
    closed_n: int,
    *,
    full_weight_n: int = 20,
) -> float | None:
    if expected_ev_pct is None:
        return realized_ev_pct
    if realized_ev_pct is None:
        return expected_ev_pct
    weight = max(0.0, min(1.0, float(closed_n) / float(max(1, full_weight_n))))
    return expected_ev_pct * (1.0 - weight) + realized_ev_pct * weight


def _graduation_blocker(
    pattern: ScanPattern | None,
    *,
    expected_ev_pct: float | None,
    calibrated_ev_pct: float | None,
    realized_ev_pct: float | None,
    closed_n: int,
    broker_rejects: int,
    edge_eval_count: int,
    min_closed: int = DEFAULT_MIN_CLOSED_EVIDENCE,
) -> str:
    lifecycle = str(getattr(pattern, "lifecycle_stage", "") or "").strip().lower()
    recert_required = bool(getattr(pattern, "recert_required", False))
    reasons = _recert_reasons(pattern)
    if recert_required:
        if reasons & HARD_RECERT_REASONS:
            return "hard_recert_blocked"
        return "recert_blocked"
    if edge_eval_count > 0 and broker_rejects > 0:
        return "execution_blocked"
    if expected_ev_pct is not None and expected_ev_pct <= 0.0:
        return "quality_blocked"
    if closed_n < min_closed:
        return "needs_more_closed_evidence"
    if realized_ev_pct is not None and realized_ev_pct <= 0.0:
        return "quality_blocked"
    if calibrated_ev_pct is not None and calibrated_ev_pct <= 0.0:
        return "quality_blocked"
    if lifecycle in {"live", "promoted", "pilot_promoted"}:
        return "graduation_ready"
    if lifecycle in {"shadow_promoted", "candidate", "backtested"}:
        return "shadow_evidence_collection"
    if lifecycle in {"challenged", "retired", "decayed"}:
        return f"lifecycle_{lifecycle}"
    return "needs_review"


def _recommended_work_event(blocker: str, *, scan_pattern_id: int | None) -> str:
    if scan_pattern_id is None:
        return PROVENANCE_BACKFILL
    if blocker in {"hard_recert_blocked", "recert_blocked"}:
        return RECERT_RESCUE_REFRESH
    if blocker in {"quality_blocked", "lifecycle_challenged"}:
        return EXIT_VARIANT_REFRESH
    return EDGE_RELIABILITY_REFRESH


def _row_fingerprint(row: dict[str, Any]) -> str:
    payload = {
        "scan_pattern_id": row.get("scan_pattern_id"),
        "snapshot_granularity": row.get("snapshot_granularity"),
        "edge_slice_id": row.get("edge_slice_id"),
        "asset_class": row.get("asset_class"),
        "signal_lane": row.get("signal_lane"),
        "probability_source": row.get("probability_source"),
        "execution_lane": row.get("execution_lane"),
        "lifecycle_stage": row.get("lifecycle_stage"),
        "window_days": row.get("window_days"),
        "edge_eval_count": row.get("edge_eval_count"),
        "closed_evidence_count": row.get("closed_evidence_count"),
        "expected_ev_pct": row.get("expected_ev_pct"),
        "realized_ev_pct": row.get("realized_ev_pct"),
        "latest_observed_at": row.get("latest_observed_at"),
        "blocker": row.get("graduation_blocker"),
    }
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:20]


def _slice_id(
    *,
    scan_pattern_id: int,
    asset_class: str,
    signal_lane: str,
    probability_source: str,
    lifecycle_stage: str,
    execution_lane: str,
    window_days: int,
) -> str:
    parts = {
        "p": int(scan_pattern_id),
        "asset": asset_class,
        "lane": signal_lane,
        "prob": probability_source,
        "life": lifecycle_stage,
        "exec": execution_lane,
        "w": int(window_days),
    }
    blob = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    suffix = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
    return (
        f"p{int(scan_pattern_id)}:{asset_class}:{signal_lane}:"
        f"{probability_source}:{lifecycle_stage}:{execution_lane}:w{int(window_days)}:{suffix}"
    )


def _run_slice_dimensions(
    run: AutoTraderRun,
    *,
    alert: BreakoutAlert | None,
    pattern: ScanPattern | None,
    edge: dict[str, Any],
) -> dict[str, str]:
    lifecycle = str(getattr(pattern, "lifecycle_stage", "") or "unknown").strip().lower()
    return {
        "asset_class": _asset_class_for_evidence(
            ticker=getattr(run, "ticker", None),
            alert=alert,
            pattern=pattern,
        ),
        "signal_lane": _signal_lane_for(alert, run),
        "probability_source": _probability_source_for(edge),
        "lifecycle_stage": lifecycle or "unknown",
        "execution_lane": _execution_lane_for(run),
    }


def _slice_key(dimensions: dict[str, str]) -> tuple[str, str, str, str, str]:
    return (
        dimensions.get("asset_class") or "stock",
        dimensions.get("signal_lane") or "standard",
        dimensions.get("probability_source") or "unknown",
        dimensions.get("lifecycle_stage") or "unknown",
        dimensions.get("execution_lane") or "unknown",
    )


def compute_pattern_edge_reliability(
    db: Session,
    scan_pattern_id: int,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    min_closed_evidence: int = DEFAULT_MIN_CLOSED_EVIDENCE,
) -> dict[str, Any]:
    """Compute one pattern's aggregate expected-vs-realized reliability."""
    pid = int(scan_pattern_id)
    pattern = db.get(ScanPattern, pid)
    cutoff = datetime.utcnow() - timedelta(days=max(1, int(window_days)))

    runs = (
        db.query(AutoTraderRun)
        .filter(AutoTraderRun.scan_pattern_id == pid)
        .filter(AutoTraderRun.created_at >= cutoff)
        .order_by(AutoTraderRun.created_at.asc())
        .all()
    )
    alert_ids = {
        int(run.breakout_alert_id)
        for run in runs
        if getattr(run, "breakout_alert_id", None) is not None
    }
    alerts_by_id: dict[int, BreakoutAlert] = {}
    if alert_ids:
        alerts_by_id = {
            int(row.id): row
            for row in db.query(BreakoutAlert).filter(BreakoutAlert.id.in_(alert_ids)).all()
        }

    expected_values: list[float] = []
    probabilities: list[float] = []
    breakevens: list[float] = []
    reason_counts: Counter[str] = Counter()
    decision_counts: Counter[str] = Counter()
    signal_lanes: Counter[str] = Counter()
    probability_sources: Counter[str] = Counter()
    asset_types: Counter[str] = Counter()
    tickers: Counter[str] = Counter()
    latest_seen: datetime | None = None
    prob_by_alert: dict[int, float] = {}

    for run in runs:
        latest_seen = max(filter(None, [latest_seen, _event_time(run)]), default=None)
        edge = _entry_edge_snapshot(run)
        expected = _safe_float(edge.get("expected_net_pct"))
        if expected is not None:
            expected_values.append(expected)
        prob = _safe_float(edge.get("probability"))
        if prob is not None:
            probabilities.append(prob)
            if getattr(run, "breakout_alert_id", None) is not None:
                prob_by_alert[int(run.breakout_alert_id)] = prob
        be = _safe_float(edge.get("breakeven_probability"))
        if be is not None:
            breakevens.append(be)
        reason = str(getattr(run, "reason", "") or "")
        reason_counts[_reason_bucket(reason)] += 1
        decision = str(getattr(run, "decision", "") or "unknown").strip().lower()
        decision_counts[decision or "unknown"] += 1
        alert = alerts_by_id.get(int(run.breakout_alert_id)) if run.breakout_alert_id else None
        signal_lanes[_signal_lane_for(alert, run)] += 1
        source = str(edge.get("probability_source") or "unknown").strip() or "unknown"
        probability_sources[source] += 1
        ticker = str(
            getattr(run, "ticker", None)
            or getattr(alert, "ticker", None)
            or ""
        ).strip().upper()
        if ticker:
            tickers[ticker] += 1
        asset = str(
            getattr(alert, "asset_type", None)
            or getattr(pattern, "asset_class", None)
            or "unknown"
        ).strip().lower()
        asset_types[asset or "unknown"] += 1

    pattern_alert_ids = [
        int(row.id)
        for row in (
            db.query(BreakoutAlert.id)
            .filter(BreakoutAlert.scan_pattern_id == pid)
            .filter(BreakoutAlert.alerted_at >= cutoff)
            .all()
        )
    ]

    paper_link_filter = (
        PaperTrade.paper_shadow_of_alert_id.in_(pattern_alert_ids)
        if pattern_alert_ids
        else false()
    )
    paper_q = db.query(PaperTrade).filter(
        PaperTrade.status == "closed",
        or_(
            PaperTrade.scan_pattern_id == pid,
            paper_link_filter,
        ),
    )
    paper_rows = [
        row
        for row in paper_q.all()
        if (_event_time(row) is None or _event_time(row) >= cutoff)
    ]

    live_rows = (
        db.query(Trade)
        .filter(Trade.scan_pattern_id == pid)
        .filter(Trade.status == "closed")
        .filter(
            or_(
                Trade.entry_date.is_(None),
                Trade.entry_date >= cutoff,
                Trade.exit_date >= cutoff,
            )
        )
        .all()
    )

    paper_returns = [_paper_return_pct(row) for row in paper_rows]
    paper_returns_f = [v for v in paper_returns if v is not None]
    live_returns = [_live_return_pct(row) for row in live_rows]
    live_returns_f = [v for v in live_returns if v is not None]
    all_returns = paper_returns_f + live_returns_f

    labels: list[int] = []
    brier_terms: list[float] = []
    fallback_p = _mean(probabilities)
    for row in paper_rows:
        label = _outcome_label(getattr(row, "pnl", None))
        if label is None:
            continue
        labels.append(label)
        alert_id = getattr(row, "paper_shadow_of_alert_id", None)
        pred = prob_by_alert.get(int(alert_id)) if alert_id is not None else fallback_p
        if pred is not None:
            brier_terms.append((float(pred) - float(label)) ** 2)
    for row in live_rows:
        label = _outcome_label(getattr(row, "pnl", None))
        if label is None:
            continue
        labels.append(label)
        alert_id = getattr(row, "related_alert_id", None)
        pred = prob_by_alert.get(int(alert_id)) if alert_id is not None else fallback_p
        if pred is not None:
            brier_terms.append((float(pred) - float(label)) ** 2)

    expected_ev = _mean(expected_values)
    realized_ev = _mean(all_returns)
    closed_n = len(all_returns)
    calibrated_ev = _calibrated_ev(expected_ev, realized_ev, closed_n)
    paper_ev = _mean(paper_returns_f)
    live_ev = _mean(live_returns_f)
    paper_live_gap = (
        live_ev - paper_ev
        if live_ev is not None and paper_ev is not None
        else None
    )
    broker_rejects = int(reason_counts.get("broker_execution_reject", 0))
    slippage_misses = int(reason_counts.get("missed_entry_slippage", 0))
    edge_eval_count = len(runs)
    winners = [v for v in all_returns if v > 0.0]
    losers = [abs(v) for v in all_returns if v <= 0.0]
    avg_win = _mean(winners)
    avg_loss = _mean(losers)
    payoff_ratio = (
        avg_win / avg_loss
        if avg_win is not None and avg_loss is not None and avg_loss > 0.0
        else None
    )
    blocker = _graduation_blocker(
        pattern,
        expected_ev_pct=expected_ev,
        calibrated_ev_pct=calibrated_ev,
        realized_ev_pct=realized_ev,
        closed_n=closed_n,
        broker_rejects=broker_rejects,
        edge_eval_count=edge_eval_count,
        min_closed=min_closed_evidence,
    )
    row = {
        "scan_pattern_id": pid,
        "snapshot_granularity": EDGE_RELIABILITY_GRANULARITY_PATTERN,
        "edge_slice_id": None,
        "pattern_name": getattr(pattern, "name", None),
        "asset_class": getattr(pattern, "asset_class", None),
        "pattern_asset_class": getattr(pattern, "asset_class", None),
        "timeframe": getattr(pattern, "timeframe", None),
        "lifecycle_stage": getattr(pattern, "lifecycle_stage", None),
        "promotion_status": getattr(pattern, "promotion_status", None),
        "recert_required": bool(getattr(pattern, "recert_required", False)) if pattern else False,
        "recert_reason": getattr(pattern, "recert_reason", None),
        "window_days": int(window_days),
        "window_start": cutoff.isoformat(),
        "window_end": datetime.utcnow().isoformat(),
        "edge_eval_count": edge_eval_count,
        "positive_expected_edge_count": sum(1 for v in expected_values if v > 0.0),
        "negative_expected_edge_count": int(reason_counts.get("negative_expected_edge", 0)),
        "shadow_block_count": int(reason_counts.get("shadow_observation", 0)),
        "recert_block_count": int(reason_counts.get("recert_required", 0)),
        "slippage_miss_count": slippage_misses,
        "broker_reject_count": broker_rejects,
        "placed_count": int(decision_counts.get("placed", 0)),
        "broker_reject_rate": _round(
            broker_rejects / edge_eval_count if edge_eval_count else 0.0,
            6,
        ),
        "slippage_miss_rate": _round(
            slippage_misses / edge_eval_count if edge_eval_count else 0.0,
            6,
        ),
        "expected_ev_pct": _round(expected_ev, 6),
        "calibrated_ev_pct": _round(calibrated_ev, 6),
        "realized_ev_pct": _round(realized_ev, 6),
        "ev_calibration_error": _round(
            realized_ev - expected_ev
            if realized_ev is not None and expected_ev is not None
            else None,
            6,
        ),
        "brier_score": _round(_mean(brier_terms), 6),
        "closed_evidence_count": closed_n,
        "paper_closed_count": len(paper_returns_f),
        "live_closed_count": len(live_returns_f),
        "paper_realized_ev_pct": _round(paper_ev, 6),
        "live_realized_ev_pct": _round(live_ev, 6),
        "paper_live_gap_pct": _round(paper_live_gap, 6),
        "observed_win_rate": _round(_mean([float(x) for x in labels]), 6),
        "payoff_ratio": _round(payoff_ratio, 6),
        "avg_probability": _round(fallback_p, 6),
        "avg_breakeven_probability": _round(_mean(breakevens), 6),
        "probability_sources": dict(probability_sources),
        "signal_lanes": dict(signal_lanes),
        "asset_types": dict(asset_types),
        "tickers": dict(tickers),
        "primary_symbol": tickers.most_common(1)[0][0] if tickers else None,
        "reason_counts": dict(reason_counts),
        "decision_counts": dict(decision_counts),
        "graduation_blocker": blocker,
        "recommended_work_event": _recommended_work_event(blocker, scan_pattern_id=pid),
        "latest_observed_at": latest_seen.isoformat() if latest_seen else None,
    }
    row["evidence_fingerprint"] = _row_fingerprint(row)
    return row


def compute_pattern_edge_reliability_slices(
    db: Session,
    scan_pattern_id: int,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    min_closed_evidence: int = DEFAULT_MIN_CLOSED_EVIDENCE,
) -> list[dict[str, Any]]:
    """Compute reliability rows by asset/lane/source/execution slice.

    The pattern-level aggregate remains useful for backward compatibility, but
    live cash deployment must not average stock, crypto, options, shadow, and
    broker-failure evidence into one authority number. Slice rows include only
    closed outcomes linked to the same alert ids as the slice, which is
    conservative when provenance is incomplete and prevents cross-asset leakage.
    """
    pid = int(scan_pattern_id)
    pattern = db.get(ScanPattern, pid)
    cutoff = datetime.utcnow() - timedelta(days=max(1, int(window_days)))

    runs = (
        db.query(AutoTraderRun)
        .filter(AutoTraderRun.scan_pattern_id == pid)
        .filter(AutoTraderRun.created_at >= cutoff)
        .order_by(AutoTraderRun.created_at.asc())
        .all()
    )
    if not runs:
        return []

    alert_ids = {
        int(run.breakout_alert_id)
        for run in runs
        if getattr(run, "breakout_alert_id", None) is not None
    }
    alerts_by_id: dict[int, BreakoutAlert] = {}
    if alert_ids:
        alerts_by_id = {
            int(row.id): row
            for row in db.query(BreakoutAlert).filter(BreakoutAlert.id.in_(alert_ids)).all()
        }

    grouped: dict[tuple[str, str, str, str, str], list[RunSliceRecord]] = {}
    for run in runs:
        alert = alerts_by_id.get(int(run.breakout_alert_id)) if run.breakout_alert_id else None
        edge = _entry_edge_snapshot(run)
        dimensions = _run_slice_dimensions(run, alert=alert, pattern=pattern, edge=edge)
        grouped.setdefault(_slice_key(dimensions), []).append((run, alert, edge, dimensions))

    pattern_alert_ids = [
        int(row.id)
        for row in (
            db.query(BreakoutAlert.id)
            .filter(BreakoutAlert.scan_pattern_id == pid)
            .filter(BreakoutAlert.alerted_at >= cutoff)
            .all()
        )
    ]
    paper_link_filter = (
        PaperTrade.paper_shadow_of_alert_id.in_(pattern_alert_ids)
        if pattern_alert_ids
        else false()
    )
    paper_rows = [
        row
        for row in (
            db.query(PaperTrade)
            .filter(
                PaperTrade.status == "closed",
                or_(
                    PaperTrade.scan_pattern_id == pid,
                    paper_link_filter,
                ),
            )
            .all()
        )
        if (_event_time(row) is None or _event_time(row) >= cutoff)
    ]
    live_rows = (
        db.query(Trade)
        .filter(Trade.scan_pattern_id == pid)
        .filter(Trade.status == "closed")
        .filter(
            or_(
                Trade.entry_date.is_(None),
                Trade.entry_date >= cutoff,
                Trade.exit_date >= cutoff,
            )
        )
        .all()
    )

    rows: list[dict[str, Any]] = []
    for key, records in grouped.items():
        asset_class, signal_lane, probability_source, lifecycle_stage, execution_lane = key
        slice_alert_ids = {
            int(run.breakout_alert_id)
            for run, _alert, _edge, _dims in records
            if getattr(run, "breakout_alert_id", None) is not None
        }

        expected_values: list[float] = []
        probabilities: list[float] = []
        breakevens: list[float] = []
        reason_counts: Counter[str] = Counter()
        decision_counts: Counter[str] = Counter()
        signal_lanes: Counter[str] = Counter()
        probability_sources: Counter[str] = Counter()
        asset_types: Counter[str] = Counter()
        tickers: Counter[str] = Counter()
        latest_seen: datetime | None = None
        prob_by_alert: dict[int, float] = {}

        for run, alert, edge, dimensions in records:
            latest_seen = max(filter(None, [latest_seen, _event_time(run)]), default=None)
            expected = _safe_float(edge.get("expected_net_pct"))
            if expected is not None:
                expected_values.append(expected)
            prob = _safe_float(edge.get("probability"))
            if prob is not None:
                probabilities.append(prob)
                if getattr(run, "breakout_alert_id", None) is not None:
                    prob_by_alert[int(run.breakout_alert_id)] = prob
            be = _safe_float(edge.get("breakeven_probability"))
            if be is not None:
                breakevens.append(be)
            reason_counts[_reason_bucket(str(getattr(run, "reason", "") or ""))] += 1
            decision = str(getattr(run, "decision", "") or "unknown").strip().lower()
            decision_counts[decision or "unknown"] += 1
            signal_lanes[dimensions["signal_lane"]] += 1
            probability_sources[dimensions["probability_source"]] += 1
            asset_types[dimensions["asset_class"]] += 1
            ticker = str(
                getattr(run, "ticker", None)
                or getattr(alert, "ticker", None)
                or ""
            ).strip().upper()
            if ticker:
                tickers[ticker] += 1

        slice_paper_rows = [
            row
            for row in paper_rows
            if getattr(row, "paper_shadow_of_alert_id", None) is not None
            and int(getattr(row, "paper_shadow_of_alert_id")) in slice_alert_ids
        ]
        slice_live_rows = [
            row
            for row in live_rows
            if getattr(row, "related_alert_id", None) is not None
            and int(getattr(row, "related_alert_id")) in slice_alert_ids
        ]

        paper_returns = [_paper_return_pct(row) for row in slice_paper_rows]
        paper_returns_f = [v for v in paper_returns if v is not None]
        live_returns = [_live_return_pct(row) for row in slice_live_rows]
        live_returns_f = [v for v in live_returns if v is not None]
        all_returns = paper_returns_f + live_returns_f

        labels: list[int] = []
        brier_terms: list[float] = []
        fallback_p = _mean(probabilities)
        for row in slice_paper_rows:
            label = _outcome_label(getattr(row, "pnl", None))
            if label is None:
                continue
            labels.append(label)
            alert_id = getattr(row, "paper_shadow_of_alert_id", None)
            pred = prob_by_alert.get(int(alert_id)) if alert_id is not None else fallback_p
            if pred is not None:
                brier_terms.append((float(pred) - float(label)) ** 2)
        for row in slice_live_rows:
            label = _outcome_label(getattr(row, "pnl", None))
            if label is None:
                continue
            labels.append(label)
            alert_id = getattr(row, "related_alert_id", None)
            pred = prob_by_alert.get(int(alert_id)) if alert_id is not None else fallback_p
            if pred is not None:
                brier_terms.append((float(pred) - float(label)) ** 2)

        expected_ev = _mean(expected_values)
        realized_ev = _mean(all_returns)
        closed_n = len(all_returns)
        calibrated_ev = _calibrated_ev(expected_ev, realized_ev, closed_n)
        paper_ev = _mean(paper_returns_f)
        live_ev = _mean(live_returns_f)
        paper_live_gap = (
            live_ev - paper_ev
            if live_ev is not None and paper_ev is not None
            else None
        )
        broker_rejects = int(reason_counts.get("broker_execution_reject", 0))
        slippage_misses = int(reason_counts.get("missed_entry_slippage", 0))
        edge_eval_count = len(records)
        winners = [v for v in all_returns if v > 0.0]
        losers = [abs(v) for v in all_returns if v <= 0.0]
        avg_win = _mean(winners)
        avg_loss = _mean(losers)
        payoff_ratio = (
            avg_win / avg_loss
            if avg_win is not None and avg_loss is not None and avg_loss > 0.0
            else None
        )
        blocker = _graduation_blocker(
            pattern,
            expected_ev_pct=expected_ev,
            calibrated_ev_pct=calibrated_ev,
            realized_ev_pct=realized_ev,
            closed_n=closed_n,
            broker_rejects=broker_rejects,
            edge_eval_count=edge_eval_count,
            min_closed=min_closed_evidence,
        )
        slice_id = _slice_id(
            scan_pattern_id=pid,
            asset_class=asset_class,
            signal_lane=signal_lane,
            probability_source=probability_source,
            lifecycle_stage=lifecycle_stage,
            execution_lane=execution_lane,
            window_days=window_days,
        )
        row = {
            "scan_pattern_id": pid,
            "snapshot_granularity": EDGE_RELIABILITY_GRANULARITY_ASSET_SLICE,
            "edge_slice_id": slice_id,
            "slice_dimensions": {
                "asset_class": asset_class,
                "signal_lane": signal_lane,
                "probability_source": probability_source,
                "lifecycle_stage": lifecycle_stage,
                "execution_lane": execution_lane,
            },
            "pattern_name": getattr(pattern, "name", None),
            "asset_class": asset_class,
            "pattern_asset_class": getattr(pattern, "asset_class", None),
            "signal_lane": signal_lane,
            "probability_source": probability_source,
            "execution_lane": execution_lane,
            "timeframe": getattr(pattern, "timeframe", None),
            "lifecycle_stage": getattr(pattern, "lifecycle_stage", None),
            "promotion_status": getattr(pattern, "promotion_status", None),
            "recert_required": bool(getattr(pattern, "recert_required", False)) if pattern else False,
            "recert_reason": getattr(pattern, "recert_reason", None),
            "window_days": int(window_days),
            "window_start": cutoff.isoformat(),
            "window_end": datetime.utcnow().isoformat(),
            "edge_eval_count": edge_eval_count,
            "positive_expected_edge_count": sum(1 for v in expected_values if v > 0.0),
            "negative_expected_edge_count": int(reason_counts.get("negative_expected_edge", 0)),
            "shadow_block_count": int(reason_counts.get("shadow_observation", 0)),
            "recert_block_count": int(reason_counts.get("recert_required", 0)),
            "slippage_miss_count": slippage_misses,
            "broker_reject_count": broker_rejects,
            "placed_count": int(decision_counts.get("placed", 0)),
            "broker_reject_rate": _round(
                broker_rejects / edge_eval_count if edge_eval_count else 0.0,
                6,
            ),
            "slippage_miss_rate": _round(
                slippage_misses / edge_eval_count if edge_eval_count else 0.0,
                6,
            ),
            "expected_ev_pct": _round(expected_ev, 6),
            "calibrated_ev_pct": _round(calibrated_ev, 6),
            "realized_ev_pct": _round(realized_ev, 6),
            "ev_calibration_error": _round(
                realized_ev - expected_ev
                if realized_ev is not None and expected_ev is not None
                else None,
                6,
            ),
            "brier_score": _round(_mean(brier_terms), 6),
            "closed_evidence_count": closed_n,
            "paper_closed_count": len(paper_returns_f),
            "live_closed_count": len(live_returns_f),
            "paper_realized_ev_pct": _round(paper_ev, 6),
            "live_realized_ev_pct": _round(live_ev, 6),
            "paper_live_gap_pct": _round(paper_live_gap, 6),
            "observed_win_rate": _round(_mean([float(x) for x in labels]), 6),
            "payoff_ratio": _round(payoff_ratio, 6),
            "avg_probability": _round(fallback_p, 6),
            "avg_breakeven_probability": _round(_mean(breakevens), 6),
            "probability_sources": dict(probability_sources),
            "signal_lanes": dict(signal_lanes),
            "asset_types": dict(asset_types),
            "tickers": dict(tickers),
            "primary_symbol": tickers.most_common(1)[0][0] if tickers else None,
            "reason_counts": dict(reason_counts),
            "decision_counts": dict(decision_counts),
            "graduation_blocker": blocker,
            "recommended_work_event": _recommended_work_event(blocker, scan_pattern_id=pid),
            "latest_observed_at": latest_seen.isoformat() if latest_seen else None,
        }
        row["evidence_fingerprint"] = _row_fingerprint(row)
        rows.append(row)

    rows.sort(
        key=lambda row: (
            row.get("asset_class") or "",
            row.get("signal_lane") or "",
            row.get("probability_source") or "",
            row.get("execution_lane") or "",
        )
    )
    return rows


def persist_edge_reliability_snapshot(
    db: Session,
    scan_pattern_id: int,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    source: str = "edge_reliability_refresh",
    parent_event_id: int | None = None,
) -> dict[str, Any]:
    row = compute_pattern_edge_reliability(
        db,
        int(scan_pattern_id),
        window_days=window_days,
    )
    fingerprint = str(row.get("evidence_fingerprint") or "none")
    dedupe = (
        f"{EDGE_RELIABILITY_SNAPSHOT}:p{int(scan_pattern_id)}:"
        f"w{int(window_days)}:{fingerprint}"
    )
    event_id = enqueue_outcome_event(
        db,
        event_type=EDGE_RELIABILITY_SNAPSHOT,
        dedupe_key=dedupe,
        payload={**row, "source": source},
        parent_event_id=parent_event_id,
        claimable=False,
    )
    row["snapshot_event_id"] = event_id
    slice_event_ids: list[int] = []
    for slice_row in compute_pattern_edge_reliability_slices(
        db,
        int(scan_pattern_id),
        window_days=window_days,
    ):
        slice_fp = str(slice_row.get("evidence_fingerprint") or "none")
        slice_id = str(slice_row.get("edge_slice_id") or "none")
        slice_event_id = enqueue_outcome_event(
            db,
            event_type=EDGE_RELIABILITY_SNAPSHOT,
            dedupe_key=(
                f"{EDGE_RELIABILITY_SNAPSHOT}:p{int(scan_pattern_id)}:"
                f"slice:{slice_id}:{slice_fp}"
            ),
            payload={**slice_row, "source": source},
            parent_event_id=parent_event_id,
            claimable=False,
        )
        if slice_event_id is not None:
            slice_event_ids.append(int(slice_event_id))
    row["asset_slice_snapshot_event_ids"] = slice_event_ids
    row["asset_slice_count"] = len(slice_event_ids)
    return row


def emit_edge_reliability_refresh_requested(
    db: Session,
    scan_pattern_id: int,
    *,
    source: str,
    window_days: int = DEFAULT_WINDOW_DAYS,
    evidence_fingerprint: str | None = None,
) -> int | None:
    fp = (evidence_fingerprint or "latest").strip()[:40]
    return enqueue_work_event(
        db,
        event_type=EDGE_RELIABILITY_REFRESH,
        dedupe_key=f"{EDGE_RELIABILITY_REFRESH}:p{int(scan_pattern_id)}:w{int(window_days)}:{fp}",
        payload={
            "scan_pattern_id": int(scan_pattern_id),
            "window_days": int(window_days),
            "source": source,
            "evidence_fingerprint": evidence_fingerprint,
        },
        lease_scope="edge",
    )


def emit_targeted_profitability_work(
    db: Session,
    *,
    event_type: str,
    scan_pattern_id: int | None,
    source: str,
    evidence_fingerprint: str | None = None,
    payload: dict[str, Any] | None = None,
) -> int | None:
    if event_type not in {
        RECERT_RESCUE_REFRESH,
        EXIT_VARIANT_REFRESH,
        PROVENANCE_BACKFILL,
    }:
        raise ValueError(f"unsupported profitability work event_type={event_type}")
    pid_key = f"p{int(scan_pattern_id)}" if scan_pattern_id is not None else "null_lineage"
    fp = (evidence_fingerprint or "latest").strip()[:40]
    body = dict(payload or {})
    if scan_pattern_id is not None:
        body["scan_pattern_id"] = int(scan_pattern_id)
    body.update({"source": source, "evidence_fingerprint": evidence_fingerprint})
    return enqueue_work_event(
        db,
        event_type=event_type,
        dedupe_key=f"{event_type}:{pid_key}:{fp}",
        payload=body,
        lease_scope="edge",
    )


def latest_edge_reliability_snapshots(
    db: Session,
    *,
    scan_pattern_ids: list[int] | set[int] | tuple[int, ...],
) -> dict[int, dict[str, Any]]:
    ids = [int(x) for x in scan_pattern_ids if x is not None]
    if not ids:
        return {}
    rows = (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.event_type == EDGE_RELIABILITY_SNAPSHOT)
        .filter(BrainWorkEvent.event_kind == "outcome")
        .filter(BrainWorkEvent.payload["scan_pattern_id"].astext.in_([str(x) for x in ids]))
        .order_by(BrainWorkEvent.created_at.desc(), BrainWorkEvent.id.desc())
        .all()
    )
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        payload = _json_dict(row.payload)
        if payload.get("snapshot_granularity") == EDGE_RELIABILITY_GRANULARITY_ASSET_SLICE:
            continue
        pid = _safe_int(payload.get("scan_pattern_id"))
        if pid is None or pid in out:
            continue
        payload["snapshot_event_id"] = int(row.id)
        payload["snapshot_created_at"] = row.created_at.isoformat() if row.created_at else None
        out[pid] = payload
    return out


def latest_edge_reliability_slice_snapshots(
    db: Session,
    *,
    scan_pattern_ids: list[int] | set[int] | tuple[int, ...],
    window_days: int | None = None,
) -> list[dict[str, Any]]:
    ids = [int(x) for x in scan_pattern_ids if x is not None]
    if not ids:
        return []
    rows = (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.event_type == EDGE_RELIABILITY_SNAPSHOT)
        .filter(BrainWorkEvent.event_kind == "outcome")
        .filter(BrainWorkEvent.payload["scan_pattern_id"].astext.in_([str(x) for x in ids]))
        .order_by(BrainWorkEvent.created_at.desc(), BrainWorkEvent.id.desc())
        .all()
    )
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        payload = _json_dict(row.payload)
        if payload.get("snapshot_granularity") != EDGE_RELIABILITY_GRANULARITY_ASSET_SLICE:
            continue
        if window_days is not None and _safe_int(payload.get("window_days")) != int(window_days):
            continue
        slice_id = str(payload.get("edge_slice_id") or "")
        if not slice_id or slice_id in seen:
            continue
        pid = _safe_int(payload.get("scan_pattern_id"))
        if pid is None:
            continue
        seen.add(slice_id)
        payload["snapshot_event_id"] = int(row.id)
        payload["snapshot_created_at"] = row.created_at.isoformat() if row.created_at else None
        out.append(payload)
    return out


def edge_supply_rows(
    db: Session,
    *,
    pattern_ids: list[int] | set[int] | tuple[int, ...] | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    limit: int = DEFAULT_TOP_LIMIT,
) -> list[dict[str, Any]]:
    if pattern_ids is None:
        cutoff = datetime.utcnow() - timedelta(days=max(1, int(window_days)))
        rows = (
            db.query(AutoTraderRun.scan_pattern_id)
            .filter(AutoTraderRun.scan_pattern_id.isnot(None))
            .filter(AutoTraderRun.created_at >= cutoff)
            .distinct()
            .limit(max(1, int(limit) * 4))
            .all()
        )
        ids = [int(r[0]) for r in rows if r[0] is not None]
    else:
        ids = [int(x) for x in pattern_ids if x is not None]

    out: list[dict[str, Any]] = []
    candidate_ids = ids[: max(1, int(limit) * 4)]
    snapshot_rows = latest_edge_reliability_slice_snapshots(
        db,
        scan_pattern_ids=candidate_ids,
        window_days=window_days,
    )
    out.extend(snapshot_rows)
    covered_ids = {
        int(row["scan_pattern_id"])
        for row in snapshot_rows
        if row.get("scan_pattern_id") is not None
    }
    for pid in candidate_ids:
        if pid in covered_ids:
            continue
        try:
            slices = compute_pattern_edge_reliability_slices(
                db,
                pid,
                window_days=window_days,
            )
            if slices:
                out.extend(slices)
            else:
                out.append(
                    compute_pattern_edge_reliability(
                        db,
                        pid,
                        window_days=window_days,
                    )
                )
        except Exception:
            continue

    def score(row: dict[str, Any]) -> float:
        if row.get("graduation_blocker") != "graduation_ready":
            return -1e9
        ev = _safe_float(row.get("calibrated_ev_pct")) or -999.0
        rejects = _safe_float(row.get("broker_reject_count")) or 0.0
        return ev - rejects

    out.sort(
        key=lambda row: (
            score(row),
            _safe_float(row.get("calibrated_ev_pct")) or -999.0,
            int(row.get("closed_evidence_count") or 0),
        ),
        reverse=True,
    )
    rank = 0
    for row in out:
        if row.get("graduation_blocker") == "graduation_ready":
            rank += 1
            row["cash_deployment_rank"] = rank
        else:
            row["cash_deployment_rank"] = None
    return out[: max(1, int(limit))]


def edge_supply_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    blockers: Counter[str] = Counter(str(row.get("graduation_blocker") or "unknown") for row in rows)
    recommended: Counter[str] = Counter(str(row.get("recommended_work_event") or "unknown") for row in rows)
    granularities: Counter[str] = Counter(str(row.get("snapshot_granularity") or "unknown") for row in rows)
    assets: Counter[str] = Counter(str(row.get("asset_class") or "unknown") for row in rows)
    return {
        "total": len(rows),
        "asset_slice_rows": int(granularities.get(EDGE_RELIABILITY_GRANULARITY_ASSET_SLICE, 0)),
        "graduation_ready": int(blockers.get("graduation_ready", 0)),
        "quality_blocked": int(blockers.get("quality_blocked", 0)),
        "recert_blocked": int(blockers.get("recert_blocked", 0) + blockers.get("hard_recert_blocked", 0)),
        "execution_blocked": int(blockers.get("execution_blocked", 0)),
        "needs_more_closed_evidence": int(blockers.get("needs_more_closed_evidence", 0)),
        "shadow_evidence_collection": int(blockers.get("shadow_evidence_collection", 0)),
        "blockers": dict(blockers),
        "asset_classes": dict(assets),
        "granularities": dict(granularities),
        "recommended_work_events": dict(recommended),
    }


def null_lineage_short_paper_candidates(
    db: Session,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    min_total_pnl: float = 100.0,
    limit: int = 25,
) -> list[dict[str, Any]]:
    cutoff = datetime.utcnow() - timedelta(days=max(1, int(window_days)))
    rows = (
        db.query(PaperTrade)
        .filter(PaperTrade.scan_pattern_id.is_(None))
        .filter(PaperTrade.status == "closed")
        .filter(PaperTrade.entry_date >= cutoff)
        .order_by(PaperTrade.exit_date.desc().nullslast(), PaperTrade.id.desc())
        .limit(1000)
        .all()
    )
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        signal = _json_dict(getattr(row, "signal_json", None))
        direction = str(getattr(row, "direction", None) or signal.get("direction") or "").lower()
        if direction != "short":
            continue
        ticker = str(getattr(row, "ticker", "") or "").upper()
        family = str(signal.get("strategy") or signal.get("source") or "null_lineage_short").strip()
        key = (ticker, family)
        bucket = buckets.setdefault(
            key,
            {
                "ticker": ticker,
                "family": family,
                "closed_count": 0,
                "total_pnl": 0.0,
                "avg_pnl_pct": None,
                "paper_trade_ids": [],
            },
        )
        pnl = _safe_float(getattr(row, "pnl", None)) or 0.0
        pct = _paper_return_pct(row)
        bucket["closed_count"] += 1
        bucket["total_pnl"] += pnl
        bucket["paper_trade_ids"].append(int(row.id))
        if pct is not None:
            vals = list(bucket.get("_pct_values", []))
            vals.append(pct)
            bucket["_pct_values"] = vals
    out = []
    for bucket in buckets.values():
        if float(bucket["total_pnl"]) < float(min_total_pnl):
            continue
        pct_values = list(bucket.pop("_pct_values", []))
        bucket["avg_pnl_pct"] = _round(_mean(pct_values), 6)
        bucket["total_pnl"] = round(float(bucket["total_pnl"]), 6)
        bucket["recommended_work_event"] = PROVENANCE_BACKFILL
        fp_blob = json.dumps(bucket, sort_keys=True, default=str)
        bucket["evidence_fingerprint"] = hashlib.sha256(fp_blob.encode("utf-8")).hexdigest()[:20]
        out.append(bucket)
    out.sort(key=lambda x: (float(x.get("total_pnl") or 0.0), int(x.get("closed_count") or 0)), reverse=True)
    return out[: max(1, int(limit))]

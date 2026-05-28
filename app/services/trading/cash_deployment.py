"""All-asset cash deployment diagnostics.

This layer ranks *only* live-eligible edge-supply rows. It is intentionally
read-only: it never promotes patterns, never clears recert debt, and never
routes an order. Blocked positive-EV rows become safer work recommendations
instead of live shortcuts.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Any

from sqlalchemy.orm import Session

from ...config import settings
from .edge_reliability import (
    DEFAULT_TOP_LIMIT,
    DEFAULT_WINDOW_DAYS,
    EDGE_RELIABILITY_REFRESH,
    EXIT_VARIANT_REFRESH,
    PROVENANCE_BACKFILL,
    RECERT_RESCUE_REFRESH,
    edge_supply_rows,
    emit_edge_reliability_refresh_requested,
    emit_targeted_profitability_work,
    null_lineage_short_paper_candidates,
)
from .portfolio_risk import get_risk_limits

LIVE_LIFECYCLES = frozenset({"live", "promoted", "pilot_promoted"})
RECERT_BLOCKERS = frozenset({"recert_blocked", "hard_recert_blocked"})
EXECUTION_BLOCKERS = frozenset({"execution_blocked"})
SHADOW_BLOCKERS = frozenset({"shadow_evidence_collection"})


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _round(value: float | None, digits: int = 6) -> float | None:
    return round(float(value), digits) if value is not None and math.isfinite(float(value)) else None


def _canonical_asset_class(value: Any) -> str | None:
    raw = str(value or "").strip().lower()
    if raw in {"stock", "stocks", "equity", "equities"}:
        return "stock"
    if raw in {"crypto", "cryptocurrency", "coin", "coinbase_spot"}:
        return "crypto"
    if raw in {"option", "options"}:
        return "options"
    if raw in {"all", "unknown", ""}:
        return None
    return raw


def _dominant_counter_key(value: Any) -> str | None:
    if not isinstance(value, dict) or not value:
        return None
    pairs: list[tuple[str, int]] = []
    for key, count in value.items():
        try:
            pairs.append((str(key), int(count)))
        except (TypeError, ValueError):
            continue
    if not pairs:
        return None
    pairs.sort(key=lambda item: item[1], reverse=True)
    return pairs[0][0]


def _asset_class_for_row(row: dict[str, Any]) -> str:
    direct = _canonical_asset_class(row.get("asset_class"))
    if direct:
        return direct
    from_counts = _canonical_asset_class(_dominant_counter_key(row.get("asset_types")))
    if from_counts:
        return from_counts
    symbol = str(row.get("primary_symbol") or "").strip().upper()
    if symbol.endswith("-USD"):
        return "crypto"
    return "stock"


def _symbol_for_row(row: dict[str, Any]) -> str | None:
    symbol = str(row.get("primary_symbol") or "").strip().upper()
    if symbol:
        return symbol
    tickers = row.get("tickers")
    if isinstance(tickers, dict):
        dom = _dominant_counter_key(tickers)
        if dom:
            return dom.strip().upper()
    return None


def _cost_pct(asset_class: str, row: dict[str, Any]) -> float:
    if asset_class == "crypto":
        base = _safe_float(getattr(settings, "chili_cash_deployment_crypto_cost_pct", 0.25), 0.25) or 0.0
    elif asset_class == "options":
        base = _safe_float(getattr(settings, "chili_cash_deployment_options_cost_pct", 1.0), 1.0) or 0.0
    elif asset_class == "stock":
        base = _safe_float(getattr(settings, "chili_cash_deployment_equity_cost_pct", 0.05), 0.05) or 0.0
    else:
        base = _safe_float(getattr(settings, "chili_cash_deployment_unknown_asset_cost_pct", 0.35), 0.35) or 0.0

    slip_rate = _safe_float(row.get("slippage_miss_rate"), 0.0) or 0.0
    reject_rate = _safe_float(row.get("broker_reject_rate"), 0.0) or 0.0
    slip_penalty = _safe_float(
        getattr(settings, "chili_cash_deployment_slippage_miss_penalty_pct", 0.5),
        0.5,
    ) or 0.0
    reject_penalty = _safe_float(
        getattr(settings, "chili_cash_deployment_broker_reject_penalty_pct", 1.0),
        1.0,
    ) or 0.0
    return max(0.0, base + (slip_rate * slip_penalty) + (reject_rate * reject_penalty))


def _venue_readiness(asset_class: str) -> tuple[str, float, str | None]:
    live_enabled = bool(getattr(settings, "chili_autotrader_live_enabled", True))
    if not live_enabled:
        return "live_disabled", 0.0, "autotrader_live_disabled"

    if asset_class == "crypto":
        if bool(getattr(settings, "chili_autotrader_crypto_enabled", False)):
            return "crypto_live_enabled", 1.0, None
        return "crypto_live_disabled", 0.0, "crypto_live_disabled"
    if asset_class == "options":
        options_enabled = bool(getattr(settings, "chili_autotrader_options_enabled", False))
        venue_enabled = bool(getattr(settings, "chili_options_venue_robinhood_enabled", options_enabled))
        if options_enabled and venue_enabled:
            return "options_live_enabled", 1.0, None
        return "options_live_disabled", 0.0, "options_live_disabled"
    return "equity_live_enabled", 1.0, None


def _correlation_bucket(symbol: str | None, asset_class: str) -> str:
    sym = (symbol or "").strip().upper()
    if asset_class == "crypto":
        base = sym.split("-")[0] if sym else "unknown"
        return f"crypto:{base}"
    if asset_class == "options":
        underlying = sym.split()[0] if sym else "unknown"
        return f"options:{underlying}"
    return f"{asset_class}:{sym[:1] or 'x'}"


def _trade_asset_class(trade: Any) -> str:
    explicit = _canonical_asset_class(getattr(trade, "asset_kind", None))
    if explicit:
        return explicit
    symbol = str(getattr(trade, "ticker", "") or "").strip().upper()
    if symbol.endswith("-USD"):
        return "crypto"
    snap = getattr(trade, "indicator_snapshot", None)
    if isinstance(snap, dict):
        explicit = _canonical_asset_class(snap.get("asset_type"))
        if explicit:
            return explicit
    return "stock"


def _trade_heat_pct(trade: Any, *, capital: float) -> float:
    if capital <= 0.0:
        return 0.0
    entry = _safe_float(getattr(trade, "entry_price", None), 0.0) or 0.0
    qty = _safe_float(getattr(trade, "quantity", None), 0.0) or 0.0
    stop = _safe_float(getattr(trade, "stop_loss", None))
    if entry <= 0.0 or qty <= 0.0 or stop is None:
        return 0.0
    multiplier = 100.0 if _trade_asset_class(trade) == "options" else 1.0
    risk = abs(entry - stop) * qty * multiplier
    return max(0.0, risk / capital * 100.0)


def _exposure_and_notional(
    db: Session,
    *,
    user_id: int | None,
    asset_class: str,
) -> tuple[str | None, float, dict[str, Any]]:
    capital = _safe_float(getattr(settings, "chili_autotrader_assumed_capital_usd", 25_000.0), 25_000.0) or 0.0
    limits = get_risk_limits()
    from ...models.trading import Trade

    open_trades = (
        db.query(Trade)
        .filter(Trade.user_id == user_id, Trade.status == "open")
        .all()
    )
    stale_broker_rows: list[dict[str, Any]] = []
    try:
        from .broker_position_truth import filter_broker_stale_open_trades

        open_trades, stale_broker_rows = filter_broker_stale_open_trades(db, open_trades)
    except Exception:
        stale_broker_rows = []

    counts: Counter[str] = Counter(_trade_asset_class(row) for row in open_trades)
    total_heat = sum(_trade_heat_pct(row, capital=capital) for row in open_trades)
    available_heat = max(0.0, limits.max_portfolio_heat_pct - total_heat)

    blocker: str | None = None
    if len(open_trades) >= limits.max_open_positions:
        blocker = "max_open_positions"
    elif total_heat >= limits.max_portfolio_heat_pct:
        blocker = "portfolio_heat_cap"
    elif asset_class == "crypto" and counts.get("crypto", 0) >= limits.max_crypto_positions:
        blocker = "crypto_position_cap"
    elif asset_class == "stock" and counts.get("stock", 0) >= limits.max_stock_positions:
        blocker = "stock_position_cap"
    elif available_heat <= 0.0:
        blocker = "portfolio_heat_cap"

    explicit_notional = _safe_float(getattr(settings, "chili_autotrader_per_trade_notional_usd", 0.0), 0.0) or 0.0
    base_notional = explicit_notional
    if base_notional <= 0.0 and capital > 0.0:
        base_notional = capital * max(0.0, limits.max_risk_per_trade_pct) / 100.0

    if asset_class == "crypto":
        cap = _safe_float(getattr(settings, "chili_coinbase_max_notional_usd", 0.0), 0.0) or 0.0
        if cap > 0.0:
            base_notional = min(base_notional, cap) if base_notional > 0.0 else cap
    elif asset_class == "options":
        cap = _safe_float(getattr(settings, "chili_autotrader_options_max_contract_notional_usd", 0.0), 0.0) or 0.0
        if cap > 0.0:
            base_notional = min(base_notional, cap) if base_notional > 0.0 else cap

    heat_fraction = 1.0
    if limits.max_risk_per_trade_pct > 0:
        heat_fraction = _clamp(available_heat / limits.max_risk_per_trade_pct)
    max_safe = 0.0 if blocker else max(0.0, base_notional * heat_fraction)
    exposure = {
        "open_positions": len(open_trades),
        "stock_positions": int(counts.get("stock", 0)),
        "crypto_positions": int(counts.get("crypto", 0)),
        "option_positions": int(counts.get("options", 0)),
        "total_heat_pct": round(total_heat, 6),
        "available_heat_pct": round(available_heat, 6),
        "max_open_positions": limits.max_open_positions,
        "max_stock_positions": limits.max_stock_positions,
        "max_crypto_positions": limits.max_crypto_positions,
        "max_risk_per_trade_pct": limits.max_risk_per_trade_pct,
        "stale_broker_open_positions": len(stale_broker_rows),
        "stale_broker_positions": stale_broker_rows,
        "stale_broker_symbols": sorted(
            {
                str(row.get("ticker") or "").strip().upper()
                for row in stale_broker_rows
                if row.get("ticker")
            }
        ),
    }
    return blocker, round(max_safe, 6), exposure


def _cash_category(
    row: dict[str, Any],
    *,
    calibrated_ev_after_cost: float | None,
    exposure_blocker: str | None,
    execution_blocker: str | None,
) -> str:
    blocker = str(row.get("graduation_blocker") or "").strip().lower()
    lifecycle = str(row.get("lifecycle_stage") or "").strip().lower()
    expected = _safe_float(row.get("expected_ev_pct"))
    calibrated = _safe_float(row.get("calibrated_ev_pct"))
    positive_supply = (
        (calibrated is not None and calibrated > 0.0)
        or (expected is not None and expected > 0.0)
        or _safe_int(row.get("positive_expected_edge_count")) > 0
    )

    if bool(row.get("stale_broker_position")):
        return "stale_broker_local_open"
    if calibrated_ev_after_cost is None or calibrated_ev_after_cost <= 0.0:
        return "negative_ev"
    if blocker in RECERT_BLOCKERS or bool(row.get("recert_required")):
        return "positive_ev_recert" if positive_supply else "negative_ev"
    if blocker in EXECUTION_BLOCKERS or _safe_int(row.get("broker_reject_count")) > 0:
        return "positive_ev_execution_blocked" if positive_supply else "negative_ev"
    if blocker in SHADOW_BLOCKERS or lifecycle not in LIVE_LIFECYCLES:
        return "positive_ev_shadow" if positive_supply else "negative_ev"

    min_closed = int(getattr(settings, "chili_cash_deployment_min_closed_evidence", 5) or 0)
    closed_n = _safe_int(row.get("closed_evidence_count"))
    brier = _safe_float(row.get("brier_score"))
    max_brier = _safe_float(getattr(settings, "chili_cash_deployment_max_brier_score", 0.28), 0.28) or 0.28
    gap = _safe_float(row.get("paper_live_gap_pct"))
    max_gap = _safe_float(
        getattr(settings, "chili_cash_deployment_max_abs_paper_live_gap_pct", 3.0),
        3.0,
    ) or 0.0

    if blocker == "needs_more_closed_evidence" or closed_n < min_closed:
        return "needs_calibration"
    if brier is None or brier > max_brier:
        return "needs_calibration"
    if gap is not None and max_gap > 0.0 and abs(gap) > max_gap:
        return "needs_calibration"
    if exposure_blocker or execution_blocker:
        return "positive_ev_execution_blocked"
    if blocker == "graduation_ready":
        return "live_deployable"
    if blocker == "quality_blocked":
        return "negative_ev"
    return "needs_calibration"


def _recommended_work_event(row: dict[str, Any], category: str) -> str:
    if category == "needs_provenance":
        return PROVENANCE_BACKFILL
    if category == "stale_broker_local_open":
        return EDGE_RELIABILITY_REFRESH
    if category == "positive_ev_recert":
        return RECERT_RESCUE_REFRESH
    if category == "positive_ev_shadow":
        closed_n = _safe_int(row.get("closed_evidence_count"))
        min_closed = int(getattr(settings, "chili_cash_deployment_min_closed_evidence", 5) or 0)
        return EXIT_VARIANT_REFRESH if closed_n >= min_closed else EDGE_RELIABILITY_REFRESH
    if category == "negative_ev":
        return EXIT_VARIANT_REFRESH
    if category == "needs_calibration":
        return EDGE_RELIABILITY_REFRESH
    return str(row.get("recommended_work_event") or EDGE_RELIABILITY_REFRESH)


def _allocation_score(
    row: dict[str, Any],
    *,
    calibrated_ev_after_cost: float | None,
    venue_score: float,
    exposure_blocker: str | None,
) -> float:
    ev_component = _clamp((calibrated_ev_after_cost or 0.0) / 5.0)
    closed_n = _safe_int(row.get("closed_evidence_count"))
    min_closed = max(1, int(getattr(settings, "chili_cash_deployment_min_closed_evidence", 5) or 5))
    evidence_component = _clamp(closed_n / (min_closed * 4.0))
    brier = _safe_float(row.get("brier_score"))
    max_brier = _safe_float(getattr(settings, "chili_cash_deployment_max_brier_score", 0.28), 0.28) or 0.28
    calibration_component = 0.35 if brier is None else _clamp(1.0 - (brier / max_brier))
    realized_component = _clamp((_safe_float(row.get("realized_ev_pct"), 0.0) or 0.0) / 5.0)
    gap = _safe_float(row.get("paper_live_gap_pct"))
    max_gap = _safe_float(
        getattr(settings, "chili_cash_deployment_max_abs_paper_live_gap_pct", 3.0),
        3.0,
    ) or 3.0
    gap_component = 1.0 if gap is None else _clamp(1.0 - (abs(gap) / max_gap))
    exposure_component = 0.0 if exposure_blocker else 1.0
    score = (
        ev_component * 0.32
        + evidence_component * 0.18
        + calibration_component * 0.18
        + realized_component * 0.14
        + gap_component * 0.08
        + venue_score * 0.06
        + exposure_component * 0.04
    )
    return round(_clamp(score) * 100.0, 4)


def annotate_cash_deployment_row(
    db: Session,
    row: dict[str, Any],
    *,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Add cash-deployment diagnostics to one edge-supply row."""
    out = dict(row)
    asset_class = _asset_class_for_row(out)
    symbol = _symbol_for_row(out)
    cost = _cost_pct(asset_class, out)
    calibrated = _safe_float(out.get("calibrated_ev_pct"))
    after_cost = calibrated - cost if calibrated is not None else None
    venue, venue_score, venue_blocker = _venue_readiness(asset_class)
    exposure_blocker, max_notional, exposure = _exposure_and_notional(
        db,
        user_id=user_id,
        asset_class=asset_class,
    )
    execution_blocker = venue_blocker
    stale_symbols = set(exposure.get("stale_broker_symbols") or [])
    stale_match = bool(symbol and symbol in stale_symbols)
    if stale_match:
        stale_detail = next(
            (
                item
                for item in exposure.get("stale_broker_positions", [])
                if str(item.get("ticker") or "").strip().upper() == symbol
            ),
            None,
        )
        out.update(
            {
                "broker_truth_status": "stale",
                "broker_truth_reason": (
                    stale_detail.get("reason") if stale_detail else "stale_broker_position"
                ),
                "stale_broker_position": True,
                "stale_reconciled_at": (
                    stale_detail.get("stale_reconciled_at") if stale_detail else None
                ),
            }
        )
    else:
        out.setdefault("broker_truth_status", "live_or_not_applicable")
        out.setdefault("broker_truth_reason", None)
        out.setdefault("stale_broker_position", False)
        out.setdefault("stale_reconciled_at", None)
    category = _cash_category(
        out,
        calibrated_ev_after_cost=after_cost,
        exposure_blocker=exposure_blocker,
        execution_blocker=execution_blocker,
    )
    work_event = _recommended_work_event(out, category)
    evidence_value = max(0.0, after_cost or 0.0) * math.log1p(
        max(0, _safe_int(out.get("edge_eval_count")) + _safe_int(out.get("closed_evidence_count")))
    )
    if category == "positive_ev_recert":
        evidence_value *= 1.25
    elif category == "positive_ev_shadow":
        evidence_value *= 1.1
    elif category == "positive_ev_execution_blocked":
        evidence_value *= 0.8

    out.update(
        {
            "asset_class": asset_class,
            "primary_symbol": symbol,
            "calibrated_ev_after_cost_pct": _round(after_cost, 6),
            "estimated_execution_cost_pct": _round(cost, 6),
            "allocation_score": _allocation_score(
                out,
                calibrated_ev_after_cost=after_cost,
                venue_score=venue_score,
                exposure_blocker=exposure_blocker,
            ),
            "cash_deployment_category": category,
            "live_deployable": category == "live_deployable",
            "max_safe_notional": max_notional if category == "live_deployable" else 0.0,
            "venue_readiness": venue,
            "venue_readiness_score": venue_score,
            "correlation_bucket": _correlation_bucket(symbol, asset_class),
            "exposure_blocker": exposure_blocker,
            "execution_blocker": execution_blocker,
            "recert_blocker": out.get("recert_reason") if category == "positive_ev_recert" else None,
            "recommended_work_event": work_event,
            "expected_evidence_value": round(evidence_value, 6),
            "portfolio_exposure": exposure,
        }
    )
    return out


def cash_deployment_rows(
    db: Session,
    *,
    user_id: int | None = None,
    pattern_ids: list[int] | set[int] | tuple[int, ...] | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    limit: int = DEFAULT_TOP_LIMIT,
) -> list[dict[str, Any]]:
    base = edge_supply_rows(
        db,
        pattern_ids=pattern_ids,
        window_days=window_days,
        limit=max(1, int(limit) * 4),
    )
    rows = [
        annotate_cash_deployment_row(db, row, user_id=user_id)
        for row in base
    ]
    rows.sort(
        key=lambda row: (
            1 if row.get("live_deployable") else 0,
            _safe_float(row.get("allocation_score"), 0.0) or 0.0,
            _safe_float(row.get("calibrated_ev_after_cost_pct"), -999.0) or -999.0,
            _safe_int(row.get("closed_evidence_count")),
            _safe_float(row.get("expected_evidence_value"), 0.0) or 0.0,
        ),
        reverse=True,
    )
    rank = 0
    for row in rows:
        if row.get("live_deployable"):
            rank += 1
            row["cash_deployment_rank"] = rank
        else:
            row["cash_deployment_rank"] = None
    return rows[: max(1, int(limit))]


def cash_deployment_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    categories: Counter[str] = Counter(str(row.get("cash_deployment_category") or "unknown") for row in rows)
    assets: Counter[str] = Counter(str(row.get("asset_class") or "unknown") for row in rows)
    work: Counter[str] = Counter(str(row.get("recommended_work_event") or "unknown") for row in rows)
    deployable = [row for row in rows if row.get("live_deployable")]
    return {
        "total": len(rows),
        "live_deployable": int(categories.get("live_deployable", 0)),
        "positive_ev_shadow": int(categories.get("positive_ev_shadow", 0)),
        "positive_ev_recert": int(categories.get("positive_ev_recert", 0)),
        "positive_ev_execution_blocked": int(categories.get("positive_ev_execution_blocked", 0)),
        "negative_ev": int(categories.get("negative_ev", 0)),
        "stale_broker_local_open": int(categories.get("stale_broker_local_open", 0)),
        "needs_provenance": int(categories.get("needs_provenance", 0)),
        "needs_calibration": int(categories.get("needs_calibration", 0)),
        "deployable_cash_notional": round(
            sum(_safe_float(row.get("max_safe_notional"), 0.0) or 0.0 for row in deployable),
            6,
        ),
        "categories": dict(categories),
        "asset_classes": dict(assets),
        "recommended_work_events": dict(work),
    }


def cash_deployment_null_lineage_candidates(
    db: Session,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    limit: int = 10,
) -> list[dict[str, Any]]:
    rows = null_lineage_short_paper_candidates(
        db,
        window_days=window_days,
        limit=limit,
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item.update(
            {
                "cash_deployment_category": "needs_provenance",
                "live_deployable": False,
                "cash_deployment_rank": None,
                "max_safe_notional": 0.0,
                "recommended_work_event": PROVENANCE_BACKFILL,
                "needs_provenance": True,
            }
        )
        out.append(item)
    return out


def enqueue_cash_deployment_work(
    db: Session,
    *,
    user_id: int | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    limit: int = DEFAULT_TOP_LIMIT,
    include_null_lineage: bool = True,
) -> dict[str, Any]:
    """Turn cash-deployment diagnostics into deduped brain work.

    This is intentionally conservative: it queues reliability refreshes,
    recert rescue, learned-variant refresh, and provenance work. It never
    promotes a pattern or routes an order.
    """
    rows = cash_deployment_rows(
        db,
        user_id=user_id,
        window_days=window_days,
        limit=limit,
    )
    created: list[int] = []
    considered = 0
    skipped = 0
    by_event: Counter[str] = Counter()
    for row in rows:
        event_type = str(row.get("recommended_work_event") or "").strip()
        pid = row.get("scan_pattern_id")
        if not event_type or pid is None:
            skipped += 1
            continue
        considered += 1
        payload = {
            "cash_deployment_category": row.get("cash_deployment_category"),
            "asset_class": row.get("asset_class"),
            "slice_asset_class": row.get("slice_asset_class"),
            "edge_slice_id": row.get("edge_slice_id"),
            "calibrated_ev_after_cost_pct": row.get("calibrated_ev_after_cost_pct"),
            "allocation_score": row.get("allocation_score"),
            "expected_evidence_value": row.get("expected_evidence_value"),
            "graduation_blocker": row.get("graduation_blocker"),
        }
        if event_type == EDGE_RELIABILITY_REFRESH:
            event_id = emit_edge_reliability_refresh_requested(
                db,
                int(pid),
                source="cash_deployment",
                asset_class=row.get("asset_class"),
                window_days=window_days,
                evidence_fingerprint=str(row.get("evidence_fingerprint") or ""),
            )
        else:
            event_id = emit_targeted_profitability_work(
                db,
                event_type=event_type,
                scan_pattern_id=int(pid),
                source="cash_deployment",
                asset_class=row.get("asset_class"),
                evidence_fingerprint=str(row.get("evidence_fingerprint") or ""),
                payload=payload,
            )
        if event_id is None:
            skipped += 1
            continue
        created.append(int(event_id))
        by_event[event_type] += 1

    null_created: list[int] = []
    if include_null_lineage:
        for row in cash_deployment_null_lineage_candidates(
            db,
            window_days=window_days,
            limit=min(10, max(1, int(limit))),
        ):
            considered += 1
            event_id = emit_targeted_profitability_work(
                db,
                event_type=PROVENANCE_BACKFILL,
                scan_pattern_id=None,
                source="cash_deployment",
                evidence_fingerprint=str(row.get("evidence_fingerprint") or ""),
                payload={
                    "cash_deployment_category": "needs_provenance",
                    "ticker": row.get("ticker"),
                    "family": row.get("family"),
                    "closed_count": row.get("closed_count"),
                    "total_pnl": row.get("total_pnl"),
                    "paper_trade_ids": row.get("paper_trade_ids"),
                    "expected_evidence_value": max(0.0, _safe_float(row.get("total_pnl"), 0.0) or 0.0),
                },
            )
            if event_id is None:
                skipped += 1
                continue
            null_created.append(int(event_id))
            by_event[PROVENANCE_BACKFILL] += 1

    return {
        "ok": True,
        "window_days": int(window_days),
        "considered": considered,
        "created": len(created) + len(null_created),
        "skipped": skipped,
        "event_ids": created + null_created,
        "event_types": dict(by_event),
    }

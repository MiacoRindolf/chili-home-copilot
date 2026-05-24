"""Signal-health diagnostics for the fast-path scalp system.

This module turns decay-table rows into an operator-readable verdict:
negative edge, below cost, positive candidate, uncertain, or no usable
statistics. It deliberately uses the same maker/taker decay-table
selection and finite-sample confidence math as the execution gates so
the diagnosis and the hot path cannot drift apart.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .calibration import (
    NEGATIVE_EDGE_CONFIDENCE,
    SUPPORTED_DECAY_TABLES,
    _bounded_confidence,
    _negative_edge_row_evidence,
    decay_table_for_execution_mode,
)
from .decay_miner import score_bucket
from .settings import FastPathSettings, load as load_settings
from .universe_status import UNIVERSE_STATUS_ACTIVE, UNIVERSE_STATUS_SHADOW


SIGNAL_HEALTH_DEFAULT_LIMIT = 100
MAKER_ATTEMPT_HEALTH_WINDOW_HOURS = 24
MAKER_ATTEMPT_UNKNOWN_ALERT_TYPE = "unknown"
MAKER_ATTEMPT_UNKNOWN_SCORE_BUCKET = "unknown"
MAKER_ATTEMPT_FILLED_OUTCOMES = frozenset({"filled", "partial"})
MAKER_ATTEMPT_UNFILLED_TERMINAL_OUTCOMES = frozenset({
    "cancelled",
    "replaced",
})
MAKER_ATTEMPT_REJECTED_OUTCOME = "rejected"
SIGNAL_HEALTH_LEARNABLE_VERDICTS = frozenset({
    "positive_edge_candidate",
    "uncertain",
    "insufficient_statistical_evidence",
})
SIGNAL_HEALTH_SPARSE_VERDICTS = frozenset({
    "insufficient_statistical_evidence",
})
SIGNAL_HEALTH_ACTIONABLE_LEARNABLE_VERDICTS = (
    SIGNAL_HEALTH_LEARNABLE_VERDICTS - SIGNAL_HEALTH_SPARSE_VERDICTS
)
SIGNAL_HEALTH_EXHAUSTED_VERDICTS = frozenset({
    "negative_edge",
    "below_cost",
})

_VERDICT_RANK = {
    "negative_edge": 0,
    "below_cost": 1,
    "uncertain": 2,
    "insufficient_statistical_evidence": 3,
    "positive_edge_candidate": 4,
}

_ACTION_BY_VERDICT = {
    "negative_edge": "suppress",
    "below_cost": "keep_shadow_or_drop",
    "uncertain": "observe_only",
    "insufficient_statistical_evidence": "collect_more_data",
    "positive_edge_candidate": "maker_shadow_candidate",
}

EDGE_DIAGNOSIS_POSITIVE_PRESENT = "positive_edge_candidate_present"
EDGE_DIAGNOSIS_NO_EVIDENCE = "no_decay_evidence"
EDGE_DIAGNOSIS_COLLECT_MORE_DATA = "collect_more_data"
EDGE_DIAGNOSIS_FEE_SPREAD_BOTTLENECK = "fee_spread_bottleneck"
EDGE_DIAGNOSIS_OBSERVE_UNCERTAIN = "observe_uncertain"
EDGE_DIAGNOSIS_NEGATIVE_DOMINANT = "negative_edge_dominant"

EDGE_PAIN_NO_POSITIVE = "no_positive_edge_candidate"
EDGE_PAIN_UPPER_BELOW_REQUIRED_NET = "optimistic_confidence_still_below_cost"
EDGE_PAIN_MEAN_BELOW_REQUIRED_NET = "best_mean_still_below_cost"
EDGE_PAIN_ONLY_SPARSE_EVIDENCE = "only_sparse_decay_evidence"
EDGE_PAIN_NEGATIVE_EDGE_PRESENT = "negative_edge_present"


def _round_or_none(value: Any, digits: int = 4) -> float | None:
    if value is None:
        return None
    return round(float(value), int(digits))


def _mean_or_none(values: Iterable[float | None]) -> float | None:
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return None
    return sum(clean) / float(len(clean))


def _rate_or_none(count: int, total: int) -> float | None:
    if total <= 0:
        return None
    return float(count) / float(total)


def _validate_decay_table(table: str) -> str:
    if table not in SUPPORTED_DECAY_TABLES:
        raise ValueError(f"unsupported decay table: {table!r}")
    return table


def _fee_bps_for_settings(settings: FastPathSettings) -> float:
    exec_mode = str(settings.execution_mode or "taker").strip().lower()
    from .fees import fee_bps_for_execution_mode

    fee_bps, _fee_detail = fee_bps_for_execution_mode(settings, exec_mode)
    return fee_bps


def _cost_bps(*, fee_bps: float, spread_bps: float | None) -> float:
    return 2.0 * (float(fee_bps or 0.0) + float(spread_bps or 0.0))


def _bucket_group_key(row: dict[str, Any], *, include_ticker: bool) -> tuple[Any, ...]:
    if include_ticker:
        return (
            row.get("ticker"),
            row.get("alert_type"),
            row.get("score_bucket"),
        )
    return (
        row.get("alert_type"),
        row.get("score_bucket"),
    )


def _group_rows(
    rows: Iterable[dict[str, Any]], *, include_ticker: bool,
) -> list[list[dict[str, Any]]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_bucket_group_key(row, include_ticker=include_ticker)].append(row)
    return list(grouped.values())


def _attempt_alert_type(row: dict[str, Any]) -> str:
    alert_type = str(row.get("alert_type") or "").strip()
    return alert_type or MAKER_ATTEMPT_UNKNOWN_ALERT_TYPE


def _attempt_score_bucket(row: dict[str, Any]) -> str:
    raw = row.get("score_bucket")
    if raw:
        return str(raw)
    if not row.get("alert_type"):
        return MAKER_ATTEMPT_UNKNOWN_SCORE_BUCKET
    try:
        return score_bucket(float(row.get("signal_score") or 0.0))
    except (TypeError, ValueError):
        return MAKER_ATTEMPT_UNKNOWN_SCORE_BUCKET


def _attempt_group_key(
    row: dict[str, Any], *, include_ticker: bool,
) -> tuple[Any, ...]:
    if include_ticker:
        return (
            row.get("ticker"),
            _attempt_alert_type(row),
            _attempt_score_bucket(row),
        )
    return (
        _attempt_alert_type(row),
        _attempt_score_bucket(row),
    )


def _group_attempt_rows(
    rows: Iterable[dict[str, Any]], *, include_ticker: bool,
) -> list[list[dict[str, Any]]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_attempt_group_key(row, include_ticker=include_ticker)].append(row)
    return list(grouped.values())


def _side_adjusted_mid_drift_bps(row: dict[str, Any]) -> float | None:
    drift = row.get("mid_drift_bps")
    if drift is None:
        return None
    signed = float(drift)
    side = str(row.get("side") or "").strip().lower()
    if side == "sell":
        return -signed
    return signed


def summarize_maker_attempt_group(
    rows: list[dict[str, Any]],
    *,
    scope: str,
) -> dict[str, Any]:
    """Summarize maker execution quality for one attempt lane.

    Positive side-adjusted drift means the mid moved in the entry's
    favor after placement. Negative side-adjusted drift means the maker
    fill or timeout happened after adverse movement.
    """
    if not rows:
        return {
            "scope": scope,
            "attempts": 0,
            "fill_rate": None,
            "pain_points": ["no_attempt_data"],
        }

    first = rows[0]
    outcomes = [str(row.get("fill_outcome") or "").strip().lower()
                for row in rows]
    filled_rows = [
        row for row, outcome in zip(rows, outcomes)
        if outcome in MAKER_ATTEMPT_FILLED_OUTCOMES
    ]
    unfilled_terminal_rows = [
        row for row, outcome in zip(rows, outcomes)
        if outcome in MAKER_ATTEMPT_UNFILLED_TERMINAL_OUTCOMES
    ]
    rejected_rows = [
        row for row, outcome in zip(rows, outcomes)
        if outcome == MAKER_ATTEMPT_REJECTED_OUTCOME
    ]
    unresolved_rows = [row for row, outcome in zip(rows, outcomes) if not outcome]

    attempts = len(rows)
    fills = len(filled_rows)
    unfilled_terminal = len(unfilled_terminal_rows)
    side_drifts = [_side_adjusted_mid_drift_bps(row) for row in rows]
    filled_side_drifts = [
        _side_adjusted_mid_drift_bps(row) for row in filled_rows
    ]
    unfilled_side_drifts = [
        _side_adjusted_mid_drift_bps(row) for row in unfilled_terminal_rows
    ]
    filled_adverse = sum(
        1 for drift in filled_side_drifts
        if drift is not None and drift < 0.0
    )
    unfilled_favorable = sum(
        1 for drift in unfilled_side_drifts
        if drift is not None and drift > 0.0
    )

    filled_avg_side_drift = _mean_or_none(filled_side_drifts)
    unfilled_avg_side_drift = _mean_or_none(unfilled_side_drifts)
    pain_points: list[str] = []
    if fills <= 0:
        pain_points.append("no_maker_fills")
    if filled_avg_side_drift is not None and filled_avg_side_drift < 0.0:
        pain_points.append("filled_after_adverse_move")
    if unfilled_avg_side_drift is not None and unfilled_avg_side_drift > 0.0:
        pain_points.append("unfilled_when_move_was_favorable")
    if rejected_rows:
        pain_points.append("maker_rejections_present")
    if unresolved_rows:
        pain_points.append("open_attempts")

    return {
        "scope": scope,
        "ticker": first.get("ticker") if scope == "ticker" else None,
        "alert_type": None if scope == "all" else _attempt_alert_type(first),
        "score_bucket": (
            None if scope == "all" else _attempt_score_bucket(first)
        ),
        "attempts": int(attempts),
        "fills": int(fills),
        "cancels": int(outcomes.count("cancelled")),
        "replaced": int(outcomes.count("replaced")),
        "rejected": int(len(rejected_rows)),
        "unresolved": int(len(unresolved_rows)),
        "fill_rate": _round_or_none(_rate_or_none(fills, attempts), 6),
        "unfilled_terminal_rate": _round_or_none(
            _rate_or_none(unfilled_terminal, attempts), 6,
        ),
        "rejected_rate": _round_or_none(
            _rate_or_none(len(rejected_rows), attempts), 6,
        ),
        "avg_time_to_fill_ms": _round_or_none(
            _mean_or_none(row.get("time_to_fill_ms") for row in filled_rows),
            2,
        ),
        "avg_spread_at_placement_bps": _round_or_none(
            _mean_or_none(row.get("spread_at_placement_bps") for row in rows),
        ),
        "avg_spread_at_resolution_bps": _round_or_none(
            _mean_or_none(row.get("spread_at_fill_bps") for row in rows),
        ),
        "avg_mid_drift_bps": _round_or_none(
            _mean_or_none(row.get("mid_drift_bps") for row in rows),
        ),
        "avg_side_mid_drift_bps": _round_or_none(_mean_or_none(side_drifts)),
        "filled_avg_side_mid_drift_bps": _round_or_none(
            filled_avg_side_drift,
        ),
        "unfilled_avg_side_mid_drift_bps": _round_or_none(
            unfilled_avg_side_drift,
        ),
        "filled_adverse_rate": _round_or_none(
            _rate_or_none(filled_adverse, fills), 6,
        ),
        "unfilled_favorable_rate": _round_or_none(
            _rate_or_none(unfilled_favorable, unfilled_terminal), 6,
        ),
        "pain_points": pain_points,
    }


def _decorate_evidence(
    evidence: dict[str, Any], *, cost_bps: float, min_net_bps: float,
) -> dict[str, Any]:
    mean_bps = float(evidence["mean_return"]) * 10000.0
    lower_bps = float(evidence["lower_ci"]) * 10000.0
    upper_bps = float(evidence["upper_ci"]) * 10000.0
    return {
        **evidence,
        "mean_bps": mean_bps,
        "lower_bps": lower_bps,
        "upper_bps": upper_bps,
        "cost_bps": float(cost_bps),
        "min_net_bps": float(min_net_bps),
        "mean_net_bps": mean_bps - float(cost_bps),
        "lower_net_bps": lower_bps - float(cost_bps),
        "upper_net_bps": upper_bps - float(cost_bps),
    }


def _compact_evidence(evidence: dict[str, Any] | None) -> dict[str, Any] | None:
    if evidence is None:
        return None
    keep = {
        "horizon_s": int(evidence["horizon_s"]),
        "sample_count": int(evidence["sample_count"]),
        "mean_bps": _round_or_none(evidence["mean_bps"]),
        "lower_bps": _round_or_none(evidence["lower_bps"]),
        "upper_bps": _round_or_none(evidence["upper_bps"]),
        "mean_net_bps": _round_or_none(evidence["mean_net_bps"]),
        "lower_net_bps": _round_or_none(evidence["lower_net_bps"]),
        "upper_net_bps": _round_or_none(evidence["upper_net_bps"]),
        "stdev_bps": _round_or_none(float(evidence["stdev"]) * 10000.0),
        "confidence": _round_or_none(evidence["confidence"], 6),
    }
    return keep


def summarize_signal_group(
    rows: list[dict[str, Any]],
    *,
    table: str,
    scope: str,
    fee_bps: float,
    spread_bps: float | None,
    min_net_bps: float = 0.0,
) -> dict[str, Any]:
    """Summarize all horizons for one ticker/lane or pooled lane."""
    table = _validate_decay_table(table)
    if not rows:
        return {
            "scope": scope,
            "decay_table": table,
            "verdict": "insufficient_statistical_evidence",
            "action": _ACTION_BY_VERDICT["insufficient_statistical_evidence"],
        }

    first = rows[0]
    score_bucket = str(first.get("score_bucket") or "")
    cost = _cost_bps(fee_bps=fee_bps, spread_bps=spread_bps)
    evidence_rows: list[dict[str, Any]] = []
    total_samples = 0
    horizons: list[int] = []
    for row in rows:
        n = int(row.get("sample_count") or 0)
        total_samples += n
        horizons.append(int(row.get("horizon_s") or 0))
        evidence = _negative_edge_row_evidence(
            row,
            bucket=score_bucket,
            table=table,
            scope=scope,
        )
        if evidence is not None:
            evidence_rows.append(
                _decorate_evidence(
                    evidence,
                    cost_bps=cost,
                    min_net_bps=min_net_bps,
                )
            )

    base: dict[str, Any] = {
        "scope": scope,
        "decay_table": table,
        "ticker": first.get("ticker") if scope == "ticker" else None,
        "alert_type": first.get("alert_type"),
        "score_bucket": score_bucket,
        "status": first.get("status"),
        "rank": int(first["rank"]) if first.get("rank") is not None else None,
        "horizons_s": sorted({h for h in horizons if h > 0}),
        "total_samples": int(total_samples),
        "fee_bps": _round_or_none(fee_bps),
        "spread_bps": _round_or_none(spread_bps),
        "cost_bps": _round_or_none(cost),
        "min_net_bps": _round_or_none(min_net_bps),
    }

    if not evidence_rows:
        verdict = "insufficient_statistical_evidence"
        return {
            **base,
            "verdict": verdict,
            "action": _ACTION_BY_VERDICT[verdict],
            "minimum_requirement": "sample_count>=2 and nonzero_variance",
        }

    worst = min(
        evidence_rows,
        key=lambda e: (float(e["upper_ci"]), -int(e["sample_count"])),
    )
    best_lower_net = max(
        evidence_rows,
        key=lambda e: (float(e["lower_net_bps"]), int(e["sample_count"])),
    )
    best_upper_net = max(
        evidence_rows,
        key=lambda e: (float(e["upper_net_bps"]), int(e["sample_count"])),
    )
    best_mean_net = max(
        evidence_rows,
        key=lambda e: (float(e["mean_net_bps"]), int(e["sample_count"])),
    )

    if float(worst["upper_ci"]) < 0.0:
        verdict = "negative_edge"
        decision_basis = "pre_cost_upper_confidence_below_zero"
    elif float(best_lower_net["lower_net_bps"]) >= float(min_net_bps):
        verdict = "positive_edge_candidate"
        decision_basis = "net_lower_confidence_clears_cost"
    elif float(best_upper_net["upper_net_bps"]) < float(min_net_bps):
        verdict = "below_cost"
        decision_basis = "net_upper_confidence_below_cost"
    else:
        verdict = "uncertain"
        decision_basis = "confidence_interval_overlaps_decision_line"

    return {
        **base,
        "verdict": verdict,
        "action": _ACTION_BY_VERDICT[verdict],
        "decision_basis": decision_basis,
        "worst_negative": _compact_evidence(worst),
        "best_lower_net": _compact_evidence(best_lower_net),
        "best_upper_net": _compact_evidence(best_upper_net),
        "best_mean_net": _compact_evidence(best_mean_net),
    }


def _fetch_median_universe_spread_bps(engine: Engine) -> float | None:
    sql = text("""
        WITH latest AS (
            SELECT MAX(rotation_at) AS ts FROM fast_path_universe
        )
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY spread_bps)
        FROM fast_path_universe
        WHERE rotation_at = (SELECT ts FROM latest)
          AND status IN (:active_status, :shadow_status)
          AND rank IS NOT NULL
          AND spread_bps IS NOT NULL
    """)
    with engine.connect() as conn:
        value = conn.execute(sql, {
            "active_status": UNIVERSE_STATUS_ACTIVE,
            "shadow_status": UNIVERSE_STATUS_SHADOW,
        }).scalar()
    return float(value) if value is not None else None


def _fetch_pooled_decay_rows(engine: Engine, *, table: str) -> list[dict[str, Any]]:
    table = _validate_decay_table(table)
    sql = text(f"""
        WITH bucket_rows AS (
            SELECT alert_type, score_bucket, horizon_s,
                   sample_count, mean_return, m2_return
            FROM {table}
            WHERE sample_count > 0
        ),
        pooled AS (
            SELECT
                alert_type,
                score_bucket,
                horizon_s,
                SUM(sample_count)::bigint AS sample_count,
                SUM(mean_return * sample_count)
                    / NULLIF(SUM(sample_count), 0) AS mean_return,
                SUM(m2_return + sample_count * POWER(mean_return, 2)) AS sum_sq
            FROM bucket_rows
            GROUP BY alert_type, score_bucket, horizon_s
        )
        SELECT
            alert_type,
            score_bucket,
            horizon_s,
            sample_count,
            mean_return,
            GREATEST(
                0.0,
                sum_sq - sample_count * POWER(mean_return, 2)
            ) AS m2_return
        FROM pooled
        ORDER BY alert_type, score_bucket, horizon_s
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql).mappings().all()
    return [dict(r) for r in rows]


def _fetch_ticker_decay_rows(engine: Engine, *, table: str) -> list[dict[str, Any]]:
    table = _validate_decay_table(table)
    sql = text(f"""
        WITH latest AS (
            SELECT MAX(rotation_at) AS ts FROM fast_path_universe
        ),
        latest_universe AS (
            SELECT ticker, status, rank, spread_bps
            FROM fast_path_universe
            WHERE rotation_at = (SELECT ts FROM latest)
        ),
        subscribed_universe AS (
            SELECT COUNT(*) AS n
            FROM latest_universe
            WHERE status = :active_status
               OR (status = :shadow_status AND rank IS NOT NULL)
        )
        SELECT d.ticker, d.alert_type, d.score_bucket, d.horizon_s,
               d.sample_count, d.mean_return, d.m2_return,
               u.status, u.rank, u.spread_bps
        FROM {table} d
        LEFT JOIN latest_universe u ON u.ticker = d.ticker
        WHERE d.sample_count > 0
          AND (
              (SELECT n FROM subscribed_universe) = 0
              OR u.status = :active_status
              OR (u.status = :shadow_status AND u.rank IS NOT NULL)
          )
        ORDER BY
            CASE WHEN u.rank IS NULL THEN 1 ELSE 0 END,
            u.rank ASC NULLS LAST,
            d.ticker, d.alert_type, d.score_bucket, d.horizon_s
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {
            "active_status": UNIVERSE_STATUS_ACTIVE,
            "shadow_status": UNIVERSE_STATUS_SHADOW,
        }).mappings().all()
    return [dict(r) for r in rows]


def _fetch_maker_attempt_rows(
    engine: Engine, *, window_hours: int,
) -> list[dict[str, Any]]:
    sql = text("""
        SELECT
            m.ticker,
            m.side,
            m.fill_outcome,
            m.time_to_fill_ms,
            m.spread_at_placement_bps,
            m.spread_at_fill_bps,
            m.mid_drift_bps,
            m.execution_mode,
            a.alert_type,
            a.signal_score
        FROM fast_path_maker_attempts m
        LEFT JOIN LATERAL (
            SELECT alert_type, signal_score
            FROM fast_alerts a
            WHERE a.id = m.alert_id
            ORDER BY fired_at DESC
            LIMIT 1
        ) a ON TRUE
        WHERE m.placed_at >= NOW() - (:hours || ' hours')::interval
        ORDER BY m.placed_at DESC
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {"hours": int(window_hours)}).mappings().all()
    return [dict(r) for r in rows]


def _sort_signal_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            _VERDICT_RANK.get(str(row.get("verdict")), 99),
            -(row.get("best_mean_net") or {}).get("mean_net_bps", -1e12),
            row.get("rank") if row.get("rank") is not None else 999_999,
            str(row.get("ticker") or ""),
            str(row.get("alert_type") or ""),
            str(row.get("score_bucket") or ""),
        ),
    )


def _sort_attempt_health_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            -len(row.get("pain_points") or []),
            row.get("filled_avg_side_mid_drift_bps")
            if row.get("filled_avg_side_mid_drift_bps") is not None
            else 1e12,
            -float(row.get("unfilled_favorable_rate") or 0.0),
            -int(row.get("attempts") or 0),
            str(row.get("ticker") or ""),
            str(row.get("alert_type") or ""),
            str(row.get("score_bucket") or ""),
        ),
    )


def _net_metric(row: dict[str, Any], section: str, field: str) -> float | None:
    value = (row.get(section) or {}).get(field)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _edge_lane_summary(row: dict[str, Any]) -> dict[str, Any]:
    best_mean = row.get("best_mean_net") or {}
    best_upper = row.get("best_upper_net") or {}
    best_lower = row.get("best_lower_net") or {}
    return {
        "scope": row.get("scope"),
        "ticker": row.get("ticker"),
        "rank": row.get("rank"),
        "alert_type": row.get("alert_type"),
        "score_bucket": row.get("score_bucket"),
        "verdict": row.get("verdict"),
        "action": row.get("action"),
        "total_samples": int(row.get("total_samples") or 0),
        "best_mean_net_bps": _round_or_none(best_mean.get("mean_net_bps")),
        "best_upper_net_bps": _round_or_none(best_upper.get("upper_net_bps")),
        "best_lower_net_bps": _round_or_none(best_lower.get("lower_net_bps")),
        "best_mean_horizon_s": best_mean.get("horizon_s"),
        "best_upper_horizon_s": best_upper.get("horizon_s"),
        "best_lower_horizon_s": best_lower.get("horizon_s"),
        "cost_bps": row.get("cost_bps"),
    }


def _best_edge_lane(
    rows: list[dict[str, Any]], *, section: str, field: str,
) -> dict[str, Any] | None:
    scored: list[tuple[float, int, dict[str, Any]]] = []
    for row in rows:
        value = _net_metric(row, section, field)
        if value is None:
            continue
        scored.append((value, int(row.get("total_samples") or 0), row))
    if not scored:
        return None
    _value, _samples, row = max(scored, key=lambda item: (item[0], item[1]))
    return _edge_lane_summary(row)


def _build_edge_diagnosis(
    rows: list[dict[str, Any]], *, min_net_bps: float,
) -> dict[str, Any]:
    """Summarize the exact cost-adjusted edge gap across health rows."""
    verdict_counts: dict[str, int] = {}
    for row in rows:
        verdict = str(row.get("verdict") or "unknown")
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1

    best_mean = _best_edge_lane(
        rows, section="best_mean_net", field="mean_net_bps",
    )
    best_upper = _best_edge_lane(
        rows, section="best_upper_net", field="upper_net_bps",
    )
    best_lower = _best_edge_lane(
        rows, section="best_lower_net", field="lower_net_bps",
    )

    positive_count = verdict_counts.get("positive_edge_candidate", 0)
    uncertain_count = verdict_counts.get("uncertain", 0)
    sparse_count = verdict_counts.get("insufficient_statistical_evidence", 0)
    negative_count = verdict_counts.get("negative_edge", 0)

    best_upper_net = (
        None if best_upper is None else best_upper.get("best_upper_net_bps")
    )
    best_mean_net = (
        None if best_mean is None else best_mean.get("best_mean_net_bps")
    )
    required_net = float(min_net_bps or 0.0)
    upper_gap = (
        None if best_upper_net is None
        else required_net - float(best_upper_net)
    )
    mean_gap = (
        None if best_mean_net is None
        else required_net - float(best_mean_net)
    )

    if positive_count > 0:
        diagnosis = EDGE_DIAGNOSIS_POSITIVE_PRESENT
    elif not rows:
        diagnosis = EDGE_DIAGNOSIS_NO_EVIDENCE
    elif sparse_count == len(rows):
        diagnosis = EDGE_DIAGNOSIS_COLLECT_MORE_DATA
    elif upper_gap is not None and upper_gap > 0.0:
        diagnosis = EDGE_DIAGNOSIS_FEE_SPREAD_BOTTLENECK
    elif uncertain_count > 0:
        diagnosis = EDGE_DIAGNOSIS_OBSERVE_UNCERTAIN
    else:
        diagnosis = EDGE_DIAGNOSIS_NEGATIVE_DOMINANT

    pain_points: list[str] = []
    if positive_count <= 0:
        pain_points.append(EDGE_PAIN_NO_POSITIVE)
    if upper_gap is not None and upper_gap > 0.0:
        pain_points.append(EDGE_PAIN_UPPER_BELOW_REQUIRED_NET)
    if mean_gap is not None and mean_gap > 0.0:
        pain_points.append(EDGE_PAIN_MEAN_BELOW_REQUIRED_NET)
    if rows and sparse_count == len(rows):
        pain_points.append(EDGE_PAIN_ONLY_SPARSE_EVIDENCE)
    if negative_count > 0:
        pain_points.append(EDGE_PAIN_NEGATIVE_EDGE_PRESENT)

    return {
        "diagnosis": diagnosis,
        "required_net_bps": _round_or_none(required_net),
        "best_mean_gap_bps": _round_or_none(mean_gap),
        "best_upper_gap_bps": _round_or_none(upper_gap),
        "best_mean_lane": best_mean,
        "best_upper_lane": best_upper,
        "best_lower_lane": best_lower,
        "pain_points": pain_points,
    }


def _build_maker_attempt_health(
    rows: list[dict[str, Any]],
    *,
    limit: int,
    include_tickers: bool,
    window_hours: int,
) -> dict[str, Any]:
    pooled = [
        summarize_maker_attempt_group(group, scope="pooled")
        for group in _group_attempt_rows(rows, include_ticker=False)
    ]
    pooled = _sort_attempt_health_rows(pooled)

    tickers_all: list[dict[str, Any]] = []
    if include_tickers:
        tickers_all = [
            summarize_maker_attempt_group(group, scope="ticker")
            for group in _group_attempt_rows(rows, include_ticker=True)
        ]
        tickers_all = _sort_attempt_health_rows(tickers_all)

    summary = summarize_maker_attempt_group(rows, scope="all") if rows else {
        "scope": "all",
        "attempts": 0,
        "fills": 0,
        "cancels": 0,
        "replaced": 0,
        "rejected": 0,
        "unresolved": 0,
        "fill_rate": None,
        "pain_points": ["no_attempt_data"],
    }

    return {
        "window_hours": int(window_hours),
        "summary": summary,
        "pooled_lanes": pooled[:limit],
        "ticker_lanes": tickers_all[:limit],
        "pooled_lanes_total": len(pooled),
        "ticker_lanes_total": len(tickers_all),
    }


def build_signal_health_report(
    engine: Engine,
    *,
    settings: FastPathSettings | None = None,
    table: str | None = None,
    limit: int = SIGNAL_HEALTH_DEFAULT_LIMIT,
    include_tickers: bool = True,
    include_maker_attempts: bool = True,
    maker_attempt_window_hours: int = MAKER_ATTEMPT_HEALTH_WINDOW_HOURS,
) -> dict[str, Any]:
    """Build the fast-path signal-health report from decay statistics."""
    fp_settings = settings or load_settings()
    exec_mode = str(fp_settings.execution_mode or "taker").strip().lower()
    decay_table = _validate_decay_table(
        table or decay_table_for_execution_mode(exec_mode)
    )
    from .fees import fee_bps_for_execution_mode

    fee_bps, fee_detail = fee_bps_for_execution_mode(fp_settings, exec_mode)
    min_net_bps = float(fp_settings.live_alpha_min_net_bps or 0.0)
    max_rows = max(1, int(limit or SIGNAL_HEALTH_DEFAULT_LIMIT))

    median_spread_bps = _fetch_median_universe_spread_bps(engine)
    pooled_rows = _fetch_pooled_decay_rows(engine, table=decay_table)
    pooled = [
        summarize_signal_group(
            group,
            table=decay_table,
            scope="pooled",
            fee_bps=fee_bps,
            spread_bps=median_spread_bps,
            min_net_bps=min_net_bps,
        )
        for group in _group_rows(pooled_rows, include_ticker=False)
    ]
    pooled = _sort_signal_rows(pooled)

    ticker_health_all: list[dict[str, Any]] = []
    if include_tickers:
        ticker_rows = _fetch_ticker_decay_rows(engine, table=decay_table)
        for group in _group_rows(ticker_rows, include_ticker=True):
            spread = group[0].get("spread_bps")
            if spread is None:
                spread = median_spread_bps
            ticker_health_all.append(
                summarize_signal_group(
                    group,
                    table=decay_table,
                    scope="ticker",
                    fee_bps=fee_bps,
                    spread_bps=spread,
                    min_net_bps=min_net_bps,
                )
            )
        ticker_health_all = _sort_signal_rows(ticker_health_all)
    ticker_health = ticker_health_all[:max_rows]

    verdict_counts: dict[str, int] = {}
    for row in pooled + ticker_health_all:
        verdict = str(row.get("verdict") or "unknown")
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
    edge_diagnosis = _build_edge_diagnosis(
        pooled + ticker_health_all,
        min_net_bps=min_net_bps,
    )

    maker_attempt_health: dict[str, Any] | None = None
    if include_maker_attempts:
        maker_attempt_rows = _fetch_maker_attempt_rows(
            engine,
            window_hours=max(1, int(maker_attempt_window_hours)),
        )
        maker_attempt_health = _build_maker_attempt_health(
            maker_attempt_rows,
            limit=max_rows,
            include_tickers=include_tickers,
            window_hours=max(1, int(maker_attempt_window_hours)),
        )

    return {
        "ok": True,
        "settings": {
            "execution_mode": exec_mode,
            "decay_table": decay_table,
            "confidence": _bounded_confidence(NEGATIVE_EDGE_CONFIDENCE),
            **fee_detail,
            "median_spread_bps": _round_or_none(median_spread_bps),
            "pooled_cost_bps": _round_or_none(
                _cost_bps(fee_bps=fee_bps, spread_bps=median_spread_bps)
            ),
            "min_net_bps": _round_or_none(min_net_bps),
        },
        "summary": {
            "pooled_lanes": len(pooled),
            "ticker_lanes": len(ticker_health_all),
            "ticker_lanes_returned": len(ticker_health),
            "verdict_counts": verdict_counts,
            "edge_diagnosis": edge_diagnosis,
        },
        "pooled": pooled[:max_rows],
        "tickers": ticker_health,
        "maker_attempts": maker_attempt_health,
    }


__all__ = [
    "MAKER_ATTEMPT_HEALTH_WINDOW_HOURS",
    "SIGNAL_HEALTH_ACTIONABLE_LEARNABLE_VERDICTS",
    "SIGNAL_HEALTH_DEFAULT_LIMIT",
    "SIGNAL_HEALTH_EXHAUSTED_VERDICTS",
    "SIGNAL_HEALTH_LEARNABLE_VERDICTS",
    "SIGNAL_HEALTH_SPARSE_VERDICTS",
    "build_signal_health_report",
    "summarize_maker_attempt_group",
    "summarize_signal_group",
]

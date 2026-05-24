"""Calibrated value lookups against fast_signal_decay (F6.5).

Helpers used by exit_manager / gates / stop_engine to read empirical
forward-return statistics instead of hardcoded magic numbers. Every
helper returns ``None`` when there's insufficient data, so each
caller can fall back to the existing constant -- F6 is NOT a hard
dependency.

Conventions:
  - "Sufficient data" means ``sample_count >= MIN_SAMPLES_FOR_CALIB``
    on the bucket row in question. Below that, the running statistics
    are too noisy to trust.
  - Sharpe-like ranking uses ``mean_return / stdev`` where stdev is
    derived from Welford's M2 column: ``stdev = sqrt(m2 / (n-1))``.
  - All fractions: ``mean_return`` and ``stdev`` are unitless return
    fractions (e.g., 0.001 = 10 bps).
  - Trading cost: ~100 bps round-trip (Coinbase ~40 bps taker × 2 +
    spread). Configurable via env so it can be tightened once we have
    a tighter measurement.

Caching: each helper opens a short-lived SQLAlchemy connection per
call. The brief said reads should be zero-cost in the hot path; the
ix_fsd_lookup composite index makes each query a single seek. If
this becomes a hot spot we can layer an in-process TTL cache, but
exit_manager bootstraps once per position (~hourly) and the gate
runs once per alert (~10/min) so the round-trip cost is irrelevant
today.
"""
from __future__ import annotations

import logging
import math
import os
from statistics import NormalDist
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .decay_miner import HORIZONS_S, score_bucket

logger = logging.getLogger(__name__)

DECAY_TABLE_NO_FRICTION = "fast_signal_decay"
DECAY_TABLE_MAKER_FILLED = "fast_signal_decay_maker_filled"
SUPPORTED_DECAY_TABLES = frozenset({
    DECAY_TABLE_NO_FRICTION,
    DECAY_TABLE_MAKER_FILLED,
})
MAKER_ATTEMPT_FILLED_OUTCOMES = frozenset({"filled", "partial"})
MAKER_ATTEMPT_UNFILLED_TERMINAL_OUTCOMES = frozenset({
    "cancelled",
    "replaced",
})


def decay_table_for_execution_mode(exec_mode: str) -> str:
    """Return the empirical decay table matching an execution mode."""
    mode = (exec_mode or "taker").strip().lower()
    if mode in ("maker_only", "maker_first_then_taker"):
        return DECAY_TABLE_MAKER_FILLED
    return DECAY_TABLE_NO_FRICTION


# ── Tunables ─────────────────────────────────────────────────────────

# Below this sample count a bucket is too thin to trust; caller falls
# back to the cold-start constant. 30 is a defensible "law of large
# numbers" floor that the brief suggested.
MIN_SAMPLES_FOR_CALIB = int(
    os.environ.get("CHILI_FAST_PATH_DECAY_MIN_SAMPLES", "30")
)

# Round-trip trading cost as a fraction. Used by ``is_score_tradeable``.
# Coinbase Advanced Trade taker fee is ~0.4% (4 bps wait, 40 bps);
# round-trip is ~80 bps, plus a 5-10 bps spread eats another 5 bps,
# so 100 bps is a reasonable headline. Override via env if a tighter
# number is justified.
TRADING_COST_FRAC = float(
    os.environ.get("CHILI_FAST_PATH_TRADING_COST_FRAC", "0.01")
)

# A score bucket is "tradeable" if the empirical mean return at its
# best (highest-Sharpe) horizon beats this multiple of trading cost.
# 2× is a defensible margin: any signal that just barely covers cost
# isn't worth the variance.
TRADEABLE_COST_MULT = float(
    os.environ.get("CHILI_FAST_PATH_TRADEABLE_COST_MULT", "2.0")
)

# F6.5: hardware/network reality, not a strategy choice. Coinbase live
# placement round-trip (place + broker confirm + exit + broker confirm)
# is ~200-500ms typical. A calibrated max_hold_s shorter than this is
# empirically saying "this signal isn't tradeable at our latency
# profile" -- the cleaner expression is "we don't try to hold for less
# than the floor; if calibration argues for that, fall through to the
# floor and let the position prove or disprove its edge over a
# survivable horizon." Override via env if hardware reality changes.
CALIB_EXEC_FLOOR_S = int(
    os.environ.get("CHILI_FAST_PATH_CALIB_EXEC_FLOOR_S", "10")
)

# F6.5: dedup tracker for the floor-substitution INFO log. Logs once
# per (ticker, alert_type, score_bucket) per process so we can see
# the floor operating without spamming. Cleared on process restart.
_FLOOR_LOG_SEEN: set[tuple[str, str, str]] = set()

# Negative-edge auto-exclusion confidence. This is a one-sided upper
# confidence bound: if the configured upper bound on mean return is
# below zero, the bucket is blocked. Default 0.975 mirrors the former
# mean + 2*stderr rule while letting finite-sample Student-t critical
# values, not a fixed sample quota, decide when sparse evidence is
# trustworthy.
NEGATIVE_EDGE_CONFIDENCE = float(
    os.environ.get("CHILI_FAST_PATH_NEGEDGE_CONFIDENCE", "0.975")
)


# ── Internal: row -> stats helpers ───────────────────────────────────


def _stdev(m2: float, n: int) -> float:
    """Welford stdev = sqrt(M2 / (n-1)). Returns 0 for n<=1."""
    if n is None or n <= 1:
        return 0.0
    return math.sqrt(max(0.0, float(m2) / float(n - 1)))


def _sharpe_like(mean_return: float, stdev: float) -> float | None:
    """Mean / stdev ratio; None if stdev is zero (degenerate)."""
    if stdev <= 0.0:
        return None
    return float(mean_return) / float(stdev)


def _bounded_confidence(value: float) -> float:
    """Clamp a confidence level into the valid open interval (0.5, 1.0)."""
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = 0.975
    return min(
        math.nextafter(1.0, 0.0),
        max(math.nextafter(0.5, 1.0), confidence),
    )


def _student_t_critical(confidence: float, degrees_of_freedom: int) -> float:
    """One-sided Student-t critical value for an upper confidence bound.

    Uses exact closed forms for df=1 and df=2, then a Cornish-Fisher
    expansion around the normal quantile for df>=3. That keeps the
    hot-path dependency-free while correcting the old fixed z-score
    for finite samples.
    """
    df = int(degrees_of_freedom or 0)
    if df <= 0:
        return math.inf
    p = _bounded_confidence(confidence)
    if df == 1:
        return math.tan(math.pi * (p - 0.5))
    if df == 2:
        q = 2.0 * p - 1.0
        return math.sqrt((2.0 * q * q) / (1.0 - q * q))

    z = NormalDist().inv_cdf(p)
    v = float(df)
    z2 = z * z
    z3 = z2 * z
    z5 = z3 * z2
    z7 = z5 * z2
    z9 = z7 * z2
    return (
        z
        + (z3 + z) / (4.0 * v)
        + (5.0 * z5 + 16.0 * z3 + 3.0 * z) / (96.0 * v * v)
        + (3.0 * z7 + 19.0 * z5 + 17.0 * z3 - 15.0 * z)
          / (384.0 * v * v * v)
        + (79.0 * z9 + 779.0 * z7 + 1482.0 * z5
           - 1920.0 * z3 - 945.0 * z)
          / (92160.0 * v * v * v * v)
    )


def _negative_edge_row_evidence(
    row: dict[str, Any],
    *,
    bucket: str,
    table: str,
    scope: str,
) -> dict[str, Any] | None:
    """Confidence-bound evidence for one decay row, if statistically usable."""
    n = int(row["sample_count"] or 0)
    if n <= 1:
        return None
    mean = float(row["mean_return"] or 0.0)
    m2 = float(row["m2_return"] or 0.0)
    stdev = _stdev(m2, n)
    if stdev <= 0.0:
        return None
    stderr = stdev / math.sqrt(float(n))
    critical = _student_t_critical(NEGATIVE_EDGE_CONFIDENCE, n - 1)
    upper_ci = mean + critical * stderr
    lower_ci = mean - critical * stderr
    return {
        "score_bucket": bucket,
        "scope": scope,
        "decay_table": table,
        "horizon_s": int(row["horizon_s"]),
        "sample_count": n,
        "mean_return": mean,
        "stdev": stdev,
        "stderr": stderr,
        "confidence": _bounded_confidence(NEGATIVE_EDGE_CONFIDENCE),
        "critical_value": critical,
        "lower_ci": lower_ci,
        "upper_ci": upper_ci,
    }


def _welford_from_values(values: list[float]) -> dict[str, float | int]:
    n = 0
    mean = 0.0
    m2 = 0.0
    for value in values:
        n += 1
        delta = float(value) - mean
        mean += delta / float(n)
        m2 += delta * (float(value) - mean)
    return {"sample_count": n, "mean": mean, "m2": m2}


def _side_adjusted_mid_drift_bps(row: dict[str, Any]) -> float | None:
    drift = row.get("mid_drift_bps")
    if drift is None:
        return None
    signed = float(drift)
    side = str(row.get("side") or "").strip().lower()
    if side == "sell":
        return -signed
    return signed


def _mid_drift_evidence(values: list[float], *, bucket: str) -> dict[str, Any] | None:
    stats = _welford_from_values(values)
    n = int(stats["sample_count"])
    evidence = _negative_edge_row_evidence(
        {
            "horizon_s": 0,
            "sample_count": n,
            "mean_return": float(stats["mean"]),
            "m2_return": float(stats["m2"]),
        },
        bucket=bucket,
        table="fast_path_maker_attempts",
        scope="maker_attempt",
    )
    if evidence is None:
        return None
    return {
        "sample_count": int(evidence["sample_count"]),
        "mean_side_mid_drift_bps": round(float(evidence["mean_return"]), 4),
        "lower_side_mid_drift_bps": round(float(evidence["lower_ci"]), 4),
        "upper_side_mid_drift_bps": round(float(evidence["upper_ci"]), 4),
        "stdev_side_mid_drift_bps": round(float(evidence["stdev"]), 4),
        "confidence": round(float(evidence["confidence"]), 6),
    }


def _execute_mapped_rows(
    source: Any,
    sql,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    connect = getattr(source, "connect", None)
    if callable(connect):
        with connect() as conn:
            rows = conn.execute(sql, params).mappings().all()
        return [dict(r) for r in rows]

    execute = getattr(source, "execute", None)
    if callable(execute):
        rows = source.execute(sql, params).mappings().all()
        return [dict(r) for r in rows]

    raise TypeError(f"unsupported SQL source for maker attempt rows: {type(source)!r}")


def _fetch_maker_attempt_drift_rows(
    engine: Engine,
    *,
    ticker: str,
    alert_type: str,
    window_hours: int,
) -> list[dict[str, Any]]:
    sql = text("""
        SELECT
            m.side,
            m.fill_outcome,
            m.mid_drift_bps,
            a.signal_score
        FROM fast_path_maker_attempts m
        JOIN LATERAL (
            SELECT alert_type, signal_score
            FROM fast_alerts a
            WHERE a.id = m.alert_id
            ORDER BY fired_at DESC
            LIMIT 1
        ) a ON TRUE
        WHERE m.ticker = :ticker
          AND a.alert_type = :alert_type
          AND m.mid_drift_bps IS NOT NULL
          AND m.placed_at >= NOW() - (:hours || ' hours')::interval
        ORDER BY m.placed_at DESC
    """)
    return _execute_mapped_rows(engine, sql, {
        "ticker": ticker,
        "alert_type": alert_type,
        "hours": int(window_hours),
    })


def _fetch_pooled_maker_attempt_drift_rows(
    engine: Engine,
    *,
    alert_type: str,
    window_hours: int,
) -> list[dict[str, Any]]:
    sql = text("""
        SELECT
            m.ticker,
            m.side,
            m.fill_outcome,
            m.mid_drift_bps,
            a.signal_score
        FROM fast_path_maker_attempts m
        JOIN LATERAL (
            SELECT alert_type, signal_score
            FROM fast_alerts a
            WHERE a.id = m.alert_id
            ORDER BY fired_at DESC
            LIMIT 1
        ) a ON TRUE
        WHERE a.alert_type = :alert_type
          AND m.mid_drift_bps IS NOT NULL
          AND m.placed_at >= NOW() - (:hours || ' hours')::interval
        ORDER BY m.placed_at DESC
    """)
    return _execute_mapped_rows(engine, sql, {
        "alert_type": alert_type,
        "hours": int(window_hours),
    })


def _maker_attempt_adverse_selection_from_rows(
    rows: list[dict[str, Any]],
    *,
    bucket: str,
    scope: str,
    window_hours: int,
    ticker: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    matching_rows = [
        row for row in rows
        if score_bucket(float(row.get("signal_score") or 0.0)) == bucket
    ]
    if not matching_rows:
        return False, {
            "verdict": "no_data",
            "scope": scope,
            "ticker": ticker if scope == "ticker" else None,
            "score_bucket": bucket,
            "window_hours": max(1, int(window_hours)),
        }

    filled_values: list[float] = []
    unfilled_terminal_values: list[float] = []
    for row in matching_rows:
        outcome = str(row.get("fill_outcome") or "").strip().lower()
        drift = _side_adjusted_mid_drift_bps(row)
        if drift is None:
            continue
        if outcome in MAKER_ATTEMPT_FILLED_OUTCOMES:
            filled_values.append(drift)
        elif outcome in MAKER_ATTEMPT_UNFILLED_TERMINAL_OUTCOMES:
            unfilled_terminal_values.append(drift)

    filled_evidence = _mid_drift_evidence(filled_values, bucket=bucket)
    unfilled_evidence = _mid_drift_evidence(
        unfilled_terminal_values,
        bucket=bucket,
    )
    filled_adverse = (
        filled_evidence is not None
        and float(filled_evidence["upper_side_mid_drift_bps"]) < 0.0
    )
    unfilled_missed_favorable = (
        unfilled_evidence is not None
        and float(unfilled_evidence["lower_side_mid_drift_bps"]) > 0.0
    )
    blocked_reasons: list[str] = []
    if filled_adverse:
        blocked_reasons.append("maker_fills_adversely")
    if unfilled_missed_favorable:
        blocked_reasons.append("maker_misses_favorable_moves")

    has_statistical_evidence = (
        filled_evidence is not None or unfilled_evidence is not None
    )
    if blocked_reasons:
        verdict = "adverse_selection"
    elif has_statistical_evidence:
        verdict = "not_excluded"
    else:
        verdict = "insufficient_statistical_evidence"

    return bool(blocked_reasons), {
        "verdict": verdict,
        "scope": scope,
        "ticker": ticker if scope == "ticker" else None,
        "score_bucket": bucket,
        "window_hours": max(1, int(window_hours)),
        "attempts": len(matching_rows),
        "filled_samples": len(filled_values),
        "unfilled_terminal_samples": len(unfilled_terminal_values),
        "filled_evidence": filled_evidence,
        "unfilled_terminal_evidence": unfilled_evidence,
        "blocked_reasons": blocked_reasons,
        "minimum_requirement": "sample_count>=2 and nonzero_variance",
    }


def maker_attempt_adverse_selection_excluded_from_rows(
    rows: list[dict[str, Any]],
    *,
    score_bucket_name: str,
    scope: str,
    window_hours: int,
    ticker: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Summarize maker-attempt toxicity for an already selected score bucket."""
    return _maker_attempt_adverse_selection_from_rows(
        rows,
        bucket=str(score_bucket_name or "").strip().lower(),
        scope=scope,
        ticker=ticker,
        window_hours=window_hours,
    )


def maker_attempt_adverse_selection_excluded_for_bucket(
    engine: Engine,
    *,
    ticker: str,
    alert_type: str,
    score_bucket_name: str,
    window_hours: int = 24,
    allow_pooled: bool = True,
) -> tuple[bool, dict[str, Any]]:
    """Return maker-attempt toxicity using the canonical score bucket directly.

    This is the bucket-native companion to
    :func:`maker_attempt_adverse_selection_excluded`; callers that already
    grouped rows by the decay miner's score bucket do not have to invent a
    representative signal score just to reuse the adverse-selection logic.
    """
    bucket = str(score_bucket_name or "").strip().lower()
    window_h = max(1, int(window_hours))
    rows = _fetch_maker_attempt_drift_rows(
        engine,
        ticker=ticker,
        alert_type=alert_type,
        window_hours=window_h,
    )
    ticker_excluded, ticker_evidence = _maker_attempt_adverse_selection_from_rows(
        rows,
        bucket=bucket,
        scope="ticker",
        ticker=ticker,
        window_hours=window_h,
    )
    if ticker_excluded or not allow_pooled:
        return ticker_excluded, ticker_evidence
    if str(ticker_evidence.get("verdict") or "") == "not_excluded":
        return False, ticker_evidence

    pooled_rows = _fetch_pooled_maker_attempt_drift_rows(
        engine,
        alert_type=alert_type,
        window_hours=window_h,
    )
    pooled_excluded, pooled_evidence = _maker_attempt_adverse_selection_from_rows(
        pooled_rows,
        bucket=bucket,
        scope="pooled",
        window_hours=window_h,
    )
    if pooled_excluded:
        pooled_evidence["ticker_scope_verdict"] = ticker_evidence.get("verdict")
        pooled_evidence["ticker_scope_attempts"] = ticker_evidence.get("attempts", 0)
        return True, pooled_evidence
    return False, ticker_evidence


def maker_attempt_adverse_selection_excluded(
    engine: Engine,
    *,
    ticker: str,
    alert_type: str,
    signal_score: float,
    window_hours: int = 24,
    allow_pooled: bool = True,
) -> tuple[bool, dict[str, Any]]:
    """Return whether recent maker attempts prove this lane is toxic.

    Uses finite-sample confidence bounds on side-adjusted mid drift:

      * filled attempts block when the upper confidence bound is below
        zero, meaning passive fills are selected into adverse movement.
      * cancelled/replaced attempts block when the lower confidence
        bound is above zero, meaning the passive order tends to miss
        favorable movement.

    There is no fixed attempt-count quota. Sparse or zero-variance rows
    simply produce no statistical verdict, matching the negative-edge
    decay gate's finite-sample behavior. When the exact ticker lane is
    sparse, pooled evidence for the same alert type and score bucket can
    still block a broadly toxic passive-execution pattern.
    """
    return maker_attempt_adverse_selection_excluded_for_bucket(
        engine,
        ticker=ticker,
        alert_type=alert_type,
        score_bucket_name=score_bucket(signal_score),
        window_hours=window_hours,
        allow_pooled=allow_pooled,
    )


def _most_negative_confidence_evidence(
    rows: list[dict[str, Any]],
    *,
    bucket: str,
    table: str,
    scope: str,
) -> dict[str, Any] | None:
    """Return the row with the lowest upper confidence bound."""
    best: dict[str, Any] | None = None
    for row in rows:
        evidence = _negative_edge_row_evidence(
            row,
            bucket=bucket,
            table=table,
            scope=scope,
        )
        if evidence is None:
            continue
        if best is None:
            best = evidence
            continue
        if evidence["upper_ci"] < best["upper_ci"] or (
            evidence["upper_ci"] == best["upper_ci"]
            and evidence["sample_count"] > best["sample_count"]
        ):
            best = evidence
    return best


def _cost_barrier_summary(
    rows: list[dict[str, Any]],
    *,
    bucket: str,
    table: str,
    scope: str,
    cost_bps: float,
    min_net_bps: float,
) -> dict[str, Any] | None:
    """Return confidence-bound cost evidence for one decay bucket."""
    evidence_rows: list[dict[str, Any]] = []
    for row in rows:
        evidence = _negative_edge_row_evidence(
            row,
            bucket=bucket,
            table=table,
            scope=scope,
        )
        if evidence is None:
            continue
        mean_bps = float(evidence["mean_return"]) * 10000.0
        lower_bps = float(evidence["lower_ci"]) * 10000.0
        upper_bps = float(evidence["upper_ci"]) * 10000.0
        evidence_rows.append({
            **evidence,
            "mean_bps": mean_bps,
            "lower_bps": lower_bps,
            "upper_bps": upper_bps,
            "cost_bps": float(cost_bps),
            "min_net_bps": float(min_net_bps),
            "mean_net_bps": mean_bps - float(cost_bps),
            "lower_net_bps": lower_bps - float(cost_bps),
            "upper_net_bps": upper_bps - float(cost_bps),
        })
    if not evidence_rows:
        return None

    worst_negative = min(
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

    if float(worst_negative["upper_ci"]) < 0.0:
        verdict = "negative_edge"
        decision_basis = "pre_cost_upper_confidence_below_zero"
        decision = worst_negative
    elif float(best_lower_net["lower_net_bps"]) >= float(min_net_bps):
        verdict = "positive_edge_candidate"
        decision_basis = "net_lower_confidence_clears_cost"
        decision = best_lower_net
    elif float(best_upper_net["upper_net_bps"]) < float(min_net_bps):
        verdict = "below_cost"
        decision_basis = "net_upper_confidence_below_cost"
        decision = best_upper_net
    else:
        verdict = "uncertain"
        decision_basis = "confidence_interval_overlaps_decision_line"
        decision = best_upper_net

    return {
        "score_bucket": bucket,
        "scope": scope,
        "decay_table": table,
        "verdict": verdict,
        "decision_basis": decision_basis,
        "cost_bps": round(float(cost_bps), 4),
        "min_net_bps": round(float(min_net_bps), 4),
        "best_horizon_s": int(best_mean_net["horizon_s"]),
        "sample_count": int(best_mean_net["sample_count"]),
        "mean_return_bps": round(float(best_mean_net["mean_bps"]), 4),
        "mean_net_bps": round(float(best_mean_net["mean_net_bps"]), 4),
        "decision_horizon_s": int(decision["horizon_s"]),
        "decision_sample_count": int(decision["sample_count"]),
        "decision_mean_bps": round(float(decision["mean_bps"]), 4),
        "decision_lower_bps": round(float(decision["lower_bps"]), 4),
        "decision_upper_bps": round(float(decision["upper_bps"]), 4),
        "decision_mean_net_bps": round(float(decision["mean_net_bps"]), 4),
        "decision_lower_net_bps": round(float(decision["lower_net_bps"]), 4),
        "decision_upper_net_bps": round(float(decision["upper_net_bps"]), 4),
        "best_lower_net_bps": round(float(best_lower_net["lower_net_bps"]), 4),
        "best_upper_net_bps": round(float(best_upper_net["upper_net_bps"]), 4),
        "worst_pre_cost_upper_bps": round(
            float(worst_negative["upper_bps"]), 4,
        ),
        "confidence": round(float(decision["confidence"]), 6),
        "minimum_requirement": "sample_count>=2 and nonzero_variance",
    }


def _fetch_bucket_rows(
    engine: Engine, *, ticker: str, alert_type: str, bucket: str,
    table: str = DECAY_TABLE_NO_FRICTION,
) -> list[dict[str, Any]]:
    """All horizon rows for one (ticker, alert_type, bucket) tuple.

    f-fastpath-maker-only (2026-05-08): the ``table`` parameter
    selects between the two decay tables:

      * ``fast_signal_decay`` (default) -- no-friction decay; assumes
        immediate fill at best price. Used by taker-mode gates.
      * ``fast_signal_decay_maker_filled`` -- adverse-selection-aware
        decay; only counts events where a maker order WOULD have
        filled. Used by maker-only gates so the cost-bar check is
        against the right realized distribution.

    The SQL is parameterized via f-string interpolation rather than a
    bound parameter because Postgres doesn't accept bound parameters
    for table names. Caller is responsible for passing only the two
    allow-listed values; ``gates.py`` enforces this.
    """
    if table not in SUPPORTED_DECAY_TABLES:
        # Defensive: never trust caller-provided strings as raw SQL.
        raise ValueError(f"unsupported decay table: {table!r}")
    sql = text(f"""
        SELECT horizon_s, sample_count, mean_return, m2_return,
               realized_validation_count, realized_validation_residual
        FROM {table}
        WHERE ticker = :t
          AND alert_type = :at
          AND score_bucket = :sb
        ORDER BY horizon_s ASC
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {
            "t": ticker, "at": alert_type, "sb": bucket,
        }).mappings().all()
    return [dict(r) for r in rows]


def _fetch_pooled_bucket_rows(
    engine: Engine, *, alert_type: str, bucket: str,
    table: str = DECAY_TABLE_NO_FRICTION,
) -> list[dict[str, Any]]:
    """Pooled horizon stats for one (alert_type, bucket) across tickers.

    This is the hierarchical fallback for sparse per-ticker maker-fill
    data. It combines Welford summaries exactly enough for gating:
    total M2 = sum(m2_i + n_i * mean_i^2) - N * pooled_mean^2.
    """
    if table not in SUPPORTED_DECAY_TABLES:
        raise ValueError(f"unsupported decay table: {table!r}")
    sql = text(f"""
        WITH bucket_rows AS (
            SELECT horizon_s, sample_count, mean_return, m2_return
            FROM {table}
            WHERE alert_type = :at
              AND score_bucket = :sb
              AND sample_count > 0
        ),
        pooled AS (
            SELECT
                horizon_s,
                SUM(sample_count)::bigint AS sample_count,
                SUM(mean_return * sample_count)
                    / NULLIF(SUM(sample_count), 0) AS mean_return,
                SUM(m2_return + sample_count * POWER(mean_return, 2)) AS sum_sq
            FROM bucket_rows
            GROUP BY horizon_s
        )
        SELECT
            horizon_s,
            sample_count,
            mean_return,
            GREATEST(
                0.0,
                sum_sq - sample_count * POWER(mean_return, 2)
            ) AS m2_return,
            0::bigint AS realized_validation_count,
            NULL::double precision AS realized_validation_residual
        FROM pooled
        ORDER BY horizon_s ASC
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {"at": alert_type, "sb": bucket}).mappings().all()
    return [dict(r) for r in rows]


def _best_sharpe_row(
    rows: list[dict[str, Any]], *, min_samples: int = MIN_SAMPLES_FOR_CALIB,
) -> dict[str, Any] | None:
    """Highest mean/stdev row above ``min_samples``. None if none qualify.

    Sharpe is computed against absolute mean (we prefer signals with
    a non-zero edge in EITHER direction; gates handle direction-of-
    profit via their long/short logic). Ties broken by sample_count
    (more data wins).
    """
    best: dict[str, Any] | None = None
    best_score = -1.0
    for row in rows:
        n = int(row["sample_count"] or 0)
        if n < min_samples:
            continue
        mean = float(row["mean_return"] or 0.0)
        stdev = _stdev(row["m2_return"], n)
        if stdev <= 0.0:
            continue
        score = abs(mean) / stdev
        if score > best_score or (
            score == best_score and best is not None
            and n > int(best["sample_count"] or 0)
        ):
            best = row
            best_score = score
    return best


# ── Public helpers ───────────────────────────────────────────────────


def get_calibrated_max_hold_s(
    engine: Engine,
    *,
    ticker: str,
    alert_type: str,
    signal_score: float,
) -> int | None:
    """Calibrated max-hold time for one signal in seconds.

    Picks the highest-Sharpe horizon (above ``MIN_SAMPLES_FOR_CALIB``)
    as the empirical "where this signal stops being predictive."
    Holding longer than that is uncalibrated speculation.

    Returns None when the bucket has no qualifying horizon -- caller
    falls back to ``MAX_HOLD_S_DEFAULT`` (the current constant).
    """
    bucket = score_bucket(signal_score)
    rows = _fetch_bucket_rows(
        engine, ticker=ticker, alert_type=alert_type, bucket=bucket,
    )
    best = _best_sharpe_row(rows)
    if best is None:
        return None
    raw_horizon = int(best["horizon_s"])
    if raw_horizon < CALIB_EXEC_FLOOR_S:
        # F6.5: latency floor. Don't schedule holds shorter than
        # round-trip placement latency -- the position can't actually
        # close in that window. Return the floor instead; let the
        # position survive long enough to prove or disprove its edge.
        key = (ticker, alert_type, bucket)
        if key not in _FLOOR_LOG_SEEN:
            _FLOOR_LOG_SEEN.add(key)
            logger.info(
                "[fast_path] calibration max_hold_s floored ticker=%s "
                "alert_type=%s bucket=%s calibrated=%ds floor=%ds "
                "(signal predictive horizon below execution latency)",
                ticker, alert_type, bucket, raw_horizon, CALIB_EXEC_FLOOR_S,
            )
        return CALIB_EXEC_FLOOR_S
    return raw_horizon


def is_score_tradeable(
    engine: Engine,
    *,
    ticker: str,
    alert_type: str,
    signal_score: float,
) -> bool | None:
    """Does this signal beat ``TRADEABLE_COST_MULT × TRADING_COST_FRAC``
    on its best horizon?

    Returns:
      - True  : mean > threshold AND has enough samples (calibrated tradeable)
      - False : has enough samples AND mean is at-or-below threshold
                (calibrated NOT tradeable -- don't trade this signal)
      - None  : insufficient data; caller falls back to MIN_SIGNAL_SCORE

    The threshold is direction-aware via ``alert_type`` suffix: long
    signals need positive mean above threshold; short signals need
    positive mean (since the table records the direction-flipped
    forward return).
    """
    bucket = score_bucket(signal_score)
    rows = _fetch_bucket_rows(
        engine, ticker=ticker, alert_type=alert_type, bucket=bucket,
    )
    best = _best_sharpe_row(rows)
    if best is None:
        return None
    threshold = TRADEABLE_COST_MULT * TRADING_COST_FRAC
    return float(best["mean_return"] or 0.0) > threshold


def compute_calibrated_bracket(
    engine: Engine,
    *,
    ticker: str,
    alert_type: str,
    signal_score: float,
    entry: float,
    direction: str = "long",
) -> tuple[float, float] | None:
    """(stop_price, target_price) sized by the empirical stdev at
    the highest-Sharpe horizon.

    Stop  = entry - 2 × stdev × entry  (long)  /  entry + 2 × stdev × entry  (short)
    Target= entry + 3 × stdev × entry  (long)  /  entry - 3 × stdev × entry  (short)

    The 2:3 R-multiple matches stop_engine's swing-side convention.
    Returns None when insufficient data -- caller falls back to
    ``stop_engine.compute_initial_bracket`` (ATR-based).
    """
    if entry <= 0.0:
        return None
    bucket = score_bucket(signal_score)
    rows = _fetch_bucket_rows(
        engine, ticker=ticker, alert_type=alert_type, bucket=bucket,
    )
    best = _best_sharpe_row(rows)
    if best is None:
        return None
    n = int(best["sample_count"] or 0)
    stdev = _stdev(best["m2_return"], n)
    if stdev <= 0.0:
        return None
    if (direction or "long").lower() == "short":
        stop = entry * (1.0 + 2.0 * stdev)
        target = entry * (1.0 - 3.0 * stdev)
    else:
        stop = entry * (1.0 - 2.0 * stdev)
        target = entry * (1.0 + 3.0 * stdev)
    if stop <= 0.0 or target <= 0.0:
        return None
    return float(stop), float(target)


def is_negative_edge_excluded(
    engine: Engine,
    *,
    ticker: str,
    alert_type: str,
    signal_score: float,
    table: str = DECAY_TABLE_NO_FRICTION,
    allow_pooled: bool = True,
) -> tuple[bool, dict[str, Any]]:
    """F6.5: statistically-significant negative edge detector.

    Looks at the (ticker, alert_type, score_bucket)'s decay horizons
    and returns ``(True, evidence)`` when the finite-sample Student
    upper confidence bound for mean return is below zero. This replaces
    the old fixed sample-count floor with the actual uncertainty of the
    bucket: small but decisive evidence can block, while one-sample or
    zero-variance buckets cannot create false certainty.

    Returns ``(False, evidence)`` when:
      - The bucket has no statistically usable horizon (caller allows
        through).
      - The most-negative horizon's upper CI is at or above zero (uncertain or
        positive; let downstream gates decide).

    Evidence dict carries the underlying numbers so the gate can
    surface them in the rejection JSON for postmortem.
    """
    bucket = score_bucket(signal_score)
    rows = _fetch_bucket_rows(
        engine,
        ticker=ticker,
        alert_type=alert_type,
        bucket=bucket,
        table=table,
    )
    ticker_evidence = _most_negative_confidence_evidence(
        rows,
        bucket=bucket,
        table=table,
        scope="ticker",
    )
    if ticker_evidence is not None and ticker_evidence["upper_ci"] < 0.0:
        ticker_evidence["verdict"] = "negative_edge"
        return True, ticker_evidence

    # Hierarchical fallback: pooled evidence can suppress a sparse or
    # uncertain ticker, but not a ticker whose own confidence interval is
    # decisively positive.
    ticker_confidently_positive = (
        ticker_evidence is not None
        and ticker_evidence["lower_ci"] > 0.0
    )
    pooled_evidence: dict[str, Any] | None = None
    if allow_pooled and not ticker_confidently_positive:
        pooled_rows = _fetch_pooled_bucket_rows(
            engine,
            alert_type=alert_type,
            bucket=bucket,
            table=table,
        )
        pooled_evidence = _most_negative_confidence_evidence(
            pooled_rows,
            bucket=bucket,
            table=table,
            scope="pooled",
        )
        if pooled_evidence is not None and pooled_evidence["upper_ci"] < 0.0:
            pooled_evidence["verdict"] = "negative_edge"
            if ticker_evidence is not None:
                pooled_evidence["ticker_scope_verdict"] = (
                    "uncertain"
                    if ticker_evidence["lower_ci"] <= 0.0 <= ticker_evidence["upper_ci"]
                    else "non_negative"
                )
                pooled_evidence["ticker_upper_ci"] = ticker_evidence["upper_ci"]
                pooled_evidence["ticker_lower_ci"] = ticker_evidence["lower_ci"]
                pooled_evidence["ticker_sample_count"] = ticker_evidence["sample_count"]
            return True, pooled_evidence

    if ticker_evidence is None and pooled_evidence is None:
        return False, {
            "score_bucket": bucket,
            "verdict": "insufficient_statistical_evidence",
            "minimum_requirement": "sample_count>=2 and nonzero_variance",
            "decay_table": table,
            "confidence": _bounded_confidence(NEGATIVE_EDGE_CONFIDENCE),
        }

    evidence = ticker_evidence or pooled_evidence
    assert evidence is not None
    if evidence["lower_ci"] > 0.0:
        evidence["verdict"] = "positive_edge"
    elif evidence["lower_ci"] <= 0.0 <= evidence["upper_ci"]:
        evidence["verdict"] = "uncertain"
    else:
        evidence["verdict"] = "non_negative"
    return False, evidence


def is_cost_barrier_excluded(
    engine: Engine,
    *,
    ticker: str,
    alert_type: str,
    signal_score: float,
    cost_bps: float,
    min_net_bps: float = 0.0,
    table: str = DECAY_TABLE_NO_FRICTION,
    allow_pooled: bool = True,
) -> tuple[bool, dict[str, Any]]:
    """Return whether empirical evidence cannot clear round-trip cost.

    This is the cost-aware counterpart to ``is_negative_edge_excluded``.
    It blocks when every statistically usable horizon's upper
    confidence bound is below ``cost_bps + min_net_bps``. Sparse
    ticker buckets can be governed by pooled lane evidence, but a
    ticker whose own lower confidence bound clears cost is allowed to
    override pooled weakness.
    """
    bucket = score_bucket(signal_score)
    cost_bps_f = max(0.0, float(cost_bps or 0.0))
    min_net_bps_f = float(min_net_bps or 0.0)
    rows = _fetch_bucket_rows(
        engine,
        ticker=ticker,
        alert_type=alert_type,
        bucket=bucket,
        table=table,
    )
    ticker_summary = _cost_barrier_summary(
        rows,
        bucket=bucket,
        table=table,
        scope="ticker",
        cost_bps=cost_bps_f,
        min_net_bps=min_net_bps_f,
    )
    exhausted_verdicts = {"negative_edge", "below_cost"}
    if ticker_summary is not None:
        if ticker_summary["verdict"] in exhausted_verdicts:
            return True, ticker_summary
        if ticker_summary["verdict"] == "positive_edge_candidate":
            return False, ticker_summary

    pooled_summary: dict[str, Any] | None = None
    if allow_pooled:
        pooled_rows = _fetch_pooled_bucket_rows(
            engine,
            alert_type=alert_type,
            bucket=bucket,
            table=table,
        )
        pooled_summary = _cost_barrier_summary(
            pooled_rows,
            bucket=bucket,
            table=table,
            scope="pooled",
            cost_bps=cost_bps_f,
            min_net_bps=min_net_bps_f,
        )
        if (
            pooled_summary is not None
            and pooled_summary["verdict"] in exhausted_verdicts
        ):
            if ticker_summary is not None:
                pooled_summary["ticker_scope_verdict"] = ticker_summary[
                    "verdict"
                ]
                pooled_summary["ticker_decision_upper_net_bps"] = (
                    ticker_summary["decision_upper_net_bps"]
                )
                pooled_summary["ticker_decision_lower_net_bps"] = (
                    ticker_summary["decision_lower_net_bps"]
                )
                pooled_summary["ticker_sample_count"] = ticker_summary[
                    "sample_count"
                ]
            return True, pooled_summary

    if ticker_summary is not None:
        return False, ticker_summary
    if pooled_summary is not None:
        return False, pooled_summary

    verdict = (
        "insufficient_statistical_evidence"
        if rows
        else "no_data"
    )
    return False, {
        "score_bucket": bucket,
        "verdict": verdict,
        "minimum_requirement": "sample_count>=2 and nonzero_variance",
        "cost_bps": round(cost_bps_f, 4),
        "min_net_bps": round(min_net_bps_f, 4),
        "decay_table": table,
        "confidence": _bounded_confidence(NEGATIVE_EDGE_CONFIDENCE),
    }


__all__ = [
    "get_calibrated_max_hold_s",
    "is_score_tradeable",
    "is_negative_edge_excluded",
    "is_cost_barrier_excluded",
    "maker_attempt_adverse_selection_excluded",
    "maker_attempt_adverse_selection_excluded_for_bucket",
    "maker_attempt_adverse_selection_excluded_from_rows",
    "compute_calibrated_bracket",
    "MIN_SAMPLES_FOR_CALIB",
    "TRADING_COST_FRAC",
    "TRADEABLE_COST_MULT",
    "CALIB_EXEC_FLOOR_S",
    "NEGATIVE_EDGE_CONFIDENCE",
    "DECAY_TABLE_NO_FRICTION",
    "DECAY_TABLE_MAKER_FILLED",
    "SUPPORTED_DECAY_TABLES",
    "decay_table_for_execution_mode",
]

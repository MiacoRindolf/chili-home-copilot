"""Phase J - canonical drift monitor (pure functions).

Given a pattern's backtest baseline win-probability (``baseline_p``)
and the recent **closed** sample from live/paper trades (a sequence of
0/1 outcomes), the drift monitor computes:

1. **Brier-style calibration delta**:
   ``observed_p - baseline_p``. Sign indicates direction; magnitude is
   in the same units as probability. A negative delta means live is
   *underperforming* the baseline; positive means live is beating
   expectation. Symmetric thresholds catch both.
2. **CUSUM statistic**: a cumulative-sum drift detector over the
   individual outcomes vs ``baseline_p`` with a two-sided reset. The
   statistic is ``max(|S+|, |S-|)`` where:
     ``S+_t = max(0, S+_{t-1} + (x_t - baseline_p) - k)``
     ``S-_t = max(0, S-_{t-1} - (x_t - baseline_p) - k)``
   ``k`` is a "reference value" slack (we use 0.05) and the
   threshold ``h`` scales with ``sqrt(n)`` to handle small samples.
3. **Severity bucket**: ``green`` | ``yellow`` | ``red`` derived from
   both metrics with hysteresis on sample size - small samples never
   promote past ``yellow``.

This module is 100% pure - no DB, no logging, no config reads. It is
safe to import from tests. All callers are responsible for
classifying their caller-side persistence ``mode`` and writing
a row to ``trading_pattern_drift_log`` via
:mod:`app.services.trading.drift_monitor_service`.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Sequence

_VALID_SEVERITIES = ("green", "yellow", "red")


@dataclass(frozen=True)
class DriftMonitorConfig:
    """Tuning knobs for the drift monitor.

    All defaults are intentionally *conservative* - thresholds should
    not fire for a handful of losing trades. Phase J.2 can retune
    after soak data is collected.
    """

    # Minimum closed-sample size for severity to reach ``red``.
    min_red_sample: int = 20
    # Minimum closed-sample size for severity to reach ``yellow``.
    min_yellow_sample: int = 10
    # Absolute calibration delta that triggers yellow on its own.
    yellow_brier_abs: float = 0.10
    # Absolute calibration delta that triggers red on its own.
    red_brier_abs: float = 0.20
    # CUSUM slack (reference value) per observation.
    cusum_k: float = 0.05
    # CUSUM threshold multiplier; threshold = mult * sqrt(n).
    cusum_threshold_mult: float = 0.6


@dataclass(frozen=True)
class DriftMonitorInput:
    """Inputs for a single ``compute_drift`` call.

    ``outcomes`` is a sequence of 0/1 outcomes ordered oldest-first
    from the recent closed sample. Non-0/1 values raise at the
    service layer; the pure function treats ``!= 0`` as 1.

    ``baseline_win_prob`` is the pattern's backtest-baseline win
    probability in ``[0, 1]``. ``None`` disables the monitor (the
    result is ``green`` / null stats).
    """

    scan_pattern_id: int
    pattern_name: str | None
    baseline_win_prob: float | None
    outcomes: Sequence[int]
    # Optional stable salt for the drift_id hash (e.g. sweep date).
    as_of_key: str | None = None


@dataclass(frozen=True)
class DriftMonitorOutput:
    """Pure result of ``compute_drift``.

    Shadow-safe: nothing in this dataclass triggers a side effect.
    """

    drift_id: str
    scan_pattern_id: int
    pattern_name: str | None
    baseline_win_prob: float | None
    observed_win_prob: float | None
    brier_delta: float | None
    cusum_statistic: float | None
    cusum_threshold: float | None
    sample_size: int
    severity: str
    payload: dict = field(default_factory=dict)


def _clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def _cusum(outcomes: Sequence[int], baseline: float, k: float) -> float:
    s_pos = 0.0
    s_neg = 0.0
    peak = 0.0
    for raw in outcomes:
        x = 1.0 if raw else 0.0
        delta = x - baseline
        s_pos = max(0.0, s_pos + delta - k)
        s_neg = max(0.0, s_neg - delta - k)
        peak = max(peak, s_pos, s_neg)
    return peak


def _classify(
    *,
    sample_size: int,
    brier_abs: float,
    cusum_stat: float,
    cusum_threshold: float,
    cfg: DriftMonitorConfig,
) -> str:
    if sample_size < cfg.min_yellow_sample:
        return "green"
    breach_red = (
        brier_abs >= cfg.red_brier_abs
        or cusum_stat >= cusum_threshold
    )
    if breach_red and sample_size >= cfg.min_red_sample:
        return "red"
    breach_yellow = (
        brier_abs >= cfg.yellow_brier_abs
        or cusum_stat >= cusum_threshold * 0.75
    )
    if breach_yellow:
        return "yellow"
    return "green"


def compute_drift_id(
    *, scan_pattern_id: int, as_of_key: str | None,
) -> str:
    """Deterministic hash for ``(pattern, as_of_key)`` dedupe."""
    basis = f"{int(scan_pattern_id)}|{as_of_key or 'no_key'}"
    return hashlib.blake2b(
        basis.encode("utf-8"), digest_size=16,
    ).hexdigest()


def compute_drift(
    inputs: DriftMonitorInput,
    *,
    config: DriftMonitorConfig | None = None,
) -> DriftMonitorOutput:
    """Pure drift evaluation for a single pattern.

    Returns a ``DriftMonitorOutput`` with frozen fields. No side
    effects, no exceptions on empty input (empty sample returns
    ``green`` with null stats).
    """
    cfg = config or DriftMonitorConfig()
    outcomes = list(inputs.outcomes)
    n = len(outcomes)
    drift_id = compute_drift_id(
        scan_pattern_id=inputs.scan_pattern_id,
        as_of_key=inputs.as_of_key,
    )

    if inputs.baseline_win_prob is None or n == 0:
        return DriftMonitorOutput(
            drift_id=drift_id,
            scan_pattern_id=int(inputs.scan_pattern_id),
            pattern_name=inputs.pattern_name,
            baseline_win_prob=inputs.baseline_win_prob,
            observed_win_prob=None,
            brier_delta=None,
            cusum_statistic=None,
            cusum_threshold=None,
            sample_size=n,
            severity="green",
            payload={
                "reason": "insufficient_inputs",
                "config": {
                    "cusum_k": cfg.cusum_k,
                    "cusum_threshold_mult": cfg.cusum_threshold_mult,
                    "min_yellow_sample": cfg.min_yellow_sample,
                    "min_red_sample": cfg.min_red_sample,
                },
            },
        )

    baseline = _clamp01(float(inputs.baseline_win_prob))
    wins = sum(1 for raw in outcomes if raw)
    observed = wins / n
    brier_delta = observed - baseline
    cusum_stat = _cusum(outcomes, baseline=baseline, k=cfg.cusum_k)
    cusum_threshold = cfg.cusum_threshold_mult * math.sqrt(n)
    severity = _classify(
        sample_size=n,
        brier_abs=abs(brier_delta),
        cusum_stat=cusum_stat,
        cusum_threshold=cusum_threshold,
        cfg=cfg,
    )
    payload = {
        "wins": int(wins),
        "losses": int(n - wins),
        "baseline": baseline,
        "cusum_k": cfg.cusum_k,
        "cusum_threshold_mult": cfg.cusum_threshold_mult,
        "yellow_brier_abs": cfg.yellow_brier_abs,
        "red_brier_abs": cfg.red_brier_abs,
    }
    return DriftMonitorOutput(
        drift_id=drift_id,
        scan_pattern_id=int(inputs.scan_pattern_id),
        pattern_name=inputs.pattern_name,
        baseline_win_prob=baseline,
        observed_win_prob=observed,
        brier_delta=brier_delta,
        cusum_statistic=cusum_stat,
        cusum_threshold=cusum_threshold,
        sample_size=n,
        severity=severity,
        payload=payload,
    )


__all__ = [
    "DriftMonitorConfig",
    "DriftMonitorInput",
    "DriftMonitorOutput",
    "compute_drift",
    "compute_drift_id",
]

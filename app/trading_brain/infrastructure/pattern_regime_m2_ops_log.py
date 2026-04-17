"""Structured one-line ops log for the Phase M.2 authoritative
consumers of the Pattern x Regime Performance Ledger.

Three independently-gated slices share this module, each with its
own prefix so release blockers can grep per-slice:

* ``[pattern_regime_tilt_ops]`` — M.2.a NetEdgeRanker sizing tilt
* ``[pattern_regime_promotion_ops]`` — M.2.b promotion gate
* ``[pattern_regime_killswitch_ops]`` — M.2.c daily kill-switch

Every line is whitespace-tokenised key=value pairs so operators can
pipe into ``Select-String`` / ``grep`` without structured parsers.
Release blockers assert that no ``mode=authoritative`` line appears
on any prefix until the corresponding slice is explicitly opened
behind a live, un-expired approval row in
``trading_governance_approvals``.
"""
from __future__ import annotations

from typing import Any

CHILI_PATTERN_REGIME_TILT_OPS_PREFIX = "[pattern_regime_tilt_ops]"
CHILI_PATTERN_REGIME_PROMOTION_OPS_PREFIX = "[pattern_regime_promotion_ops]"
CHILI_PATTERN_REGIME_KILLSWITCH_OPS_PREFIX = "[pattern_regime_killswitch_ops]"


def _format_val(k: str, v: Any, parts: list[str]) -> None:
    if v is None:
        return
    if isinstance(v, bool):
        parts.append(f"{k}={str(v).lower()}")
    elif isinstance(v, str):
        if any(c.isspace() for c in v) or v == "":
            parts.append(f'{k}="{v}"')
        else:
            parts.append(f"{k}={v}")
    elif isinstance(v, float):
        parts.append(f"{k}={v:.6g}")
    else:
        parts.append(f"{k}={v}")


def format_tilt_ops_line(
    *,
    event: str,
    # Accepted events:
    #   "tilt_computed"                    — shadow-or-higher decision produced
    #   "tilt_applied"                     — authoritative applied to sizer
    #   "tilt_refused_authoritative"       — auth requested without approval
    #   "tilt_skipped"                     — off-mode short circuit or bad input
    #   "tilt_fallback"                    — ledger too sparse, used safe 1.0
    mode: str,
    pattern_id: int | None = None,
    ticker: str | None = None,
    source: str | None = None,
    multiplier: float | None = None,
    baseline_size_dollars: float | None = None,
    consumer_size_dollars: float | None = None,
    reason_code: str | None = None,
    diff_category: str | None = None,
    n_confident_dimensions: int | None = None,
    fallback_used: bool | None = None,
    context_hash: str | None = None,
    evaluation_id: str | None = None,
    reason: str | None = None,
    **extra: Any,
) -> str:
    parts: list[str] = [
        CHILI_PATTERN_REGIME_TILT_OPS_PREFIX,
        f"event={event}",
        f"mode={mode}",
    ]
    _format_val("evaluation_id", evaluation_id, parts)
    _format_val("pattern_id", pattern_id, parts)
    _format_val("ticker", ticker, parts)
    _format_val("source", source, parts)
    _format_val("multiplier", multiplier, parts)
    _format_val("baseline_size_dollars", baseline_size_dollars, parts)
    _format_val("consumer_size_dollars", consumer_size_dollars, parts)
    _format_val("reason_code", reason_code, parts)
    _format_val("diff_category", diff_category, parts)
    _format_val("n_confident_dimensions", n_confident_dimensions, parts)
    _format_val("fallback_used", fallback_used, parts)
    _format_val("context_hash", context_hash, parts)
    _format_val("reason", reason, parts)
    for k, v in extra.items():
        _format_val(k, v, parts)
    return " ".join(parts)


def format_promotion_ops_line(
    *,
    event: str,
    # Accepted events:
    #   "promotion_evaluated"              — shadow-or-higher decision produced
    #   "promotion_applied"                — authoritative decision applied
    #   "promotion_refused_authoritative"  — auth requested without approval
    #   "promotion_skipped"                — off-mode short circuit or bad input
    #   "promotion_fallback"               — ledger too sparse, used baseline
    mode: str,
    pattern_id: int | None = None,
    source: str | None = None,
    baseline_allow: bool | None = None,
    consumer_allow: bool | None = None,
    reason_code: str | None = None,
    diff_category: str | None = None,
    n_confident_dimensions: int | None = None,
    fallback_used: bool | None = None,
    n_blocking_dimensions: int | None = None,
    context_hash: str | None = None,
    evaluation_id: str | None = None,
    reason: str | None = None,
    **extra: Any,
) -> str:
    parts: list[str] = [
        CHILI_PATTERN_REGIME_PROMOTION_OPS_PREFIX,
        f"event={event}",
        f"mode={mode}",
    ]
    _format_val("evaluation_id", evaluation_id, parts)
    _format_val("pattern_id", pattern_id, parts)
    _format_val("source", source, parts)
    _format_val("baseline_allow", baseline_allow, parts)
    _format_val("consumer_allow", consumer_allow, parts)
    _format_val("reason_code", reason_code, parts)
    _format_val("diff_category", diff_category, parts)
    _format_val("n_confident_dimensions", n_confident_dimensions, parts)
    _format_val("n_blocking_dimensions", n_blocking_dimensions, parts)
    _format_val("fallback_used", fallback_used, parts)
    _format_val("context_hash", context_hash, parts)
    _format_val("reason", reason, parts)
    for k, v in extra.items():
        _format_val(k, v, parts)
    return " ".join(parts)


def format_killswitch_ops_line(
    *,
    event: str,
    # Accepted events:
    #   "killswitch_evaluated"             — shadow-or-higher decision produced
    #   "killswitch_applied"               — authoritative quarantine applied
    #   "killswitch_refused_authoritative" — auth requested without approval
    #   "killswitch_skipped"               — off-mode short circuit
    #   "killswitch_fallback"              — ledger too sparse, no-op
    #   "killswitch_circuit_breaker"       — per-pattern quota exceeded
    #   "killswitch_sweep_summary"         — end-of-daily-run aggregate
    mode: str,
    pattern_id: int | None = None,
    baseline_status: str | None = None,
    consumer_quarantine: bool | None = None,
    reason_code: str | None = None,
    diff_category: str | None = None,
    consecutive_days_negative: int | None = None,
    worst_dimension: str | None = None,
    worst_expectancy: float | None = None,
    n_confident_dimensions: int | None = None,
    fallback_used: bool | None = None,
    context_hash: str | None = None,
    evaluation_id: str | None = None,
    patterns_evaluated: int | None = None,
    patterns_quarantined: int | None = None,
    reason: str | None = None,
    **extra: Any,
) -> str:
    parts: list[str] = [
        CHILI_PATTERN_REGIME_KILLSWITCH_OPS_PREFIX,
        f"event={event}",
        f"mode={mode}",
    ]
    _format_val("evaluation_id", evaluation_id, parts)
    _format_val("pattern_id", pattern_id, parts)
    _format_val("baseline_status", baseline_status, parts)
    _format_val("consumer_quarantine", consumer_quarantine, parts)
    _format_val("reason_code", reason_code, parts)
    _format_val("diff_category", diff_category, parts)
    _format_val("consecutive_days_negative", consecutive_days_negative, parts)
    _format_val("worst_dimension", worst_dimension, parts)
    _format_val("worst_expectancy", worst_expectancy, parts)
    _format_val("n_confident_dimensions", n_confident_dimensions, parts)
    _format_val("fallback_used", fallback_used, parts)
    _format_val("context_hash", context_hash, parts)
    _format_val("patterns_evaluated", patterns_evaluated, parts)
    _format_val("patterns_quarantined", patterns_quarantined, parts)
    _format_val("reason", reason, parts)
    for k, v in extra.items():
        _format_val(k, v, parts)
    return " ".join(parts)


__all__ = [
    "CHILI_PATTERN_REGIME_TILT_OPS_PREFIX",
    "CHILI_PATTERN_REGIME_PROMOTION_OPS_PREFIX",
    "CHILI_PATTERN_REGIME_KILLSWITCH_OPS_PREFIX",
    "format_tilt_ops_line",
    "format_promotion_ops_line",
    "format_killswitch_ops_line",
]

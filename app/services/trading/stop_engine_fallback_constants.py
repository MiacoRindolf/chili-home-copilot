"""Stop-engine fallback constants — single source of truth.

Phase 4 (2026-05-01) consolidates the magic 0.92 / 1.15 / 0.95 / 5%
fallback values that were scattered across `stop_engine`, `live_exit_engine`,
`signal_emit`, and `alerts`. This module is the *only* place these
numbers exist as code constants.

**These are fallbacks**, not policy. They fire only when the upstream
brain pipeline can't provide ATR. Every fallback firing is logged at
CRITICAL by the caller — observe the rate; if it's non-zero in steady
state, fix the upstream pipeline, don't retune the constants.

The values here intentionally match the operator's prior risk policy
(8% stop / 15% target on long entries) so re-routing through this
module is a pure refactor, not a behavior change.
"""
from __future__ import annotations


# ── Long-side fallbacks ────────────────────────────────────────────────

FALLBACK_STOP_PCT_LONG: float = 0.08
"""Default stop distance for long fallback: 8% below entry. Matches the
historical `entry * 0.92` magic in stop_engine.py."""

FALLBACK_TP_MULT_LONG: float = 1.15
"""Default target multiple for long fallback: 15% above entry. Matches
the historical `entry * 1.15` magic in stop_engine.py."""


# ── Short-side fallbacks ───────────────────────────────────────────────

FALLBACK_STOP_PCT_SHORT: float = 0.08
"""Default stop distance for short fallback: 8% above entry."""

FALLBACK_TP_MULT_SHORT: float = 0.85
"""Default target multiple for short fallback: 15% below entry."""


# ── Live-exit + signal-emit defaults ───────────────────────────────────
#
# These were `0.97` (3%) in `live_exit_engine.py:43-45` and
# `signal_emit.py:212`, vs `0.95` (5%) in `alerts.py:1291-1292`. The
# audit found the inconsistency. Until the brain emits a per-trade
# stop policy explicitly, callers route through this single value.
#
# Choosing 3% (the 2-of-3 majority value, and the one already in the
# autotrader's live_exit_engine + signal_emit) keeps the most recent
# behavior consistent. alerts.py callers should also import from here.

FALLBACK_INITIAL_STOP_PCT: float = 0.03
"""3% stop. Used by signal_emit, live_exit_engine, and (after Phase 4)
alerts.py. The previous 5% value in alerts.py is being retired."""

FALLBACK_DEFAULT_RISK_PCT: float = 0.03
"""Risk-per-trade fallback when the brain hasn't emitted an explicit
risk budget. Same scale as FALLBACK_INITIAL_STOP_PCT — a 3% stop with
the position sized to risk 3% of capital is unit-R sizing."""


# ── Capital fallback ───────────────────────────────────────────────────
#
# When the broker's ``get_broker_account_info()`` returns None or
# fails, sizing code in ``alerts.py`` was using inline ``or 10000.0``
# fallback. This violates the user policy ("don't lie to the brain
# about missing measurements") and silently sizes positions against
# phantom capital. The Phase 4 compromise is the same as the stop_engine
# pattern: keep the fallback (so nothing crashes when broker is down),
# but channel it through ``resolve_capital_with_critical_log()`` below
# so every fallback firing is visible. Long-term fix: callers route
# ``None`` upward and refuse to emit a proposal when capital is unknown.

FALLBACK_BUYING_POWER_USD: float = 10_000.0
"""Last-resort capital placeholder when the broker is unavailable.
Operator should investigate any ``CAPITAL_FALLBACK_FIRED`` log line —
a proposal sized against this number is wrong by definition."""


def resolve_capital_with_critical_log(
    buying_power: float | None,
    *,
    caller: str,
) -> float:
    """Return ``buying_power`` if non-None, else log CRITICAL and fall
    back to ``FALLBACK_BUYING_POWER_USD``.

    Use this at every site that previously did ``buying_power or 10000.0``.
    The CRITICAL log makes the fallback observable so the upstream broker
    fetch can be fixed.
    """
    if buying_power is not None:
        try:
            v = float(buying_power)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    import logging
    logging.getLogger(__name__).critical(
        "[capital_fallback] CAPITAL_FALLBACK_FIRED caller=%s buying_power=%r "
        "— sizing against placeholder $%s; fix the upstream broker fetch.",
        caller, buying_power, FALLBACK_BUYING_POWER_USD,
    )
    return FALLBACK_BUYING_POWER_USD


__all__ = [
    "FALLBACK_BUYING_POWER_USD",
    "FALLBACK_DEFAULT_RISK_PCT",
    "FALLBACK_INITIAL_STOP_PCT",
    "FALLBACK_STOP_PCT_LONG",
    "FALLBACK_STOP_PCT_SHORT",
    "FALLBACK_TP_MULT_LONG",
    "FALLBACK_TP_MULT_SHORT",
    "resolve_capital_with_critical_log",
]

"""Dynamic priors for missing measurements.

Operator feedback 2026-04-29 (memory ``feedback_no_hardcoded_fallbacks``):
when code needs a "neutral" value for a missing/None measurement, do NOT
hardcode a constant like ``or 0.5`` or ``confidence=0.5``. Compute it from
real data instead. The 0.5 win-rate fallback in ``ai_context.py:619`` and
``alpha_decay.py:124`` is the prototype: it floors a real losing pattern
at coin-flip-equivalent and prevents the system from learning the pattern
is below baseline.

This module supplies dynamically-computed replacements:

* :func:`population_win_rate` — recent realized win rate across all
  closed trades. Used wherever a "neutral" win-rate prior is needed.
* :func:`population_avg_return_pct` — recent realized average return
  (in percent). Used when ranking patterns and a pattern has no data.
* :func:`bayesian_pattern_win_rate` — Beta-shrinkage estimate using the
  population win-rate as the prior mean and ``min_trades`` as the prior
  strength.
* :func:`bayesian_pattern_confidence` — confidence proxy derived from
  realized sample size (n / (n + prior_strength)) — never a constant.

Every function returns ``None`` when there is no data to compute from.
Callers MUST treat ``None`` as a signal to abstain (skip the term, log
a warn, or refuse the decision) — never fall back to a magic constant.

Cached (60s TTL by default) because every call site potentially fires
on the autotrader hot path. Cache is per-process (no DB write).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from .realized_pnl_sql import trade_return_fraction_sql

logger = logging.getLogger(__name__)


# Process-local cache. Tiny -- only a handful of values.
_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL_S = 60.0


def _cache_get(key: str) -> Any | None:
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        ts, val = entry
        if (time.time() - ts) > _CACHE_TTL_S:
            del _CACHE[key]
            return None
        return val


def _cache_set(key: str, val: Any) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), val)


def _settings_get(name: str, default: Any) -> Any:
    try:
        from ...config import settings
        return getattr(settings, name, default)
    except Exception:
        return default


def population_win_rate(db: Session, *, lookback_days: int = 90) -> float | None:
    """Population realized win rate across all closed trades in the last
    ``lookback_days``. Returns ``None`` if no data.

    Used as the *prior mean* for Bayesian shrinkage and as the neutral
    value to substitute for None win-rates at scoring sites. Never
    falls back to 0.5.
    """
    cache_key = f"pop_wr:{lookback_days}"
    c = _cache_get(cache_key)
    if c is not None:
        return c
    try:
        row = db.execute(
            text("""
                SELECT
                  COUNT(*) AS n,
                  AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END) AS wr
                FROM trading_trades
                WHERE status = 'closed'
                  AND pnl IS NOT NULL
                  AND entry_price IS NOT NULL
                  AND entry_price > 0
                  AND quantity IS NOT NULL
                  AND quantity > 0
                  AND exit_date IS NOT NULL
                  AND exit_date > NOW() - make_interval(days => :ld)
            """),
            {"ld": int(lookback_days)},
        ).fetchone()
    except Exception:
        logger.debug("[dynamic_priors] population_win_rate query failed", exc_info=True)
        return None
    if row is None or not row.n or row.wr is None:
        return None
    val = float(row.wr)
    _cache_set(cache_key, val)
    return val


def population_avg_return_pct(db: Session, *, lookback_days: int = 90) -> float | None:
    """Population realized average return (in percent) across all closed
    trades in the last ``lookback_days``. Returns ``None`` if no data.
    """
    cache_key = f"pop_ar:{lookback_days}"
    c = _cache_get(cache_key)
    if c is not None:
        return c
    try:
        row = db.execute(
            text(f"""
                SELECT
                  COUNT(*) AS n,
                  AVG(({trade_return_fraction_sql()} * 100.0)) AS ar
                FROM trading_trades
                WHERE status = 'closed'
                  AND pnl IS NOT NULL
                  AND entry_price IS NOT NULL
                  AND entry_price > 0
                  AND quantity IS NOT NULL
                  AND quantity > 0
                  AND exit_date IS NOT NULL
                  AND exit_date > NOW() - make_interval(days => :ld)
            """),
            {"ld": int(lookback_days)},
        ).fetchone()
    except Exception:
        logger.debug("[dynamic_priors] population_avg_return_pct query failed", exc_info=True)
        return None
    if row is None or not row.n or row.ar is None:
        return None
    val = float(row.ar)
    _cache_set(cache_key, val)
    return val


def bayesian_pattern_win_rate(
    db: Session,
    *,
    pattern_wins: int | None,
    pattern_n: int | None,
    prior_strength: int | None = None,
) -> float | None:
    """Beta-Bernoulli shrinkage of a pattern's realized win rate toward the
    population prior.

    Equivalent to::

        wr_hat = (pattern_wins + alpha) / (pattern_n + alpha + beta)

    where ``alpha = prior_strength * pop_wr`` and
    ``beta = prior_strength * (1 - pop_wr)``.

    Returns ``None`` if either:
      * the population prior is unknown (no closed trades), or
      * inputs are non-numeric / nonsensical (negative wins, etc.)

    ``prior_strength`` defaults to ``chili_realized_ev_min_trades`` (5)
    so a pattern with fewer than 5 realized trades is dominated by the
    population prior. NOT a magic constant -- it's the same setting the
    realized-EV gate uses.
    """
    pop_wr = population_win_rate(db)
    if pop_wr is None:
        return None
    if prior_strength is None:
        prior_strength = int(_settings_get("chili_realized_ev_min_trades", 5))
    try:
        n = max(0, int(pattern_n or 0))
        w = max(0, int(pattern_wins or 0))
    except (TypeError, ValueError):
        return None
    if w > n:
        # nonsensical input
        return None
    alpha = float(prior_strength) * pop_wr
    beta = float(prior_strength) * (1.0 - pop_wr)
    return (w + alpha) / (n + alpha + beta)


def bayesian_pattern_confidence(
    pattern_n: int | None,
    *,
    prior_strength: int | None = None,
) -> float | None:
    """Confidence proxy = ``n / (n + prior_strength)``.

    Returns a value in ``[0.0, 1.0)`` that approaches 1 as the realized
    sample size grows. Never a constant; never assumes a "neutral" 0.5.

    Returns ``None`` for non-numeric input.
    """
    if prior_strength is None:
        prior_strength = int(_settings_get("chili_realized_ev_min_trades", 5))
    try:
        n = max(0, int(pattern_n or 0))
    except (TypeError, ValueError):
        return None
    if n == 0 and prior_strength == 0:
        return None
    return float(n) / float(n + prior_strength)

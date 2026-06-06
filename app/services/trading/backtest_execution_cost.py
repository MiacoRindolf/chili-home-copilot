"""Data-derived execution cost for backtests — backtest<->live parity.

Operator directive (2026-06-05): backtests must mirror live execution so they are
actually predictive, and the cost inputs must be DERIVED from the system's own
MEASURED execution reality — NO magic numbers. The forensic attribution showed
the entry estimator / backtest is systematically over-optimistic (crypto +4.5%
expected vs -2.8% realized) in part because the backtest priced execution with
hardcoded floors (5/10/20 bps crypto, 2 bps equity) and a flat commission while
live pays real spread + slippage + venue fees.

This module replaces those magic floors with the system's OWN measured realized
round-trip execution cost, per asset class:
  PRIMARY:  ``trading_venue_truth_log.realized_cost_fraction`` — the measured
            realized round-trip cost (spread + slippage + fees) as a fraction of
            notional, recorded per live fill. This already INCLUDES venue fees.
  FALLBACK: ``trading_execution_cost_estimates`` median spread + slippage (well
            sampled, but excludes the explicit fee) when truth-log samples are
            thin for an asset class.
Both are the system's measured data, not assumptions. The only tunables are a
sample-size guard (config, documented) — never a cost number.

The result feeds ``run_pattern_backtest`` so a pattern's backtested net edge is
charged the same cost it will actually pay live; a pattern whose edge does not
survive its OWN measured execution cost no longer backtests positive.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _settings_get(name: str, default: Any) -> Any:
    try:
        from ...config import settings
        return getattr(settings, name, default)
    except Exception:
        return default


def _is_crypto_ticker(ticker: str) -> bool:
    t = (ticker or "").upper()
    base = t.replace("-USD", "") if t.endswith("-USD") else ""
    return t.endswith("-USD") and bool(base) and (not base.isdigit() or base in {"00"})


# crypto = ticker ends in -USD; equity = everything else. Matches _is_crypto_ticker.
_ASSET_CLAUSE = {
    "crypto": "UPPER(ticker) LIKE '%-USD'",
    "equity": "UPPER(ticker) NOT LIKE '%-USD'",
}


def derive_asset_class_backtest_costs(db: Session) -> dict[str, dict[str, Any]]:
    """Return per-asset-class measured round-trip backtest cost.

    ``{ "crypto": {round_trip_cost_fraction, spread, commission, source, n},
        "equity": {...} }``  — values DERIVED from measured realized cost (no
    magic numbers). An asset class maps to ``None`` only when there is genuinely
    zero measured data for it (callers then keep their existing fallback).

    The round-trip cost is charged in the backtest as a per-leg ``commission``
    (= round_trip / 2, applied on entry AND exit), with ``spread`` left at 0 so
    the cost is not double-counted; ``backtest_commission_used`` then records the
    measured per-leg cost.
    """
    min_n = int(_settings_get("chili_backtest_cost_min_measured_samples", 8))
    out: dict[str, dict[str, Any]] = {}

    for asset, clause in _ASSET_CLAUSE.items():
        rt: float | None = None
        source = "no_measured_data"
        n = 0

        # PRIMARY: measured realized round-trip cost (incl. fees) from the truth log.
        try:
            row = db.execute(text(
                f"""
                SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY realized_cost_fraction) AS m,
                       COUNT(*) AS n
                  FROM trading_venue_truth_log
                 WHERE realized_cost_fraction IS NOT NULL
                   AND realized_cost_fraction >= 0
                   AND COALESCE(paper_bool, FALSE) = FALSE
                   AND {clause}
                """
            )).mappings().first()
            if row and row["m"] is not None and int(row["n"] or 0) >= min_n:
                rt = float(row["m"])
                n = int(row["n"])
                source = f"venue_truth_log(realized_cost_fraction,n={n})"
        except Exception:
            logger.debug("[backtest_cost] truth-log query failed for %s", asset, exc_info=True)

        # FALLBACK: measured median spread + slippage from the cost estimates
        # (well sampled; excludes explicit fee, so a mild under-estimate).
        if rt is None:
            try:
                row = db.execute(text(
                    f"""
                    SELECT percentile_cont(0.5) WITHIN GROUP
                               (ORDER BY (COALESCE(median_spread_bps,0) + COALESCE(median_slippage_bps,0))) AS m_bps,
                           SUM(sample_trades) AS n
                      FROM trading_execution_cost_estimates
                     WHERE COALESCE(sample_trades,0) > 0
                       AND {clause}
                    """
                )).mappings().first()
                if row and row["m_bps"] is not None and int(row["n"] or 0) >= min_n:
                    rt = float(row["m_bps"]) / 10_000.0
                    n = int(row["n"])
                    source = f"execution_cost_estimates(median_spread+slip,n={n})"
            except Exception:
                logger.debug("[backtest_cost] estimates query failed for %s", asset, exc_info=True)

        if rt is None:
            out[asset] = None  # type: ignore[assignment]
            continue

        out[asset] = {
            "round_trip_cost_fraction": rt,
            "spread": 0.0,
            "commission": rt / 2.0,  # per-leg; entry + exit = round-trip
            "source": source,
            "n": n,
        }
    return out


def backtest_costs_for_ticker(
    ticker: str, asset_class_costs: dict[str, Any] | None
) -> tuple[float, float] | None:
    """Map a ticker to its measured (spread, commission) from a pre-derived
    per-asset-class table, or None when no measured cost is available."""
    if not asset_class_costs:
        return None
    asset = "crypto" if _is_crypto_ticker(ticker) else "equity"
    entry = asset_class_costs.get(asset)
    if not entry:
        return None
    return float(entry.get("spread", 0.0)), float(entry.get("commission", 0.0))


def asset_class_cost_floor(db: Session, ticker: str) -> tuple[float | None, str]:
    """Measured round-trip cost FLOOR (incl. venue fees) for the ticker's asset
    class -- the minimum cost any ticker of that asset pays. Same data source as
    the backtest (``derive_asset_class_backtest_costs``), so the entry gate and the
    backtest charge the SAME measured cost. Returns (fraction|None, source)."""
    try:
        costs = derive_asset_class_backtest_costs(db)
    except Exception:
        return None, "derive_failed"
    asset = "crypto" if _is_crypto_ticker(ticker) else "equity"
    entry = costs.get(asset)
    if not entry:
        return None, f"no_measured_data:{asset}"
    return float(entry["round_trip_cost_fraction"]), f"{asset}:{entry.get('source','')}"


def realized_cost_bias_fraction(
    db: Session,
    ticker: str,
    *,
    max_fraction: float,
    min_obs: int,
    lookback_days: int,
) -> tuple[float, dict[str, Any]]:
    """Fix 4 feedback: the MEASURED (realized - expected) entry-cost gap for this
    ticker from the venue-truth log, so the cost estimate self-corrects its
    optimism. Upward-only (we only inflate when we under-estimated; a single bad
    fill cannot lock a ticker out -- bounded by ``max_fraction``). Returns
    (bias_fraction, snapshot)."""
    try:
        row = db.execute(text(
            """
            SELECT percentile_cont(0.5) WITHIN GROUP
                       (ORDER BY (realized_cost_fraction - expected_cost_fraction)) AS gap,
                   COUNT(*) AS n
              FROM trading_venue_truth_log
             WHERE UPPER(ticker) = UPPER(:t)
               AND realized_cost_fraction IS NOT NULL
               AND expected_cost_fraction IS NOT NULL
               AND COALESCE(paper_bool, FALSE) = FALSE
               AND created_at >= NOW() - make_interval(days => :lb)
            """
        ), {"t": ticker, "lb": int(lookback_days)}).mappings().first()
    except Exception:
        return 0.0, {"used": False, "reason": "query_failed"}
    n = int(row["n"] or 0) if row else 0
    gap = float(row["gap"]) if (row and row["gap"] is not None) else None
    if gap is None or n < int(min_obs):
        return 0.0, {"used": False, "reason": "insufficient_obs", "n": n, "min_obs": int(min_obs)}
    bias = max(0.0, min(gap, float(max_fraction)))
    return bias, {"used": True, "n": n, "measured_gap_fraction": round(gap, 6),
                  "bias_fraction": round(bias, 6), "max_fraction": float(max_fraction)}

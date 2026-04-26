"""Q1.T5 — Hierarchical Risk Parity portfolio sizing.

When ``CHILI_HRP_SIZING_ENABLED=True``, replaces the naive 2%-per-trade
sizing with HRP-allocated sizing across the active position covariance
matrix. When OFF (default), naive sizing is preserved and HRP is logged
in shadow for comparison.

HRP implementation
------------------

Lopez de Prado's Hierarchical Risk Parity (2016):

    1. Compute the covariance matrix Σ over the trailing returns of
       all active symbols + the candidate symbol.
    2. Cluster symbols hierarchically using single-linkage on the
       distance matrix d(i,j) = sqrt(0.5 * (1 - corr(i,j))).
    3. Quasi-diagonalize Σ by reordering rows/cols to follow the
       cluster tree.
    4. Recursive bisection: split the ordered list, compute inverse-
       variance weights for each half, scale by their cluster
       variance, allocate.

We use a minimal pure-numpy/scipy implementation rather than depending
on ``riskfolio-lib`` (heavyweight; brings in cvxopt + lots of optional
optimization deps). Falls back to naive when the active position set is
< 2 symbols (HRP is undefined for n=1).

Covariance source
-----------------

Trailing 60 trading days of close-to-close log returns from
``trading_snapshots`` per symbol. If a symbol has fewer than 30 obs,
HRP is skipped for that symbol and naive sizing is used instead. This
is conservative: small-history symbols can produce numerically unstable
covariance estimates that swing weights wildly.

Contract
--------

``decide_position_size(db, symbol, account_equity_usd, ...)`` returns
``SizingDecision`` with ``naive_size_usd``, ``hrp_size_usd``,
``hrp_weight``, ``chosen_sizing``, plus diagnostic fields. Always
writes one row to ``portfolio_sizing_log``. Caller uses ``chosen_sizing``
to determine which value to honor.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# --- Tunables ----------------------------------------------------------

_RETURN_WINDOW_DAYS = 60
_MIN_OBS_PER_SYMBOL = 30
_NAIVE_RISK_FRAC = 0.02       # 2% per trade — current default
_MAX_HRP_WEIGHT = 0.30        # cap any single HRP weight at 30% of total
_MIN_HRP_WEIGHT = 0.005       # floor below which we treat as zero


@dataclass
class SizingDecision:
    symbol: str
    account_equity_usd: float
    naive_size_usd: float
    hrp_size_usd: Optional[float]
    hrp_weight: Optional[float]
    chosen_sizing: str   # 'naive' | 'hrp'
    n_active_positions: int
    cov_condition_number: Optional[float]
    hrp_cluster_label: Optional[str]
    meta: dict = field(default_factory=dict)


# --- Naive sizing ------------------------------------------------------

def _naive_size(account_equity_usd: float) -> float:
    return round(float(account_equity_usd) * _NAIVE_RISK_FRAC, 2)


# --- HRP math ----------------------------------------------------------

def _correlation_to_distance(corr: "np.ndarray") -> "np.ndarray":
    import numpy as np
    return np.sqrt(0.5 * (1.0 - corr))


def _quasi_diag_order(linkage: "np.ndarray", n: int) -> list[int]:
    """Single-linkage quasi-diagonal ordering."""
    sort_ix = list(linkage[-1, :2].astype(int))
    while max(sort_ix) >= n:
        new = []
        for i in sort_ix:
            if i < n:
                new.append(i)
            else:
                row = linkage[i - n]
                new.extend([int(row[0]), int(row[1])])
        sort_ix = new
    return sort_ix


def _cluster_variance(
    cov: "np.ndarray", indices: list[int]
) -> float:
    import numpy as np
    sub = cov[np.ix_(indices, indices)]
    inv_var = 1.0 / np.diag(sub)
    w = inv_var / inv_var.sum()
    return float(w @ sub @ w)


def _recursive_bisection(
    cov: "np.ndarray", ordered: list[int]
) -> dict[int, float]:
    weights = {i: 1.0 for i in ordered}
    clusters = [ordered]
    while clusters:
        cluster = clusters.pop(0)
        if len(cluster) <= 1:
            continue
        mid = len(cluster) // 2
        left, right = cluster[:mid], cluster[mid:]
        v_left = _cluster_variance(cov, left)
        v_right = _cluster_variance(cov, right)
        alpha = 1.0 - v_left / (v_left + v_right) if (v_left + v_right) > 0 else 0.5
        for i in left:
            weights[i] *= alpha
        for i in right:
            weights[i] *= (1.0 - alpha)
        clusters.extend([left, right])
    return weights


def _compute_hrp_weights(
    returns_matrix: "np.ndarray", symbols: list[str]
) -> tuple[Optional[dict[str, float]], Optional[float]]:
    """Returns (weights_by_symbol, condition_number) or (None, None) on failure."""
    try:
        import numpy as np
        from scipy.cluster.hierarchy import linkage
    except ImportError as e:
        logger.debug("[hrp] scipy/numpy unavailable: %s", e)
        return None, None

    if returns_matrix.shape[1] < 2 or returns_matrix.shape[0] < _MIN_OBS_PER_SYMBOL:
        return None, None

    # Drop columns with constant (zero-var) returns — they break correlation.
    var = np.var(returns_matrix, axis=0)
    keep_mask = var > 1e-12
    if keep_mask.sum() < 2:
        return None, None
    returns_matrix = returns_matrix[:, keep_mask]
    symbols = [s for s, k in zip(symbols, keep_mask) if k]

    cov = np.cov(returns_matrix, rowvar=False)
    corr = np.corrcoef(returns_matrix, rowvar=False)

    # Numerical safety.
    cond = float(np.linalg.cond(cov)) if np.all(np.isfinite(cov)) else None
    if cond is None or cond > 1e8:
        # Singular or near-singular covariance; HRP will produce noise.
        return None, cond

    dist = _correlation_to_distance(corr)
    np.fill_diagonal(dist, 0.0)

    # Convert to condensed form for scipy.cluster.hierarchy.linkage
    n = len(symbols)
    condensed = []
    for i in range(n):
        for j in range(i + 1, n):
            condensed.append(dist[i, j])
    if len(condensed) == 0:
        return None, cond

    Z = linkage(np.array(condensed), method="single")
    ordered = _quasi_diag_order(Z, n)
    weights_by_idx = _recursive_bisection(cov, ordered)

    # Apply caps and floors, then renormalize.
    raw = {i: weights_by_idx[i] for i in range(n)}
    capped = {i: min(_MAX_HRP_WEIGHT, max(0.0, w)) for i, w in raw.items()}
    capped = {i: (w if w >= _MIN_HRP_WEIGHT else 0.0) for i, w in capped.items()}
    total = sum(capped.values()) or 1.0
    final = {symbols[i]: capped[i] / total for i in range(n) if capped[i] > 0}
    return final, cond


# --- Returns fetch -----------------------------------------------------

def _fetch_returns_matrix(
    db: Session, symbols: list[str]
) -> tuple[Optional["np.ndarray"], list[str]]:
    """Fetch trailing daily-bar returns per symbol from trading_snapshots.

    Returns ``(matrix, symbols_kept)`` where ``matrix`` has shape
    (T, n_kept) — rows are days, columns are symbols. Symbols with
    insufficient history are dropped.
    """
    try:
        import numpy as np
    except ImportError:
        return None, []

    cutoff = datetime.utcnow() - timedelta(days=_RETURN_WINDOW_DAYS * 2)
    series_by_symbol: dict[str, list[float]] = {}

    try:
        rows = db.execute(
            text(
                """
                SELECT ticker, bar_start_at, last_price
                FROM trading_snapshots
                WHERE bar_start_at >= :cutoff
                  AND ticker = ANY(:symbols)
                  AND last_price IS NOT NULL AND last_price > 0
                ORDER BY ticker, bar_start_at
                """
            ),
            {"cutoff": cutoff, "symbols": [s.upper() for s in symbols]},
        ).fetchall()
    except Exception as e:
        logger.debug("[hrp] returns fetch failed: %s", e)
        return None, []

    by_sym: dict[str, list[tuple[datetime, float]]] = {}
    for tk, ts, px in rows:
        by_sym.setdefault(tk.upper(), []).append((ts, float(px)))

    kept: list[str] = []
    matrix_cols: list[list[float]] = []
    target_len = None
    for sym in symbols:
        sym_upper = sym.upper()
        bars = by_sym.get(sym_upper, [])
        if len(bars) < _MIN_OBS_PER_SYMBOL:
            continue
        # Compute log returns.
        bars.sort(key=lambda x: x[0])
        prices = [b[1] for b in bars[-_RETURN_WINDOW_DAYS:]]
        if len(prices) < _MIN_OBS_PER_SYMBOL:
            continue
        returns = [
            math.log(prices[i] / prices[i - 1])
            for i in range(1, len(prices))
            if prices[i - 1] > 0
        ]
        if target_len is None:
            target_len = len(returns)
        if len(returns) < target_len:
            continue
        # Truncate to common length.
        matrix_cols.append(returns[-target_len:])
        kept.append(sym_upper)

    if not matrix_cols or len(matrix_cols) < 2:
        return None, []

    try:
        m = np.array(matrix_cols).T  # (T, n)
        return m, kept
    except Exception as e:
        logger.debug("[hrp] matrix assembly failed: %s", e)
        return None, []


def _fetch_active_position_symbols(db: Session, user_id: Optional[int]) -> list[str]:
    """Symbols with currently-open positions for this user."""
    try:
        rows = db.execute(
            text(
                """
                SELECT DISTINCT ticker FROM trading_trades
                WHERE status IN ('open', 'pending')
                  AND (user_id = :uid OR :uid IS NULL)
                """
            ),
            {"uid": user_id},
        ).fetchall()
        return [r[0] for r in rows or [] if r[0]]
    except Exception as e:
        logger.debug("[hrp] active positions fetch failed: %s", e)
        return []


# --- Public entry point ------------------------------------------------

def decide_position_size(
    db: Session,
    symbol: str,
    account_equity_usd: float,
    *,
    user_id: Optional[int] = None,
    persist_log: bool = True,
) -> SizingDecision:
    """Compute naive + HRP sizing, log to ``portfolio_sizing_log``, return decision.

    The chosen_sizing field reflects the live flag state:

      * ``CHILI_HRP_SIZING_ENABLED=False`` → ``chosen_sizing='naive'``
        (HRP fields populated for shadow comparison).
      * ``CHILI_HRP_SIZING_ENABLED=True`` AND HRP succeeded
        → ``chosen_sizing='hrp'``.
      * Flag ON but HRP failed (insufficient history, ill-conditioned
        cov, etc.) → ``chosen_sizing='naive'`` with ``hrp_size_usd=None``
        and a meta entry explaining the fallback.
    """
    naive = _naive_size(account_equity_usd)
    decision = SizingDecision(
        symbol=symbol.upper(),
        account_equity_usd=float(account_equity_usd),
        naive_size_usd=naive,
        hrp_size_usd=None,
        hrp_weight=None,
        chosen_sizing="naive",
        n_active_positions=0,
        cov_condition_number=None,
        hrp_cluster_label=None,
        meta={},
    )

    # Read flag.
    try:
        from ...config import settings
        hrp_enabled = bool(getattr(settings, "chili_hrp_sizing_enabled", False))
    except Exception:
        hrp_enabled = False

    # Compute HRP regardless of flag (shadow mode); cost is bounded.
    active = _fetch_active_position_symbols(db, user_id)
    universe = list({s.upper() for s in active + [symbol]})
    decision.n_active_positions = len(active)

    if len(universe) >= 2:
        returns, kept = _fetch_returns_matrix(db, universe)
        if returns is not None and symbol.upper() in kept:
            weights, cond = _compute_hrp_weights(returns, kept)
            decision.cov_condition_number = cond
            if weights is not None and symbol.upper() in weights:
                w = weights[symbol.upper()]
                decision.hrp_weight = w
                decision.hrp_size_usd = round(
                    account_equity_usd * w * _MAX_HRP_WEIGHT, 2
                )
                decision.meta["weights_universe"] = list(weights.keys())
                decision.meta["weights_sample"] = {
                    k: round(v, 4) for k, v in list(weights.items())[:5]
                }
            else:
                decision.meta["hrp_skip"] = "weights_undefined"
        else:
            decision.meta["hrp_skip"] = (
                "insufficient_returns_history" if returns is None else "symbol_dropped"
            )
    else:
        decision.meta["hrp_skip"] = "fewer_than_2_symbols"

    # Choose.
    if hrp_enabled and decision.hrp_size_usd is not None:
        decision.chosen_sizing = "hrp"
    else:
        decision.chosen_sizing = "naive"
        if hrp_enabled and decision.hrp_size_usd is None:
            decision.meta["fallback_reason"] = "hrp_unavailable"

    if persist_log:
        _persist_sizing_log(db, user_id, decision)

    return decision


def _persist_sizing_log(
    db: Session, user_id: Optional[int], d: SizingDecision
) -> None:
    try:
        db.execute(
            text(
                """
                INSERT INTO portfolio_sizing_log
                    (user_id, symbol, decision_at, account_equity_usd,
                     naive_size_usd, hrp_size_usd, hrp_weight, chosen_sizing,
                     n_active_positions, cov_condition_number,
                     hrp_cluster_label, meta)
                VALUES
                    (:uid, :sym, NOW(), :eq, :naive, :hrp, :w, :chosen,
                     :n, :cond, :cluster, :m)
                """
            ),
            {
                "uid": user_id,
                "sym": d.symbol,
                "eq": d.account_equity_usd,
                "naive": d.naive_size_usd,
                "hrp": d.hrp_size_usd,
                "w": d.hrp_weight,
                "chosen": d.chosen_sizing,
                "n": d.n_active_positions,
                "cond": d.cov_condition_number,
                "cluster": d.hrp_cluster_label,
                "m": json.dumps(d.meta) if d.meta else None,
            },
        )
        db.commit()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.debug("[hrp] persist sizing log failed: %s", e)

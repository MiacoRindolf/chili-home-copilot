"""Regime gate at the auto-trader entry funnel.

Reads ``trading_pattern_regime_performance_daily`` (the ledger built by
:mod:`pattern_regime_ledger`) plus the four regime-label feeds to
answer: *given this pattern is about to fire on this ticker right now,
how many of the four regime dimensions show confident-negative-EV?*

Multi-dimension consensus (2026-04-28 deep-audit FIX 10): the gate
consults all four dimensions written by the ledger:

* ``ticker_regime``      — per-ticker, from ``trading_ticker_regime_snapshots``
* ``breadth_regime``     — global, from ``trading_breadth_relstr_snapshots``
* ``cross_asset_regime`` — global, from ``trading_cross_asset_snapshots``
* ``vol_regime``         — global, from ``trading_vol_dispersion_snapshots``

For each dimension, look up the latest ledger row for
``(pattern_id, current_label_for_that_dimension)`` within
``max_age_days``. A dimension counts as a NEGATIVE vote when:

    has_confidence = TRUE
    AND n_trades   >= min_trades
    AND mean_pnl_pct <= 0

If at least ``min_negatives`` (default: 2) dimensions vote negative,
block the entry. Single-dimension noise (e.g., one ticker_regime cell
showing -0.1% mean PnL) no longer blocks alone — that was the previous
behaviour (single ``ticker_regime`` only).

This change is the gate-side prerequisite for accepting mig 199's
regime-conditional promotions: those patterns rely on the gate
correctly blocking their losing regimes on the dimensions other than
ticker_regime.

Default mode is **shadow** — the gate evaluates and logs the decision
but does NOT block trading. Operator flips ``chili_regime_gate_mode``
to ``live`` to start blocking.

Tunable::

    chili_regime_gate_enabled          = True
    chili_regime_gate_mode             = "shadow"   # or "live"
    chili_regime_gate_min_trades       = 5          # evidence threshold per dim
    chili_regime_gate_max_age_days     = 7          # ignore ledger rows older than this
    chili_regime_gate_min_negatives    = 2          # how many dimensions must agree to block
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DimensionVote:
    """Per-dimension evaluation result. ``negative=True`` means this
    dimension counts as a vote-to-block."""

    dimension: str
    label: str | None
    negative: bool
    n_trades: int | None
    hit_rate: float | None
    mean_pnl_pct: float | None
    reason: str  # 'negative' | 'positive_or_low_conf' | 'no_label' | 'no_evidence'


@dataclass(frozen=True)
class RegimeGateResult:
    blocked: bool
    mode: str
    reason: str
    pattern_id: int
    ticker: str
    # Aggregate (worst-dimension) view for backward compatibility:
    regime_label: str | None
    n_trades: int | None
    hit_rate: float | None
    mean_pnl_pct: float | None
    # Multi-dim breakdown (FIX 10): one entry per dimension consulted.
    votes: tuple[DimensionVote, ...] = ()
    n_negative: int = 0
    n_dimensions_with_evidence: int = 0

    def to_audit_str(self) -> str:
        votes_str = ",".join(
            f"{v.dimension}:{v.label or '?'}={'NEG' if v.negative else v.reason[:10]}"
            for v in self.votes
        )
        return (
            f"regime_gate[{self.mode}]:pid={self.pattern_id}"
            f":tk={self.ticker}:neg={self.n_negative}/{self.n_dimensions_with_evidence}"
            f":votes=[{votes_str}]:reason={self.reason}"
        )


def _settings_get(name: str, default: Any) -> Any:
    try:
        from ...config import settings
        return getattr(settings, name, default)
    except Exception:
        return default


# ── 4-dimension regime label feed ──────────────────────────────────────
# Each entry: (regime_dimension_name_in_ledger, sql_to_resolve_label,
#              params_extractor, source_table_for_logs)
# The 'global' dimensions (breadth/cross_asset/vol) take no ticker; the
# ticker_regime is per-ticker.

_TICKER_REGIME_SQL = """
    SELECT ticker_regime_label AS lbl
    FROM trading_ticker_regime_snapshots
    WHERE ticker = :ticker
    ORDER BY as_of_date DESC, computed_at DESC
    LIMIT 1
"""
_BREADTH_REGIME_SQL = """
    SELECT breadth_label AS lbl
    FROM trading_breadth_relstr_snapshots
    ORDER BY as_of_date DESC, computed_at DESC
    LIMIT 1
"""
_CROSS_ASSET_REGIME_SQL = """
    SELECT cross_asset_label AS lbl
    FROM trading_cross_asset_snapshots
    ORDER BY as_of_date DESC, computed_at DESC
    LIMIT 1
"""
_VOL_REGIME_SQL = """
    SELECT vol_regime_label AS lbl
    FROM trading_vol_dispersion_snapshots
    ORDER BY as_of_date DESC, computed_at DESC
    LIMIT 1
"""


def _current_regime_labels(sess: Session, ticker: str) -> dict[str, str | None]:
    """Resolve the current label for each of the four regime dimensions.

    Returns a dict keyed by ``regime_dimension`` value (matching the
    ``trading_pattern_regime_performance_daily.regime_dimension`` column):
    ``ticker_regime``, ``breadth_regime``, ``cross_asset_regime``,
    ``vol_regime``. Missing rows or query errors yield ``None`` for that
    dimension; callers must treat None as "no label, can't vote".
    """
    out: dict[str, str | None] = {}
    try:
        row = sess.execute(text(_TICKER_REGIME_SQL),
                           {"ticker": ticker.upper()}).fetchone()
        out["ticker_regime"] = str(row.lbl) if row and row.lbl is not None else None
    except Exception:
        out["ticker_regime"] = None
    for dim, sql in (
        ("breadth_regime", _BREADTH_REGIME_SQL),
        ("cross_asset_regime", _CROSS_ASSET_REGIME_SQL),
        ("vol_regime", _VOL_REGIME_SQL),
    ):
        try:
            row = sess.execute(text(sql)).fetchone()
            out[dim] = str(row.lbl) if row and row.lbl is not None else None
        except Exception:
            out[dim] = None
    return out


def _ledger_row(
    sess: Session, pattern_id: int, dimension: str, regime_label: str,
    *, max_age_days: int,
) -> Any | None:
    """Most recent ledger row for (pattern, dimension, regime) within
    max_age_days. Returns None if no row exists."""
    try:
        row = sess.execute(text("""
            SELECT n_trades, n_wins, hit_rate, mean_pnl_pct, expectancy,
                   has_confidence, as_of_date
            FROM trading_pattern_regime_performance_daily
            WHERE pattern_id = :pid
              AND regime_dimension = :dim
              AND regime_label = :lab
              AND as_of_date > CURRENT_DATE - make_interval(days => :max_age)
            ORDER BY as_of_date DESC, computed_at DESC
            LIMIT 1
        """), {
            "pid": int(pattern_id),
            "dim": str(dimension),
            "lab": str(regime_label),
            "max_age": int(max_age_days),
        }).fetchone()
        return row
    except Exception:
        return None


def evaluate_regime_gate(
    sess: Session,
    *,
    pattern_id: int | None,
    ticker: str,
) -> RegimeGateResult:
    """Decide whether this (pattern, ticker) entry should be blocked by regime
    evidence. Pure: doesn't write anything; caller decides whether to honor
    the block based on ``mode``.
    """
    enabled = bool(_settings_get("chili_regime_gate_enabled", True))
    mode = (str(_settings_get("chili_regime_gate_mode", "shadow") or "shadow")).strip().lower()
    min_n = int(_settings_get("chili_regime_gate_min_trades", 5))
    max_age = int(_settings_get("chili_regime_gate_max_age_days", 7))
    min_negatives = int(_settings_get("chili_regime_gate_min_negatives", 2))

    if not enabled:
        return RegimeGateResult(
            blocked=False, mode="disabled", reason="gate_disabled",
            pattern_id=int(pattern_id or 0), ticker=ticker,
            regime_label=None, n_trades=None, hit_rate=None, mean_pnl_pct=None,
        )

    if not pattern_id:
        return RegimeGateResult(
            blocked=False, mode=mode, reason="no_pattern_id",
            pattern_id=0, ticker=ticker,
            regime_label=None, n_trades=None, hit_rate=None, mean_pnl_pct=None,
        )

    labels = _current_regime_labels(sess, ticker)
    votes: list[DimensionVote] = []
    n_with_evidence = 0
    n_negative = 0
    worst_dim_view: dict[str, Any] | None = None

    for dim in ("ticker_regime", "breadth_regime", "cross_asset_regime", "vol_regime"):
        lbl = labels.get(dim)
        if lbl is None:
            votes.append(DimensionVote(
                dimension=dim, label=None, negative=False,
                n_trades=None, hit_rate=None, mean_pnl_pct=None,
                reason="no_label",
            ))
            continue
        row = _ledger_row(sess, pattern_id, dim, lbl, max_age_days=max_age)
        if row is None:
            votes.append(DimensionVote(
                dimension=dim, label=lbl, negative=False,
                n_trades=None, hit_rate=None, mean_pnl_pct=None,
                reason="no_evidence",
            ))
            continue
        n = int(row.n_trades or 0)
        hr = float(row.hit_rate) if row.hit_rate is not None else None
        mp = float(row.mean_pnl_pct) if row.mean_pnl_pct is not None else None
        has_conf = bool(row.has_confidence)
        is_negative = bool(has_conf and n >= min_n and (mp is not None and mp <= 0))
        n_with_evidence += 1
        if is_negative:
            n_negative += 1
            if worst_dim_view is None or (mp is not None and (worst_dim_view["mp"] is None or mp < worst_dim_view["mp"])):
                worst_dim_view = {
                    "dim": dim, "lbl": lbl, "n": n, "hr": hr, "mp": mp,
                }
        votes.append(DimensionVote(
            dimension=dim, label=lbl, negative=is_negative,
            n_trades=n, hit_rate=hr, mean_pnl_pct=mp,
            reason="negative" if is_negative else "positive_or_low_conf",
        ))

    # Aggregate (worst-dim) view for backward compat with existing audit
    # readers that expect single regime_label / n_trades / hit_rate / mp.
    if worst_dim_view is not None:
        agg_label = f"{worst_dim_view['dim']}={worst_dim_view['lbl']}"
        agg_n = worst_dim_view["n"]
        agg_hr = worst_dim_view["hr"]
        agg_mp = worst_dim_view["mp"]
    else:
        agg_label = None
        agg_n = None
        agg_hr = None
        agg_mp = None

    if n_negative >= min_negatives:
        reason = (
            f"negative_ev_consensus:n_neg={n_negative}/{n_with_evidence}"
            f":worst_dim={worst_dim_view['dim'] if worst_dim_view else 'n/a'}"
        )
        result = RegimeGateResult(
            blocked=True, mode=mode, reason=reason,
            pattern_id=int(pattern_id), ticker=ticker,
            regime_label=agg_label, n_trades=agg_n,
            hit_rate=agg_hr, mean_pnl_pct=agg_mp,
            votes=tuple(votes),
            n_negative=n_negative,
            n_dimensions_with_evidence=n_with_evidence,
        )
        logger.info("[regime_gate] %s %s",
                    "BLOCK" if mode == "live" else "WOULD-BLOCK (shadow)",
                    result.to_audit_str())
        return result

    if n_with_evidence == 0:
        reason = "no_ledger_evidence"
    else:
        reason = f"insufficient_negative_consensus:n_neg={n_negative}/{n_with_evidence}"

    return RegimeGateResult(
        blocked=False, mode=mode, reason=reason,
        pattern_id=int(pattern_id), ticker=ticker,
        regime_label=agg_label, n_trades=agg_n,
        hit_rate=agg_hr, mean_pnl_pct=agg_mp,
        votes=tuple(votes),
        n_negative=n_negative,
        n_dimensions_with_evidence=n_with_evidence,
    )


def regime_gate_blocks_entry(sess: Session, *, pattern_id: int | None, ticker: str) -> tuple[bool, str]:
    """Convenience for the auto-trader: returns ``(should_block, reason_str)``
    where ``should_block`` honors the live/shadow mode."""
    result = evaluate_regime_gate(sess, pattern_id=pattern_id, ticker=ticker)
    if result.mode == "live" and result.blocked:
        return True, result.reason
    return False, result.reason

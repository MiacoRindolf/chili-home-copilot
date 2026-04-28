"""Regime gate at the auto-trader entry funnel.

Reads ``trading_pattern_regime_performance_daily`` (the ledger built by
:mod:`pattern_regime_ledger`) and ``trading_ticker_regime_snapshots``
(the per-ticker regime label feed) to answer: *given this pattern is
about to fire on this ticker right now, has this pattern historically
made money in this ticker's CURRENT regime?*

If the most recent ledger row for ``(pattern_id, current_regime_label)``
has ``has_confidence=true`` AND ``mean_pnl_pct <= 0``, block the entry.

Default mode is **shadow** — the gate evaluates and logs the decision
but does NOT block trading. Once we've watched a few days of shadow
logs and confirmed the gate is calling things correctly, the operator
flips ``chili_regime_gate_mode`` to ``live`` to start blocking.

Tunable::

    chili_regime_gate_enabled          = True
    chili_regime_gate_mode             = "shadow"   # or "live"
    chili_regime_gate_min_trades       = 5          # evidence threshold
    chili_regime_gate_max_age_days     = 7          # ignore ledger rows older than this
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RegimeGateResult:
    blocked: bool
    mode: str
    reason: str
    pattern_id: int
    ticker: str
    regime_label: str | None
    n_trades: int | None
    hit_rate: float | None
    mean_pnl_pct: float | None

    def to_audit_str(self) -> str:
        return (
            f"regime_gate[{self.mode}]:pid={self.pattern_id}"
            f":tk={self.ticker}:reg={self.regime_label}"
            f":n={self.n_trades}:hr={self.hit_rate}:mp={self.mean_pnl_pct}"
            f":reason={self.reason}"
        )


def _settings_get(name: str, default: Any) -> Any:
    try:
        from ...config import settings
        return getattr(settings, name, default)
    except Exception:
        return default


def _current_ticker_regime(sess: Session, ticker: str) -> str | None:
    """Most recent ``ticker_regime_label`` for the given ticker, or None."""
    try:
        row = sess.execute(text("""
            SELECT ticker_regime_label
            FROM trading_ticker_regime_snapshots
            WHERE ticker = :t
            ORDER BY as_of_date DESC, computed_at DESC
            LIMIT 1
        """), {"t": ticker.upper()}).fetchone()
        return str(row.ticker_regime_label) if row else None
    except Exception:
        return None


def _ledger_row(
    sess: Session, pattern_id: int, regime_label: str, *, max_age_days: int,
) -> Any | None:
    """Most recent ledger row for (pattern, regime) within max_age_days."""
    try:
        row = sess.execute(text("""
            SELECT n_trades, n_wins, hit_rate, mean_pnl_pct, expectancy,
                   has_confidence, as_of_date
            FROM trading_pattern_regime_performance_daily
            WHERE pattern_id = :pid
              AND regime_dimension = 'ticker_regime'
              AND regime_label = :lab
              AND as_of_date > CURRENT_DATE - make_interval(days => :max_age)
            ORDER BY as_of_date DESC, computed_at DESC
            LIMIT 1
        """), {
            "pid": int(pattern_id),
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

    regime = _current_ticker_regime(sess, ticker)
    if regime is None:
        # No regime data for this ticker (unknown / not in coverage). Don't
        # block — we have no basis to.
        return RegimeGateResult(
            blocked=False, mode=mode, reason="ticker_regime_unknown",
            pattern_id=int(pattern_id), ticker=ticker,
            regime_label=None, n_trades=None, hit_rate=None, mean_pnl_pct=None,
        )

    row = _ledger_row(sess, pattern_id, regime, max_age_days=max_age)
    if row is None:
        # No ledger evidence for this (pattern, regime). Don't block — the
        # pattern may simply not have traded in this regime yet.
        return RegimeGateResult(
            blocked=False, mode=mode, reason="no_ledger_evidence",
            pattern_id=int(pattern_id), ticker=ticker,
            regime_label=regime, n_trades=None, hit_rate=None, mean_pnl_pct=None,
        )

    n = int(row.n_trades or 0)
    hr = float(row.hit_rate) if row.hit_rate is not None else None
    mp = float(row.mean_pnl_pct) if row.mean_pnl_pct is not None else None
    has_conf = bool(row.has_confidence)

    # Three explicit cases:
    # 1. confident + negative EV  -> BLOCK
    # 2. confident + positive EV  -> ALLOW (fast-path)
    # 3. low confidence           -> ALLOW (need more evidence)
    if has_conf and n >= min_n and (mp is not None and mp <= 0):
        hr_str = f"{hr:.3f}" if hr is not None else "n/a"
        reason = f"negative_ev_in_regime:n={n}:hr={hr_str}:mp={mp:.3f}"
        result = RegimeGateResult(
            blocked=True, mode=mode, reason=reason,
            pattern_id=int(pattern_id), ticker=ticker,
            regime_label=regime, n_trades=n, hit_rate=hr, mean_pnl_pct=mp,
        )
        # Always log the would-be-block decision so shadow mode is auditable.
        logger.info("[regime_gate] %s %s",
                    "BLOCK" if mode == "live" else "WOULD-BLOCK (shadow)",
                    result.to_audit_str())
        return result

    return RegimeGateResult(
        blocked=False, mode=mode, reason="positive_or_low_confidence",
        pattern_id=int(pattern_id), ticker=ticker,
        regime_label=regime, n_trades=n, hit_rate=hr, mean_pnl_pct=mp,
    )


def regime_gate_blocks_entry(sess: Session, *, pattern_id: int | None, ticker: str) -> tuple[bool, str]:
    """Convenience for the auto-trader: returns ``(should_block, reason_str)``
    where ``should_block`` honors the live/shadow mode."""
    result = evaluate_regime_gate(sess, pattern_id=pattern_id, ticker=ticker)
    if result.mode == "live" and result.blocked:
        return True, result.reason
    return False, result.reason

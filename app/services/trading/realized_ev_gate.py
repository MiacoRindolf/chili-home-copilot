"""Realized expected-value (EV) promotion gate.

Independent of CPCV. The CPCV gate already in :mod:`promotion_gate` checks
risk-adjusted, OOS-validated metrics (DSR, PBO, path Sharpes). Those are
necessary but not sufficient: a pattern can pass CPCV (positive Sharpe
on OOS folds) and still be a money-loser end-to-end if the live exit
slippage / commissions / regime drift erode the edge after promotion.

Pattern 1047 (rsi_bullish_divergence_reversal_breakout [No-BOS-breakout])
is the textbook failure: it was rescued twice via migrations 168 and 170
on the strength of CPCV evidence, yet its realized stats on 2026-04-28
were ``avg_return_pct = -3.97`` with 3 live trades, 1 win, -$29.93 PnL.

This gate adds a simple, brutally honest check::

    Promotion blocked unless
      avg_return_pct > 0   AND   win_rate > 0   AND   trade_count >= MIN

The gate is read against ``ScanPattern`` columns directly (now reliable
after the 2026-04-28 EWMA drop — :mod:`learning.py` writes raw
``wins/total`` instead of smoothing). No re-running CPCV needed.

EV ≡ ``WR × avg_win - (1 - WR) × avg_loss`` is mathematically equivalent
to the mean of trade returns, so checking ``avg_return_pct > 0`` IS the
EV check. We additionally require ``trade_count >= chili_realized_ev_min_trades``
(default 5) so we don't fail patterns on a 1-trade fluke.

Tunable via :class:`~app.config.Settings`::

    chili_realized_ev_gate_enabled        = True   # kill-switch
    chili_realized_ev_min_trades          = 5      # min realized n
    chili_realized_ev_min_avg_return_pct  = 0.0    # threshold (pct)
    chili_realized_ev_min_win_rate        = 0.0    # threshold (fraction)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvGateResult:
    passed: bool
    reasons: tuple[str, ...]
    snapshot: dict[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reasons": list(self.reasons),
            "snapshot": dict(self.snapshot),
        }


def _settings_get(name: str, default: Any) -> Any:
    try:
        from ...config import settings
        return getattr(settings, name, default)
    except Exception:
        return default


def evaluate_realized_ev(scan_pattern: Any) -> EvGateResult:
    """Check the pattern's realized stats against the EV gate.

    Reads ``scan_pattern.win_rate`` (fraction in [0,1] post-migration 193),
    ``avg_return_pct`` (in %), and ``trade_count``. None of these are
    re-derived — we trust the values written by the realized-PnL update
    paths in :mod:`learning.py`.

    Returns :class:`EvGateResult` with the snapshot of inputs (for
    auditability) and the pass/fail reasons. The snapshot is what gets
    persisted alongside any blocking decision.
    """
    enabled = bool(_settings_get("chili_realized_ev_gate_enabled", True))
    min_n = int(_settings_get("chili_realized_ev_min_trades", 5))
    min_ret = float(_settings_get("chili_realized_ev_min_avg_return_pct", 0.0))
    min_wr = float(_settings_get("chili_realized_ev_min_win_rate", 0.0))

    snapshot = {
        "win_rate": getattr(scan_pattern, "win_rate", None),
        "avg_return_pct": getattr(scan_pattern, "avg_return_pct", None),
        "trade_count": getattr(scan_pattern, "trade_count", None),
        "min_trades_required": min_n,
        "min_avg_return_pct_required": min_ret,
        "min_win_rate_required": min_wr,
        "enabled": enabled,
    }

    if not enabled:
        # Kill-switch off: pass-through but record snapshot so audits can
        # see what would have happened.
        return EvGateResult(passed=True, reasons=("gate_disabled",), snapshot=snapshot)

    reasons: list[str] = []
    n = int(getattr(scan_pattern, "trade_count", 0) or 0)
    wr = getattr(scan_pattern, "win_rate", None)
    ret = getattr(scan_pattern, "avg_return_pct", None)

    # Sample-size requirement: never promote on a 1-trade fluke.
    if n < min_n:
        reasons.append(f"realized_n_below_min:{n}<{min_n}")

    # Win-rate floor (default 0.0 — any non-zero WR passes).
    if wr is None:
        reasons.append("realized_win_rate_missing")
    else:
        try:
            if float(wr) <= min_wr:
                reasons.append(f"realized_win_rate_not_positive:{float(wr):.4f}<={min_wr}")
        except (TypeError, ValueError):
            reasons.append(f"realized_win_rate_not_numeric:{wr!r}")

    # Average return floor (default 0.0). This is the actual EV check —
    # mean(trade_returns) is mathematically EV.
    if ret is None:
        reasons.append("realized_avg_return_missing")
    else:
        try:
            if float(ret) <= min_ret:
                reasons.append(f"realized_avg_return_not_positive:{float(ret):.4f}<={min_ret}")
        except (TypeError, ValueError):
            reasons.append(f"realized_avg_return_not_numeric:{ret!r}")

    passed = len(reasons) == 0
    return EvGateResult(passed=passed, reasons=tuple(reasons), snapshot=snapshot)


def check_realized_ev_blocking(scan_pattern: Any) -> tuple[bool, list[str], dict[str, Any]]:
    """Convenience wrapper: ``(blocked, reasons, snapshot)``.

    ``blocked`` is True when promotion should be blocked. Used by call
    sites that already speak the "blocked" / "reasons" idiom (e.g.
    :func:`promotion_gate.finalize_promotion_with_cpcv`).
    """
    result = evaluate_realized_ev(scan_pattern)
    if not result.passed:
        logger.info(
            "[realized_ev_gate] BLOCK pattern_id=%s reasons=%s snapshot=%s",
            getattr(scan_pattern, "id", None),
            list(result.reasons),
            result.snapshot,
        )
    return (not result.passed), list(result.reasons), result.snapshot

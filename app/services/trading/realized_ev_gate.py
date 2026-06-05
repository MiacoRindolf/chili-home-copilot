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
    chili_realized_ev_gate_allow_raw_fallback = True
"""
from __future__ import annotations

import logging
import math
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


def _safe_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out) or out < 0.0 or out != int(out):
        return None
    return int(out)


def _safe_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _win_rate_or_none(value: Any) -> float | None:
    out = _safe_float(value)
    if out is None or out < 0.0 or out > 1.0:
        return None
    return out


def _positive_int_setting(name: str, default: int) -> int:
    value = _settings_get(name, default)
    out = _safe_int(value)
    if out is None or out <= 0:
        return int(default)
    return out


def _finite_float_setting(name: str, default: float) -> float:
    out = _safe_float(_settings_get(name, default))
    return float(default) if out is None else out


def _win_rate_setting(name: str, default: float) -> float:
    out = _win_rate_or_none(_settings_get(name, default))
    return float(default) if out is None else out


def evaluate_realized_ev(scan_pattern: Any) -> EvGateResult:
    """Check the pattern's realized stats against the EV gate.

    Reads ``corrected_*`` first (the authoritative live-trade columns owned
    by :func:`learning.update_pattern_stats_from_closed_trades`), then the
    legacy ``{win_rate, avg_return_pct, trade_count}`` columns. When that
    sample is missing or below the required minimum, the gate may use
    ``raw_realized_*`` as a bootstrap fallback. That raw family is refreshed
    from live trades plus qualified AutoTrader paper/shadow outcomes, so
    paper evidence can graduate a pattern without letting it override clear
    live/corrected losses.

    Returns :class:`EvGateResult` with the snapshot of inputs (for
    auditability) and the pass/fail reasons. The snapshot is what gets
    persisted alongside any blocking decision.
    """
    from .pattern_stats_accessor import get_corrected_pattern_stats

    enabled = bool(_settings_get("chili_realized_ev_gate_enabled", True))
    min_n = _positive_int_setting("chili_realized_ev_min_trades", 5)
    min_ret = _finite_float_setting("chili_realized_ev_min_avg_return_pct", 0.0)
    min_wr = _win_rate_setting("chili_realized_ev_min_win_rate", 0.0)
    allow_raw_fallback = bool(
        _settings_get("chili_realized_ev_gate_allow_raw_fallback", True)
    )

    stats = get_corrected_pattern_stats(scan_pattern)
    raw_n = _safe_int(getattr(scan_pattern, "raw_realized_trade_count", None))
    raw_wr = _win_rate_or_none(getattr(scan_pattern, "raw_realized_win_rate", None))
    raw_ret = _safe_float(getattr(scan_pattern, "raw_realized_avg_return_pct", None))

    n = int(stats.trade_count or 0)
    wr = stats.win_rate
    ret = stats.avg_return_pct
    stats_source = "corrected_or_legacy"
    raw_fallback_blocked_reason: str | None = None

    # f-realized-ev-legacy-not-authoritative (2026-06-05): ONLY clean
    # ``corrected_*`` evidence may pre-empt the clean ``raw_realized_*``
    # fallback. The legacy ``{trade_count, win_rate, avg_return_pct}`` columns
    # are conflated -- they are overwritten by mining
    # (``ensure_mined_scan_pattern``), backtests (``backtest_queue``), AND the
    # realized writer, and pre-fix they also absorbed DIRTY reconcile /
    # sync-gone / position-gone placeholder exits. So a legacy-SOURCED sample
    # must not be treated as authoritative realized evidence: it can neither
    # be "sufficient" on its own nor assert a "clear live loss" that blocks the
    # raw fallback. Pattern 1246 is the canonical case -- corrected_*=NULL (all
    # its closed trades are dirty, so the cleaned writer can't compute a
    # corrected stat), legacy avg=-0.14% (7 dirty sync-gone rows whose real
    # fills were positive), raw_realized=+2.66% (12 clean rows). The gate must
    # consult the clean +2.66% signal, not block on the polluted -0.14%.
    # corrected_* remains the authoritative LIVE-loss signal (a genuine clean
    # corrected loss still takes precedence over paper/raw -- safety preserved).
    sample_from_corrected = (
        stats.source_trade_count == "corrected"
        and stats.source_avg_return_pct == "corrected"
    )

    corrected_clear_loss = False
    if n > 0 and sample_from_corrected:
        try:
            corrected_clear_loss = (
                (ret is not None and float(ret) <= min_ret)
                or (wr is not None and float(wr) <= min_wr)
            )
        except (TypeError, ValueError):
            corrected_clear_loss = False

    if not allow_raw_fallback:
        raw_fallback_blocked_reason = "disabled"
    elif raw_n is None or raw_n < min_n:
        raw_fallback_blocked_reason = f"raw_realized_n_below_min:{int(raw_n or 0)}<{min_n}"
    elif n >= min_n and sample_from_corrected:
        raw_fallback_blocked_reason = f"corrected_sample_sufficient:{n}>={min_n}"
    elif corrected_clear_loss:
        raw_fallback_blocked_reason = "corrected_live_loss_takes_precedence"
    else:
        n = int(raw_n)
        wr = raw_wr
        ret = raw_ret
        stats_source = "raw_realized_fallback"
        raw_fallback_blocked_reason = None

    snapshot = {
        "win_rate": wr,
        "avg_return_pct": ret,
        "trade_count": n,
        "stats_source": stats_source,
        "stats_source_win_rate": stats.source_win_rate,
        "stats_source_avg_return_pct": stats.source_avg_return_pct,
        "stats_source_trade_count": stats.source_trade_count,
        "corrected_or_legacy_win_rate": stats.win_rate,
        "corrected_or_legacy_avg_return_pct": stats.avg_return_pct,
        "corrected_or_legacy_trade_count": stats.trade_count,
        "raw_realized_win_rate": raw_wr,
        "raw_realized_avg_return_pct": raw_ret,
        "raw_realized_trade_count": raw_n,
        "raw_realized_fallback_allowed": allow_raw_fallback,
        "raw_realized_fallback_used": stats_source == "raw_realized_fallback",
        "raw_realized_fallback_blocked_reason": raw_fallback_blocked_reason,
        "sample_from_corrected": sample_from_corrected,
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

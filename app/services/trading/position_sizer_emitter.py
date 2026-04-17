"""Phase H - thin shadow-emitter used by the 4 legacy sizer call-sites.

The emitter exists so that :func:`alerts._compute_position_size`,
:func:`portfolio_risk.size_position_kelly`,
:func:`portfolio_risk.size_with_drawdown_scaling`, and the
paper-trading entry path can all call a *single* helper without
having to know about :mod:`position_sizer_model`, correlation /
portfolio budgets, or the write API. The helper is:

* **Off-mode short-circuited.** When ``brain_position_sizer_mode``
  is ``"off"`` the call returns ``None`` immediately - there is no
  DB query and no Kelly math.
* **Shadow-only.** In Phase H the emitter never alters the legacy
  sizer's decision. Callers always keep their own notional/quantity.
* **Defensive.** Every exception is swallowed and logged; Phase H
  cannot break legacy sizing, by design.
* **NetEdgeRanker-aware.** When a caller already scored the signal
  via :mod:`net_edge_ranker` it can pass the ``NetEdgeScore`` in and
  save a redundant calibration. Otherwise the emitter tries to
  score on demand when the ranker itself is active; failing that,
  it falls back to simple geometric inputs (target/stop distances)
  so shadow coverage does not drop to zero while Phase E is still
  rolling out.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy.orm import Session

from ...config import settings
from .correlation_budget import (
    compute_correlation_budget,
    compute_portfolio_budget,
    is_crypto_symbol,
)
from .position_sizer_model import PositionSizerInput
from .position_sizer_writer import (
    LegacySizing,
    WriteResult,
    mode_is_active,
    write_proposal,
)

logger = logging.getLogger(__name__)

__all__ = [
    "EmitterSignal",
    "emit_shadow_proposal",
]


# ---------------------------------------------------------------------------
# Caller-facing signal shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmitterSignal:
    """Everything a call-site needs to hand to the Phase H shadow sizer.

    The fields are intentionally *broad* - not every legacy call-site
    has pattern_id or regime, but the emitter will degrade gracefully.
    """

    source: str  # 'alerts' | 'portfolio_risk.kelly' | 'portfolio_risk.dd' | 'paper_trading'
    ticker: str
    direction: str  # 'long' | 'short'
    entry_price: float
    stop_price: float
    capital: float
    target_price: Optional[float] = None
    asset_class: Optional[str] = None
    user_id: Optional[int] = None
    pattern_id: Optional[int] = None
    regime: Optional[str] = None
    # Confidence the caller has in the signal; used as a fallback
    # calibrated_prob when the NetEdgeRanker is off.
    confidence: Optional[float] = None


# ---------------------------------------------------------------------------
# Input derivation
# ---------------------------------------------------------------------------


def _infer_asset_class(ticker: str, explicit: Optional[str]) -> str:
    if explicit:
        return explicit.strip().lower()
    return "crypto" if is_crypto_symbol(ticker) else "equity"


def _clamp01(value: float, lo: float = 0.01, hi: float = 0.99) -> float:
    try:
        v = float(value)
    except Exception:
        return 0.55
    if v < 0:
        v = 0.0
    if v > 1:
        # Treat 0-100 style confidence as a percentage.
        v = v / 100.0 if v <= 100.0 else 1.0
    return max(lo, min(hi, v))


def _loss_per_unit(entry: float, stop: float) -> float:
    if entry <= 0 or stop <= 0:
        return 0.0
    return max(1e-6, abs(entry - stop) / entry)


def _payoff_fraction(entry: float, target: Optional[float], loss: float) -> float:
    """Estimate win-leg payoff as fraction of notional.

    When the caller has an explicit target, use it. Otherwise assume
    a 1.5R win (symmetric-ish R-multiple) so the shadow log is not
    biased against signals that simply forgot to pass ``target``.
    """
    if target and target > 0 and entry > 0:
        try:
            return max(0.0, float(target - entry) / entry)
        except Exception:
            pass
    return max(0.0, loss * 1.5)


def _try_netedge_score(db: Session, signal: EmitterSignal) -> Optional[Any]:
    """Best-effort NetEdgeRanker score. Never raises."""
    try:
        from . import net_edge_ranker as _net  # type: ignore
        if not _net.mode_is_active():
            return None
        ctx = _net.NetEdgeSignalContext(
            ticker=signal.ticker,
            asset_class=_infer_asset_class(signal.ticker, signal.asset_class),
            scan_pattern_id=signal.pattern_id,
            raw_prob=float(signal.confidence or 0.55),
            entry_price=float(signal.entry_price),
            stop_price=float(signal.stop_price),
            target_price=float(signal.target_price) if signal.target_price else None,
            regime=signal.regime,
            timeframe=None,
            heuristic_score=None,
        )
        return _net.score(db, ctx)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("[position_sizer_emitter] net_edge score failed: %s", exc)
        return None


def _build_input(
    signal: EmitterSignal,
    net_edge_score: Optional[Any],
) -> PositionSizerInput:
    """Translate the emitter-facing signal + optional ranker score into the pure sizer input."""
    asset_class = _infer_asset_class(signal.ticker, signal.asset_class)
    if net_edge_score is not None:
        calibrated_prob = float(getattr(net_edge_score, "calibrated_prob", signal.confidence or 0.55))
        payoff_fraction = float(getattr(net_edge_score, "expected_payoff", 0.0))
        loss_per_unit = float(getattr(net_edge_score, "loss_per_unit", 0.0))
        costs = getattr(net_edge_score, "costs", None)
        cost_fraction = float(getattr(costs, "total", 0.0)) if costs is not None else 0.0
        expected_net_pnl = float(getattr(net_edge_score, "expected_net_pnl", 0.0))
    else:
        loss = _loss_per_unit(signal.entry_price, signal.stop_price)
        calibrated_prob = _clamp01(signal.confidence if signal.confidence is not None else 0.55)
        payoff_fraction = _payoff_fraction(signal.entry_price, signal.target_price, loss)
        loss_per_unit = loss
        cost_fraction = 0.0
        expected_net_pnl = calibrated_prob * payoff_fraction - (1.0 - calibrated_prob) * loss_per_unit

    qty_rounding = "decimal" if asset_class == "crypto" else "int"
    return PositionSizerInput(
        ticker=signal.ticker,
        direction=(signal.direction or "long").strip().lower() or "long",
        asset_class=asset_class,
        entry_price=float(signal.entry_price),
        stop_price=float(signal.stop_price),
        capital=float(signal.capital),
        calibrated_prob=calibrated_prob,
        payoff_fraction=payoff_fraction,
        loss_per_unit=loss_per_unit,
        cost_fraction=cost_fraction,
        expected_net_pnl=expected_net_pnl,
        target_price=float(signal.target_price) if signal.target_price else None,
        regime=signal.regime,
        pattern_id=signal.pattern_id,
        user_id=signal.user_id,
        kelly_scale=float(getattr(settings, "brain_position_sizer_kelly_scale", 0.25)),
        max_risk_pct=float(getattr(settings, "brain_position_sizer_max_risk_pct", 2.0)),
        equity_bucket_cap_pct=float(
            getattr(settings, "brain_position_sizer_equity_bucket_cap_pct", 15.0),
        ),
        crypto_bucket_cap_pct=float(
            getattr(settings, "brain_position_sizer_crypto_bucket_cap_pct", 10.0),
        ),
        single_ticker_cap_pct=float(
            getattr(settings, "brain_position_sizer_single_ticker_cap_pct", 7.5),
        ),
        qty_rounding=qty_rounding,
    )


# ---------------------------------------------------------------------------
# Public emitter
# ---------------------------------------------------------------------------


def emit_shadow_proposal(
    db: Session,
    *,
    signal: EmitterSignal,
    legacy: LegacySizing,
    net_edge_score: Optional[Any] = None,
) -> Optional[WriteResult]:
    """Emit a Phase H shadow sizing proposal, defensively.

    Contract:
      * Returns ``None`` when Phase H is off, when inputs are invalid,
        or when anything at all goes wrong. Callers MUST NOT rely on
        the return value for trading decisions.
      * Never raises.
    """
    try:
        if not mode_is_active():
            return None
        if signal.entry_price is None or signal.stop_price is None:
            return None
        if float(signal.entry_price) <= 0 or float(signal.stop_price) <= 0:
            return None
        if float(signal.capital) <= 0:
            return None

        score_obj = net_edge_score or _try_netedge_score(db, signal)
        inp = _build_input(signal, score_obj)

        try:
            corr = compute_correlation_budget(
                db,
                user_id=signal.user_id,
                ticker=signal.ticker,
                capital=signal.capital,
                asset_class=inp.asset_class,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("[position_sizer_emitter] correlation budget failed: %s", exc)
            corr = None

        try:
            port = compute_portfolio_budget(
                db,
                user_id=signal.user_id,
                ticker=signal.ticker,
                capital=signal.capital,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("[position_sizer_emitter] portfolio budget failed: %s", exc)
            port = None

        result = write_proposal(
            db,
            inp=inp,
            correlation=corr,
            portfolio=port,
            source=signal.source,
            legacy=legacy,
        )

        try:
            from . import pattern_regime_tilt_service as _tilt
            if _tilt.mode_is_active() and result is not None and signal.pattern_id:
                _tilt.evaluate_tilt_for_proposal(
                    db,
                    pattern_id=int(signal.pattern_id),
                    ticker=signal.ticker,
                    source=signal.source,
                    baseline_notional=float(result.proposed_notional),
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(
                "[position_sizer_emitter] pattern_regime tilt shadow failed: %s",
                exc,
            )

        return result
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[position_sizer_emitter] emit failed for %s/%s: %s",
            signal.source,
            signal.ticker,
            exc,
        )
        return None

"""Pure, DB-free logic for the canonical Phase H position sizer.

Responsibilities
----------------

Given a :class:`NetEdgeScore`-shaped signal (calibrated probability,
payoff fraction, cost fraction, loss-per-unit) plus the current
portfolio / correlation budget, compute a proposed notional and
quantity under a Kelly-derived risk envelope.

Design notes
------------

* **Stateless.** No DB session, no broker, no network. All inputs
  are provided explicitly by the caller. This keeps the function
  cheap to unit-test and free of hidden coupling.
* **Kelly denominator is the stop.** We derive the per-trade
  risk-of-capital from the ranker's ``loss_per_unit`` (fraction of
  notional between entry and stop). This is the same denominator
  :mod:`.bracket_intent` persists for live brackets, so Phase H and
  Phase G stay numerically consistent.
* **Quarter-Kelly by default** (``kelly_scale = 0.25``). The caller
  can pass a different scale (e.g. Phase I's risk-dial multiplier).
* **Caps are hard, not soft.** Single-ticker cap and correlation-
  bucket cap trim the proposal; they do **not** veto it outright
  unless the resulting size rounds to zero.
* **Negative edge -> zero size.** If ``expected_net_pnl <= 0`` the
  sizer refuses to propose a position. The caller's legacy sizer
  still returns whatever it always has; Phase H is shadow-only.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "PositionSizerInput",
    "CorrelationBudget",
    "PortfolioBudget",
    "PositionSizerOutput",
    "compute_proposal",
    "compute_proposal_id",
]


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PositionSizerInput:
    """Everything the pure sizer needs for one proposal.

    Attributes
    ----------
    ticker, direction, asset_class, regime, pattern_id, user_id
        Context / keys passed through to the log row.
    entry_price, stop_price, target_price
        Prices the sizer operates on. ``entry_price`` and
        ``stop_price`` must both be positive and directionally valid:
        long stops sit below entry, short stops sit above entry.
    capital
        Total capital (buying power) the sizer may allocate against.
    calibrated_prob
        NetEdgeRanker ``NetEdgeScore.calibrated_prob`` (probability
        of a winning trade after calibration). Must be a unit-interval
        probability, not a percentage.
    payoff_fraction
        NetEdgeRanker ``NetEdgeScore.expected_payoff`` - fraction of
        notional realized on a winning trade (target - entry) / entry.
    loss_per_unit
        NetEdgeRanker ``NetEdgeScore.loss_per_unit`` - fraction of
        notional lost on a stop-out (entry - stop) / entry.
    cost_fraction
        NetEdgeRanker ``NetEdgeScore.costs.total`` - round-trip
        cost-of-notional.
    expected_net_pnl
        Optional; recomputed if not provided. Kept as a separate
        field so the sizer log preserves the exact value the caller
        actually saw from the ranker.
    kelly_scale
        Multiplier applied to raw Kelly. Default 0.25 (quarter-Kelly).
    max_risk_pct
        Hard cap on fraction of capital risked per trade, in percent.
        Default 2.0.
    equity_bucket_cap_pct
    crypto_bucket_cap_pct
        Hard caps on total notional per correlation bucket, in
        percent of ``capital``.
    single_ticker_cap_pct
        Hard cap on notional for this one ticker, in percent of
        ``capital``.
    qty_rounding
        If ``"int"`` the proposed quantity is rounded to a whole
        share count (equities). If ``"decimal"`` it is left as a
        float (crypto, fractional shares).
    unit_multiplier
        Notional multiplier per one quoted unit. Equities/crypto use
        ``1``; listed option contracts use ``100`` because entry/stop
        are option premiums but quantity is whole contracts.
    """

    ticker: str
    direction: str  # 'long' | 'short'
    asset_class: str  # 'equity' | 'crypto' | 'stock'
    entry_price: float
    stop_price: float
    capital: float
    calibrated_prob: float
    payoff_fraction: float
    loss_per_unit: float
    cost_fraction: float = 0.0
    expected_net_pnl: float | None = None
    target_price: float | None = None
    regime: str | None = None
    pattern_id: int | None = None
    user_id: int | None = None
    kelly_scale: float = 0.25
    max_risk_pct: float = 2.0
    equity_bucket_cap_pct: float = 15.0
    crypto_bucket_cap_pct: float = 10.0
    single_ticker_cap_pct: float = 7.5
    qty_rounding: Literal["int", "decimal"] = "int"
    correlation_bucket: str | None = None
    unit_multiplier: float = 1.0


@dataclass(frozen=True)
class CorrelationBudget:
    """Exposure currently deployed in this proposal's correlation bucket.

    ``bucket`` is the canonical correlation key (see
    :func:`correlation_budget.bucket_for`). ``open_notional`` is the
    summed notional of open trades already sharing that bucket.
    ``max_bucket_notional`` is ``equity_bucket_cap_pct`` or
    ``crypto_bucket_cap_pct`` of capital, pre-computed by the caller.
    """

    bucket: str
    open_notional: float
    max_bucket_notional: float


@dataclass(frozen=True)
class PortfolioBudget:
    """Portfolio-level exposure state passed into the pure sizer.

    Used for the single-ticker notional cap and the total-deployed
    safety floor. The covariance allocator is deliberately **out of
    scope for Phase H** (it is Phase I).
    """

    total_capital: float
    deployed_notional: float
    max_total_notional: float
    ticker_open_notional: float = 0.0


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PositionSizerOutput:
    """Shadow sizing proposal. Never directly applied in Phase H."""

    proposal_id: str
    proposed_notional: float
    proposed_quantity: float
    proposed_risk_pct: float
    kelly_fraction: float
    kelly_scaled_fraction: float
    expected_net_pnl: float
    correlation_cap_triggered: bool
    correlation_bucket: str | None
    max_bucket_notional: float | None
    notional_cap_triggered: bool
    reasoning: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Deterministic proposal-id
# ---------------------------------------------------------------------------


def compute_proposal_id(
    *, source: str, ticker: str, user_id: int | None, entry_price: float, stop_price: float,
    calibrated_prob: float, payoff_fraction: float, loss_per_unit: float,
) -> str:
    """Deterministic proposal id.

    Stable across emitter retries within the same call-site inputs, so
    callers that re-propose the same signal produce the same
    ``proposal_id`` (the DB log is append-only; diagnostics aggregate
    by the latest row).
    """
    blob = json.dumps(
        {
            "src": source,
            "t": ticker,
            "u": user_id,
            "e": round(float(entry_price), 6),
            "s": round(float(stop_price), 6),
            "p": round(float(calibrated_prob), 6),
            "py": round(float(payoff_fraction), 6),
            "lp": round(float(loss_per_unit), 6),
        },
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_crypto(asset_class: str) -> bool:
    return (asset_class or "").strip().lower() == "crypto"


def _round_qty(
    notional: float,
    price: float,
    qty_rounding: str,
    unit_multiplier: float = 1.0,
) -> float:
    unit_notional = price * unit_multiplier
    if unit_notional <= 0 or notional <= 0:
        return 0.0
    raw = notional / unit_notional
    if qty_rounding == "int":
        return float(int(raw))
    # Keep 8 decimals for crypto-style precision without float noise.
    return round(raw, 8)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _non_negative_finite(value: Any, default: float = 0.0) -> float:
    parsed = _finite_float(value)
    if parsed is None:
        return default
    return max(0.0, parsed)


def _normalize_direction(value: Any) -> str | None:
    raw = str(value or "").strip().lower()
    if raw in {"long", "buy"}:
        return "long"
    if raw in {"short", "sell"}:
        return "short"
    return None


def _directional_loss_fraction(entry: float, stop: float, direction: str) -> float:
    if direction == "short":
        return max(0.0, (stop - entry) / entry)
    return max(0.0, (entry - stop) / entry)


def _directional_target_fraction(entry: float, target: float, direction: str) -> float:
    if direction == "short":
        return max(0.0, (entry - target) / entry)
    return max(0.0, (target - entry) / entry)


# ---------------------------------------------------------------------------
# Core math
# ---------------------------------------------------------------------------


def _kelly_raw(
    calibrated_prob: float,
    payoff_fraction: float,
    loss_per_unit: float,
    cost_fraction: float,
) -> tuple[float, float]:
    """Return ``(kelly_raw, expected_net_pnl)`` for the given edge.

    Applies costs symmetrically by shrinking the win fraction and
    growing the loss fraction by half the round-trip cost each. This
    matches the NetEdgeRanker's ``expected_net_pnl`` composition so
    the Phase H sizer and the Phase E ranker cannot disagree about
    the sign of edge.
    """
    p = _clamp(float(calibrated_prob), 0.0, 1.0)
    q = 1.0 - p
    half_cost = max(0.0, float(cost_fraction)) / 2.0
    # Net win / loss per unit notional, treating costs as paid both on
    # entry and exit (half on each leg).
    w = max(0.0, float(payoff_fraction) - half_cost)
    l = max(1e-9, float(loss_per_unit) + half_cost)
    net = p * w - q * l
    # Classic Kelly denominator for binary outcomes: w * l.
    denom = w * l
    if denom <= 0:
        return 0.0, net
    kelly = net / denom
    return max(0.0, kelly), net


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_proposal(
    *,
    inp: PositionSizerInput,
    correlation: CorrelationBudget | None = None,
    portfolio: PortfolioBudget | None = None,
    source: str = "unknown",
) -> PositionSizerOutput:
    """Compute a shadow sizing proposal.

    The function is **pure and deterministic**: same inputs always
    produce the same output. Callers should pass the same
    ``correlation`` and ``portfolio`` snapshot they observed at the
    time the legacy sizer ran, so divergence logs are meaningful.
    """
    reasoning: dict[str, Any] = {}
    entry = _finite_float(inp.entry_price) or 0.0
    stop = _finite_float(inp.stop_price) or 0.0
    capital = _finite_float(inp.capital) or 0.0
    unit_multiplier = _finite_float(inp.unit_multiplier) or 0.0
    loss_per_unit = _finite_float(inp.loss_per_unit) or 0.0
    calibrated_prob = _finite_float(inp.calibrated_prob)
    payoff_fraction = _finite_float(inp.payoff_fraction)
    cost_fraction = _finite_float(inp.cost_fraction)
    kelly_scale = _finite_float(inp.kelly_scale)
    max_risk_pct = _finite_float(inp.max_risk_pct)
    equity_bucket_cap_pct = _finite_float(inp.equity_bucket_cap_pct)
    crypto_bucket_cap_pct = _finite_float(inp.crypto_bucket_cap_pct)
    single_ticker_cap_pct = _finite_float(inp.single_ticker_cap_pct)
    direction = _normalize_direction(inp.direction)
    target_price = _finite_float(inp.target_price) if inp.target_price is not None else None
    expected_net_pnl_override = (
        _finite_float(inp.expected_net_pnl)
        if inp.expected_net_pnl is not None
        else None
    )

    # --- Input sanity --------------------------------------------------
    reject_reason: str | None = None
    if (
        not math.isfinite(entry)
        or not math.isfinite(stop)
        or not math.isfinite(capital)
        or not math.isfinite(unit_multiplier)
        or entry <= 0
        or stop <= 0
        or entry == stop
        or capital <= 0
        or unit_multiplier <= 0
    ):
        reject_reason = "invalid_prices_or_capital"
    elif direction is None:
        reject_reason = "invalid_direction"
    elif direction == "long" and stop >= entry:
        reject_reason = "invalid_directional_stop"
    elif direction == "short" and stop <= entry:
        reject_reason = "invalid_directional_stop"
    elif not math.isfinite(loss_per_unit) or loss_per_unit <= 0:
        reject_reason = "invalid_loss_per_unit"
    elif inp.target_price is not None and (target_price is None or target_price <= 0):
        reject_reason = "invalid_target_price"
    elif loss_per_unit + 1e-9 < _directional_loss_fraction(entry, stop, direction):
        reject_reason = "loss_per_unit_understates_stop_risk"
    elif calibrated_prob is not None and not (0.0 <= calibrated_prob <= 1.0):
        reject_reason = "invalid_probability"
    elif (
        calibrated_prob is None
        or payoff_fraction is None
        or cost_fraction is None
        or kelly_scale is None
        or max_risk_pct is None
        or equity_bucket_cap_pct is None
        or crypto_bucket_cap_pct is None
        or single_ticker_cap_pct is None
        or (
            inp.expected_net_pnl is not None
            and expected_net_pnl_override is None
        )
    ):
        reject_reason = "invalid_edge_inputs"
    elif not (0.0 <= max_risk_pct <= 100.0):
        reject_reason = "invalid_risk_cap_pct"
    elif not (0.0 <= equity_bucket_cap_pct <= 100.0):
        reject_reason = "invalid_bucket_cap_pct"
    elif not (0.0 <= crypto_bucket_cap_pct <= 100.0):
        reject_reason = "invalid_bucket_cap_pct"
    elif not (0.0 <= single_ticker_cap_pct <= 100.0):
        reject_reason = "invalid_single_ticker_cap_pct"
    elif target_price is not None:
        target_fraction = _directional_target_fraction(entry, target_price, direction)
        if target_fraction <= 0:
            reject_reason = "invalid_directional_target"
        elif payoff_fraction > target_fraction + 1e-9:
            reject_reason = "payoff_fraction_exceeds_target_reward"

    if reject_reason:
        reasoning["reject_reason"] = reject_reason
        id_entry = entry if math.isfinite(entry) else 0.0
        id_stop = stop if math.isfinite(stop) else 0.0
        id_loss = loss_per_unit if math.isfinite(loss_per_unit) else 0.0
        id_prob = calibrated_prob if calibrated_prob is not None else 0.0
        id_payoff = payoff_fraction if payoff_fraction is not None else 0.0
        return PositionSizerOutput(
            proposal_id=compute_proposal_id(
                source=source,
                ticker=inp.ticker,
                user_id=inp.user_id,
                entry_price=id_entry,
                stop_price=id_stop,
                calibrated_prob=id_prob,
                payoff_fraction=id_payoff,
                loss_per_unit=id_loss,
            ),
            proposed_notional=0.0,
            proposed_quantity=0.0,
            proposed_risk_pct=0.0,
            kelly_fraction=0.0,
            kelly_scaled_fraction=0.0,
            expected_net_pnl=expected_net_pnl_override or 0.0,
            correlation_cap_triggered=False,
            correlation_bucket=(correlation.bucket if correlation else inp.correlation_bucket),
            max_bucket_notional=(
                _non_negative_finite(correlation.max_bucket_notional)
                if correlation
                else None
            ),
            notional_cap_triggered=False,
            reasoning=reasoning,
        )

    # --- Kelly math ----------------------------------------------------
    kelly_raw, net_pnl = _kelly_raw(
        calibrated_prob=calibrated_prob,
        payoff_fraction=payoff_fraction,
        loss_per_unit=loss_per_unit,
        cost_fraction=cost_fraction,
    )
    kelly_scaled = max(0.0, kelly_raw * max(0.0, kelly_scale))

    expected_net_pnl = (
        expected_net_pnl_override
        if expected_net_pnl_override is not None
        else net_pnl
    )
    reasoning["expected_net_pnl"] = expected_net_pnl
    reasoning["kelly_raw"] = kelly_raw
    reasoning["kelly_scaled"] = kelly_scaled

    if expected_net_pnl <= 0 or kelly_scaled <= 0:
        reasoning["reject_reason"] = "non_positive_edge"
        return PositionSizerOutput(
            proposal_id=compute_proposal_id(
                source=source,
                ticker=inp.ticker,
                user_id=inp.user_id,
                entry_price=entry,
                stop_price=stop,
                calibrated_prob=calibrated_prob,
                payoff_fraction=payoff_fraction,
                loss_per_unit=loss_per_unit,
            ),
            proposed_notional=0.0,
            proposed_quantity=0.0,
            proposed_risk_pct=0.0,
            kelly_fraction=kelly_raw,
            kelly_scaled_fraction=kelly_scaled,
            expected_net_pnl=expected_net_pnl,
            correlation_cap_triggered=False,
            correlation_bucket=(correlation.bucket if correlation else inp.correlation_bucket),
            max_bucket_notional=(
                _non_negative_finite(correlation.max_bucket_notional)
                if correlation
                else None
            ),
            notional_cap_triggered=False,
            reasoning=reasoning,
        )

    # --- Translate kelly fraction-of-capital into risk-of-capital -----
    # Kelly is the fraction of capital to *stake*. Here the stake is the
    # notional, and the at-risk amount per trade is ``notional * loss``.
    loss_fraction = max(1e-9, loss_per_unit)
    risk_of_capital = kelly_scaled * loss_fraction  # fraction of capital
    max_risk_frac = max(0.0, max_risk_pct) / 100.0
    if risk_of_capital > max_risk_frac:
        # Trim Kelly so the per-trade risk equals the cap.
        kelly_scaled = max_risk_frac / loss_fraction
        risk_of_capital = max_risk_frac
        reasoning["risk_cap_triggered"] = True
    else:
        reasoning["risk_cap_triggered"] = False

    proposed_notional = kelly_scaled * capital

    # --- Hard caps: single-ticker + correlation bucket ----------------
    single_ticker_cap = max(0.0, single_ticker_cap_pct) / 100.0 * capital
    # Deduct what is already open in this exact ticker.
    ticker_open = _non_negative_finite(portfolio.ticker_open_notional) if portfolio else 0.0
    ticker_headroom = max(0.0, single_ticker_cap - ticker_open)
    notional_cap_triggered = False
    if proposed_notional > ticker_headroom:
        proposed_notional = ticker_headroom
        notional_cap_triggered = True
        reasoning["single_ticker_cap_headroom"] = ticker_headroom

    correlation_cap_triggered = False
    bucket_label: str | None = None
    max_bucket_notional: float | None = None
    if correlation is not None:
        bucket_label = correlation.bucket
        max_bucket_notional = _non_negative_finite(correlation.max_bucket_notional)
        open_notional = _non_negative_finite(correlation.open_notional)
        bucket_headroom = max(
            0.0, max_bucket_notional - open_notional,
        )
        if proposed_notional > bucket_headroom:
            proposed_notional = bucket_headroom
            correlation_cap_triggered = True
            reasoning["correlation_bucket_headroom"] = bucket_headroom
    else:
        bucket_label = inp.correlation_bucket
        # Derive a reasonable bucket cap even when caller did not pass
        # a correlation budget (pure unit-test callers).
        bucket_cap_pct = (
            crypto_bucket_cap_pct if _is_crypto(inp.asset_class)
            else equity_bucket_cap_pct
        )
        max_bucket_notional = max(0.0, bucket_cap_pct) / 100.0 * capital
        if proposed_notional > max_bucket_notional:
            proposed_notional = max_bucket_notional
            correlation_cap_triggered = True
            reasoning["correlation_bucket_headroom"] = max_bucket_notional

    # --- Portfolio-level safety floor --------------------------------
    if portfolio is not None:
        max_total_notional = _non_negative_finite(portfolio.max_total_notional)
        deployed_notional = _non_negative_finite(portfolio.deployed_notional)
        portfolio_headroom = max(
            0.0, max_total_notional - deployed_notional,
        )
        if proposed_notional > portfolio_headroom:
            proposed_notional = portfolio_headroom
            notional_cap_triggered = True
            reasoning["portfolio_cap_headroom"] = portfolio_headroom

    # --- Finalize ----------------------------------------------------
    proposed_notional = max(0.0, proposed_notional)
    proposed_quantity = _round_qty(
        proposed_notional,
        entry,
        inp.qty_rounding,
        unit_multiplier,
    )
    # If rounding to whole shares pushed notional below the proposal,
    # reflect the *achievable* notional so divergence math is fair.
    achievable_notional = proposed_quantity * entry * unit_multiplier
    if achievable_notional < proposed_notional:
        proposed_notional = achievable_notional

    proposed_risk_pct = (proposed_notional * loss_fraction / capital * 100.0) if capital > 0 else 0.0

    return PositionSizerOutput(
        proposal_id=compute_proposal_id(
            source=source,
            ticker=inp.ticker,
            user_id=inp.user_id,
            entry_price=entry,
            stop_price=stop,
            calibrated_prob=calibrated_prob,
            payoff_fraction=payoff_fraction,
            loss_per_unit=loss_per_unit,
        ),
        proposed_notional=round(proposed_notional, 6),
        proposed_quantity=round(proposed_quantity, 8),
        proposed_risk_pct=round(proposed_risk_pct, 6),
        kelly_fraction=round(kelly_raw, 8),
        kelly_scaled_fraction=round(kelly_scaled, 8),
        expected_net_pnl=round(expected_net_pnl, 8),
        correlation_cap_triggered=correlation_cap_triggered,
        correlation_bucket=bucket_label,
        max_bucket_notional=max_bucket_notional,
        notional_cap_triggered=notional_cap_triggered,
        reasoning=reasoning,
    )

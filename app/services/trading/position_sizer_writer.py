"""Phase H - persistence layer for the canonical position sizer.

Writes one row into ``trading_position_sizer_log`` per actionable
pick. Shadow-safe: never affects the notional legacy sizers return.

Design
------

* **Single surface.** All four emitter call-sites go through
  :func:`write_proposal`; there is no other writer path into the log.
* **Append-only.** Every call inserts a new row, even when the
  caller re-proposes for the same signal. ``proposal_id`` stays
  stable across retries (pure module guarantees this) so the
  diagnostics endpoint can aggregate by latest-row-per-proposal.
* **Legacy comparison.** Callers pass the notional their legacy
  sizer actually chose, and this writer computes ``divergence_bps``
  before persisting. That way release-blocker scripts have a single
  scalar to threshold on.
* **Off-mode short-circuit.** When ``brain_position_sizer_mode ==
  'off'`` the writer is a no-op and returns ``None``. Emitter
  call-sites can call it unconditionally without gating.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings
from ...trading_brain.infrastructure.position_sizer_ops_log import (
    format_position_sizer_ops_line,
)
from .position_sizer_model import (
    CorrelationBudget,
    PortfolioBudget,
    PositionSizerInput,
    PositionSizerOutput,
    compute_proposal,
)
from .risk_dial_service import (
    get_latest_dial as _risk_dial_get_latest,
    mode_is_active as _risk_dial_mode_is_active,
)

logger = logging.getLogger(__name__)
_ALLOWED_MODES = ("off", "shadow", "compare", "authoritative")


def _effective_mode(override: str | None = None) -> str:
    m = (override or getattr(settings, "brain_position_sizer_mode", "off") or "off").lower()
    return m if m in _ALLOWED_MODES else "off"


def mode_is_active(override: str | None = None) -> bool:
    return _effective_mode(override) != "off"


def mode_is_authoritative(override: str | None = None) -> bool:
    return _effective_mode(override) == "authoritative"


def _ops_log_enabled() -> bool:
    return bool(getattr(settings, "brain_position_sizer_ops_log_enabled", True))


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WriteResult:
    log_id: int
    proposal_id: str
    mode: str
    divergence_bps: float | None
    proposed_notional: float
    proposed_quantity: float
    cap_triggered: bool


@dataclass(frozen=True)
class LegacySizing:
    """What the legacy sizer decided at this call-site."""

    notional: float | None = None
    quantity: float | None = None
    source: str = "unknown"


# ---------------------------------------------------------------------------
# Divergence
# ---------------------------------------------------------------------------


def _divergence_bps(proposed: float, legacy: float | None) -> float | None:
    """Absolute relative difference, in basis points of the legacy size.

    Returns ``None`` when legacy is unknown (we have no base to compare
    against). When legacy is exactly zero but proposed is positive the
    divergence is reported as a large sentinel value rather than
    ``inf`` so SQL scalar aggregates still work.
    """
    if legacy is None:
        return None
    try:
        lv = float(legacy)
    except Exception:
        return None
    if lv <= 0:
        return 1_000_000.0 if proposed > 0 else 0.0
    return abs(proposed - lv) / abs(lv) * 10_000.0


# ---------------------------------------------------------------------------
# Public write API
# ---------------------------------------------------------------------------


def write_proposal(
    db: Session,
    *,
    inp: PositionSizerInput,
    output: PositionSizerOutput | None = None,
    correlation: CorrelationBudget | None = None,
    portfolio: PortfolioBudget | None = None,
    source: str,
    legacy: LegacySizing | None = None,
    mode_override: str | None = None,
) -> WriteResult | None:
    """Write one proposal row and emit the ops log line.

    When ``output`` is ``None`` the writer computes the proposal via
    :func:`compute_proposal` using the provided ``inp`` + budgets.
    Callers that already computed a proposal (for determinism checks)
    can pass it back in and the writer will just persist it.
    """
    mode = _effective_mode(mode_override)
    if mode == "off":
        return None

    if output is None:
        output = compute_proposal(
            inp=inp,
            correlation=correlation,
            portfolio=portfolio,
            source=source,
        )

    legacy_notional = legacy.notional if legacy else None
    legacy_quantity = legacy.quantity if legacy else None
    legacy_source = legacy.source if legacy else None
    divergence = _divergence_bps(output.proposed_notional, legacy_notional)

    # Phase I shadow integration: record the active risk dial for this user
    # so diagnostics can compare proposal volume by dial bucket. Shadow
    # mode only - the dial is NOT applied to ``proposed_notional``; that
    # cutover is Phase I.2.
    risk_dial_multiplier: float | None = None
    if _risk_dial_mode_is_active():
        try:
            risk_dial_multiplier = float(
                _risk_dial_get_latest(db, user_id=inp.user_id, default=1.0)
            )
        except Exception:
            risk_dial_multiplier = None

    payload = {
        "kelly_scale": inp.kelly_scale,
        "max_risk_pct": inp.max_risk_pct,
        "single_ticker_cap_pct": inp.single_ticker_cap_pct,
        "equity_bucket_cap_pct": inp.equity_bucket_cap_pct,
        "crypto_bucket_cap_pct": inp.crypto_bucket_cap_pct,
        "qty_rounding": inp.qty_rounding,
        "reasoning": output.reasoning,
    }
    if correlation is not None:
        payload["correlation_open_notional"] = correlation.open_notional
    if portfolio is not None:
        payload["portfolio_deployed_notional"] = portfolio.deployed_notional
        payload["portfolio_max_total_notional"] = portfolio.max_total_notional
        payload["portfolio_ticker_open"] = portfolio.ticker_open_notional

    now = datetime.utcnow()
    row = db.execute(text("""
        INSERT INTO trading_position_sizer_log (
            proposal_id, source, ticker, direction, user_id, pattern_id,
            asset_class, regime, entry_price, stop_price, target_price, capital,
            calibrated_prob, payoff_fraction, cost_fraction, expected_net_pnl,
            kelly_fraction, kelly_scaled_fraction,
            proposed_notional, proposed_quantity, proposed_risk_pct,
            correlation_cap_triggered, correlation_bucket, max_bucket_notional,
            notional_cap_triggered,
            legacy_notional, legacy_quantity, legacy_source, divergence_bps,
            mode, payload_json, observed_at, risk_dial_multiplier
        ) VALUES (
            :proposal_id, :source, :ticker, :direction, :user_id, :pattern_id,
            :asset_class, :regime, :entry_price, :stop_price, :target_price, :capital,
            :calibrated_prob, :payoff_fraction, :cost_fraction, :expected_net_pnl,
            :kelly_fraction, :kelly_scaled_fraction,
            :proposed_notional, :proposed_quantity, :proposed_risk_pct,
            :correlation_cap_triggered, :correlation_bucket, :max_bucket_notional,
            :notional_cap_triggered,
            :legacy_notional, :legacy_quantity, :legacy_source, :divergence_bps,
            :mode, CAST(:payload_json AS JSONB), :now, :risk_dial_multiplier
        )
        RETURNING id
    """), {
        "proposal_id": output.proposal_id,
        "source": source,
        "ticker": inp.ticker,
        "direction": inp.direction,
        "user_id": inp.user_id,
        "pattern_id": inp.pattern_id,
        "asset_class": inp.asset_class,
        "regime": inp.regime,
        "entry_price": float(inp.entry_price),
        "stop_price": float(inp.stop_price),
        "target_price": float(inp.target_price) if inp.target_price is not None else None,
        "capital": float(inp.capital),
        "calibrated_prob": float(inp.calibrated_prob),
        "payoff_fraction": float(inp.payoff_fraction),
        "cost_fraction": float(inp.cost_fraction),
        "expected_net_pnl": float(output.expected_net_pnl),
        "kelly_fraction": float(output.kelly_fraction),
        "kelly_scaled_fraction": float(output.kelly_scaled_fraction),
        "proposed_notional": float(output.proposed_notional),
        "proposed_quantity": float(output.proposed_quantity),
        "proposed_risk_pct": float(output.proposed_risk_pct),
        "correlation_cap_triggered": bool(output.correlation_cap_triggered),
        "correlation_bucket": output.correlation_bucket,
        "max_bucket_notional": output.max_bucket_notional,
        "notional_cap_triggered": bool(output.notional_cap_triggered),
        "legacy_notional": float(legacy_notional) if legacy_notional is not None else None,
        "legacy_quantity": float(legacy_quantity) if legacy_quantity is not None else None,
        "legacy_source": legacy_source,
        "divergence_bps": float(divergence) if divergence is not None else None,
        "mode": mode,
        "payload_json": json.dumps(payload, default=str, separators=(",", ":")),
        "now": now,
        "risk_dial_multiplier": risk_dial_multiplier,
    })
    new_id = int(row.scalar_one())
    db.commit()

    if _ops_log_enabled():
        logger.info(
            format_position_sizer_ops_line(
                event="proposal",
                mode=mode,
                proposal_id=output.proposal_id,
                source=source,
                ticker=inp.ticker,
                direction=inp.direction,
                user_id=inp.user_id,
                pattern_id=inp.pattern_id,
                asset_class=inp.asset_class,
                regime=inp.regime,
                entry_price=inp.entry_price,
                stop_price=inp.stop_price,
                target_price=inp.target_price,
                capital=inp.capital,
                calibrated_prob=inp.calibrated_prob,
                payoff_fraction=inp.payoff_fraction,
                cost_fraction=inp.cost_fraction,
                expected_net_pnl=output.expected_net_pnl,
                kelly_fraction=output.kelly_fraction,
                kelly_scaled_fraction=output.kelly_scaled_fraction,
                proposed_notional=output.proposed_notional,
                proposed_quantity=output.proposed_quantity,
                proposed_risk_pct=output.proposed_risk_pct,
                correlation_cap_triggered=output.correlation_cap_triggered,
                correlation_bucket=output.correlation_bucket,
                max_bucket_notional=output.max_bucket_notional,
                notional_cap_triggered=output.notional_cap_triggered,
                legacy_notional=legacy_notional,
                legacy_quantity=legacy_quantity,
                legacy_source=legacy_source,
                divergence_bps=divergence,
                risk_dial_multiplier=risk_dial_multiplier,
            )
        )

    return WriteResult(
        log_id=new_id,
        proposal_id=output.proposal_id,
        mode=mode,
        divergence_bps=divergence,
        proposed_notional=output.proposed_notional,
        proposed_quantity=output.proposed_quantity,
        cap_triggered=bool(
            output.correlation_cap_triggered or output.notional_cap_triggered
        ),
    )


# ---------------------------------------------------------------------------
# Diagnostics summary
# ---------------------------------------------------------------------------


def _bucket_divergence(d_bps: float | None) -> str:
    if d_bps is None:
        return "unknown"
    if d_bps < 100.0:
        return "under_100_bps"
    if d_bps < 500.0:
        return "100_500_bps"
    if d_bps < 2000.0:
        return "500_2000_bps"
    return "over_2000_bps"


def _bucket_dial(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 0.5:
        return "under_0_5"
    if value < 0.8:
        return "0_5_to_0_8"
    if value <= 1.0:
        return "0_8_to_1_0"
    if value <= 1.2:
        return "1_0_to_1_2"
    return "over_1_2"


def proposals_summary(
    db: Session,
    *,
    lookback_hours: int = 24,
) -> dict[str, Any]:
    """Frozen-shape diagnostics summary.

    Keys (stable, order-preserving):
      * mode
      * lookback_hours
      * proposals_total
      * by_source { source: count }
      * by_divergence_bucket { under_100_bps / 100_500_bps / 500_2000_bps / over_2000_bps: count }
      * mean_divergence_bps
      * p90_divergence_bps
      * cap_trigger_counts { correlation_cap, notional_cap }
      * by_dial_bucket { unknown / under_0_5 / 0_5_to_0_8 / 0_8_to_1_0 /
                         1_0_to_1_2 / over_1_2 : count }
      * latest_proposal { proposal_id, source, ticker, proposed_notional,
                          legacy_notional, divergence_bps, observed_at,
                          risk_dial_multiplier }
    """
    mode = _effective_mode()

    total_row = db.execute(text("""
        SELECT COUNT(*) FROM trading_position_sizer_log
        WHERE observed_at >= (NOW() - (:lh || ' hours')::INTERVAL)
    """), {"lh": int(lookback_hours)}).scalar_one()
    proposals_total = int(total_row or 0)

    src_rows = db.execute(text("""
        SELECT source, COUNT(*)
        FROM trading_position_sizer_log
        WHERE observed_at >= (NOW() - (:lh || ' hours')::INTERVAL)
        GROUP BY source
    """), {"lh": int(lookback_hours)}).fetchall()
    by_source = {r[0]: int(r[1]) for r in src_rows}

    div_rows = db.execute(text("""
        SELECT divergence_bps FROM trading_position_sizer_log
        WHERE observed_at >= (NOW() - (:lh || ' hours')::INTERVAL)
    """), {"lh": int(lookback_hours)}).fetchall()
    div_values = [float(r[0]) for r in div_rows if r[0] is not None]

    by_divergence_bucket: dict[str, int] = {
        "under_100_bps": 0,
        "100_500_bps": 0,
        "500_2000_bps": 0,
        "over_2000_bps": 0,
    }
    for v in div_values:
        by_divergence_bucket[_bucket_divergence(v)] += 1

    mean_div = sum(div_values) / len(div_values) if div_values else 0.0
    p90_div = 0.0
    if div_values:
        sorted_v = sorted(div_values)
        idx = int(0.9 * (len(sorted_v) - 1))
        p90_div = float(sorted_v[idx])

    cap_rows = db.execute(text("""
        SELECT
            SUM(CASE WHEN correlation_cap_triggered THEN 1 ELSE 0 END),
            SUM(CASE WHEN notional_cap_triggered THEN 1 ELSE 0 END)
        FROM trading_position_sizer_log
        WHERE observed_at >= (NOW() - (:lh || ' hours')::INTERVAL)
    """), {"lh": int(lookback_hours)}).fetchone()
    corr_cap = int((cap_rows[0] if cap_rows and cap_rows[0] is not None else 0) or 0)
    notional_cap = int((cap_rows[1] if cap_rows and cap_rows[1] is not None else 0) or 0)

    dial_rows = db.execute(text("""
        SELECT risk_dial_multiplier FROM trading_position_sizer_log
        WHERE observed_at >= (NOW() - (:lh || ' hours')::INTERVAL)
    """), {"lh": int(lookback_hours)}).fetchall()
    by_dial_bucket: dict[str, int] = {
        "unknown": 0,
        "under_0_5": 0,
        "0_5_to_0_8": 0,
        "0_8_to_1_0": 0,
        "1_0_to_1_2": 0,
        "over_1_2": 0,
    }
    for r in dial_rows:
        v = float(r[0]) if r[0] is not None else None
        by_dial_bucket[_bucket_dial(v)] += 1

    latest = db.execute(text("""
        SELECT proposal_id, source, ticker, proposed_notional,
               legacy_notional, divergence_bps, observed_at,
               risk_dial_multiplier
        FROM trading_position_sizer_log
        ORDER BY observed_at DESC
        LIMIT 1
    """)).fetchone()
    latest_payload: dict[str, Any] | None = None
    if latest:
        latest_payload = {
            "proposal_id": latest[0],
            "source": latest[1],
            "ticker": latest[2],
            "proposed_notional": float(latest[3]) if latest[3] is not None else None,
            "legacy_notional": float(latest[4]) if latest[4] is not None else None,
            "divergence_bps": float(latest[5]) if latest[5] is not None else None,
            "observed_at": latest[6].isoformat() if latest[6] else None,
            "risk_dial_multiplier": float(latest[7]) if latest[7] is not None else None,
        }

    return {
        "mode": mode,
        "lookback_hours": int(lookback_hours),
        "proposals_total": proposals_total,
        "by_source": by_source,
        "by_divergence_bucket": by_divergence_bucket,
        "mean_divergence_bps": round(mean_div, 2),
        "p90_divergence_bps": round(p90_div, 2),
        "cap_trigger_counts": {
            "correlation_cap": corr_cap,
            "notional_cap": notional_cap,
        },
        "by_dial_bucket": by_dial_bucket,
        "latest_proposal": latest_payload,
    }


__all__ = [
    "LegacySizing",
    "WriteResult",
    "mode_is_active",
    "mode_is_authoritative",
    "proposals_summary",
    "write_proposal",
]

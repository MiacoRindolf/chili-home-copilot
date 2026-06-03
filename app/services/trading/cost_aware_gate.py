"""f-coinbase-autotrader-enablement-phase-5-cost-aware-sizing (2026-05-09).

Cost-aware min-edge gate + per-venue cap helpers + Coinbase
buying-power resolver.

Three layers, intentionally separable so each can be replaced /
tightened in isolation:

  1. :func:`resolve_coinbase_buying_power` — returns
     ``{usd, usdc, total, last_updated}``. 30s in-process cache.
     Reads ``cash`` (USD wallet) from
     :func:`coinbase_service.get_portfolio` AND USDC quantity from
     :func:`coinbase_service.get_positions` so the total reflects
     actual buying power per Phase 2 G1 (operator's funded $2.2k is
     held as USDC; ``portfolio.cash`` reports USD-only).

  2. :func:`cost_aware_min_edge_gate` — returns ``{allowed, reason,
     fee_bps, threshold_bps, edge_bps}``. For RH-eligible tickers
     fee=0 (RH crypto is fee-free, equity is sub-bps); the gate is
     a no-op. For Coinbase-only tickers, the projected edge must
     clear ``fee_bps + buffer_bps`` else block.

  3. :func:`per_venue_cap_check` — returns ``{allowed, reason,
     current_positions, current_notional}``. Per-venue caps are
     INDEPENDENT (Phase 1 design constraint #1; no cross-venue
     aggregation) and apply to CHILI-managed autotrader exposure, not
     passive broker-sync holdings.

Helper-level testable: every public function accepts injection
seams (settings_, db, fast_path_active) so unit tests can run
without hitting production state.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)


# Reason constants — pinned by tests so a typo flips visibly red.
REASON_GATE_RH_FEE_FREE = "rh_fee_free"
REASON_GATE_COINBASE_PASSED = "coinbase_clears_fee_threshold"
REASON_GATE_COINBASE_BLOCKED = "coinbase_below_fee_threshold"
REASON_GATE_RH_TCA_PASSED = "rh_clears_tca_threshold"
REASON_GATE_RH_TCA_BLOCKED = "rh_below_tca_threshold"
REASON_GATE_NO_VENUE = "no_venue_supports"
REASON_GATE_TCA_INVALID = "coinbase_tca_estimate_invalid"
REASON_GATE_TCA_UNPROVEN = "coinbase_tca_estimate_unproven"
REASON_CAP_OK = "within_cap"
REASON_CAP_NOTIONAL = "venue_notional_cap_exceeded"
REASON_CAP_POSITIONS = "venue_concurrent_positions_cap_exceeded"

PHASE5K_COINBASE_CAP_ENV = "CHILI_PHASE5K_COINBASE_CAP_USE_ENVELOPES"
_COINBASE_CAP_COMPAT_RELATION = "trading_trades"
_COINBASE_CAP_ENVELOPE_RELATION = "trading_management_envelopes"


@dataclass(frozen=True)
class CostGateDecision:
    allowed: bool
    reason: str
    fee_bps: int
    threshold_bps: int
    edge_bps: int
    tca_cost_bps: int = 0
    tca_snapshot: dict[str, Any] | None = None


@dataclass(frozen=True)
class CapDecision:
    allowed: bool
    reason: str
    current_positions: int
    current_notional_usd: float


def _finite_float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _nonnegative_float(value: Any) -> float | None:
    out = _finite_float_or_none(value)
    if out is None or out < 0.0:
        return None
    return out


def _nonnegative_int(value: Any, *, default: int = 0) -> int:
    out = _nonnegative_float(value)
    if out is None:
        return int(default)
    return max(0, int(out))


def _truthy_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _coinbase_cap_source_relation(settings_: Any | None) -> str:
    raw = getattr(settings_, "chili_phase5k_coinbase_cap_use_envelopes", False)
    if _truthy_flag(raw):
        return _COINBASE_CAP_ENVELOPE_RELATION
    return _COINBASE_CAP_COMPAT_RELATION


# ── Buying-power resolver ────────────────────────────────────────────


_BUYING_POWER_CACHE: dict[str, Any] = {"value": None, "ts": 0.0}
_BUYING_POWER_CACHE_TTL_S = 30.0


def resolve_coinbase_buying_power(
    *, force_refresh: bool = False,
    portfolio_fn=None, positions_fn=None,
) -> dict[str, Any]:
    """Returns ``{usd, usdc, total, last_updated}``.

    * ``usd`` — `cash` field from :func:`coinbase_service.get_portfolio`.
    * ``usdc`` — quantity of the ``USDC-USD`` position from
      :func:`coinbase_service.get_positions` (treated 1:1 with USD).
    * ``total`` — sum of the two; the autotrader's effective Coinbase
      buying power.
    * ``last_updated`` — unix ts of resolution.

    30s in-process cache. ``portfolio_fn`` / ``positions_fn`` are the
    test-injection seams; production callers leave None.
    """
    now = time.time()
    if (
        not force_refresh
        and _BUYING_POWER_CACHE["value"] is not None
        and (now - _BUYING_POWER_CACHE["ts"]) < _BUYING_POWER_CACHE_TTL_S
    ):
        return dict(_BUYING_POWER_CACHE["value"])

    if portfolio_fn is None or positions_fn is None:
        try:
            from ..coinbase_service import get_portfolio, get_positions
            portfolio_fn = portfolio_fn or get_portfolio
            positions_fn = positions_fn or get_positions
        except Exception as exc:
            logger.warning(
                "[cost_aware_gate] coinbase_service import failed: %s", exc,
            )
            return {
                "usd": 0.0, "usdc": 0.0, "total": 0.0,
                "last_updated": now, "error": str(exc),
            }

    try:
        portfolio = portfolio_fn() or {}
    except Exception as exc:
        logger.warning(
            "[cost_aware_gate] get_portfolio failed: %s", exc,
        )
        portfolio = {}
    try:
        positions = positions_fn() or []
    except Exception as exc:
        logger.warning(
            "[cost_aware_gate] get_positions failed: %s", exc,
        )
        positions = []

    usd = _nonnegative_float(portfolio.get("cash")) or 0.0

    usdc_qty = 0.0
    for p in positions:
        if not isinstance(p, dict):
            continue
        ticker = str(p.get("ticker") or "").upper()
        if ticker in ("USDC-USD", "USDC"):
            usdc_qty = _nonnegative_float(p.get("quantity")) or 0.0
            break

    total = usd + usdc_qty
    result = {
        "usd": usd,
        "usdc": usdc_qty,
        "total": total,
        "last_updated": now,
    }
    _BUYING_POWER_CACHE["value"] = result
    _BUYING_POWER_CACHE["ts"] = now
    return dict(result)


# ── Cost-aware min-edge gate ─────────────────────────────────────────


def cost_aware_min_edge_gate(
    *,
    ticker: str,
    projected_profit_pct: Optional[float],
    db=None,
    settings_=None,
    selected_venue: str | None = None,
) -> CostGateDecision:
    """Refuses Coinbase entries whose projected edge does not clear
    (fee + buffer). RH-eligible tickers pass with fee=0 (no behavior
    change vs pre-Phase-5).

    ``projected_profit_pct`` is the percent (e.g. 6.71 for 6.71%).
    Converted to bps (671) for the fee comparison.
    ``selected_venue`` is an optional advisory route from the broker
    selector; when present it prevents this gate from re-inferring a
    different venue from whitelist state.
    """
    s = settings_
    if s is None:
        try:
            from ...config import settings as _s
            s = _s
        except Exception:
            s = None

    fee_bps = _nonnegative_int(
        getattr(s, "chili_coinbase_taker_fee_bps_round_trip", 120),
        default=120,
    )
    buffer_bps = _nonnegative_int(
        getattr(s, "chili_min_edge_safety_buffer_bps", 30),
        default=30,
    )
    threshold_bps = fee_bps + buffer_bps

    projected_edge = _finite_float_or_none(projected_profit_pct)
    edge_bps = int(round(projected_edge * 100.0)) if projected_edge is not None else 0

    # Routing-aware: RH-eligible tickers pay no explicit fee at the
    # autotrader level, but observed adverse entry slippage still has to
    # come out of expected edge once we have enough usable TCA evidence.
    try:
        from .broker_selector import (
            resolve_coinbase_whitelist,
            resolve_rh_whitelist,
            rh_crypto_degradation_state,
        )
    except Exception:
        # If selector module isn't importable, default to "no opinion"
        # — the existing rule_gate's min_projected_profit_pct handles
        # the floor. Cost-gate becomes a soft pass.
        return CostGateDecision(
            allowed=True, reason=REASON_GATE_RH_FEE_FREE,
            fee_bps=0, threshold_bps=0, edge_bps=edge_bps,
        )

    selected = str(selected_venue or "").strip().lower()
    if selected in {"rh", "robinhood"}:
        rh_whitelisted = True
        coinbase_whitelisted = False
        rh_crypto_degraded = False
    elif selected in {"coinbase", "cb"}:
        rh_whitelisted = False
        coinbase_whitelisted = True
        rh_crypto_degraded = True
    elif selected == "skip":
        rh_whitelisted = False
        coinbase_whitelisted = False
        rh_crypto_degraded = False
    else:
        rh_whitelisted = resolve_rh_whitelist(ticker)
        coinbase_whitelisted = resolve_coinbase_whitelist(ticker)
        rh_crypto_degraded = False
        if rh_whitelisted and coinbase_whitelisted:
            rh_crypto_degraded = rh_crypto_degradation_state(
                ticker,
                db=db,
                settings_=settings_,
            ).degraded

    if rh_whitelisted and not rh_crypto_degraded:
        rh_tca_cost_bps = 0
        rh_tca_snapshot: dict[str, Any] | None = None
        if bool(getattr(s, "chili_robinhood_cost_gate_include_tca_estimates", True)):
            min_samples = _nonnegative_int(
                getattr(s, "chili_robinhood_cost_gate_min_tca_samples", 5),
                default=5,
            )
            window_days = _nonnegative_int(
                getattr(s, "chili_robinhood_cost_gate_window_days", 30),
                default=30,
            )
            rh_tca_cost_bps, rh_tca_snapshot = _robinhood_entry_tca_cost_bps(
                db=db,
                ticker=ticker,
                side="long",
                min_samples=min_samples,
                window_days=max(1, window_days),
                settings_=s,
            )
        if rh_tca_snapshot and rh_tca_snapshot.get("used") is True:
            rh_threshold_bps = rh_tca_cost_bps + buffer_bps
            if edge_bps >= rh_threshold_bps:
                return CostGateDecision(
                    allowed=True, reason=REASON_GATE_RH_TCA_PASSED,
                    fee_bps=0, threshold_bps=rh_threshold_bps, edge_bps=edge_bps,
                    tca_cost_bps=rh_tca_cost_bps, tca_snapshot=rh_tca_snapshot,
                )
            return CostGateDecision(
                allowed=False, reason=REASON_GATE_RH_TCA_BLOCKED,
                fee_bps=0, threshold_bps=rh_threshold_bps, edge_bps=edge_bps,
                tca_cost_bps=rh_tca_cost_bps, tca_snapshot=rh_tca_snapshot,
            )
        return CostGateDecision(
            allowed=True, reason=REASON_GATE_RH_FEE_FREE,
            fee_bps=0, threshold_bps=0, edge_bps=edge_bps,
            tca_cost_bps=rh_tca_cost_bps, tca_snapshot=rh_tca_snapshot,
        )

    if not coinbase_whitelisted:
        # No venue supports — selector will skip downstream.
        return CostGateDecision(
            allowed=False, reason=REASON_GATE_NO_VENUE,
            fee_bps=0, threshold_bps=0, edge_bps=edge_bps,
        )

    # Coinbase routing: must clear fee + buffer, and optionally the live
    # p90 spread/slippage estimate from TCA-derived cost rows.
    tca_cost_bps = 0
    tca_snapshot: dict[str, Any] | None = None
    if bool(getattr(s, "chili_coinbase_cost_gate_include_tca_estimates", False)):
        min_samples = _nonnegative_int(
            getattr(s, "chili_coinbase_cost_gate_min_tca_samples", 5),
            default=5,
        )
        tca_cost_bps, tca_snapshot = _coinbase_tca_cost_bps(
            db=db, ticker=ticker, side="long", min_samples=min_samples,
            settings_=s,
        )
        if tca_snapshot and tca_snapshot.get("used") is False:
            tca_reason = str(tca_snapshot.get("reason") or "")
            reason = (
                REASON_GATE_TCA_INVALID
                if tca_reason in {"invalid_tca_estimate", "usable_sample_check_failed"}
                else REASON_GATE_TCA_UNPROVEN
            )
            return CostGateDecision(
                allowed=False, reason=reason,
                fee_bps=fee_bps, threshold_bps=threshold_bps,
                edge_bps=edge_bps, tca_cost_bps=tca_cost_bps,
                tca_snapshot=tca_snapshot,
            )
        threshold_bps += tca_cost_bps

    if edge_bps >= threshold_bps:
        return CostGateDecision(
            allowed=True, reason=REASON_GATE_COINBASE_PASSED,
            fee_bps=fee_bps, threshold_bps=threshold_bps,
            edge_bps=edge_bps, tca_cost_bps=tca_cost_bps,
            tca_snapshot=tca_snapshot,
        )
    return CostGateDecision(
        allowed=False, reason=REASON_GATE_COINBASE_BLOCKED,
        fee_bps=fee_bps, threshold_bps=threshold_bps,
        edge_bps=edge_bps, tca_cost_bps=tca_cost_bps,
        tca_snapshot=tca_snapshot,
    )


def _coinbase_tca_cost_bps(
    *,
    db,
    ticker: str,
    side: str,
    min_samples: int,
    settings_: Any | None,
) -> tuple[int, dict[str, Any] | None]:
    """Return Coinbase execution-cost bps for this ticker/side.

    Prefer raw Coinbase trade TCA filtered by ``broker_source``. Fall back to
    the legacy aggregate estimate only when there is no Coinbase-specific TCA
    yet; aggregate rows are still ticker/side/window scoped and may mix venues.
    """
    if db is None:
        return 0, None
    min_samples_i = max(1, _nonnegative_int(min_samples, default=1))

    venue_cost_bps, venue_snapshot = _coinbase_entry_tca_cost_bps(
        db=db,
        ticker=ticker,
        side=side,
        min_samples=min_samples_i,
        window_days=30,
        settings_=settings_,
    )
    if venue_snapshot and venue_snapshot.get("used") is True:
        return venue_cost_bps, venue_snapshot
    if venue_snapshot and _nonnegative_int(
        venue_snapshot.get("sample_trades"),
        default=0,
    ) > 0:
        return venue_cost_bps, venue_snapshot

    try:
        row = db.execute(text("""
            SELECT sample_trades, window_days, p90_spread_bps, p90_slippage_bps,
                   median_spread_bps, median_slippage_bps, last_updated_at
            FROM trading_execution_cost_estimates
            WHERE UPPER(ticker) = UPPER(:ticker)
              AND LOWER(side) = LOWER(:side)
            ORDER BY last_updated_at DESC
            LIMIT 1
        """), {"ticker": ticker, "side": side}).mappings().first()
    except Exception:
        return 0, None

    if not row:
        return 0, None

    samples = _nonnegative_int(row.get("sample_trades"), default=0)
    if samples < min_samples_i:
        return 0, {
            "sample_trades": samples,
            "min_samples": min_samples_i,
            "used": False,
            "reason": "insufficient_samples",
        }
    raw_window_days = row.get("window_days")
    try:
        window_days = 30 if isinstance(raw_window_days, bool) else int(raw_window_days or 30)
    except (TypeError, ValueError):
        window_days = 30
    window_days = max(1, window_days)

    fields = {
        name: _nonnegative_float(row.get(name))
        for name in (
            "p90_spread_bps",
            "p90_slippage_bps",
            "median_spread_bps",
            "median_slippage_bps",
        )
    }
    invalid_fields = sorted(name for name, value in fields.items() if value is None)
    if invalid_fields:
        return 0, {
            "sample_trades": samples,
            "min_samples": min_samples_i,
            "used": False,
            "reason": "invalid_tca_estimate",
            "invalid_fields": invalid_fields,
        }

    usable_samples = _coinbase_tca_backing_usable_samples(
        db=db, ticker=ticker, side=side, window_days=window_days,
    )
    if usable_samples is None:
        return 0, {
            "sample_trades": samples,
            "min_samples": min_samples_i,
            "used": False,
            "reason": "usable_sample_check_failed",
        }
    if usable_samples < min_samples_i:
        return 0, {
            "sample_trades": samples,
            "usable_samples": usable_samples,
            "min_samples": min_samples_i,
            "used": False,
            "reason": "insufficient_usable_samples",
        }

    spread_bps = fields["p90_spread_bps"] or 0.0
    slippage_bps = fields["p90_slippage_bps"] or 0.0
    tca_cost_bps = int(round(spread_bps + slippage_bps))
    last_updated = row.get("last_updated_at")
    return tca_cost_bps, {
        "sample_trades": samples,
        "usable_samples": usable_samples,
        "sample_basis": "aggregate_usable_finite_tca_trades",
        "source": "trading_execution_cost_estimates",
        "window_days": window_days,
        "p90_spread_bps": spread_bps,
        "p90_slippage_bps": slippage_bps,
        "median_spread_bps": fields["median_spread_bps"] or 0.0,
        "median_slippage_bps": fields["median_slippage_bps"] or 0.0,
        "tca_cost_bps": tca_cost_bps,
        "last_updated_at": (
            last_updated.isoformat() if hasattr(last_updated, "isoformat") else last_updated
        ),
        "used": True,
    }


def _cost_gate_tca_outlier_bps(settings_: Any | None) -> float:
    configured = _nonnegative_float(
        getattr(settings_, "brain_execution_cost_unverified_tca_outlier_bps", 500.0)
    )
    if configured is None or configured <= 0:
        return 500.0
    return float(configured)


def _coinbase_entry_tca_cost_bps(
    *,
    db,
    ticker: str,
    side: str,
    min_samples: int,
    window_days: int,
    settings_: Any | None,
) -> tuple[int, dict[str, Any] | None]:
    """Return recent adverse Coinbase entry slippage bps from raw trades."""
    if db is None:
        return 0, None

    min_samples_i = max(1, _nonnegative_int(min_samples, default=1))
    window_days_i = max(1, _nonnegative_int(window_days, default=30))
    outlier_bps = _cost_gate_tca_outlier_bps(settings_)
    try:
        row = db.execute(text("""
            WITH usable AS (
                SELECT
                    CASE
                        WHEN tca_entry_slippage_bps > 0
                        THEN tca_entry_slippage_bps
                        ELSE 0.0
                    END AS adverse_entry_slippage_bps
                FROM trading_trades
                WHERE UPPER(ticker) = UPPER(:ticker)
                  AND status = 'closed'
                  AND LOWER(COALESCE(broker_source, '')) IN ('coinbase', 'cb')
                  AND LOWER(COALESCE(direction, 'long')) = LOWER(:side)
                  AND entry_date >= NOW() - (:window_days * INTERVAL '1 day')
                  AND tca_entry_slippage_bps IS NOT NULL
                  AND CAST(tca_entry_slippage_bps AS TEXT) NOT IN (
                      'NaN', 'Infinity', '-Infinity'
                  )
                  AND (
                      ABS(tca_entry_slippage_bps) <= :outlier_bps
                      OR COALESCE(avg_fill_price, 0) > 0
                      OR COALESCE(NULLIF(TRIM(broker_order_id), ''), '') <> ''
                      OR LOWER(COALESCE(broker_status, '')) IN (
                          'filled', 'partially_filled'
                      )
                  )
            )
            SELECT
                CAST(COUNT(*) AS INTEGER) AS sample_trades,
                AVG(adverse_entry_slippage_bps) AS avg_entry_slippage_bps,
                percentile_cont(0.9) WITHIN GROUP (
                    ORDER BY adverse_entry_slippage_bps
                ) AS p90_entry_slippage_bps
            FROM usable
        """), {
            "ticker": ticker,
            "side": side,
            "window_days": window_days_i,
            "outlier_bps": outlier_bps,
        }).mappings().first()
    except Exception:
        logger.warning(
            "[cost_aware_gate] Coinbase TCA cost lookup failed for %s/%s",
            ticker,
            side,
            exc_info=True,
        )
        return 0, None

    if not row:
        return 0, None

    samples = _nonnegative_int(row.get("sample_trades"), default=0)
    avg_entry = _nonnegative_float(row.get("avg_entry_slippage_bps"))
    p90_entry = _nonnegative_float(row.get("p90_entry_slippage_bps"))
    if samples < min_samples_i:
        return 0, {
            "sample_trades": samples,
            "min_samples": min_samples_i,
            "window_days": window_days_i,
            "used": False,
            "reason": "insufficient_samples",
        }
    invalid_fields = []
    if avg_entry is None:
        invalid_fields.append("avg_entry_slippage_bps")
    if p90_entry is None:
        invalid_fields.append("p90_entry_slippage_bps")
    if invalid_fields:
        return 0, {
            "sample_trades": samples,
            "min_samples": min_samples_i,
            "window_days": window_days_i,
            "used": False,
            "reason": "invalid_tca_estimate",
            "invalid_fields": invalid_fields,
        }

    cost_basis_bps = max(avg_entry, p90_entry)
    tca_cost_bps = int(round(cost_basis_bps))
    return tca_cost_bps, {
        "sample_trades": samples,
        "min_samples": min_samples_i,
        "sample_basis": "usable_coinbase_adverse_entry_tca_trades",
        "source": "trading_trades_broker_source",
        "window_days": window_days_i,
        "avg_entry_slippage_bps": round(avg_entry, 4),
        "p90_entry_slippage_bps": round(p90_entry, 4),
        "cost_basis": "max_avg_p90_adverse_entry_slippage_bps",
        "cost_basis_bps": round(cost_basis_bps, 4),
        "tca_cost_bps": tca_cost_bps,
        "outlier_bps": outlier_bps,
        "used": True,
    }


def _robinhood_entry_tca_cost_bps(
    *,
    db,
    ticker: str,
    side: str,
    min_samples: int,
    window_days: int,
    settings_: Any | None,
) -> tuple[int, dict[str, Any] | None]:
    """Return recent average adverse RH entry slippage bps.

    Missing/insufficient evidence is fail-open for Robinhood so legacy
    admission remains unchanged until live fills prove the cost.
    """
    if db is None:
        return 0, None

    min_samples_i = max(1, _nonnegative_int(min_samples, default=1))
    window_days_i = max(1, _nonnegative_int(window_days, default=30))
    outlier_bps = _cost_gate_tca_outlier_bps(settings_)
    try:
        row = db.execute(text("""
            WITH usable AS (
                SELECT
                    CASE
                        WHEN tca_entry_slippage_bps > 0
                        THEN tca_entry_slippage_bps
                        ELSE 0.0
                    END AS adverse_entry_slippage_bps
                FROM trading_trades
                WHERE UPPER(ticker) = UPPER(:ticker)
                  AND (
                      LOWER(COALESCE(status, '')) = 'closed'
                      OR (
                          LOWER(COALESCE(status, '')) IN (
                              'open', 'filled', 'partially_filled'
                          )
                          AND (
                              COALESCE(avg_fill_price, 0) > 0
                              OR LOWER(COALESCE(broker_status, '')) IN (
                                  'filled', 'partially_filled'
                              )
                          )
                      )
                  )
                  AND LOWER(COALESCE(broker_source, '')) IN ('robinhood', 'rh')
                  AND LOWER(COALESCE(direction, 'long')) = LOWER(:side)
                  AND entry_date >= NOW() - (:window_days * INTERVAL '1 day')
                  AND tca_entry_slippage_bps IS NOT NULL
                  AND CAST(tca_entry_slippage_bps AS TEXT) NOT IN (
                      'NaN', 'Infinity', '-Infinity'
                  )
                  AND (
                      ABS(tca_entry_slippage_bps) <= :outlier_bps
                      OR COALESCE(avg_fill_price, 0) > 0
                      OR COALESCE(NULLIF(TRIM(broker_order_id), ''), '') <> ''
                      OR LOWER(COALESCE(broker_status, '')) IN (
                          'filled', 'partially_filled'
                      )
                  )
            )
            SELECT
                CAST(COUNT(*) AS INTEGER) AS sample_trades,
                AVG(adverse_entry_slippage_bps) AS avg_entry_slippage_bps,
                percentile_cont(0.9) WITHIN GROUP (
                    ORDER BY adverse_entry_slippage_bps
                ) AS p90_entry_slippage_bps
            FROM usable
        """), {
            "ticker": ticker,
            "side": side,
            "window_days": window_days_i,
            "outlier_bps": outlier_bps,
        }).mappings().first()
    except Exception:
        logger.warning(
            "[cost_aware_gate] RH TCA cost lookup failed for %s/%s",
            ticker,
            side,
            exc_info=True,
        )
        return 0, None

    if not row:
        return 0, None

    samples = _nonnegative_int(row.get("sample_trades"), default=0)
    avg_entry = _nonnegative_float(row.get("avg_entry_slippage_bps"))
    p90_entry = _nonnegative_float(row.get("p90_entry_slippage_bps"))
    if samples < min_samples_i:
        return 0, {
            "sample_trades": samples,
            "min_samples": min_samples_i,
            "window_days": window_days_i,
            "used": False,
            "reason": "insufficient_samples",
        }
    invalid_fields = []
    if avg_entry is None:
        invalid_fields.append("avg_entry_slippage_bps")
    if p90_entry is None:
        invalid_fields.append("p90_entry_slippage_bps")
    if invalid_fields:
        return 0, {
            "sample_trades": samples,
            "min_samples": min_samples_i,
            "window_days": window_days_i,
            "used": False,
            "reason": "invalid_tca_estimate",
            "invalid_fields": invalid_fields,
        }

    cost_basis_bps = max(avg_entry, p90_entry)
    tca_cost_bps = int(round(cost_basis_bps))
    return tca_cost_bps, {
        "sample_trades": samples,
        "min_samples": min_samples_i,
        "sample_basis": "usable_robinhood_adverse_entry_tca_trades",
        "included_trade_statuses": "closed_or_fill_backed_open",
        "window_days": window_days_i,
        "avg_entry_slippage_bps": round(avg_entry, 4),
        "p90_entry_slippage_bps": round(p90_entry, 4),
        "cost_basis": "max_avg_p90_adverse_entry_slippage_bps",
        "cost_basis_bps": round(cost_basis_bps, 4),
        "tca_cost_bps": tca_cost_bps,
        "outlier_bps": outlier_bps,
        "used": True,
    }


def _coinbase_tca_backing_usable_samples(
    *,
    db,
    ticker: str,
    side: str,
    window_days: int,
) -> int | None:
    try:
        from ...config import settings as _settings

        outlier_bps = _nonnegative_float(
            getattr(_settings, "brain_execution_cost_unverified_tca_outlier_bps", 500.0)
        )
    except Exception:
        outlier_bps = 500.0
    if outlier_bps is None or outlier_bps <= 0:
        outlier_bps = 500.0
    try:
        result = db.execute(text("""
            SELECT CAST(COUNT(*) AS INTEGER) AS usable_samples
            FROM trading_trades
            WHERE UPPER(ticker) = UPPER(:ticker)
              AND status = 'closed'
              AND LOWER(COALESCE(direction, 'long')) = LOWER(:side)
              AND entry_date >= NOW() - (:window_days * INTERVAL '1 day')
              AND (
                  (
                      tca_entry_slippage_bps IS NOT NULL
                      AND CAST(tca_entry_slippage_bps AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity')
                      AND (
                          ABS(tca_entry_slippage_bps) <= :outlier_bps
                          OR COALESCE(avg_fill_price, 0) > 0
                          OR COALESCE(NULLIF(TRIM(broker_order_id), ''), '') <> ''
                          OR LOWER(COALESCE(broker_status, '')) IN ('filled', 'partially_filled')
                      )
                  )
                  OR (
                      tca_exit_slippage_bps IS NOT NULL
                      AND CAST(tca_exit_slippage_bps AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity')
                      AND (
                          ABS(tca_exit_slippage_bps) <= :outlier_bps
                          OR COALESCE(avg_fill_price, 0) > 0
                          OR COALESCE(NULLIF(TRIM(broker_order_id), ''), '') <> ''
                          OR LOWER(COALESCE(broker_status, '')) IN ('filled', 'partially_filled')
                      )
                  )
              )
        """), {
            "ticker": ticker,
            "side": side,
            "window_days": int(max(1, window_days)),
            "outlier_bps": float(outlier_bps),
        })
        value = result.scalar()
        if isinstance(value, bool):
            return None
        return max(0, int(value or 0))
    except Exception:
        logger.warning(
            "[cost_aware_gate] TCA usable-sample validation failed for %s/%s",
            ticker,
            side,
            exc_info=True,
        )
        return None


# ── Per-venue cap check ──────────────────────────────────────────────


def per_venue_cap_check(
    *,
    venue: str,
    proposed_notional_usd: float,
    db,
    user_id: Optional[int] = None,
    settings_=None,
) -> CapDecision:
    """Per-venue notional + concurrent-position cap. Independent
    per-venue per Phase 1 design constraint #1.

    For Coinbase: caps come from ``chili_coinbase_max_notional_usd``
    and ``chili_coinbase_max_concurrent_positions``. For other
    venues today this returns ``allowed=True`` (the existing RH
    autotrader has its own size/heat gates upstream).

    Reads currently-open CHILI-managed/autotrader Trades from
    ``trading_trades`` filtered to ``broker_source = venue``. Passive
    broker-sync rows are intentionally ignored here; otherwise an old
    manually-held Coinbase position consumes the strategy's concurrency lane.
    """
    venue_l = (venue or "").strip().lower()
    s = settings_
    if s is None:
        try:
            from ...config import settings as _s
            s = _s
        except Exception:
            s = None

    if venue_l != "coinbase":
        # RH cap stays in existing RH-side logic; Phase 5 doesn't
        # touch it.
        return CapDecision(
            allowed=True, reason=REASON_CAP_OK,
            current_positions=0, current_notional_usd=0.0,
        )

    max_notional = (
        _nonnegative_float(getattr(s, "chili_coinbase_max_notional_usd", 0.0))
        or 0.0
    )
    max_positions = _nonnegative_int(
        getattr(s, "chili_coinbase_max_concurrent_positions", 0),
        default=0,
    )

    try:
        from sqlalchemy import text
        relation = _coinbase_cap_source_relation(s)
        rows = db.execute(text(f"""
            SELECT id,
                   COALESCE(quantity * entry_price, 0.0) AS notional
              FROM {relation}
             WHERE status = 'open'
               AND LOWER(COALESCE(broker_source, '')) = 'coinbase'
               AND (
                    LOWER(COALESCE(auto_trader_version, '')) = 'v1'
                    OR LOWER(COALESCE(management_scope, '')) = 'auto_trader_v1'
               )
        """)).fetchall()
        current_positions = len(rows)
        current_notional = sum(_nonnegative_float(r.notional) or 0.0 for r in rows)
    except Exception as exc:
        logger.warning(
            "[cost_aware_gate] per_venue_cap_check query failed: %s", exc,
            exc_info=True,
        )
        # Conservative on failure: assume cap is consumed -> block.
        return CapDecision(
            allowed=False, reason=REASON_CAP_NOTIONAL,
            current_positions=999, current_notional_usd=99999.0,
        )

    if max_positions > 0 and current_positions >= max_positions:
        return CapDecision(
            allowed=False, reason=REASON_CAP_POSITIONS,
            current_positions=current_positions,
            current_notional_usd=current_notional,
        )
    proposed_notional = _nonnegative_float(proposed_notional_usd)
    if proposed_notional is None:
        return CapDecision(
            allowed=False, reason=REASON_CAP_NOTIONAL,
            current_positions=current_positions,
            current_notional_usd=current_notional,
        )
    if max_notional > 0 and (current_notional + proposed_notional) > max_notional:
        return CapDecision(
            allowed=False, reason=REASON_CAP_NOTIONAL,
            current_positions=current_positions,
            current_notional_usd=current_notional,
        )
    return CapDecision(
        allowed=True, reason=REASON_CAP_OK,
        current_positions=current_positions,
        current_notional_usd=current_notional,
    )


__all__ = [
    "CapDecision",
    "CostGateDecision",
    "REASON_CAP_NOTIONAL",
    "REASON_CAP_OK",
    "REASON_CAP_POSITIONS",
    "REASON_GATE_COINBASE_BLOCKED",
    "REASON_GATE_COINBASE_PASSED",
    "REASON_GATE_NO_VENUE",
    "REASON_GATE_RH_FEE_FREE",
    "REASON_GATE_RH_TCA_BLOCKED",
    "REASON_GATE_RH_TCA_PASSED",
    "REASON_GATE_TCA_INVALID",
    "REASON_GATE_TCA_UNPROVEN",
    "cost_aware_min_edge_gate",
    "per_venue_cap_check",
    "resolve_coinbase_buying_power",
]

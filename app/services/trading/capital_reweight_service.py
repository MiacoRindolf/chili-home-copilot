"""Phase I - persistence layer for the weekly capital re-weighter.

Computes the inverse-vol proposal via the pure model and writes one
row into ``trading_capital_reweight_log`` per sweep. Shadow-safe:
never resizes open positions until Phase I.2 promotion.

Design
------

* **Single public entry-point.** The APScheduler job and the
  diagnostics endpoint both call :func:`run_sweep`.
* **Refuses authoritative.** Until Phase I.2 opens explicitly the
  service raises :class:`RuntimeError` if any caller sets
  ``mode_override="authoritative"`` or the setting is flipped to
  authoritative. Release-blocker scripts will also scan for
  ``mode=authoritative`` in the ops log as a belt-and-braces gate.
* **Append-only.** Every sweep appends a new row; the
  ``reweight_id`` from the pure model stays stable for idempotent
  same-day re-runs so callers can dedupe.
* **Off-mode short-circuit.** When ``brain_capital_reweight_mode ==
  'off'`` :func:`run_sweep` is a no-op and returns ``None``.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings
from ...trading_brain.infrastructure.capital_reweight_ops_log import (
    format_capital_reweight_ops_line,
)
from .capital_reweight_model import (
    BucketAllocation,
    BucketContext,
    CapitalReweightConfig,
    CapitalReweightInput,
    CapitalReweightOutput,
    CovMatrixProvider,
    compute_reweight,
)

logger = logging.getLogger(__name__)
_ALLOWED_MODES = ("off", "shadow", "compare", "authoritative")


def _effective_mode(override: str | None = None) -> str:
    m = (override or getattr(settings, "brain_capital_reweight_mode", "off") or "off").lower()
    return m if m in _ALLOWED_MODES else "off"


def mode_is_active(override: str | None = None) -> bool:
    return _effective_mode(override) != "off"


def mode_is_authoritative(override: str | None = None) -> bool:
    return _effective_mode(override) == "authoritative"


def _ops_log_enabled() -> bool:
    return bool(getattr(settings, "brain_capital_reweight_ops_log_enabled", True))


def _config_from_settings() -> CapitalReweightConfig:
    return CapitalReweightConfig(
        max_single_bucket_pct=float(
            getattr(settings, "brain_capital_reweight_max_single_bucket_pct", 35.0)
        ),
        min_weight_pct=0.0,
        regime_tilt_enabled=True,
    )


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SweepResult:
    log_id: int
    reweight_id: str
    mode: str
    mean_drift_bps: float
    p90_drift_bps: float
    single_bucket_cap_triggered: bool
    concentration_cap_triggered: bool


# ---------------------------------------------------------------------------
# Allocations (de)serialization
# ---------------------------------------------------------------------------


def _allocations_to_proposed_json(allocs: tuple[BucketAllocation, ...]) -> list[dict[str, Any]]:
    return [
        {
            "bucket": a.bucket,
            "target_notional": float(a.target_notional),
            "target_weight_pct": float(a.target_weight_pct),
            "drift_bps": float(a.drift_bps),
            "cap_triggered": bool(a.cap_triggered),
            "rationale": a.rationale,
        }
        for a in allocs
    ]


def _allocations_to_current_json(allocs: tuple[BucketAllocation, ...]) -> list[dict[str, Any]]:
    return [
        {
            "bucket": a.bucket,
            "current_notional": float(a.current_notional),
            "current_weight_pct": float(a.current_weight_pct),
        }
        for a in allocs
    ]


def _drift_buckets(allocs: tuple[BucketAllocation, ...]) -> dict[str, int]:
    buckets = {
        "under_50_bps": 0,
        "50_200_bps": 0,
        "200_1000_bps": 0,
        "over_1000_bps": 0,
    }
    for a in allocs:
        d = float(a.drift_bps)
        if d < 50.0:
            buckets["under_50_bps"] += 1
        elif d < 200.0:
            buckets["50_200_bps"] += 1
        elif d < 1000.0:
            buckets["200_1000_bps"] += 1
        else:
            buckets["over_1000_bps"] += 1
    return buckets


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_sweep(
    db: Session,
    *,
    user_id: int | None,
    as_of_date: date | str,
    total_capital: float,
    regime: str | None,
    dial_value: float,
    buckets: Sequence[BucketContext],
    mode_override: str | None = None,
    config: CapitalReweightConfig | None = None,
    cov_matrix_provider: CovMatrixProvider | None = None,
) -> SweepResult | None:
    """Compute and persist one capital re-weight proposal.

    Returns ``None`` when the mode is ``off``. Raises
    :class:`RuntimeError` when any caller tries to run the sweep in
    ``authoritative`` mode before Phase I.2 opens explicitly.
    """
    mode = _effective_mode(mode_override)
    if mode == "off":
        return None
    if mode == "authoritative":
        if _ops_log_enabled():
            logger.warning(
                format_capital_reweight_ops_line(
                    event="sweep_refused_authoritative",
                    mode=mode,
                    user_id=user_id,
                    reason="phase_i_2_not_opened",
                )
            )
        raise RuntimeError(
            "capital_reweight authoritative mode is not permitted "
            "until Phase I.2 is explicitly opened",
        )

    cfg = config or _config_from_settings()
    as_of_str = (
        as_of_date.isoformat() if isinstance(as_of_date, date) else str(as_of_date)
    )

    inp = CapitalReweightInput(
        user_id=user_id,
        as_of_date=as_of_str,
        total_capital=float(total_capital),
        regime=regime,
        dial_value=float(dial_value),
        buckets=tuple(buckets),
    )
    out: CapitalReweightOutput = compute_reweight(
        inp,
        config=cfg,
        cov_matrix_provider=cov_matrix_provider,
    )

    proposed_json = _allocations_to_proposed_json(out.allocations)
    current_json = _allocations_to_current_json(out.allocations)
    drift_bucket_json = _drift_buckets(out.allocations)

    single_cap = bool(out.cap_triggers.get("single_bucket", 0))
    concentration_cap = bool(out.cap_triggers.get("concentration", 0))

    now = datetime.utcnow()
    row = db.execute(text("""
        INSERT INTO trading_capital_reweight_log (
            reweight_id, user_id, as_of_date, regime,
            total_capital, proposed_allocations_json, current_allocations_json,
            drift_bucket_json, mean_drift_bps, p90_drift_bps,
            cap_triggers_json, mode, observed_at
        ) VALUES (
            :reweight_id, :user_id, CAST(:as_of_date AS DATE), :regime,
            :total_capital, CAST(:proposed AS JSONB), CAST(:current AS JSONB),
            CAST(:drift_bucket AS JSONB), :mean_drift, :p90_drift,
            CAST(:cap_triggers AS JSONB), :mode, :now
        )
        RETURNING id
    """), {
        "reweight_id": out.reweight_id,
        "user_id": user_id,
        "as_of_date": as_of_str,
        "regime": out.regime,
        "total_capital": float(out.total_capital),
        "proposed": json.dumps(proposed_json, default=str, separators=(",", ":")),
        "current": json.dumps(current_json, default=str, separators=(",", ":")),
        "drift_bucket": json.dumps(drift_bucket_json, default=str, separators=(",", ":")),
        "mean_drift": float(out.mean_drift_bps),
        "p90_drift": float(out.p90_drift_bps),
        "cap_triggers": json.dumps(out.cap_triggers, default=str, separators=(",", ":")),
        "mode": mode,
        "now": now,
    })
    new_id = int(row.scalar_one())
    db.commit()

    if _ops_log_enabled():
        logger.info(
            format_capital_reweight_ops_line(
                event="sweep_persisted",
                mode=mode,
                reweight_id=out.reweight_id,
                user_id=user_id,
                as_of_date=as_of_str,
                regime=out.regime,
                total_capital=float(out.total_capital),
                mean_drift_bps=float(out.mean_drift_bps),
                p90_drift_bps=float(out.p90_drift_bps),
                bucket_count=len(out.allocations),
                single_bucket_cap_triggered=single_cap,
                concentration_cap_triggered=concentration_cap,
                bucket_resized=False,  # always false in shadow
            )
        )

    return SweepResult(
        log_id=new_id,
        reweight_id=out.reweight_id,
        mode=mode,
        mean_drift_bps=float(out.mean_drift_bps),
        p90_drift_bps=float(out.p90_drift_bps),
        single_bucket_cap_triggered=single_cap,
        concentration_cap_triggered=concentration_cap,
    )


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def sweep_summary(
    db: Session,
    *,
    lookback_days: int = 14,
) -> dict[str, Any]:
    """Frozen-shape diagnostics summary for capital re-weights.

    Keys (stable, order-preserving):
      * mode
      * lookback_days
      * sweeps_total
      * mean_mean_drift_bps
      * p90_p90_drift_bps
      * single_bucket_cap_trigger_count
      * concentration_cap_trigger_count
      * latest_sweep {reweight_id, user_id, as_of_date, regime,
                      mean_drift_bps, p90_drift_bps, observed_at}
    """
    mode = _effective_mode()

    total = int(db.execute(text("""
        SELECT COUNT(*) FROM trading_capital_reweight_log
        WHERE observed_at >= (NOW() - (:ld || ' days')::INTERVAL)
    """), {"ld": int(lookback_days)}).scalar_one() or 0)

    agg = db.execute(text("""
        SELECT AVG(mean_drift_bps), AVG(p90_drift_bps)
        FROM trading_capital_reweight_log
        WHERE observed_at >= (NOW() - (:ld || ' days')::INTERVAL)
    """), {"ld": int(lookback_days)}).fetchone()
    mean_mean = float(agg[0]) if agg and agg[0] is not None else 0.0
    p90_p90 = float(agg[1]) if agg and agg[1] is not None else 0.0

    trig = db.execute(text("""
        SELECT
            COUNT(*) FILTER (WHERE (cap_triggers_json->>'single_bucket')::INT > 0),
            COUNT(*) FILTER (WHERE (cap_triggers_json->>'concentration')::INT > 0)
        FROM trading_capital_reweight_log
        WHERE observed_at >= (NOW() - (:ld || ' days')::INTERVAL)
    """), {"ld": int(lookback_days)}).fetchone()
    single_count = int(trig[0] if trig and trig[0] is not None else 0)
    concentration_count = int(trig[1] if trig and trig[1] is not None else 0)

    latest = db.execute(text("""
        SELECT reweight_id, user_id, as_of_date, regime,
               mean_drift_bps, p90_drift_bps, observed_at
        FROM trading_capital_reweight_log
        ORDER BY observed_at DESC
        LIMIT 1
    """)).fetchone()
    latest_payload: dict[str, Any] | None = None
    if latest:
        latest_payload = {
            "reweight_id": latest[0],
            "user_id": latest[1],
            "as_of_date": latest[2].isoformat() if latest[2] else None,
            "regime": latest[3],
            "mean_drift_bps": float(latest[4]) if latest[4] is not None else None,
            "p90_drift_bps": float(latest[5]) if latest[5] is not None else None,
            "observed_at": latest[6].isoformat() if latest[6] else None,
        }

    return {
        "mode": mode,
        "lookback_days": int(lookback_days),
        "sweeps_total": total,
        "mean_mean_drift_bps": round(mean_mean, 2),
        "p90_p90_drift_bps": round(p90_p90, 2),
        "single_bucket_cap_trigger_count": single_count,
        "concentration_cap_trigger_count": concentration_count,
        "latest_sweep": latest_payload,
    }


__all__ = [
    "SweepResult",
    "mode_is_active",
    "mode_is_authoritative",
    "run_sweep",
    "sweep_summary",
]

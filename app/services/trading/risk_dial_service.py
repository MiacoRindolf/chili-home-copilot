"""Phase I - persistence layer for the canonical risk dial.

Resolves the risk dial via the pure model and writes one row into
``trading_risk_dial_state`` per resolution event. Shadow-safe: never
affects sizing until Phase I.2 integrates the dial inside
:mod:`position_sizer_model`.

Design
------

* **Single public entry-point.** All callers (scheduler job, diagnostic
  endpoint, ad-hoc overrides) go through :func:`resolve_dial`.
* **Append-only.** Every resolution appends a new row - history is the
  audit trail. ``dial_id`` from the pure model stays stable so
  release-blocker scripts can grep by ``dial_id=`` if needed.
* **Off-mode short-circuit.** When ``brain_risk_dial_mode == 'off'``
  the service is a no-op and returns ``None``. Callers can invoke it
  unconditionally.
* **Pure -> persist -> log.** The resolution is computed in the pure
  module, then the service persists and emits the structured ops line.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings
from ...trading_brain.infrastructure.risk_dial_ops_log import (
    format_risk_dial_ops_line,
)
from .risk_dial_model import (
    RiskDialConfig,
    RiskDialInput,
    RiskDialOutput,
    compute_dial,
    compute_dial_id,
)

logger = logging.getLogger(__name__)
_ALLOWED_MODES = ("off", "shadow", "compare", "authoritative")


def _effective_mode(override: str | None = None) -> str:
    m = (override or getattr(settings, "brain_risk_dial_mode", "off") or "off").lower()
    return m if m in _ALLOWED_MODES else "off"


def mode_is_active(override: str | None = None) -> bool:
    return _effective_mode(override) != "off"


def mode_is_authoritative(override: str | None = None) -> bool:
    return _effective_mode(override) == "authoritative"


def _ops_log_enabled() -> bool:
    return bool(getattr(settings, "brain_risk_dial_ops_log_enabled", True))


def _config_from_settings() -> RiskDialConfig:
    return RiskDialConfig(
        default_risk_on=float(getattr(settings, "brain_risk_dial_default_risk_on", 1.0)),
        default_cautious=float(getattr(settings, "brain_risk_dial_default_cautious", 0.7)),
        default_risk_off=float(getattr(settings, "brain_risk_dial_default_risk_off", 0.3)),
        drawdown_floor=float(getattr(settings, "brain_risk_dial_drawdown_floor", 0.5)),
        drawdown_trigger_pct=float(
            getattr(settings, "brain_risk_dial_drawdown_trigger_pct", 10.0)
        ),
        ceiling=float(getattr(settings, "brain_risk_dial_ceiling", 1.5)),
    )


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DialResolution:
    log_id: int
    dial_id: str
    dial_value: float
    regime: str | None
    mode: str
    override_rejected: bool
    capped_at_ceiling: bool


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_dial(
    db: Session,
    *,
    user_id: int | None,
    regime: str | None,
    drawdown_pct: float = 0.0,
    user_override_multiplier: float | None = None,
    source: str,
    reason: str | None = None,
    mode_override: str | None = None,
    config: RiskDialConfig | None = None,
) -> DialResolution | None:
    """Resolve and persist the risk dial. No-op when mode is ``off``."""
    mode = _effective_mode(mode_override)
    if mode == "off":
        return None

    cfg = config or _config_from_settings()
    dial_input = RiskDialInput(
        regime=regime,
        drawdown_pct=float(drawdown_pct),
        user_override_multiplier=user_override_multiplier,
        user_id=user_id,
    )
    out: RiskDialOutput = compute_dial(dial_input, config=cfg)
    dial_id = compute_dial_id(
        user_id=user_id,
        regime=regime,
        config=cfg,
    )

    payload = {
        "dial_id": dial_id,
        "regime_default": out.regime_default,
        "drawdown_pct": float(drawdown_pct),
        "drawdown_multiplier": out.drawdown_multiplier,
        "override_multiplier": out.override_multiplier,
        "override_rejected": out.override_rejected,
        "capped_at_ceiling": out.capped_at_ceiling,
        "ceiling": cfg.ceiling,
        "reasoning": out.reasoning,
    }

    now = datetime.utcnow()
    row = db.execute(text("""
        INSERT INTO trading_risk_dial_state (
            user_id, dial_value, regime, source, reason,
            mode, payload_json, observed_at
        ) VALUES (
            :user_id, :dial_value, :regime, :source, :reason,
            :mode, CAST(:payload_json AS JSONB), :now
        )
        RETURNING id
    """), {
        "user_id": user_id,
        "dial_value": float(out.dial_value),
        "regime": out.regime,
        "source": source,
        "reason": reason,
        "mode": mode,
        "payload_json": json.dumps(payload, default=str, separators=(",", ":")),
        "now": now,
    })
    new_id = int(row.scalar_one())
    db.commit()

    if _ops_log_enabled():
        logger.info(
            format_risk_dial_ops_line(
                event="dial_persisted",
                mode=mode,
                user_id=user_id,
                dial_value=out.dial_value,
                regime=out.regime,
                source=source,
                reason=reason,
                regime_default=out.regime_default,
                drawdown_pct=float(drawdown_pct),
                drawdown_multiplier=out.drawdown_multiplier,
                override_multiplier=out.override_multiplier,
                ceiling=cfg.ceiling,
                capped_at_ceiling=out.capped_at_ceiling,
                dial_id=dial_id,
                override_rejected=out.override_rejected,
            )
        )

    return DialResolution(
        log_id=new_id,
        dial_id=dial_id,
        dial_value=float(out.dial_value),
        regime=out.regime,
        mode=mode,
        override_rejected=out.override_rejected,
        capped_at_ceiling=out.capped_at_ceiling,
    )


def get_latest_dial(
    db: Session,
    *,
    user_id: int | None,
    default: float = 1.0,
) -> float:
    """Return the most recent persisted dial for ``user_id``.

    Falls back to ``default`` if no rows exist or the mode is ``off``.
    This is the read path Phase H emitters will consume once the Phase
    I.2 integration is flipped on (shadow-only in Phase I).
    """
    if _effective_mode() == "off":
        return float(default)
    row = db.execute(text("""
        SELECT dial_value FROM trading_risk_dial_state
        WHERE (user_id = :user_id OR (user_id IS NULL AND :user_id IS NULL))
        ORDER BY observed_at DESC
        LIMIT 1
    """), {"user_id": user_id}).fetchone()
    if not row:
        return float(default)
    return float(row[0])


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


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


def dial_state_summary(
    db: Session,
    *,
    lookback_hours: int = 24,
) -> dict[str, Any]:
    """Frozen-shape diagnostics summary for the risk dial.

    Keys (stable, order-preserving):
      * mode
      * lookback_hours
      * dial_events_total
      * by_regime { regime: count }
      * by_source { source: count }
      * by_dial_bucket { under_0_5 / 0_5_to_0_8 / 0_8_to_1_0 / 1_0_to_1_2 / over_1_2 }
      * mean_dial_value
      * latest_dial { user_id, dial_value, regime, source, observed_at }
      * override_rejected_count
      * capped_at_ceiling_count
    """
    mode = _effective_mode()

    total = int(db.execute(text("""
        SELECT COUNT(*) FROM trading_risk_dial_state
        WHERE observed_at >= (NOW() - (:lh || ' hours')::INTERVAL)
    """), {"lh": int(lookback_hours)}).scalar_one() or 0)

    regime_rows = db.execute(text("""
        SELECT COALESCE(regime, 'none'), COUNT(*)
        FROM trading_risk_dial_state
        WHERE observed_at >= (NOW() - (:lh || ' hours')::INTERVAL)
        GROUP BY COALESCE(regime, 'none')
    """), {"lh": int(lookback_hours)}).fetchall()
    by_regime = {r[0]: int(r[1]) for r in regime_rows}

    source_rows = db.execute(text("""
        SELECT source, COUNT(*) FROM trading_risk_dial_state
        WHERE observed_at >= (NOW() - (:lh || ' hours')::INTERVAL)
        GROUP BY source
    """), {"lh": int(lookback_hours)}).fetchall()
    by_source = {r[0]: int(r[1]) for r in source_rows}

    value_rows = db.execute(text("""
        SELECT dial_value FROM trading_risk_dial_state
        WHERE observed_at >= (NOW() - (:lh || ' hours')::INTERVAL)
    """), {"lh": int(lookback_hours)}).fetchall()
    values = [float(r[0]) for r in value_rows if r[0] is not None]

    by_dial_bucket = {
        "under_0_5": 0,
        "0_5_to_0_8": 0,
        "0_8_to_1_0": 0,
        "1_0_to_1_2": 0,
        "over_1_2": 0,
    }
    for v in values:
        by_dial_bucket[_bucket_dial(v)] += 1

    mean_val = sum(values) / len(values) if values else 0.0

    latest = db.execute(text("""
        SELECT user_id, dial_value, regime, source, observed_at
        FROM trading_risk_dial_state
        ORDER BY observed_at DESC
        LIMIT 1
    """)).fetchone()
    latest_payload: dict[str, Any] | None = None
    if latest:
        latest_payload = {
            "user_id": latest[0],
            "dial_value": float(latest[1]) if latest[1] is not None else None,
            "regime": latest[2],
            "source": latest[3],
            "observed_at": latest[4].isoformat() if latest[4] else None,
        }

    reject_row = db.execute(text("""
        SELECT
            COUNT(*) FILTER (WHERE payload_json->>'override_rejected' = 'true'),
            COUNT(*) FILTER (WHERE payload_json->>'capped_at_ceiling' = 'true')
        FROM trading_risk_dial_state
        WHERE observed_at >= (NOW() - (:lh || ' hours')::INTERVAL)
    """), {"lh": int(lookback_hours)}).fetchone()
    reject_count = int(reject_row[0] if reject_row and reject_row[0] is not None else 0)
    cap_count = int(reject_row[1] if reject_row and reject_row[1] is not None else 0)

    return {
        "mode": mode,
        "lookback_hours": int(lookback_hours),
        "dial_events_total": total,
        "by_regime": by_regime,
        "by_source": by_source,
        "by_dial_bucket": by_dial_bucket,
        "mean_dial_value": round(mean_val, 4),
        "latest_dial": latest_payload,
        "override_rejected_count": reject_count,
        "capped_at_ceiling_count": cap_count,
    }


__all__ = [
    "DialResolution",
    "dial_state_summary",
    "get_latest_dial",
    "mode_is_active",
    "mode_is_authoritative",
    "resolve_dial",
]

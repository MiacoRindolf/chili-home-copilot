"""Code Brain runtime state — singleton row in ``code_brain_runtime_state``.

This is the single source of truth for:
  * dispatch mode (``reactive`` | ``legacy_60s`` | ``paused``)
  * daily premium-USD budget cap and current spend
  * decision-router thresholds (template confidence, novelty)
  * distillation promotion state (whether to prefer the local model)
  * last pattern-mining timestamp

Mirrors ``app/services/trading/governance.py`` and ``trading_risk_state``
patterns from the trading brain. All reads/writes go through this module
so the schema stays the only authority — callers don't ad-hoc the SQL.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass
class CodeBrainRuntimeState:
    mode: str
    daily_premium_usd_cap: Decimal
    spent_today_usd: Decimal
    spend_reset_date: date
    template_min_confidence: Decimal
    novelty_premium_threshold: Decimal
    local_model_promoted: bool
    local_model_tag: Optional[str]
    last_pattern_mining_at: Optional[datetime]
    updated_at: Optional[datetime]


def _row_to_state(row: Any) -> CodeBrainRuntimeState:
    return CodeBrainRuntimeState(
        mode=str(row[0]),
        daily_premium_usd_cap=Decimal(row[1] or 0),
        spent_today_usd=Decimal(row[2] or 0),
        spend_reset_date=row[3] or date.today(),
        template_min_confidence=Decimal(row[4] or 0),
        novelty_premium_threshold=Decimal(row[5] or 0),
        local_model_promoted=bool(row[6]),
        local_model_tag=(row[7] if row[7] else None),
        last_pattern_mining_at=row[8],
        updated_at=row[9],
    )


def get_state(db: Session) -> CodeBrainRuntimeState:
    """Read the singleton row. Caller is responsible for the session."""
    row = db.execute(
        text(
            "SELECT mode, daily_premium_usd_cap, spent_today_usd, "
            "       spend_reset_date, template_min_confidence, "
            "       novelty_premium_threshold, local_model_promoted, "
            "       local_model_tag, last_pattern_mining_at, updated_at "
            "FROM code_brain_runtime_state WHERE id = 1"
        )
    ).fetchone()
    if not row:
        # Defensive: table exists but row missing (migration ran with empty insert?).
        # Insert defaults on demand.
        db.execute(text("INSERT INTO code_brain_runtime_state (id) VALUES (1) ON CONFLICT DO NOTHING"))
        db.commit()
        row = db.execute(
            text(
                "SELECT mode, daily_premium_usd_cap, spent_today_usd, "
                "       spend_reset_date, template_min_confidence, "
                "       novelty_premium_threshold, local_model_promoted, "
                "       local_model_tag, last_pattern_mining_at, updated_at "
                "FROM code_brain_runtime_state WHERE id = 1"
            )
        ).fetchone()
    return _row_to_state(row)


_VALID_MODES = frozenset({"reactive", "legacy_60s", "paused"})


def set_mode(db: Session, mode: str) -> None:
    if mode not in _VALID_MODES:
        raise ValueError(f"invalid mode {mode!r}; expected one of {sorted(_VALID_MODES)}")
    db.execute(
        text(
            "UPDATE code_brain_runtime_state "
            "SET mode = :m, updated_at = NOW() WHERE id = 1"
        ),
        {"m": mode},
    )
    db.commit()
    logger.info("[code_brain.runtime_state] mode=%s", mode)


def reset_daily_spend_if_new_day(db: Session) -> None:
    """Reset ``spent_today_usd`` to 0 when the calendar day rolls over.

    Idempotent — safe to call from any cycle. Uses the database's CURRENT_DATE
    so client clock skew doesn't matter.
    """
    db.execute(
        text(
            "UPDATE code_brain_runtime_state "
            "SET spent_today_usd = 0, "
            "    spend_reset_date = CURRENT_DATE, "
            "    updated_at = NOW() "
            "WHERE id = 1 AND spend_reset_date < CURRENT_DATE"
        )
    )
    db.commit()


def record_premium_spend(db: Session, cost_usd: float) -> Decimal:
    """Add ``cost_usd`` to today's running total. Returns new total.

    Also performs the daily reset on the same call if the day flipped, so
    callers don't have to coordinate.
    """
    if cost_usd <= 0:
        return get_state(db).spent_today_usd
    reset_daily_spend_if_new_day(db)
    row = db.execute(
        text(
            "UPDATE code_brain_runtime_state "
            "SET spent_today_usd = spent_today_usd + :c, "
            "    updated_at = NOW() "
            "WHERE id = 1 "
            "RETURNING spent_today_usd"
        ),
        {"c": Decimal(str(cost_usd))},
    ).fetchone()
    db.commit()
    return Decimal(row[0]) if row else Decimal(0)


def get_daily_remaining_usd(db: Session) -> Decimal:
    """Return how much paid-LLM budget is left for today (>=0)."""
    reset_daily_spend_if_new_day(db)
    s = get_state(db)
    rem = s.daily_premium_usd_cap - s.spent_today_usd
    return rem if rem > 0 else Decimal(0)


def is_local_model_promoted(db: Session) -> bool:
    return get_state(db).local_model_promoted


def mark_pattern_mining_complete(db: Session) -> None:
    db.execute(
        text(
            "UPDATE code_brain_runtime_state "
            "SET last_pattern_mining_at = NOW(), updated_at = NOW() "
            "WHERE id = 1"
        )
    )
    db.commit()


def set_thresholds(
    db: Session,
    *,
    template_min_confidence: Optional[float] = None,
    novelty_premium_threshold: Optional[float] = None,
    daily_premium_usd_cap: Optional[float] = None,
) -> None:
    """Operator-facing tuning. None means leave-as-is."""
    parts: list[str] = []
    params: dict[str, Any] = {}
    if template_min_confidence is not None:
        parts.append("template_min_confidence = :tmc")
        params["tmc"] = Decimal(str(template_min_confidence))
    if novelty_premium_threshold is not None:
        parts.append("novelty_premium_threshold = :npt")
        params["npt"] = Decimal(str(novelty_premium_threshold))
    if daily_premium_usd_cap is not None:
        parts.append("daily_premium_usd_cap = :cap")
        params["cap"] = Decimal(str(daily_premium_usd_cap))
    if not parts:
        return
    parts.append("updated_at = NOW()")
    db.execute(
        text(
            "UPDATE code_brain_runtime_state SET "
            + ", ".join(parts)
            + " WHERE id = 1"
        ),
        params,
    )
    db.commit()

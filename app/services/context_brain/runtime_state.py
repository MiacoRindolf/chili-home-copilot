"""Singleton row in ``context_brain_runtime_state``.

Mirrors the trading and code brain runtime-state pattern. Single
source of truth for: mode, token budget, distillation cap, learning
toggles, current strategy version.
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
class ContextBrainRuntimeState:
    mode: str
    token_budget_per_request: int
    distillation_threshold_tokens: int
    daily_distillation_usd_cap: Decimal
    spent_today_distillation_usd: Decimal
    spend_reset_date: date
    learning_enabled: bool
    distillation_enabled: bool
    learned_strategy_version: int
    last_learning_cycle_at: Optional[datetime]
    updated_at: Optional[datetime]


_VALID_MODES = frozenset({"reactive", "learning", "paused", "shadow"})


def _row_to_state(row: Any) -> ContextBrainRuntimeState:
    return ContextBrainRuntimeState(
        mode=str(row[0]),
        token_budget_per_request=int(row[1] or 8000),
        distillation_threshold_tokens=int(row[2] or 12000),
        daily_distillation_usd_cap=Decimal(row[3] or 0),
        spent_today_distillation_usd=Decimal(row[4] or 0),
        spend_reset_date=row[5] or date.today(),
        learning_enabled=bool(row[6]),
        distillation_enabled=bool(row[7]),
        learned_strategy_version=int(row[8] or 1),
        last_learning_cycle_at=row[9],
        updated_at=row[10],
    )


def get_state(db: Session) -> ContextBrainRuntimeState:
    row = db.execute(
        text(
            "SELECT mode, token_budget_per_request, distillation_threshold_tokens, "
            "       daily_distillation_usd_cap, spent_today_distillation_usd, "
            "       spend_reset_date, learning_enabled, distillation_enabled, "
            "       learned_strategy_version, last_learning_cycle_at, updated_at "
            "FROM context_brain_runtime_state WHERE id = 1"
        )
    ).fetchone()
    if not row:
        # Migration didn't seed for some reason; insert defaults.
        db.execute(text(
            "INSERT INTO context_brain_runtime_state (id) "
            "VALUES (1) ON CONFLICT DO NOTHING"
        ))
        db.commit()
        row = db.execute(
            text(
                "SELECT mode, token_budget_per_request, distillation_threshold_tokens, "
                "       daily_distillation_usd_cap, spent_today_distillation_usd, "
                "       spend_reset_date, learning_enabled, distillation_enabled, "
                "       learned_strategy_version, last_learning_cycle_at, updated_at "
                "FROM context_brain_runtime_state WHERE id = 1"
            )
        ).fetchone()
    return _row_to_state(row)


def set_mode(db: Session, mode: str) -> None:
    if mode not in _VALID_MODES:
        raise ValueError(f"invalid mode {mode!r}; expected {sorted(_VALID_MODES)}")
    db.execute(
        text(
            "UPDATE context_brain_runtime_state "
            "SET mode = :m, updated_at = NOW() WHERE id = 1"
        ),
        {"m": mode},
    )
    db.commit()
    logger.info("[context_brain.runtime_state] mode=%s", mode)


def reset_daily_spend_if_new_day(db: Session) -> None:
    db.execute(text(
        "UPDATE context_brain_runtime_state "
        "SET spent_today_distillation_usd = 0, "
        "    spend_reset_date = CURRENT_DATE, "
        "    updated_at = NOW() "
        "WHERE id = 1 AND spend_reset_date < CURRENT_DATE"
    ))
    db.commit()


def record_distillation_spend(db: Session, cost_usd: float) -> Decimal:
    if cost_usd <= 0:
        return get_state(db).spent_today_distillation_usd
    reset_daily_spend_if_new_day(db)
    row = db.execute(
        text(
            "UPDATE context_brain_runtime_state "
            "SET spent_today_distillation_usd = spent_today_distillation_usd + :c, "
            "    updated_at = NOW() "
            "WHERE id = 1 RETURNING spent_today_distillation_usd"
        ),
        {"c": Decimal(str(cost_usd))},
    ).fetchone()
    db.commit()
    return Decimal(row[0]) if row else Decimal(0)


def can_distill(db: Session) -> bool:
    s = get_state(db)
    if not s.distillation_enabled:
        return False
    if s.spent_today_distillation_usd >= s.daily_distillation_usd_cap:
        return False
    return True


def mark_learning_cycle_complete(db: Session) -> None:
    db.execute(text(
        "UPDATE context_brain_runtime_state "
        "SET last_learning_cycle_at = NOW(), updated_at = NOW() "
        "WHERE id = 1"
    ))
    db.commit()


def bump_strategy_version(db: Session) -> int:
    """Called by the learning cycle when a new weight set is promoted."""
    row = db.execute(text(
        "UPDATE context_brain_runtime_state "
        "SET learned_strategy_version = learned_strategy_version + 1, "
        "    updated_at = NOW() "
        "WHERE id = 1 RETURNING learned_strategy_version"
    )).fetchone()
    db.commit()
    return int(row[0]) if row else 1

"""Realized-PnL promotion pass — graduate patterns that prove themselves on
clean realized PnL even when their backtest CPCV/OOS gates disagree.

Operator directive (2026-06-05): RANK BY REALIZED PnL. Once the realized-EV
signal is trustworthy -- clean exits, clean ``corrected_*``/``raw_realized_*``
columns, post-floor instrumentation, and no backtest/mining bleed into the
realized-EV gate (the realized_ev_gate / clean-window / stat-conflation work) --
a pattern that is winning on CLEAN realized PnL should be able to graduate on
that evidence. Live realized PnL is the higher-information signal for a pattern
that has actually been trading; CPCV/OOS backtests can be overfit or stale (the
"graduation trap"). The canonical example: pattern 537 (+3.35% / 66.7% WR over 9
clean live trades) sat ``challenged`` because OOS=-4.18% / CPCV=0.63.

Selection (all required):
  * active, and NOT already promoted (lifecycle in candidate/challenged/backtested
    -- not the shadow/pilot ladder, not retired/decayed);
  * passes the clean realized-EV gate (``check_realized_ev_blocking`` -> not
    blocked: net-positive realized avg AND win-rate, with >= min clean trades,
    legacy columns non-authoritative per PR #366);
  * clean realized sample >= ``chili_realized_pnl_promotion_min_trades`` AND
    realized avg >= ``chili_realized_pnl_promotion_min_avg_return_pct`` (a
    MEANINGFUL realized edge, not barely-positive noise).

Ranked by realized average return (best first), capped at
``chili_realized_pnl_promotion_max_per_run`` per run. The KILL SWITCH is honored
here (no new live-eligible patterns during a kill event); the kill switch AND the
drawdown breaker still gate the actual trade at execution time (Hard Rules 1-2),
so promotion never bypasses live-trade safety.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Stuck-backlog stages eligible for realized-PnL graduation. Excludes the
# shadow/pilot promotion ladder (managed elsewhere) and dead stages.
_PROMOTABLE_STAGES = ("candidate", "challenged", "backtested", "pending_oos")


def _settings_get(name: str, default: Any) -> Any:
    try:
        from ...config import settings
        return getattr(settings, name, default)
    except Exception:
        return default


def run_realized_pnl_promotion_pass(db: Session) -> dict[str, Any]:
    """Promote not-yet-promoted patterns proving themselves on clean realized PnL.

    Returns::

        {
          "evaluated": int, "eligible": int, "promoted": int,
          "skipped_disabled": bool, "skipped_kill_switch": bool,
          "promoted_details": [{"id","from_stage","realized_avg_pct","n","source"}],
          "promoted_pattern_ids": [int, ...],
        }
    """
    from ...models.trading import ScanPattern
    from .pattern_stats_accessor import get_realized_pattern_stats
    from .realized_ev_gate import check_realized_ev_blocking

    base: dict[str, Any] = {
        "evaluated": 0, "eligible": 0, "promoted": 0,
        "skipped_disabled": False, "skipped_kill_switch": False,
        "promoted_details": [], "promoted_pattern_ids": [],
    }

    if not bool(_settings_get("chili_realized_pnl_promotion_enabled", True)):
        base["skipped_disabled"] = True
        return base

    # Hard Rule 1: don't add live-eligible patterns during a kill event.
    try:
        from .governance import is_kill_switch_active
        if is_kill_switch_active():
            base["skipped_kill_switch"] = True
            logger.warning("[realized_pnl_promotion] skipped: kill switch active")
            return base
    except Exception:
        pass

    min_trades = int(_settings_get("chili_realized_pnl_promotion_min_trades", 8))
    min_avg = float(_settings_get("chili_realized_pnl_promotion_min_avg_return_pct", 0.5))
    max_promote = int(_settings_get("chili_realized_pnl_promotion_max_per_run", 10))

    candidates = (
        db.query(ScanPattern)
        .filter(
            ScanPattern.active.is_(True),
            ScanPattern.lifecycle_stage.in_(_PROMOTABLE_STAGES),
        )
        .all()
    )
    base["evaluated"] = len(candidates)

    eligible: list[tuple[float, Any, int, str]] = []
    for p in candidates:
        rs = get_realized_pattern_stats(p)  # corrected_* -> raw_realized_* -> missing
        n = rs.trade_count
        avg = rs.avg_return_pct
        if n is None or n < min_trades:
            continue
        if avg is None or float(avg) < min_avg:
            continue
        # Must also pass the clean realized-EV gate (net-positive avg AND win-rate,
        # legacy non-authoritative). This is the same authority used everywhere.
        try:
            blocked, _, _ = check_realized_ev_blocking(p)
        except Exception:
            blocked = True
        if blocked:
            continue
        src = rs.source_avg_return_pct
        eligible.append((float(avg), p, int(n), src))

    eligible.sort(key=lambda t: -t[0])  # rank by realized PnL (best first)
    base["eligible"] = len(eligible)

    now = datetime.utcnow()
    for avg, p, n, src in eligible[:max_promote]:
        from_stage = p.lifecycle_stage
        p.lifecycle_stage = "promoted"
        p.promotion_status = "promoted_via_realized_pnl"[:30]
        p.active = True
        p.lifecycle_changed_at = now
        p.updated_at = now
        base["promoted_details"].append({
            "id": int(p.id), "from_stage": from_stage,
            "realized_avg_pct": round(float(avg), 3), "n": n, "source": src,
        })
        base["promoted_pattern_ids"].append(int(p.id))
        logger.warning(
            "[realized_pnl_promotion] PROMOTE id=%s from=%s realized_avg=%.3f n=%s source=%s",
            p.id, from_stage, float(avg), n, src,
        )

    base["promoted"] = len(base["promoted_pattern_ids"])
    db.commit()
    logger.info("[realized_pnl_promotion] %s", {k: base[k] for k in ("evaluated", "eligible", "promoted")})
    return base

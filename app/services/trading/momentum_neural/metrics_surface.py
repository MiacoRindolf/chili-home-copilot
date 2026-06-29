"""Read-only operator-journaling metrics for the momentum lane.

Two additive, flag-gated reporting surfaces — NEITHER has any trading impact (they
never gate, size, or veto an entry; they only READ already-computed closed-session
outcome rows for the operator's journal / a lane-health endpoint / a daily log line):

  * PROCESS-OVER-PROFITS SCORE (item 4, chili_momentum_process_score_enabled): a rolling
    rule-adherence score (entered-on-trigger / honored-stop / no-chase) distinct from
    realized PnL — measures whether the lane FOLLOWED ITS PLAN, not whether it won.
  * CHALLENGE METRICS SURFACE (item 6, chili_momentum_challenge_metrics_enabled): accuracy%
    + profit-loss ratio + consecutive-green-day streak for operator KPI tracking.

Both reuse the deterministic ``outcome_labels`` classes and the ``is_real_entry_outcome``
filter so only REAL ENTERED trades are scored (a $0 cancelled_pre_entry never pollutes the
rule-adherence read). Every function FAIL-NEUTRALs to an empty/None result on thin history
or any error, and returns nothing at all when its flag is OFF (byte-identical). IO is a
single bounded read-only query per call; no writes, no caching that outlives the call.
"""

from __future__ import annotations

import logging
from typing import Any

from ....config import settings
from .outcome_labels import (
    OUTCOME_BAILOUT,
    OUTCOME_GOVERNANCE_EXIT,
    OUTCOME_SMALL_WIN,
    OUTCOME_STOP_LOSS,
    OUTCOME_SUCCESS,
    OUTCOME_TIMED_EXIT,
    is_real_entry_outcome,
)

logger = logging.getLogger(__name__)

# Rule-adherence weights for the PROCESS score (distinct from PnL): did the trade follow
# the PLAN? SUCCESS (entered on trigger, honored stop, reached target) = full credit;
# SMALL_WIN / TIMED_EXIT (plan-honored, modest) = full credit; STOP_LOSS (the stop did its
# job exactly as planned) = NEUTRAL (not a process failure — a planned stop is GOOD process);
# BAILOUT (discretionary bail, no trigger follow-through) and GOVERNANCE_EXIT (forced flat by
# a breaker — a process miss upstream) = process DEMERITS. Other entered classes = neutral.
_PROCESS_ADHERENCE: dict[str, float] = {
    OUTCOME_SUCCESS: 1.0,
    OUTCOME_SMALL_WIN: 1.0,
    OUTCOME_TIMED_EXIT: 1.0,
    OUTCOME_STOP_LOSS: 0.5,        # planned stop = acceptable process (neutral midpoint)
    OUTCOME_BAILOUT: 0.0,         # discretionary bail = poor adherence
    OUTCOME_GOVERNANCE_EXIT: 0.0,  # forced exit by a breaker = process miss
}

# Default rolling window of REAL ENTERED closed trades for the process score.
_PROCESS_DEFAULT_WINDOW = 30
# Default closed-session window for the challenge metrics surface.
_CHALLENGE_DEFAULT_WINDOW = 50
# Profit-loss ratio cap so a tiny loss-denominator can't explode the ratio.
_PNL_RATIO_CAP = 5.0


def _recent_real_outcomes(
    db: Any, *, execution_family: str | None, limit: int
) -> list[tuple[str | None, float | None]]:
    """The most-recent ``limit`` closed LIVE outcomes for the lane as
    ``[(outcome_class, realized_pnl_usd), ...]``, newest first. Read-only / bounded /
    indexed (execution_family, terminal_at desc). Returns ``[]`` on any error."""
    if db is None or limit <= 0:
        return []
    try:
        from ....models.trading import MomentumAutomationOutcome

        q = db.query(
            MomentumAutomationOutcome.outcome_class,
            MomentumAutomationOutcome.realized_pnl_usd,
        ).filter(MomentumAutomationOutcome.mode == "live")
        if execution_family:
            q = q.filter(MomentumAutomationOutcome.execution_family == execution_family)
        rows = (
            q.order_by(MomentumAutomationOutcome.terminal_at.desc())
            .limit(int(limit))
            .all()
        )
        return [(r[0], r[1]) for r in rows]
    except Exception:
        logger.debug("[metrics_surface] recent-outcomes read failed", exc_info=True)
        return []


def process_over_profits_score(
    db: Any, *, execution_family: str | None = None
) -> dict[str, Any] | None:
    """ITEM 4 — rolling PROCESS-OVER-PROFITS (rule-adherence) score for the lane.

    A LOGGED-ONLY metric: did the lane FOLLOW ITS PLAN (enter on trigger, honor the stop,
    not chase)? Scored from the deterministic outcome classes — NOT from realized PnL — over
    the last N REAL ENTERED closed trades. Returns ``None`` when the flag is OFF (so the caller
    emits nothing / is byte-identical) or on thin history. NEVER gates / sizes / vetoes — it is
    a journaling surface only.

    Returns ``{"process_score": 0.72, "n": 18, "wins": 13, "real_trades": 18, ...}``.
    """
    if not bool(getattr(settings, "chili_momentum_process_score_enabled", False)):
        return None
    try:
        window = int(getattr(settings, "chili_momentum_process_score_window", _PROCESS_DEFAULT_WINDOW)
                     or _PROCESS_DEFAULT_WINDOW)
    except (TypeError, ValueError):
        window = _PROCESS_DEFAULT_WINDOW
    rows = _recent_real_outcomes(db, execution_family=execution_family, limit=window)
    adher: list[float] = []
    wins = 0
    real = 0
    for oc, pnl in rows:
        if not is_real_entry_outcome(oc):
            continue  # never-entered (pre-entry cancel / no-fill / risk-block / error) ⇒ skip
        if pnl is None:
            continue  # belt-and-suspenders: a real entered trade carries realized PnL
        real += 1
        key = str(oc or "").strip().lower()
        adher.append(_PROCESS_ADHERENCE.get(key, 0.5))  # unknown entered class ⇒ neutral
        try:
            if float(pnl) > 0.0:
                wins += 1
        except (TypeError, ValueError):
            pass
    if real == 0 or not adher:
        return {"process_score": None, "n": 0, "real_trades": 0, "reason": "thin_history"}
    score = sum(adher) / len(adher)
    return {
        "process_score": round(score, 4),
        "n": real,
        "real_trades": real,
        "wins": wins,
        "win_rate": round(wins / real, 4),
        "window": window,
        "execution_family": execution_family,
    }


def challenge_metrics_accuracy_pct(
    db: Any, *, execution_family: str | None = None, window: int = _CHALLENGE_DEFAULT_WINDOW
) -> float | None:
    """Rule-adherence accuracy% over the lane's recent REAL ENTERED closed trades:
    (plan-honored trades) / (real entered trades) * 100. A plan-honored trade is one whose
    outcome-class adherence weight is >= 0.5 (success / small_win / timed_exit / stop_loss —
    a planned stop is honored process). Read-only; ``None`` on thin history."""
    rows = _recent_real_outcomes(db, execution_family=execution_family, limit=window)
    real = 0
    honored = 0
    for oc, _pnl in rows:
        if not is_real_entry_outcome(oc):
            continue
        real += 1
        if _PROCESS_ADHERENCE.get(str(oc or "").strip().lower(), 0.5) >= 0.5:
            honored += 1
    if real == 0:
        return None
    return round(100.0 * honored / real, 2)


def challenge_metrics_pnl_ratio(
    db: Any, *, execution_family: str | None = None, window: int = _CHALLENGE_DEFAULT_WINDOW
) -> float | None:
    """Profit-loss ratio = sum(winning realized PnL) / |sum(losing realized PnL)| over the
    lane's recent REAL ENTERED closed trades, CAPPED at ``_PNL_RATIO_CAP`` so a near-zero loss
    denominator cannot explode it. Read-only; ``None`` when there are no real trades."""
    rows = _recent_real_outcomes(db, execution_family=execution_family, limit=window)
    wins_usd = 0.0
    loss_usd = 0.0
    seen = 0
    for oc, pnl in rows:
        if not is_real_entry_outcome(oc) or pnl is None:
            continue
        try:
            p = float(pnl)
        except (TypeError, ValueError):
            continue
        seen += 1
        if p > 0.0:
            wins_usd += p
        elif p < 0.0:
            loss_usd += -p
    if seen == 0:
        return None
    if loss_usd <= 0.0:
        # No losses in the window: report the cap (all-green ⇒ ratio is "very high", bounded).
        return _PNL_RATIO_CAP if wins_usd > 0.0 else None
    return round(min(_PNL_RATIO_CAP, wins_usd / loss_usd), 4)


def challenge_metrics_daily_streak(
    db: Any, *, execution_family: str | None = None
) -> dict[str, Any]:
    """Consecutive green-day streak (+ trailing green/red totals) for the lane, reusing the
    risk-policy daily-bucketing helper. Read-only; ``{"consecutive_green": 0, ...}`` on thin
    history. Independent of the green-day GRADUATION flag (that gates SIZING; this only reads)."""
    try:
        from .risk_policy import consecutive_green_days

        lookback = int(getattr(settings, "chili_momentum_green_day_lookback_days", 30) or 30)
        streak, meta = consecutive_green_days(
            db, execution_family=execution_family, lookback_days=lookback
        )
        return {
            "consecutive_green": int(streak),
            "green_usd": meta.get("green_usd"),
            "days_seen": meta.get("days_seen"),
        }
    except Exception:
        logger.debug("[metrics_surface] daily-streak read failed", exc_info=True)
        return {"consecutive_green": 0, "reason": "error"}


def challenge_metrics_summary(
    db: Any, *, execution_family: str | None = None
) -> dict[str, Any]:
    """ITEM 6 — aggregate the read-only KPI surface for operator journaling.

    Returns ``{}`` when the flag is OFF (caller emits nothing / byte-identical). Otherwise:
    ``{"accuracy_pct": 87.5, "pnl_ratio": 2.1, "streak": {"consecutive_green": 3, ...}}``.
    Purely a data surface — NO trading impact (never gates / sizes / vetoes)."""
    if not bool(getattr(settings, "chili_momentum_challenge_metrics_enabled", False)):
        return {}
    try:
        window = int(getattr(settings, "chili_momentum_challenge_metrics_window", _CHALLENGE_DEFAULT_WINDOW)
                     or _CHALLENGE_DEFAULT_WINDOW)
    except (TypeError, ValueError):
        window = _CHALLENGE_DEFAULT_WINDOW
    return {
        "accuracy_pct": challenge_metrics_accuracy_pct(
            db, execution_family=execution_family, window=window),
        "pnl_ratio": challenge_metrics_pnl_ratio(
            db, execution_family=execution_family, window=window),
        "streak": challenge_metrics_daily_streak(db, execution_family=execution_family),
        "window": window,
        "execution_family": execution_family,
    }


# multi_scalp monitor: surface the live scale-in / re-entry tally for the cockpit.
# Reads ONLY le keys already maintained by the pyramid + micropullback + recycle
# paths (pyramid_add_count, micropullback_reentry_count, stopout_cycles,
# trade_cycles) — no new state, no I/O beyond the le it is handed.
def multi_scalp_summary(le: dict) -> dict:
    le = le if isinstance(le, dict) else {}
    return {
        "pyramid_adds": int(le.get("pyramid_add_count") or 0),
        "micropullback_reloads": int(le.get("micropullback_reentry_count") or 0),
        "stopout_reentries": int(le.get("stopout_cycles") or 0),
        "trade_cycles": int(le.get("trade_cycles") or 0),
    }

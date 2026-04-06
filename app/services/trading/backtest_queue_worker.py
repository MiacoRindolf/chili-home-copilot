"""Picklable entry point for process-pool pattern queue backtests (one DB session per job).

``execute_queue_backtest_for_pattern`` is used from threads (parent process).
``run_one_pattern_job`` wraps it for ``ProcessPoolExecutor`` after child DB env is configured.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

CHILD_ENV_FLAG = "CHILI_MP_BACKTEST_CHILD"


def configure_multiprocess_child_db_env(pool_size: int, max_overflow: int) -> None:
    """Run in pool initializer: small SQLAlchemy pool per child process."""
    os.environ[CHILD_ENV_FLAG] = "1"
    os.environ["DATABASE_POOL_SIZE"] = str(max(1, int(pool_size)))
    os.environ["DATABASE_MAX_OVERFLOW"] = str(max(0, int(max_overflow)))


def execute_queue_backtest_for_pattern(pattern_id: int, user_id: int | None) -> tuple[int, int]:
    """Run queue backtest for one pattern (thread-safe: own session). Used by parent threads."""
    from ...config import settings
    from ...db import SessionLocal
    from ...models.trading import ScanPattern, TradingInsight
    from .backtest_queue import mark_pattern_tested
    from .backtest_engine import hydrate_scan_pattern_rules_json, smart_backtest_insight
    from .learning_events import log_learning_event
    from datetime import datetime

    db = SessionLocal()
    try:
        pattern = db.query(ScanPattern).filter(ScanPattern.id == pattern_id).first()
        if not pattern:
            return (0, 0)
        insight = db.query(TradingInsight).filter(
            TradingInsight.scan_pattern_id == pattern.id
        ).first()
        if not insight:
            _parts: list[str] = []
            if getattr(pattern, "name", None) and str(pattern.name).strip():
                _parts.append(str(pattern.name).strip())
            _pd = (getattr(pattern, "description", None) or "").strip()
            if _pd and _pd not in _parts:
                _parts.append(_pd)
            _pdesc = " | ".join(_parts) if _parts else (
                f"{pattern.name or 'Pattern'} — Composable pattern backtest"
            )
            insight = TradingInsight(
                user_id=user_id,
                pattern_description=_pdesc,
                confidence=pattern.confidence or 0.5,
                evidence_count=0,
                scan_pattern_id=pattern.id,
            )
            db.add(insight)
            db.commit()

        hydrate_scan_pattern_rules_json(db, pattern, insight)
        db.refresh(pattern)

        prio: list[str] = []
        if getattr(settings, "brain_queue_priority_stored_refresh", True):
            from .backtest_engine import priority_tickers_from_stored_backtests_for_refresh

            prio = priority_tickers_from_stored_backtests_for_refresh(
                db,
                insight_id=int(insight.id),
                scan_pattern_id=int(pattern.id),
                pattern_name=str(pattern.name or ""),
                max_tickers=int(getattr(settings, "brain_queue_stored_refresh_max_tickers", 40)),
                stale_trade_cap=int(getattr(settings, "brain_queue_stored_stale_trade_cap", 2)),
                stale_days=int(getattr(settings, "brain_queue_stored_stale_days", 14)),
            )
            if prio:
                logger.info(
                    "[backtest_queue] stored_refresh_priority pattern_id=%s insight_id=%s n=%d sample=%s",
                    pattern.id,
                    insight.id,
                    len(prio),
                    prio[:8],
                )

        _tier = (getattr(pattern, "queue_tier", None) or "full").strip().lower()
        _prescreen = bool(getattr(settings, "brain_queue_prescreen_enabled", False))
        if _prescreen and _tier == "prescreen":
            result = smart_backtest_insight(
                db,
                insight,
                target_tickers=max(
                    2, int(getattr(settings, "brain_queue_prescreen_tickers", 4))
                ),
                update_confidence=True,
                period=getattr(settings, "brain_queue_prescreen_period", "3mo"),
                priority_tickers=prio if prio else None,
            )
            total = result.get("total", 0)
            wins = result.get("wins", 0)
            backtests_run = result.get("backtests_run", 0)
            wr_pct = (wins / total * 100.0) if total > 0 else 0.0
            min_pre = float(
                getattr(settings, "brain_queue_prescreen_min_win_rate_pct", 45.0)
            )
            win_rate = wins / total if total >= 3 else None
            avg_return = result.get("avg_return")
            mark_pattern_tested(db, pattern, win_rate=win_rate, avg_return=avg_return)
            pattern = db.query(ScanPattern).filter(ScanPattern.id == pattern_id).first()
            if pattern and total >= 2 and wr_pct >= min_pre:
                pattern.queue_tier = "full"
                pattern.backtest_priority = max(int(pattern.backtest_priority or 0), 50)
                db.commit()
                from .backtest_queue import invalidate_queue_status_cache

                invalidate_queue_status_cache()
                log_learning_event(
                    db,
                    user_id,
                    "pattern_backtest_queue",
                    f"Prescreen pass: '{pattern.name}' ({wins}/{total}, {wr_pct:.0f}% wr) → full tier",
                    related_insight_id=insight.id,
                )
            elif pattern and total >= 2:
                pattern.active = False
                pattern.promotion_status = "rejected_prescreen"
                pattern.lifecycle_stage = "retired"
                pattern.lifecycle_changed_at = datetime.utcnow()
                db.commit()
                from .backtest_queue import invalidate_queue_status_cache

                invalidate_queue_status_cache()
                log_learning_event(
                    db,
                    user_id,
                    "pattern_backtest_queue",
                    f"Prescreen reject: '{pattern.name}' ({wins}/{total}, {wr_pct:.0f}% wr)",
                    related_insight_id=insight.id,
                )
            else:
                mark_pattern_tested(db, pattern, win_rate=win_rate, avg_return=avg_return)
            return (backtests_run, 1)

        result = smart_backtest_insight(
            db,
            insight,
            target_tickers=max(20, getattr(settings, "brain_queue_target_tickers", 50)),
            update_confidence=True,
            priority_tickers=prio if prio else None,
        )
        total = result.get("total", 0)
        wins = result.get("wins", 0)
        backtests_run = result.get("backtests_run", 0)
        win_rate = wins / total if total >= 3 else None
        avg_return = result.get("avg_return")
        mark_pattern_tested(db, pattern, win_rate=win_rate, avg_return=avg_return)
        if total >= 3:
            log_learning_event(
                db,
                user_id,
                "pattern_backtest_queue",
                f"Queue backtest: '{pattern.name}' ({wins}/{total} profitable, "
                f"{win_rate * 100:.0f}%wr) — priority was {pattern.backtest_priority}",
                related_insight_id=insight.id,
            )
        return (backtests_run, 1)
    except Exception as e:
        logger.warning("[backtest_queue] Failed to backtest pattern %s: %s", pattern_id, e)
        try:
            pattern = db.query(ScanPattern).filter(ScanPattern.id == pattern_id).first()
            if pattern:
                mark_pattern_tested(db, pattern)
        except Exception:
            pass
        return (0, 1)
    finally:
        db.close()


def run_one_pattern_job(pattern_id: int, user_id: int | None) -> tuple[int, int]:
    """Process-pool entry point. Initializer must set DB env; this is a safety net for tests."""
    if "DATABASE_POOL_SIZE" not in os.environ:
        configure_multiprocess_child_db_env(1, 2)
    return execute_queue_backtest_for_pattern(pattern_id, user_id)

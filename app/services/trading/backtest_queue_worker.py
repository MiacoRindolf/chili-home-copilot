"""Picklable entry point for process-pool pattern queue backtests (one DB session per job).

``execute_queue_backtest_for_pattern`` is used from threads (parent process).
``run_one_pattern_job`` wraps it for ``ProcessPoolExecutor`` after child DB env is configured.
"""
from __future__ import annotations

from contextlib import contextmanager
import logging
import os
import signal
from collections.abc import Mapping
from types import FrameType

logger = logging.getLogger(__name__)

CHILD_ENV_FLAG = "CHILI_MP_BACKTEST_CHILD"
MIN_QUEUE_TICKER_COUNT = 1
DEFAULT_OPERATIONAL_REFRESH_LIFECYCLES = (
    "promoted",
    "live",
    "shadow_promoted",
    "pilot_promoted",
)
PRESCREEN_MIN_TICKERS_FALLBACK = 2
PRESCREEN_TICKERS_FALLBACK = 4
PRESCREEN_MIN_WIN_RATE_PCT_FALLBACK = 45.0
PRESCREEN_FULL_TIER_PRIORITY_FLOOR = 50
STORED_REFRESH_STALE_TRADE_CAP_FALLBACK = 2
STORED_REFRESH_STALE_DAYS_FALLBACK = 14
QUEUE_PATTERN_WALLTIME_SECONDS_ENV = "CHILI_BACKTEST_QUEUE_PATTERN_WALLTIME_SECONDS"
DEFAULT_QUEUE_PATTERN_WALLTIME_SECONDS = 900.0
DISABLED_QUEUE_PATTERN_WALLTIME_SECONDS = 0.0


class QueuePatternWalltimeExceeded(TimeoutError):
    """Raised inside a process-pool child when one pattern monopolizes the worker."""


def _positive_int(value: object, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(fallback)
    return max(MIN_QUEUE_TICKER_COUNT, parsed)


def _non_negative_float(value: object, fallback: float) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        parsed = float(fallback)
    return max(DISABLED_QUEUE_PATTERN_WALLTIME_SECONDS, parsed)


def queue_pattern_walltime_seconds(
    settings_obj: object | None = None,
    environ: Mapping[str, str] | None = None,
) -> float:
    """Configured wall-time budget for one process-pool pattern backtest."""
    env = environ if environ is not None else os.environ
    raw_env = env.get(QUEUE_PATTERN_WALLTIME_SECONDS_ENV)
    if raw_env is not None and str(raw_env).strip():
        return _non_negative_float(raw_env, DEFAULT_QUEUE_PATTERN_WALLTIME_SECONDS)
    if settings_obj is None:
        try:
            from ...config import settings as settings_obj
        except Exception:
            settings_obj = None
    return _non_negative_float(
        getattr(
            settings_obj,
            "brain_queue_pattern_walltime_seconds",
            DEFAULT_QUEUE_PATTERN_WALLTIME_SECONDS,
        ),
        DEFAULT_QUEUE_PATTERN_WALLTIME_SECONDS,
    )


@contextmanager
def _queue_pattern_walltime_guard(pattern_id: int, timeout_seconds: float):
    """Interrupt runaway child work on platforms with SIGALRM support."""
    timeout = _non_negative_float(timeout_seconds, DEFAULT_QUEUE_PATTERN_WALLTIME_SECONDS)
    if (
        timeout <= DISABLED_QUEUE_PATTERN_WALLTIME_SECONDS
        or not hasattr(signal, "SIGALRM")
        or not hasattr(signal, "setitimer")
    ):
        yield
        return

    previous_handler = signal.getsignal(signal.SIGALRM)

    def _raise_timeout(_signum: int, _frame: FrameType | None) -> None:
        raise QueuePatternWalltimeExceeded(
            f"pattern_id={pattern_id} exceeded queue walltime budget {timeout:.1f}s"
        )

    signal.signal(signal.SIGALRM, _raise_timeout)
    try:
        signal.setitimer(signal.ITIMER_REAL, timeout)
    except Exception:
        signal.signal(signal.SIGALRM, previous_handler)
        yield
        return
    try:
        yield
    finally:
        try:
            signal.setitimer(signal.ITIMER_REAL, DISABLED_QUEUE_PATTERN_WALLTIME_SECONDS)
        finally:
            signal.signal(signal.SIGALRM, previous_handler)


def _csv_tokens(raw: object) -> set[str]:
    return {
        part.strip().lower()
        for part in str(raw or "").split(",")
        if part.strip()
    }


def _operational_refresh_lane(settings: object, pattern: object) -> bool:
    if not bool(getattr(settings, "brain_queue_operational_refresh_enabled", True)):
        return False
    lifecycle = str(getattr(pattern, "lifecycle_stage", "") or "").strip().lower()
    allowed = _csv_tokens(
        getattr(
            settings,
            "brain_queue_operational_refresh_lifecycles",
            ",".join(DEFAULT_OPERATIONAL_REFRESH_LIFECYCLES),
        )
    )
    return lifecycle in allowed


def queue_target_tickers_for_pattern(settings: object, pattern: object) -> int:
    full_target = _positive_int(
        getattr(settings, "brain_queue_target_tickers", None),
        MIN_QUEUE_TICKER_COUNT,
    )
    if not _operational_refresh_lane(settings, pattern):
        return full_target
    operational_target = _positive_int(
        getattr(settings, "brain_queue_operational_target_tickers", None),
        full_target,
    )
    return min(full_target, operational_target)


def queue_stored_refresh_max_tickers_for_pattern(settings: object, pattern: object) -> int:
    full_target = _positive_int(
        getattr(settings, "brain_queue_stored_refresh_max_tickers", None),
        queue_target_tickers_for_pattern(settings, pattern),
    )
    if not _operational_refresh_lane(settings, pattern):
        return full_target
    operational_target = _positive_int(
        getattr(settings, "brain_queue_operational_stored_refresh_max_tickers", None),
        full_target,
    )
    return min(full_target, operational_target)


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
    from .backtest_queue import (
        mark_pattern_tested,
        release_pattern_backtest_lock,
        try_acquire_pattern_backtest_lock,
    )
    from .backtest_engine import hydrate_scan_pattern_rules_json, smart_backtest_insight
    from .learning_events import log_learning_event
    from datetime import datetime

    def _complete_recert_if_open(
        *,
        total: int | None,
        wins: int | None,
        win_rate: float | None,
        avg_return: float | None,
        backtests_run: int | None,
    ) -> None:
        try:
            from .recert_queue_service import complete_open_recerts_from_backtest

            complete_open_recerts_from_backtest(
                db,
                scan_pattern_id=int(pattern.id),
                total=total,
                wins=wins,
                win_rate=win_rate,
                avg_return=avg_return,
                backtests_run=backtests_run,
            )
        except Exception:
            logger.debug(
                "[backtest_queue] recert completion failed pattern_id=%s",
                getattr(pattern, "id", None),
                exc_info=True,
            )

    db = SessionLocal()
    lock_acquired = False
    try:
        if not try_acquire_pattern_backtest_lock(db, int(pattern_id)):
            logger.info(
                "[backtest_queue] pattern_id=%s already leased by another worker; skipping",
                pattern_id,
            )
            return (0, 0)
        lock_acquired = True
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
            # FIX E-1 (2026-04-29 audit): no hardcoded 0.5 fallback for
            # confidence. Use the pattern's real confidence if non-None;
            # otherwise compute a Bayesian-shrinkage value from realized n
            # (which yields ~0 for a brand-new pattern with no trades --
            # honest "we don't know yet" -- never a synthesized 0.5).
            from .dynamic_priors import bayesian_pattern_confidence as _bpc
            _pc_raw = getattr(pattern, "confidence", None)
            try:
                _pc = float(_pc_raw) if _pc_raw is not None else None
            except (TypeError, ValueError):
                _pc = None
            if _pc is None:
                _pc = _bpc(getattr(pattern, "trade_count", None))
            insight_confidence = _pc if _pc is not None else 0.0
            insight = TradingInsight(
                user_id=user_id,
                pattern_description=_pdesc,
                confidence=insight_confidence,
                evidence_count=0,
                scan_pattern_id=pattern.id,
            )
            db.add(insight)
            db.commit()

        hydrate_scan_pattern_rules_json(db, pattern, insight)
        db.refresh(pattern)

        target_tickers = queue_target_tickers_for_pattern(settings, pattern)
        stored_refresh_max_tickers = queue_stored_refresh_max_tickers_for_pattern(
            settings, pattern,
        )
        if _operational_refresh_lane(settings, pattern):
            logger.info(
                "[backtest_queue] operational_refresh_budget pattern_id=%s "
                "lifecycle=%s target_tickers=%s stored_refresh_max_tickers=%s",
                pattern.id,
                getattr(pattern, "lifecycle_stage", None),
                target_tickers,
                stored_refresh_max_tickers,
            )

        prio: list[str] = []
        if getattr(settings, "brain_queue_priority_stored_refresh", True):
            from .backtest_engine import priority_tickers_from_stored_backtests_for_refresh

            prio = priority_tickers_from_stored_backtests_for_refresh(
                db,
                insight_id=int(insight.id),
                scan_pattern_id=int(pattern.id),
                pattern_name=str(pattern.name or ""),
                max_tickers=stored_refresh_max_tickers,
                stale_trade_cap=int(
                    getattr(
                        settings,
                        "brain_queue_stored_stale_trade_cap",
                        STORED_REFRESH_STALE_TRADE_CAP_FALLBACK,
                    )
                ),
                stale_days=int(
                    getattr(
                        settings,
                        "brain_queue_stored_stale_days",
                        STORED_REFRESH_STALE_DAYS_FALLBACK,
                    )
                ),
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
                    PRESCREEN_MIN_TICKERS_FALLBACK,
                    int(
                        getattr(
                            settings,
                            "brain_queue_prescreen_tickers",
                            PRESCREEN_TICKERS_FALLBACK,
                        )
                    ),
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
                getattr(
                    settings,
                    "brain_queue_prescreen_min_win_rate_pct",
                    PRESCREEN_MIN_WIN_RATE_PCT_FALLBACK,
                )
            )
            win_rate = wins / total if total >= 3 else None
            avg_return = result.get("avg_return")
            mark_pattern_tested(
                db,
                pattern,
                win_rate=win_rate,
                avg_return=avg_return,
                backtests_run=backtests_run,
                trade_bearing_tickers=total,
            )
            _complete_recert_if_open(
                total=total,
                wins=wins,
                win_rate=win_rate,
                avg_return=avg_return,
                backtests_run=backtests_run,
            )
            pattern = db.query(ScanPattern).filter(ScanPattern.id == pattern_id).first()
            if pattern and total >= 2 and wr_pct >= min_pre:
                pattern.queue_tier = "full"
                pattern.backtest_priority = max(
                    int(pattern.backtest_priority or 0),
                    PRESCREEN_FULL_TIER_PRIORITY_FLOOR,
                )
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
                _op = (pattern.promotion_status or "").strip()
                _ol = (pattern.lifecycle_stage or "").strip()
                pattern.active = False
                pattern.promotion_status = "rejected_prescreen"
                pattern.lifecycle_stage = "retired"
                pattern.lifecycle_changed_at = datetime.utcnow()
                try:
                    from .brain_work.promotion_surface import emit_promotion_surface_change

                    emit_promotion_surface_change(
                        db,
                        scan_pattern_id=int(pattern.id),
                        old_promotion_status=_op,
                        old_lifecycle_stage=_ol,
                        new_promotion_status=(pattern.promotion_status or "").strip(),
                        new_lifecycle_stage=(pattern.lifecycle_stage or "").strip(),
                        source="queue_prescreen_reject",
                    )
                except Exception:
                    pass
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
                _complete_recert_if_open(
                    total=total,
                    wins=wins,
                    win_rate=win_rate,
                    avg_return=avg_return,
                    backtests_run=backtests_run,
                )
            return (backtests_run, 1)

        result = smart_backtest_insight(
            db,
            insight,
            target_tickers=target_tickers,
            update_confidence=True,
            priority_tickers=prio if prio else None,
        )
        total = result.get("total", 0)
        wins = result.get("wins", 0)
        backtests_run = result.get("backtests_run", 0)
        win_rate = wins / total if total >= 3 else None
        avg_return = result.get("avg_return")
        mark_pattern_tested(
            db,
            pattern,
            win_rate=win_rate,
            avg_return=avg_return,
            backtests_run=backtests_run,
            trade_bearing_tickers=total,
        )
        _complete_recert_if_open(
            total=total,
            wins=wins,
            win_rate=win_rate,
            avg_return=avg_return,
            backtests_run=backtests_run,
        )
        if total >= 3:
            log_learning_event(
                db,
                user_id,
                "pattern_backtest_queue",
                f"Queue backtest: '{pattern.name}' ({wins}/{total} profitable, "
                f"{win_rate * 100:.0f}%wr) — priority was {pattern.backtest_priority}",
                related_insight_id=insight.id,
            )
        # f-fix-backtest-completed-emitter (2026-05-05): emit so
        # cpcv_gate handler runs the CPCV promotion gate. Pre-fix the
        # FIX 34 loop bypassed the event path entirely. Per-call
        # try/except so a broken emit can't block the backtest return.
        try:
            from .brain_work.emitters import emit_backtest_completed_outcome
            emit_backtest_completed_outcome(
                db,
                scan_pattern_id=int(pattern.id),
                user_id=user_id,
                backtests_run=int(backtests_run),
                win_rate=win_rate,
                avg_return=avg_return,
            )
            db.commit()
        except Exception:
            logger.warning(
                "[backtest_queue] emit_backtest_completed failed pattern_id=%s",
                pattern.id, exc_info=True,
            )
            try:
                db.rollback()
            except Exception:
                pass
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
        if lock_acquired:
            try:
                db.rollback()
            except Exception:
                pass
            release_pattern_backtest_lock(db, int(pattern_id))
        # FIX 46 pattern (rollback before close).
        try:
            db.rollback()
        except Exception:
            pass
        db.close()


def run_one_pattern_job(pattern_id: int, user_id: int | None) -> tuple[int, int]:
    """Process-pool entry point. Initializer must set DB env; this is a safety net for tests."""
    if "DATABASE_POOL_SIZE" not in os.environ:
        configure_multiprocess_child_db_env(1, 2)
    timeout_seconds = queue_pattern_walltime_seconds()
    try:
        with _queue_pattern_walltime_guard(pattern_id, timeout_seconds):
            return execute_queue_backtest_for_pattern(pattern_id, user_id)
    except QueuePatternWalltimeExceeded as exc:
        logger.warning("[backtest_queue] process child walltime exceeded: %s", exc)
        return (0, 1)

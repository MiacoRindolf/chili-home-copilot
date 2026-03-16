"""Background scheduler for continuous trading AI learning.

Runs learning cycles (scan → snapshot → backfill → mine → journal)
automatically on a schedule so the AI Brain is always growing.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
_lock = threading.Lock()


def _run_learning_job():
    """Executed by APScheduler in a background thread."""
    from ..db import SessionLocal
    from . import trading_service as ts

    logger.info("[scheduler] Starting scheduled learning cycle")
    db = SessionLocal()
    try:
        result = ts.run_learning_cycle(db, user_id=None, full_universe=True)
        logger.info(f"[scheduler] Learning cycle result: {result}")
    except Exception as e:
        logger.error(f"[scheduler] Learning cycle failed: {e}")
    finally:
        db.close()


def _run_weekly_review_job():
    """Weekly performance review job."""
    from ..db import SessionLocal
    from . import trading_service as ts

    logger.info("[scheduler] Starting weekly review")
    db = SessionLocal()
    try:
        ts.weekly_performance_review(db, user_id=None)
    except Exception as e:
        logger.error(f"[scheduler] Weekly review failed: {e}")
    finally:
        db.close()


def _run_broker_sync_job():
    """Sync Robinhood orders + positions to local DB during market hours."""
    from . import broker_service

    if not broker_service.is_connected():
        return

    from ..db import SessionLocal
    logger.info("[scheduler] Starting Robinhood order + position sync")
    db = SessionLocal()
    try:
        order_result = broker_service.sync_orders_to_db(db, user_id=None)
        logger.info(f"[scheduler] Order sync result: {order_result}")
        pos_result = broker_service.sync_positions_to_db(db, user_id=None)
        logger.info(f"[scheduler] Position sync result: {pos_result}")
    except Exception as e:
        logger.error(f"[scheduler] Broker sync failed: {e}")
    finally:
        db.close()


def _run_price_monitor_job():
    """Check positions/breakouts/picks and dispatch alerts every 5 minutes."""
    from ..db import SessionLocal
    from .trading.alerts import run_price_monitor

    logger.info("[scheduler] Starting price monitor check")
    db = SessionLocal()
    try:
        result = run_price_monitor(db, user_id=None)
        logger.info(f"[scheduler] Price monitor result: {result}")
    except Exception as e:
        logger.error(f"[scheduler] Price monitor failed: {e}")
    finally:
        db.close()


_crypto_alert_cooldown: dict[str, float] = {}


def _run_crypto_breakout_job():
    """24/7 crypto breakout scanner: detect intraday setups on 15m candles.

    Alerts BEFORE breakouts happen -- prioritises pre-breakout precursors:
      1. BB squeeze + ATR compressed (coiled spring)
      2. BB squeeze firing (squeeze just releasing)
      3. BB squeeze + bullish EMA stack + rising volume
      4. ATR compressed with strong momentum setup
      5. High-score pre-breakout with volume
      6. Freshly confirmed breakout (still actionable with tighter stop)

    All thresholds are brain-adaptive and evolve via the learning cycle.
    """
    import time as _t
    from .trading.scanner import run_crypto_breakout_scan, get_adaptive_weight
    from .trading.alerts import dispatch_alert

    logger.info("[scheduler] Running crypto breakout scanner")
    try:
        result = run_crypto_breakout_scan(max_results=20)
        now = _t.time()

        stale = [k for k, v in _crypto_alert_cooldown.items() if now - v > 7200]
        for k in stale:
            del _crypto_alert_cooldown[k]

        t_coiled = get_adaptive_weight("crypto_alert_coiled_spring_min")
        t_squeeze = get_adaptive_weight("crypto_alert_squeeze_firing_min")
        t_building = get_adaptive_weight("crypto_alert_building_min")
        t_range = get_adaptive_weight("crypto_alert_range_tight_min")
        t_high = get_adaptive_weight("crypto_alert_high_score_min")
        rvol_building = get_adaptive_weight("crypto_alert_rvol_building_min")
        rvol_high = get_adaptive_weight("crypto_alert_rvol_high_score_min")

        all_results = result.get("results", [])

        # Diagnostic: score distribution
        score_buckets = {"8+": 0, "7-8": 0, "6-7": 0, "5-6": 0, "<5": 0}
        for r in all_results:
            s = r.get("score", 0)
            if s >= 8:
                score_buckets["8+"] += 1
            elif s >= 7:
                score_buckets["7-8"] += 1
            elif s >= 6:
                score_buckets["6-7"] += 1
            elif s >= 5:
                score_buckets["5-6"] += 1
            else:
                score_buckets["<5"] += 1

        logger.info(
            f"[scheduler] Crypto score distribution: "
            + ", ".join(f"{k}={v}" for k, v in score_buckets.items())
        )

        # Diagnostic: log top 3 setups regardless of alert qualification
        for i, r in enumerate(all_results[:3]):
            logger.info(
                f"[scheduler] Top-{i+1}: {r['ticker']} score={r['score']} "
                f"squeeze={r.get('bb_squeeze')} firing={r.get('bb_squeeze_firing')} "
                f"atr={r.get('atr_state')} ema={r.get('ema_alignment')} "
                f"rvol={r.get('rvol')} confirmed={r.get('breakout_confirmed')} "
                f"sigs={r.get('signals', [])[:3]}"
            )

        alertable: list[tuple[dict, str, str]] = []
        for r in all_results:
            score = r.get("score", 0)
            squeeze = r.get("bb_squeeze", False)
            squeeze_firing = r.get("bb_squeeze_firing", False)
            atr = r.get("atr_state", "normal")
            ema = r.get("ema_alignment", "neutral")
            rvol = r.get("rvol", 1.0)
            confirmed = r.get("breakout_confirmed", False)

            # Tier 1: Coiled spring -- squeeze + ATR compressed (highest edge)
            if not confirmed and squeeze and atr == "compressed" and score >= t_coiled:
                alertable.append((r, "COILED SPRING", "crypto_squeeze_firing"))

            # Tier 2: Squeeze just releasing -- imminent move
            elif not confirmed and squeeze_firing and score >= t_squeeze:
                alertable.append((r, "SQUEEZE FIRING", "crypto_squeeze_firing"))

            # Tier 3: Squeeze + bullish alignment + volume picking up
            elif not confirmed and squeeze and ema in ("bullish_stack", "bullish") and rvol >= rvol_building and score >= t_building:
                alertable.append((r, "BREAKOUT BUILDING", "crypto_breakout"))

            # Tier 4: ATR compressed with strong momentum setup
            elif not confirmed and atr == "compressed" and score >= t_range:
                alertable.append((r, "RANGE TIGHTENING", "crypto_breakout"))

            # Tier 5: High-score pre-breakout with volume
            elif not confirmed and score >= t_high and rvol >= rvol_high:
                alertable.append((r, "HIGH-SCORE SETUP", "crypto_breakout"))

            # Tier 6: Freshly confirmed breakout (still actionable)
            elif confirmed and score >= t_high and rvol >= rvol_high:
                alertable.append((r, "BREAKOUT CONFIRMED", "crypto_breakout"))

        sent = 0
        cooldown_skipped = 0
        _max_alerts = int(get_adaptive_weight("crypto_alert_max_per_cycle"))
        for setup, prefix, alert_type in alertable[:_max_alerts]:
            ticker = setup["ticker"]
            last_sent = _crypto_alert_cooldown.get(ticker, 0)
            if now - last_sent < get_adaptive_weight("crypto_alert_cooldown_s"):
                cooldown_skipped += 1
                logger.debug(f"[scheduler] {ticker} skipped (cooldown)")
                continue

            flags = []
            if setup.get("bb_squeeze"):
                flags.append("BB squeeze")
            if setup.get("bb_squeeze_firing"):
                flags.append("squeeze releasing")
            if setup.get("atr_state") == "compressed":
                flags.append("ATR compressed")
            if setup.get("ema_alignment") in ("bullish_stack",):
                flags.append("full EMA stack")

            flag_line = " + ".join(flags) if flags else ""
            sig_text = "; ".join(setup.get("signals", [])[:3])

            msg = (
                f"{prefix}: {ticker}\n"
                f"Score {setup['score']}/10 | ${setup['price']} "
                f"({setup.get('change_24h', 0):+.1f}% 24h)\n"
                f"RVOL {setup.get('rvol', 0):.1f}x | "
                f"EMA: {setup.get('ema_alignment', 'n/a').replace('_', ' ')}\n"
                + (f"{flag_line}\n" if flag_line else "")
                + f"Entry ${setup.get('entry_price')} | "
                f"Stop ${setup.get('stop_loss')} | "
                f"Target ${setup.get('take_profit')}\n"
                f"{sig_text}"
            )

            dispatch_alert(
                ticker=ticker,
                alert_type=alert_type,
                message=msg,
                price=setup["price"],
            )
            _crypto_alert_cooldown[ticker] = now
            sent += 1

        logger.info(
            f"[scheduler] Crypto breakout scan done: "
            f"{result.get('total_scanned', 0)} scanned, "
            f"{len(all_results)} scored, "
            f"{len(alertable)} alertable ({cooldown_skipped} cooldown-skipped), "
            f"{sent} SMS sent"
        )
    except Exception as e:
        logger.error(f"[scheduler] Crypto breakout scan failed: {e}")


def _run_momentum_scanner_job():
    """Active momentum scanner: find immaculate day-trade setups and alert."""
    from .trading.scanner import run_momentum_scanner, get_adaptive_weight
    from .trading.alerts import dispatch_alert

    logger.info("[scheduler] Running momentum scanner")
    try:
        result = run_momentum_scanner(max_results=int(get_adaptive_weight("momentum_max_results")))
        immaculate = [r for r in result.get("results", []) if r.get("immaculate")]
        if immaculate:
            for setup in immaculate:
                msg = (
                    f"MOMENTUM ALERT: {setup['ticker']} "
                    f"Score {setup['score']}/10 | "
                    f"${setup['price']} | "
                    f"Vol {setup.get('vol_ratio', 0):.1f}x | "
                    f"R:R {setup.get('risk_reward', 0):.1f} | "
                    f"{', '.join(setup.get('signals', [])[:3])}"
                )
                dispatch_alert(
                    ticker=setup["ticker"],
                    alert_type="momentum_immaculate",
                    message=msg,
                    price=setup["price"],
                )
            logger.info(
                f"[scheduler] Momentum scanner found {len(immaculate)} immaculate setup(s)"
            )
        else:
            logger.info(
                f"[scheduler] Momentum scanner: {result.get('matches', 0)} decent, 0 immaculate"
            )
    except Exception as e:
        logger.error(f"[scheduler] Momentum scanner failed: {e}")


def _run_code_learning_job():
    """Executed by APScheduler: Code Brain learning cycle."""
    from ..db import SessionLocal
    from .code_brain.learning import run_code_learning_cycle

    logger.info("[scheduler] Starting Code Brain learning cycle")
    db = SessionLocal()
    try:
        result = run_code_learning_cycle(db, user_id=None)
        logger.info("[scheduler] Code Brain learning result: %s", result)
    except Exception as e:
        logger.error("[scheduler] Code Brain learning failed: %s", e)
    finally:
        db.close()


def _run_reasoning_learning_job():
    """Executed by APScheduler: Reasoning Brain cycle for the primary user (if any)."""
    from ..db import SessionLocal
    from ..models import User
    from .reasoning_brain.learning import run_reasoning_cycle
    from ..config import settings as _settings

    if not _settings.reasoning_enabled:
        return

    logger.info("[scheduler] Starting Reasoning Brain cycle")
    db = SessionLocal()
    try:
        user = db.query(User).order_by(User.id.asc()).first()
        if not user:
            logger.info("[scheduler] No users found; skipping Reasoning Brain cycle")
            return
        result = run_reasoning_cycle(db, user.id, trace_id="scheduler")
        logger.info("[scheduler] Reasoning Brain result: %s", result)
    except Exception as e:
        logger.error("[scheduler] Reasoning Brain failed: %s", e)
    finally:
        db.close()


def start_scheduler():
    """Start the background scheduler. Safe to call multiple times."""
    global _scheduler
    with _lock:
        if _scheduler is not None:
            return

        from ..config import settings
        _learning_hours = max(1, settings.learning_interval_hours)

        _scheduler = BackgroundScheduler(daemon=True)

        _scheduler.add_job(
            _run_learning_job,
            trigger=IntervalTrigger(hours=_learning_hours),
            id="learning_cycle",
            name=f"Full market learning cycle (every {_learning_hours}h)",
            replace_existing=True,
            max_instances=1,
            next_run_time=datetime.now(),  # run immediately on startup
        )

        _scheduler.add_job(
            _run_weekly_review_job,
            trigger=CronTrigger(day_of_week="sun", hour=18, minute=0),
            id="weekly_review",
            name="Weekly performance review",
            replace_existing=True,
            max_instances=1,
        )

        _scheduler.add_job(
            _run_broker_sync_job,
            trigger=CronTrigger(
                day_of_week="mon-fri",
                hour="9-16",
                minute="*/2",
            ),
            id="broker_sync",
            name="Robinhood order+position sync (market hours every 2min)",
            replace_existing=True,
            max_instances=1,
        )

        _scheduler.add_job(
            _run_price_monitor_job,
            trigger=CronTrigger(
                day_of_week="mon-fri",
                hour="9-16",
                minute="*/5",
            ),
            id="price_monitor",
            name="Price monitor & alerts (market hours every 5min)",
            replace_existing=True,
            max_instances=1,
        )

        _scheduler.add_job(
            _run_momentum_scanner_job,
            trigger=CronTrigger(
                day_of_week="mon-fri",
                hour="9-10",
                minute="*/15",
                timezone="US/Eastern",
            ),
            id="momentum_scanner",
            name="Momentum scanner (9:30-11AM ET every 15min)",
            replace_existing=True,
            max_instances=1,
        )

        _scheduler.add_job(
            _run_crypto_breakout_job,
            trigger=IntervalTrigger(minutes=15),
            id="crypto_breakout_scanner",
            name="Crypto breakout scanner (every 15min, 24/7)",
            replace_existing=True,
            max_instances=1,
        )

        _code_hours = max(1, settings.code_brain_interval_hours)
        _scheduler.add_job(
            _run_code_learning_job,
            trigger=IntervalTrigger(hours=_code_hours),
            id="code_learning_cycle",
            name=f"Code Brain learning cycle (every {_code_hours}h)",
            replace_existing=True,
            max_instances=1,
        )

        _reasoning_hours = max(1, settings.reasoning_interval_hours)
        _scheduler.add_job(
            _run_reasoning_learning_job,
            trigger=IntervalTrigger(hours=_reasoning_hours),
            id="reasoning_cycle",
            name=f"Reasoning Brain cycle (every {_reasoning_hours}h)",
            replace_existing=True,
            max_instances=1,
        )

        _scheduler.start()
        logger.info(
            f"[scheduler] Trading scheduler started (learning every {_learning_hours}h, "
            f"code brain every {_code_hours}h, "
            f"reasoning brain every {_reasoning_hours}h, "
            "weekly review Sun 6PM, broker sync every 15min, price monitor every 5min, "
            "momentum scanner 9:30-11AM ET, crypto breakout scanner every 15min 24/7)"
        )


def stop_scheduler():
    """Gracefully stop the scheduler and signal background tasks to abort."""
    global _scheduler
    from . import trading_service as ts
    ts.signal_shutdown()
    with _lock:
        if _scheduler is not None:
            _scheduler.shutdown(wait=True)
            _scheduler = None
            logger.info("[scheduler] Trading scheduler stopped")


def get_scheduler_info() -> dict:
    """Info about the scheduler and its jobs for the Brain dashboard."""
    if _scheduler is None:
        return {"running": False, "jobs": []}

    jobs = []
    for job in _scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
        })

    return {
        "running": _scheduler.running,
        "jobs": jobs,
    }


def trigger_learning_now():
    """Manually trigger a learning cycle if not already running."""
    from . import trading_service as ts
    if ts.get_learning_status()["running"]:
        return False

    thread = threading.Thread(target=_run_learning_job, daemon=True)
    thread.start()
    return True

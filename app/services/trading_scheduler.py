"""Background scheduler for continuous trading AI learning.

Runs learning cycles (scan → snapshot → backfill → mine → journal)
automatically on a schedule so the AI Brain is always growing.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta

from apscheduler.executors.pool import ThreadPoolExecutor as APSchedulerThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
_lock = threading.Lock()


def run_scheduler_job_guarded(job_id: str, fn: Callable[[], None]) -> None:
    """Run a scheduler callback with structured logs; swallow exceptions after logging.

    APScheduler must not crash the process on job failure; failures are recorded
    with ``logger.exception`` and a duration field for ops triage.
    """
    t0 = time.monotonic()
    logger.info("[scheduler_job] job_id=%s phase=start", job_id)
    try:
        fn()
    except Exception:
        dur_ms = int((time.monotonic() - t0) * 1000)
        logger.exception(
            "[scheduler_job] job_id=%s phase=fail duration_ms=%s",
            job_id,
            dur_ms,
        )
        return
    dur_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "[scheduler_job] job_id=%s phase=ok duration_ms=%s",
        job_id,
        dur_ms,
    )


def _run_daily_prescreen_job():
    """Persist global prescreen candidates (~2 AM America/Los_Angeles)."""
    from ..config import settings as _settings

    if not getattr(_settings, "brain_prescreen_scheduler_enabled", True):
        return

    def _work() -> None:
        from ..db import SessionLocal
        from .trading.prescreen_job import run_daily_prescreen_job as _prescreen_run

        db = SessionLocal()
        try:
            result = _prescreen_run(db)
            logger.info("[scheduler] Daily prescreen result: %s", result)
        finally:
            db.close()

    run_scheduler_job_guarded("daily_prescreen", _work)


def _run_daily_market_scan_job():
    """Full market scan over prescreen DB rows (~2:30 AM America/Los_Angeles)."""
    from ..config import settings as _settings

    if not getattr(_settings, "brain_daily_market_scan_scheduler_enabled", True):
        return

    from ..db import SessionLocal
    from .trading.brain_batch_job_log import brain_batch_job_begin, brain_batch_job_finish
    from .trading.scanner import clear_scanner_caches, run_full_market_scan

    def _work() -> None:
        db = SessionLocal()
        _uid = getattr(_settings, "brain_default_user_id", None)
        job_id = brain_batch_job_begin(db, "daily_market_scan", user_id=_uid)
        db.commit()
        try:
            results = run_full_market_scan(db, _uid, use_full_universe=True)
            brain_batch_job_finish(
                db,
                job_id,
                ok=True,
                meta={
                    "tickers_scored": len(results),
                    "user_id": _uid,
                },
            )
            db.commit()
            logger.info("[scheduler] Daily market scan done: %s scored", len(results))
        except Exception as e:
            logger.error("[scheduler] Daily market scan failed: %s", e)
            try:
                brain_batch_job_finish(db, job_id, ok=False, error=str(e))
                db.commit()
            except Exception:
                logger.exception("[scheduler] Failed to record daily_market_scan batch job failure")
        finally:
            clear_scanner_caches()
            db.close()

    run_scheduler_job_guarded("daily_market_scan", _work)


def _run_brain_market_snapshot_job():
    """Write daily + intraday ``trading_snapshots`` (decoupled from ``run_learning_cycle`` by default)."""
    from ..config import settings as _settings

    if not getattr(_settings, "brain_market_snapshot_scheduler_enabled", True):
        return

    from ..db import SessionLocal
    from .trading.batch_job_constants import JOB_BRAIN_MARKET_SNAPSHOTS
    from .trading.brain_batch_job_log import brain_batch_job_begin, brain_batch_job_finish
    from .trading import learning as _learning

    def _work() -> None:
        _uid = getattr(_settings, "brain_default_user_id", None)
        db = SessionLocal()
        jid = None
        try:
            jid = brain_batch_job_begin(db, JOB_BRAIN_MARKET_SNAPSHOTS, user_id=_uid)
            db.commit()
            out = _learning.run_scheduled_market_snapshots(db, _uid)
            brain_batch_job_finish(db, jid, ok=True, payload_json=out, meta={})
            try:
                from .trading.brain_neural_mesh.publisher import publish_market_snapshots_refreshed

                publish_market_snapshots_refreshed(
                    db,
                    meta={
                        "daily": out.get("snapshots_taken_daily"),
                        "intraday": out.get("intraday_snapshots_taken"),
                    },
                )
            except Exception as _nm_e:
                logger.debug("[scheduler] neural mesh snapshot publish skipped: %s", _nm_e)
            db.commit()
            logger.info(
                "[scheduler] brain_market_snapshots ok daily=%s intra=%s universe=%s",
                out.get("snapshots_taken_daily"),
                out.get("intraday_snapshots_taken"),
                out.get("universe_size"),
            )
        except Exception as e:
            logger.warning("[scheduler] brain_market_snapshots failed: %s", e, exc_info=True)
            if jid:
                try:
                    brain_batch_job_finish(db, jid, ok=False, error=str(e))
                    db.commit()
                except Exception:
                    logger.exception("[scheduler] brain_market_snapshots batch finish failed")
        finally:
            db.close()

    run_scheduler_job_guarded("brain_market_snapshots", _work)


def _run_paper_trade_check_job():
    """Check open paper trades for stop/target/expiry exits."""

    def _work() -> None:
        from ..db import SessionLocal
        from .trading.paper_trading import check_paper_exits

        db = SessionLocal()
        try:
            result = check_paper_exits(db)
            if result.get("closed", 0) > 0:
                logger.info("[scheduler] Paper trades: checked %d, closed %d",
                            result["checked"], result["closed"])
        finally:
            db.close()

    run_scheduler_job_guarded("paper_trade_check", _work)


def _run_momentum_paper_runner_batch_job():
    """Advance queued/active momentum *paper* automation sessions (simulated only; Phase 7)."""

    def _work() -> None:
        from ..config import settings as _settings
        from ..db import SessionLocal
        from .trading.momentum_neural.paper_runner import run_paper_runner_batch

        if not _settings.chili_momentum_paper_runner_enabled:
            return
        if not _settings.chili_momentum_paper_runner_scheduler_enabled:
            return

        db = SessionLocal()
        try:
            results = run_paper_runner_batch(db, limit=30)
            db.commit()
            if results:
                logger.info("[scheduler] Momentum paper runner: ticked %d session(s)", len(results))
        finally:
            db.close()

    run_scheduler_job_guarded("momentum_paper_runner_batch", _work)


def _run_momentum_live_runner_batch_job():
    """Advance queued/active momentum *live* automation sessions (real Coinbase orders — Phase 8)."""

    def _work() -> None:
        from ..config import settings as _settings
        from ..db import SessionLocal
        from .trading.momentum_neural.live_runner import run_live_runner_batch

        if not _settings.chili_momentum_live_runner_enabled:
            return
        if not _settings.chili_momentum_live_runner_scheduler_enabled:
            return

        db = SessionLocal()
        try:
            results = run_live_runner_batch(db, limit=15)
            db.commit()
            if results:
                logger.info("[scheduler] Momentum live runner: ticked %d session(s)", len(results))
        finally:
            db.close()

    run_scheduler_job_guarded("momentum_live_runner_batch", _work)


def _run_data_retention_job():
    """Daily sweep: archive old snapshots, prune stale batch job payloads."""

    def _work() -> None:
        from ..db import SessionLocal
        from .trading.data_retention import run_retention_policy

        logger.info("[scheduler] Data retention sweep starting")
        db = SessionLocal()
        try:
            results = run_retention_policy(db)
            logger.info("[scheduler] Data retention done: %s", results)
        finally:
            db.close()

    run_scheduler_job_guarded("data_retention", _work)


def _run_weekly_review_job():
    """Weekly performance review job."""
    from ..db import SessionLocal
    from .trading.public_api import weekly_performance_review as _weekly_review

    def _work() -> None:
        logger.info("[scheduler] Starting weekly review")
        db = SessionLocal()
        try:
            _weekly_review(db, user_id=None)
        finally:
            db.close()

    run_scheduler_job_guarded("weekly_review", _work)


def _run_broker_sync_job():
    """Sync Robinhood orders + positions to local DB during market hours."""
    from . import broker_service

    if not broker_service.is_connected():
        return

    def _work() -> None:
        from ..db import SessionLocal

        logger.info("[scheduler] Starting Robinhood order + position sync")
        db = SessionLocal()
        try:
            order_result = broker_service.sync_orders_to_db(db, user_id=None)
            logger.info(f"[scheduler] Order sync result: {order_result}")
            pos_result = broker_service.sync_positions_to_db(db, user_id=None)
            logger.info(f"[scheduler] Position sync result: {pos_result}")
        finally:
            db.close()

    run_scheduler_job_guarded("broker_sync", _work)


def _run_price_monitor_job():
    """Check positions/breakouts/picks and dispatch alerts every 5 minutes."""
    from ..db import SessionLocal
    from .trading.alerts import run_price_monitor

    def _work() -> None:
        logger.info("[scheduler] Starting price monitor check")
        db = SessionLocal()
        try:
            result = run_price_monitor(db, user_id=None)
            logger.info(f"[scheduler] Price monitor result: {result}")
        finally:
            db.close()

    run_scheduler_job_guarded("price_monitor", _work)


def _run_pattern_imminent_job():
    """Scan active ScanPatterns for near-complete setups; alert within configured ETA window.

    Crypto-friendly patterns run 24/7; stock patterns only during US equity session
    (handled inside ``run_pattern_imminent_scan``).
    """
    import time as _t
    from ..config import settings as _settings
    from ..db import SessionLocal
    from .trading.batch_job_constants import JOB_PATTERN_IMMINENT_SCANNER
    from .trading.brain_batch_job_log import brain_batch_job_begin, brain_batch_job_finish
    from .trading.pattern_imminent_alerts import run_pattern_imminent_scan

    if not getattr(_settings, "pattern_imminent_alert_enabled", True):
        return

    def _work() -> None:
        logger.info("[scheduler] Pattern imminent breakout scan starting")
        db = SessionLocal()
        jid = None
        t_wall = _t.time()
        try:
            _uid = getattr(_settings, "brain_default_user_id", None)
            jid = brain_batch_job_begin(db, JOB_PATTERN_IMMINENT_SCANNER, _uid)
            db.commit()

            result = run_pattern_imminent_scan(db, user_id=_uid)
            logger.info("[scheduler] Pattern imminent result: %s", result)

            duration = round(_t.time() - t_wall, 1)
            if not result.get("ok", True):
                brain_batch_job_finish(
                    db,
                    jid,
                    ok=False,
                    error=str(result.get("reason") or "pattern imminent failed"),
                    meta={"duration_s": duration},
                    payload_json=dict(result),
                )
                db.commit()
                return

            brain_batch_job_finish(
                db,
                jid,
                ok=True,
                meta={
                    "duration_s": duration,
                    "alerts_sent": result.get("alerts_sent", 0),
                    "tickers_scored": result.get("tickers_scored", 0),
                    "candidates": result.get("candidates", 0),
                },
                payload_json=dict(result),
            )
            db.commit()
        except Exception as e:
            logger.error("[scheduler] Pattern imminent scan failed: %s", e)
            if jid:
                try:
                    brain_batch_job_finish(db, jid, ok=False, error=str(e))
                    db.commit()
                except Exception:
                    logger.exception("[scheduler] pattern_imminent batch_job_finish failed")
        finally:
            db.close()

    run_scheduler_job_guarded("pattern_imminent_scanner", _work)


_crypto_alert_cooldown: dict[str, float] = {}
_stock_alert_cooldown: dict[str, float] = {}


def _record_breakout_alert(
    setup: dict, alert_tier: str, asset_type: str,
    scan_cycle_id: str | None = None,
    timeframe: str | None = None,
) -> None:
    """Write a BreakoutAlert row for outcome tracking."""
    import json as _json
    try:
        from ..config import settings as _settings
        from ..db import SessionLocal
        from ..models.trading import BreakoutAlert
        from .trading.market_data import get_market_regime

        _regime = "unknown"
        try:
            _regime = get_market_regime().get("regime", "unknown")
        except Exception:
            pass

        _sector = setup.get("sector") or ("crypto" if asset_type == "crypto" else None)
        _news_sent = setup.get("news_sentiment")
        _uid = setup.get("user_id") or getattr(_settings, "brain_default_user_id", None)

        # Extract best scan_pattern_id from pattern engine matches if available
        _spid: int | None = setup.get("scan_pattern_id")
        if not _spid:
            _pe_matches = setup.get("pattern_engine_matches") or []
            if _pe_matches:
                _spid = _pe_matches[0].get("pattern_id")

        db = SessionLocal()
        try:
            row = BreakoutAlert(
                ticker=setup.get("ticker", ""),
                asset_type=asset_type,
                alert_tier=alert_tier,
                score_at_alert=setup.get("score", 0),
                indicator_snapshot=_json.dumps(setup.get("indicators", {})),
                price_at_alert=setup.get("price", 0),
                entry_price=setup.get("entry_price"),
                stop_loss=setup.get("stop_loss"),
                target_price=setup.get("take_profit"),
                signals_snapshot=_json.dumps(setup.get("signals", [])[:10]),
                outcome="pending",
                regime_at_alert=_regime,
                scan_cycle_id=scan_cycle_id,
                timeframe=timeframe,
                sector=_sector,
                news_sentiment_at_alert=_news_sent,
                user_id=_uid,
                scan_pattern_id=_spid,
            )
            db.add(row)
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"[scheduler] Failed to record breakout alert: {e}", exc_info=True)


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
    import uuid as _uuid
    from ..config import settings as _settings
    from ..db import SessionLocal
    from .trading.batch_job_constants import JOB_CRYPTO_BREAKOUT_SCANNER
    from .trading.brain_batch_job_log import brain_batch_job_begin, brain_batch_job_finish
    from .trading.scanner import (
        _crypto_breakout_payload_from_run,
        get_adaptive_weight,
        run_crypto_breakout_scan,
    )
    from .trading.alerts import dispatch_alert

    logger.info("[scheduler] Running crypto breakout scanner")
    db = SessionLocal()
    jid = None
    t_wall = _t.time()
    try:
        jid = brain_batch_job_begin(
            db,
            JOB_CRYPTO_BREAKOUT_SCANNER,
            getattr(_settings, "brain_default_user_id", None),
        )
        db.commit()

        result = run_crypto_breakout_scan(
            max_results=20,
            batch_job_id=jid,
            skip_db_ttl_check=True,
        )
        now = _t.time()
        _cycle_id = str(_uuid.uuid4())[:12]

        # BTC dump filter — reduce alert volume when BTC is crashing
        _btc_dump_halve = False
        try:
            from .trading.market_data import get_btc_state
            _btc = get_btc_state()
            if (_btc.get("btc_change_pct") or 0) < -5:
                _btc_dump_halve = True
                logger.info("[scheduler] BTC dumping >5% — halving crypto alert cap")
        except Exception:
            pass

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

        all_results = list(result.get("all_results") or result.get("results") or [])

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
        if _btc_dump_halve:
            _max_alerts = max(1, _max_alerts // 2)

        _sector_counts: dict[str, int] = {}
        _sector_cap = int(get_adaptive_weight("alert_max_per_sector"))

        for setup, prefix, alert_type in alertable[:_max_alerts * 2]:
            if sent >= _max_alerts:
                break
            ticker = setup["ticker"]
            last_sent = _crypto_alert_cooldown.get(ticker, 0)
            if now - last_sent < get_adaptive_weight("crypto_alert_cooldown_s"):
                cooldown_skipped += 1
                logger.debug(f"[scheduler] {ticker} skipped (cooldown)")
                continue

            _sect = setup.get("sector") or "crypto_other"
            if _sector_counts.get(_sect, 0) >= _sector_cap:
                continue
            _sector_counts[_sect] = _sector_counts.get(_sect, 0) + 1

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

            _hold_est = setup.get("hold_estimate") or {}
            _hold = _hold_est.get("label", "")
            from .trading.scanner import classify_trade_type
            _tc = classify_trade_type(
                setup.get("signals", []), _hold_est,
                setup, is_crypto=True,
            )
            msg = (
                f"{_tc['label']}: {ticker}\n"
                f"Score {setup['score']}/10 | ${setup['price']} "
                f"({setup.get('change_24h', 0):+.1f}% 24h)\n"
                f"RVOL {setup.get('rvol', 0):.1f}x | "
                f"EMA: {setup.get('ema_alignment', 'n/a').replace('_', ' ')}\n"
                + (f"{flag_line}\n" if flag_line else "")
                + f"Entry ${setup.get('entry_price')} | "
                f"Stop ${setup.get('stop_loss')} | "
                f"Target ${setup.get('take_profit')}\n"
                + (f"ETA: {_tc['duration']}\n" if _tc['duration'] else "")
                + f"{sig_text}"
                + "\nSource: crypto breakout scan (heuristic; not a Brain ScanPattern)."
            )

            dispatch_alert(
                ticker=ticker,
                alert_type=alert_type,
                message=msg,
                price=setup["price"],
                trade_type=_tc["type"],
                duration_estimate=_tc["duration"] or None,
            )
            _crypto_alert_cooldown[ticker] = now
            _record_breakout_alert(setup, prefix, "crypto",
                                   scan_cycle_id=_cycle_id, timeframe="15m")
            sent += 1

        logger.info(
            f"[scheduler] Crypto breakout scan done: "
            f"{result.get('total_scanned', 0)} scanned, "
            f"{len(all_results)} scored, "
            f"{len(alertable)} alertable ({cooldown_skipped} cooldown-skipped), "
            f"{sent} alerts sent"
        )

        duration = round(_t.time() - t_wall, 1)
        if not result.get("ok", True):
            brain_batch_job_finish(
                db,
                jid,
                ok=False,
                error=str(result.get("error") or "scan failed"),
                meta={"duration_s": duration},
            )
            db.commit()
            return

        payload = _crypto_breakout_payload_from_run(
            all_results,
            total=int(result.get("total_scanned") or 0),
            scan_time_iso=str(result.get("scan_time") or ""),
            elapsed_s=float(result.get("elapsed_s") or 0),
            errors=int(result.get("errors") or 0),
        )
        brain_batch_job_finish(
            db,
            jid,
            ok=True,
            meta={
                "duration_s": duration,
                "total_scanned": result.get("total_scanned", 0),
                "scored": len(all_results),
                "score_buckets": score_buckets,
                "alerts_sent": sent,
                "alertable": len(alertable),
            },
            payload_json=payload,
        )
        db.commit()
    except Exception as e:
        logger.error(f"[scheduler] Crypto breakout scan failed: {e}")
        if jid:
            try:
                brain_batch_job_finish(db, jid, ok=False, error=str(e))
                db.commit()
            except Exception:
                logger.exception("[scheduler] crypto breakout batch_job_finish failed")
    finally:
        db.close()


def _run_stock_breakout_job():
    """Stock breakout scanner: detect consolidation-to-breakout setups during market hours.

    Uses the same tier logic as crypto but with stock-specific thresholds.
    All thresholds are brain-adaptive.
    """
    import time as _t
    import uuid as _uuid
    from ..config import settings as _settings
    from ..db import SessionLocal
    from .trading.batch_job_constants import JOB_STOCK_BREAKOUT_SCANNER
    from .trading.brain_batch_job_log import brain_batch_job_begin, brain_batch_job_finish
    from .trading.scanner import _stock_breakout_payload_from_run, get_adaptive_weight, run_breakout_scan
    from .trading.alerts import dispatch_alert

    logger.info("[scheduler] Running stock breakout scanner")
    db = SessionLocal()
    jid = None
    t_wall = _t.time()
    try:
        jid = brain_batch_job_begin(
            db,
            JOB_STOCK_BREAKOUT_SCANNER,
            getattr(_settings, "brain_default_user_id", None),
        )
        db.commit()

        result = run_breakout_scan(max_results=20, batch_job_id=jid, skip_db_ttl_check=True)
        now = _t.time()
        _cycle_id = str(_uuid.uuid4())[:12]

        stale = [k for k, v in _stock_alert_cooldown.items() if now - v > 7200]
        for k in stale:
            del _stock_alert_cooldown[k]

        t_coiled = get_adaptive_weight("stock_alert_coiled_spring_min")
        t_squeeze = get_adaptive_weight("stock_alert_squeeze_firing_min")
        t_high = get_adaptive_weight("stock_alert_high_score_min")

        all_results = list(result.get("all_results") or result.get("results") or [])
        logger.info(
            f"[scheduler] Stock breakout scan: {result.get('candidates_scanned', 0)} scanned, "
            f"{len(all_results)} scored"
        )

        alertable: list[tuple[dict, str, str]] = []
        for r in all_results:
            score = r.get("score", 0)
            squeeze = r.get("bb_squeeze", False)
            status = r.get("status", "wait")
            adx = r.get("adx")
            adx_low = adx is not None and adx < 20

            if squeeze and adx_low and score >= t_coiled:
                alertable.append((r, "STOCK COILED SPRING", "stock_breakout"))
            elif status == "breaking_out" and score >= t_squeeze:
                alertable.append((r, "STOCK BREAKOUT", "stock_breakout"))
            elif squeeze and score >= t_squeeze:
                alertable.append((r, "STOCK SQUEEZE SETUP", "stock_breakout"))
            elif score >= t_high:
                alertable.append((r, "STOCK HIGH-SCORE SETUP", "stock_breakout"))

        sent = 0
        cooldown_skipped = 0
        _max_alerts = int(get_adaptive_weight("stock_alert_max_per_cycle"))
        _sector_counts: dict[str, int] = {}
        _sector_cap = int(get_adaptive_weight("alert_max_per_sector"))

        for setup, prefix, alert_type in alertable[:_max_alerts * 2]:
            if sent >= _max_alerts:
                break
            ticker = setup["ticker"]
            last_sent = _stock_alert_cooldown.get(ticker, 0)
            if now - last_sent < get_adaptive_weight("stock_alert_cooldown_s"):
                cooldown_skipped += 1
                continue

            _sect = setup.get("sector") or "unknown"
            if _sector_counts.get(_sect, 0) >= _sector_cap:
                continue
            _sector_counts[_sect] = _sector_counts.get(_sect, 0) + 1

            flags = []
            if setup.get("bb_squeeze"):
                flags.append("BB squeeze")
            if setup.get("adx") and setup["adx"] < 20:
                flags.append(f"ADX {setup['adx']:.0f}")
            flag_line = " + ".join(flags) if flags else ""
            sig_text = "; ".join(setup.get("signals", [])[:3])

            _hold_est = setup.get("hold_estimate") or {}
            _hold = _hold_est.get("label", "")
            from .trading.scanner import classify_trade_type
            _tc = classify_trade_type(
                setup.get("signals", []), _hold_est,
                setup, is_crypto=False,
            )
            msg = (
                f"{_tc['label']}: {ticker}\n"
                f"Score {setup['score']}/10 | ${setup['price']}\n"
                f"Dist to breakout: {setup.get('dist_to_breakout', 0):.1f}%\n"
                + (f"{flag_line}\n" if flag_line else "")
                + f"Entry ${setup.get('entry_price')} | "
                f"Stop ${setup.get('stop_loss')} | "
                f"Target ${setup.get('take_profit')}\n"
                + (f"ETA: {_tc['duration']}\n" if _tc['duration'] else "")
                + f"{sig_text}"
                + "\nSource: stock breakout scan (heuristic; not a Brain ScanPattern)."
            )

            dispatch_alert(
                ticker=ticker,
                alert_type=alert_type,
                message=msg,
                price=setup["price"],
                trade_type=_tc["type"],
                duration_estimate=_tc["duration"] or None,
            )
            _stock_alert_cooldown[ticker] = now
            _record_breakout_alert(setup, prefix, "stock",
                                   scan_cycle_id=_cycle_id, timeframe="1d")
            sent += 1

        logger.info(
            f"[scheduler] Stock breakout scan done: "
            f"{len(alertable)} alertable ({cooldown_skipped} cooldown-skipped), "
            f"{sent} alerts sent"
        )

        duration = round(_t.time() - t_wall, 1)
        if not result.get("ok", True):
            brain_batch_job_finish(
                db,
                jid,
                ok=False,
                error="stock breakout scan failed",
                meta={"duration_s": duration},
            )
            db.commit()
            return

        payload = _stock_breakout_payload_from_run(
            all_results,
            candidates_scanned=int(result.get("candidates_scanned") or 0),
            total_sourced=int(result.get("total_sourced") or 0),
            elapsed_s=float(result.get("elapsed_s") or 0),
        )
        brain_batch_job_finish(
            db,
            jid,
            ok=True,
            meta={
                "duration_s": duration,
                "alerts_sent": sent,
                "alertable": len(alertable),
                "scored": len(all_results),
            },
            payload_json=payload,
        )
        db.commit()
    except Exception as e:
        logger.error(f"[scheduler] Stock breakout scan failed: {e}")
        if jid:
            try:
                brain_batch_job_finish(db, jid, ok=False, error=str(e))
                db.commit()
            except Exception:
                logger.exception("[scheduler] stock breakout batch_job_finish failed")
    finally:
        db.close()


def _run_momentum_scanner_job():
    """Active momentum scanner: find immaculate day-trade setups and alert."""
    import time as _t
    from ..config import settings as _settings
    from ..db import SessionLocal
    from .trading.batch_job_constants import JOB_MOMENTUM_SCANNER
    from .trading.brain_batch_job_log import brain_batch_job_begin, brain_batch_job_finish
    from .trading.scanner import _momentum_payload_from_run, get_adaptive_weight, run_momentum_scanner
    from .trading.alerts import dispatch_alert

    logger.info("[scheduler] Running momentum scanner")
    db = SessionLocal()
    jid = None
    t_wall = _t.time()
    try:
        jid = brain_batch_job_begin(
            db,
            JOB_MOMENTUM_SCANNER,
            getattr(_settings, "brain_default_user_id", None),
        )
        db.commit()

        result = run_momentum_scanner(
            max_results=int(get_adaptive_weight("momentum_max_results")),
            batch_job_id=jid,
            skip_db_ttl_check=True,
        )
        immaculate = [r for r in result.get("results", []) if r.get("immaculate")]
        if immaculate:
            for setup in immaculate:
                from .trading.scanner import classify_trade_type
                _hold_est = setup.get("hold_estimate") or {}
                _tc = classify_trade_type(
                    setup.get("signals", []), _hold_est, setup,
                )
                _dur_part = f" | ETA {_tc['duration']}" if _tc["duration"] else ""
                msg = (
                    f"MOMENTUM {_tc['label']}: {setup['ticker']} "
                    f"Score {setup['score']}/10 | "
                    f"${setup['price']} | "
                    f"Vol {setup.get('vol_ratio', 0):.1f}x | "
                    f"R:R {setup.get('risk_reward', 0):.1f}{_dur_part} | "
                    f"{', '.join(setup.get('signals', [])[:3])}"
                )
                dispatch_alert(
                    ticker=setup["ticker"],
                    alert_type="momentum_immaculate",
                    message=msg,
                    price=setup["price"],
                    trade_type=_tc["type"],
                    duration_estimate=_tc["duration"] or None,
                )
            logger.info(
                f"[scheduler] Momentum scanner found {len(immaculate)} immaculate setup(s)"
            )
        else:
            logger.info(
                f"[scheduler] Momentum scanner: {result.get('matches', 0)} decent, 0 immaculate"
            )

        duration = round(_t.time() - t_wall, 1)
        if not result.get("ok", True):
            brain_batch_job_finish(
                db,
                jid,
                ok=False,
                error="momentum scan failed",
                meta={"duration_s": duration},
            )
            db.commit()
            return

        res_list = result.get("results") or []
        payload = _momentum_payload_from_run(
            res_list,
            candidates_scanned=int(result.get("candidates_scanned") or 0),
            total_sourced=int(result.get("total_sourced") or 0),
            elapsed_s=float(result.get("elapsed_s") or 0),
            immaculate_count=int(result.get("immaculate_count") or 0),
        )
        brain_batch_job_finish(
            db,
            jid,
            ok=True,
            meta={
                "duration_s": duration,
                "immaculate_count": len(immaculate),
                "matches": result.get("matches", 0),
            },
            payload_json=payload,
        )
        db.commit()
    except Exception as e:
        logger.error(f"[scheduler] Momentum scanner failed: {e}")
        if jid:
            try:
                brain_batch_job_finish(db, jid, ok=False, error=str(e))
                db.commit()
            except Exception:
                logger.exception("[scheduler] momentum batch_job_finish failed")
    finally:
        db.close()


def _check_breakout_outcomes():
    """Hourly job: check price outcomes for pending breakout alerts.

    For each pending BreakoutAlert:
      - If >=1h old: fill price_1h, compute gain
      - If >=4h old: fill price_4h
      - If >=24h old: fill price_24h, classify outcome, close the record
    """
    from datetime import timedelta
    import time as _t
    from ..db import SessionLocal
    from ..models.trading import BreakoutAlert
    from .trading.market_data import fetch_quote

    logger.info("[scheduler] Checking breakout alert outcomes")
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        pending = db.query(BreakoutAlert).filter(
            BreakoutAlert.outcome == "pending",
        ).all()

        if not pending:
            logger.info("[scheduler] No pending breakout alerts to check")
            return

        from .trading.market_data import fetch_quotes_batch
        unique_tickers = list({a.ticker for a in pending})
        try:
            quotes_map = fetch_quotes_batch(unique_tickers)
        except Exception:
            quotes_map = {}

        updated = 0
        closed = 0
        for alert in pending:
            age = now - alert.alerted_at
            age_hours = age.total_seconds() / 3600

            q = quotes_map.get(alert.ticker)
            current_price = q.get("price") if q else None

            if current_price is None:
                continue

            if alert.price_at_alert <= 0:
                continue

            gain_pct = (current_price - alert.price_at_alert) / alert.price_at_alert * 100

            prev_max_gain = alert.max_gain_pct
            if alert.max_gain_pct is None:
                alert.max_gain_pct = max(0, gain_pct)
            else:
                alert.max_gain_pct = max(alert.max_gain_pct, gain_pct)

            if alert.max_drawdown_pct is None:
                alert.max_drawdown_pct = min(0, gain_pct)
            else:
                alert.max_drawdown_pct = min(alert.max_drawdown_pct, gain_pct)

            # Track time-to-peak: update when new high is set
            if alert.max_gain_pct > (prev_max_gain or 0):
                alert.time_to_peak_hours = round(age_hours, 2)
                alert.price_at_peak = current_price

            # Track time-to-stop: when drawdown first crosses stop distance
            if alert.time_to_stop_hours is None and alert.stop_loss is not None:
                stop_dist_pct = (alert.price_at_alert - alert.stop_loss) / alert.price_at_alert * 100
                if alert.max_drawdown_pct <= -stop_dist_pct:
                    alert.time_to_stop_hours = round(age_hours, 2)

            # Trailing stop simulation: 50% of gain as trailing stop
            if alert.max_gain_pct and alert.max_gain_pct > 0.5:
                trailing_exit = alert.max_gain_pct * 0.5
                if gain_pct <= trailing_exit and (alert.optimal_exit_pct is None or alert.max_gain_pct > alert.optimal_exit_pct):
                    alert.optimal_exit_pct = round(alert.max_gain_pct * 0.75, 2)

            if age_hours >= 1 and alert.price_1h is None:
                alert.price_1h = current_price
                updated += 1

            if age_hours >= 4 and alert.price_4h is None:
                alert.price_4h = current_price
                updated += 1

            if age_hours >= 24:
                alert.price_24h = current_price
                alert.outcome_checked_at = now

                hit_target = (
                    alert.target_price is not None
                    and alert.max_gain_pct >= (
                        (alert.target_price - alert.price_at_alert) / alert.price_at_alert * 100
                    ) * 0.5
                )
                hit_stop = (
                    alert.stop_loss is not None
                    and alert.max_drawdown_pct <= -(
                        (alert.price_at_alert - alert.stop_loss) / alert.price_at_alert * 100
                    )
                )

                if hit_target or alert.max_gain_pct >= 2.0:
                    alert.outcome = "winner"
                    alert.breakout_occurred = True
                elif hit_stop:
                    alert.outcome = "loser"
                    alert.breakout_occurred = False
                elif alert.max_gain_pct >= 1.0 and gain_pct < 0:
                    alert.outcome = "fakeout"
                    alert.breakout_occurred = False
                elif gain_pct > 0:
                    alert.outcome = "winner"
                    alert.breakout_occurred = True
                else:
                    alert.outcome = "loser"
                    alert.breakout_occurred = False

                closed += 1
                updated += 1

        # Expire permanently-pending alerts older than 48h (e.g. no quote data)
        cutoff_expire = now - timedelta(hours=48)
        stale = db.query(BreakoutAlert).filter(
            BreakoutAlert.outcome == "pending",
            BreakoutAlert.alerted_at < cutoff_expire,
        ).all()
        for alert in stale:
            alert.outcome = "expired"
            alert.outcome_checked_at = now
            alert.outcome_notes = (alert.outcome_notes or "") + " [auto-expired: no resolution within 48h]"
            updated += 1

        db.commit()
        logger.info(
            f"[scheduler] Breakout outcome check: {len(pending)} pending, "
            f"{updated} updated, {closed} closed"
        )
    except Exception as e:
        logger.error(f"[scheduler] Breakout outcome check failed: {e}")
    finally:
        db.close()


def _run_promoted_fast_eval_job():
    """Refresh prediction cache using promoted ScanPatterns only (no full learning cycle)."""
    from ..config import settings
    from ..db import SessionLocal
    from .trading.learning import run_promoted_pattern_fast_eval

    if not getattr(settings, "brain_fast_eval_enabled", True):
        return
    logger.info("[scheduler] Promoted-pattern fast eval starting")
    db = SessionLocal()
    try:
        result = run_promoted_pattern_fast_eval(db)
        logger.info("[scheduler] Promoted fast eval result: %s", result)
    except Exception as e:
        logger.error("[scheduler] Promoted fast eval failed: %s", e, exc_info=True)
    finally:
        db.close()



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


def _run_project_brain_job():
    """Run all active Project Brain agent cycles."""
    from ..db import SessionLocal
    from ..models import User
    from ..config import settings as _settings

    if not getattr(_settings, "project_brain_enabled", True):
        return

    logger.info("[scheduler] Starting Project Brain cycle")
    db = SessionLocal()
    try:
        user = db.query(User).order_by(User.id.asc()).first()
        if not user:
            logger.info("[scheduler] No users found; skipping Project Brain cycle")
            return
        from .project_brain.learning import run_project_brain_cycle
        result = run_project_brain_cycle(db, user.id)
        logger.info("[scheduler] Project Brain result: %s", result)
    except Exception as e:
        logger.error("[scheduler] Project Brain failed: %s", e)
    finally:
        db.close()


def _run_scheduler_worker_heartbeat():
    """Record liveness for Jobs UI (scheduler-worker container)."""
    from datetime import datetime as _dt

    from ..db import SessionLocal
    from .trading.batch_job_constants import JOB_SCHEDULER_WORKER_HEARTBEAT
    from .trading.brain_batch_job_log import brain_batch_job_record_completed

    db = SessionLocal()
    try:
        brain_batch_job_record_completed(
            db,
            JOB_SCHEDULER_WORKER_HEARTBEAT,
            ok=True,
            meta={"ts": _dt.utcnow().isoformat() + "Z"},
        )
        db.commit()
    except Exception as e:
        logger.warning("[scheduler] heartbeat failed: %s", e)
        db.rollback()
    finally:
        db.close()


def start_scheduler():
    """Start the background scheduler. Safe to call multiple times."""
    global _scheduler
    with _lock:
        if _scheduler is not None:
            return

        import os

        from ..config import settings

        role = (getattr(settings, "chili_scheduler_role", None) or "all").strip().lower()
        if role == "none":
            logger.info(
                "[scheduler] Role=none — APScheduler disabled (CHILI_SCHEDULER_ROLE env=%r parsed=%r)",
                os.environ.get("CHILI_SCHEDULER_ROLE"),
                role,
            )
            return
        if role not in ("all", "web", "worker"):
            logger.warning(
                "[scheduler] invalid CHILI_SCHEDULER_ROLE=%r; using 'all' "
                "(if you meant API-only web, set none and rebuild image — see docker-compose.yml)",
                role,
            )
            role = "all"
        include_heavy = role in ("all", "worker")
        include_web_light = role in ("all", "web")
        _hb_env = os.environ.get("CHILI_SCHEDULER_EMIT_HEARTBEAT", "").strip().lower()
        emit_worker_heartbeat = role == "worker" or (
            role == "all" and _hb_env in ("1", "true", "yes", "on")
        )
        scheduler_workers = max(
            1,
            int(getattr(settings, "chili_scheduler_executor_workers", 2) or 2),
        )

        _scheduler = BackgroundScheduler(
            daemon=True,
            executors={"default": APSchedulerThreadPoolExecutor(max_workers=scheduler_workers)},
            job_defaults={"coalesce": True},
        )
        logger.info(
            "[scheduler] Role=%s (heavy_scan_jobs=%s web_jobs=%s emit_heartbeat=%s scheduler_workers=%s)",
            role,
            include_heavy,
            include_web_light,
            emit_worker_heartbeat,
            scheduler_workers,
        )

        if include_web_light and getattr(settings, "brain_prescreen_scheduler_enabled", True):
            _scheduler.add_job(
                _run_daily_prescreen_job,
                trigger=CronTrigger(hour=2, minute=0, timezone="America/Los_Angeles"),
                id="daily_prescreen",
                name="Daily prescreen (2:00 America/Los_Angeles)",
                replace_existing=True,
                max_instances=1,
                next_run_time=datetime.now() + timedelta(seconds=25),
            )

        if include_web_light and getattr(settings, "brain_daily_market_scan_scheduler_enabled", True):
            _scheduler.add_job(
                _run_daily_market_scan_job,
                trigger=CronTrigger(hour=2, minute=30, timezone="America/Los_Angeles"),
                id="daily_market_scan",
                name="Daily market scan (2:30 America/Los_Angeles)",
                replace_existing=True,
                max_instances=1,
                next_run_time=datetime.now() + timedelta(seconds=35),
            )

        if getattr(settings, "brain_market_snapshot_scheduler_enabled", True) and (
            include_web_light or role in ("all", "worker")
        ):
            _bsm = max(5, int(getattr(settings, "brain_market_snapshot_interval_minutes", 15)))
            _scheduler.add_job(
                _run_brain_market_snapshot_job,
                trigger=IntervalTrigger(minutes=_bsm),
                id="brain_market_snapshots",
                name=f"Brain market snapshots (every {_bsm}min)",
                replace_existing=True,
                max_instances=1,
                next_run_time=datetime.now() + timedelta(seconds=45),
            )

        if include_web_light:
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

        if include_heavy:
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
                next_run_time=datetime.now() + timedelta(seconds=10),
            )

            _scheduler.add_job(
                _run_stock_breakout_job,
                trigger=CronTrigger(
                    day_of_week="mon-fri",
                    hour="9-15",
                    minute="*/15",
                    timezone="US/Eastern",
                ),
                id="stock_breakout_scanner",
                name="Stock breakout scanner (market hours every 15min)",
                replace_existing=True,
                max_instances=1,
                next_run_time=datetime.now() + timedelta(seconds=15),
            )

            _scheduler.add_job(
                _run_pattern_imminent_job,
                trigger=IntervalTrigger(minutes=15),
                id="pattern_imminent_scanner",
                name="ScanPattern imminent breakout alerts (every 15min; stocks US hours only)",
                replace_existing=True,
                max_instances=1,
                next_run_time=datetime.now() + timedelta(seconds=20),
            )

        if include_web_light:
            _scheduler.add_job(
                _check_breakout_outcomes,
                trigger=IntervalTrigger(hours=1),
                id="breakout_outcome_checker",
                name="Breakout outcome checker (hourly)",
                replace_existing=True,
                max_instances=1,
            )

        if include_web_light:
            _fe_m = max(1, int(getattr(settings, "brain_fast_eval_interval_minutes", 10)))
            if getattr(settings, "brain_fast_eval_enabled", True) and getattr(
                settings, "brain_fast_eval_scheduler_enabled", False
            ):
                _scheduler.add_job(
                    _run_promoted_fast_eval_job,
                    trigger=IntervalTrigger(minutes=_fe_m),
                    id="promoted_pattern_fast_eval",
                    name=f"Promoted pattern prediction refresh (every {_fe_m}m)",
                    replace_existing=True,
                    max_instances=1,
                    next_run_time=datetime.now() + timedelta(seconds=45),
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

            _pb_minutes = max(15, getattr(settings, "project_brain_auto_cycle_minutes", 60))
            if getattr(settings, "project_brain_enabled", True) and getattr(
                settings, "project_brain_scheduler_enabled", False
            ):
                _scheduler.add_job(
                    _run_project_brain_job,
                    trigger=IntervalTrigger(minutes=_pb_minutes),
                    id="project_brain_cycle",
                    name=f"Project Brain cycle (every {_pb_minutes}min)",
                    replace_existing=True,
                    max_instances=1,
                )
        else:
            _code_hours = max(1, settings.code_brain_interval_hours)
            _reasoning_hours = max(1, settings.reasoning_interval_hours)
            _pb_minutes = max(15, getattr(settings, "project_brain_auto_cycle_minutes", 60))

        if emit_worker_heartbeat:
            _scheduler.add_job(
                _run_scheduler_worker_heartbeat,
                trigger=IntervalTrigger(minutes=5),
                id="scheduler_worker_heartbeat",
                name="Scheduler worker heartbeat (every 5min)",
                replace_existing=True,
                max_instances=1,
                next_run_time=datetime.now() + timedelta(seconds=5),
            )

        # Paper trade exit checking: every 15 min during market hours
        if include_web_light:
            _scheduler.add_job(
                _run_paper_trade_check_job,
                trigger=IntervalTrigger(minutes=15),
                id="paper_trade_check",
                name="Paper trade exit check (every 15min)",
                replace_existing=True,
                max_instances=1,
            )

        if (
            include_web_light
            and settings.chili_momentum_paper_runner_enabled
            and settings.chili_momentum_paper_runner_scheduler_enabled
        ):
            _pr_m = max(2, int(settings.chili_momentum_paper_runner_scheduler_interval_minutes))
            _scheduler.add_job(
                _run_momentum_paper_runner_batch_job,
                trigger=IntervalTrigger(minutes=_pr_m),
                id="momentum_paper_runner_batch",
                name=f"Momentum paper automation runner (every {_pr_m}min, simulated)",
                replace_existing=True,
                max_instances=1,
                next_run_time=datetime.now() + timedelta(seconds=55),
            )

        if (
            include_web_light
            and settings.chili_momentum_live_runner_enabled
            and settings.chili_momentum_live_runner_scheduler_enabled
        ):
            _lr_m = max(2, int(settings.chili_momentum_live_runner_scheduler_interval_minutes))
            _scheduler.add_job(
                _run_momentum_live_runner_batch_job,
                trigger=IntervalTrigger(minutes=_lr_m),
                id="momentum_live_runner_batch",
                name=f"Momentum live automation runner (every {_lr_m}min; real orders)",
                replace_existing=True,
                max_instances=1,
                next_run_time=datetime.now() + timedelta(seconds=65),
            )

        # Data retention: archive old snapshots, prune payloads daily at 3:30 AM
        if include_web_light:
            _scheduler.add_job(
                _run_data_retention_job,
                trigger=CronTrigger(hour=3, minute=30, timezone="America/Los_Angeles"),
                id="data_retention",
                name="Data retention sweep (daily 3:30AM PT)",
                replace_existing=True,
                max_instances=1,
            )

        _scheduler.start()
        _ps_note = (
            "daily prescreen 2AM America/Los_Angeles; "
            if getattr(settings, "brain_prescreen_scheduler_enabled", True)
            else ""
        )
        logger.info(
            f"[scheduler] Trading scheduler started (brain worker runs run_learning_cycle; {_ps_note}"
            f"code brain every {_code_hours}h, "
            f"reasoning brain every {_reasoning_hours}h, "
            f"project brain every {_pb_minutes}min, "
            "weekly review Sun 6PM, broker sync market hours every 2min, price monitor every 5min, "
            "momentum scanner 9:30-11AM ET, crypto breakout scanner every 15min 24/7, "
            "stock breakout scanner market hours every 15min, "
            "pattern imminent scanner every 15min; "
            "web pattern research + variant evolution run inside the brain worker learning cycle)"
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
    for _i, job in enumerate(_scheduler.get_jobs()):
        try:
            jid = str(getattr(job, "id", ""))
            jname = getattr(job, "name", None)
            jname_s = str(jname) if jname is not None else ""
            nrt = getattr(job, "next_run_time", None)
            next_iso = nrt.isoformat() if nrt is not None else None
        except Exception as e:
            logger.warning(
                "[scheduler] get_scheduler_info: skip job type=%s: %s",
                type(job).__name__,
                e,
            )
            continue
        jobs.append(
            {
                "id": jid,
                "name": jname_s,
                "next_run": next_iso,
            }
        )

    return {
        "running": _scheduler.running,
        "jobs": jobs,
    }



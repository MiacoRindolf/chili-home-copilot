"""Background scheduler for continuous trading AI learning.

Runs learning cycles (scan → snapshot → backfill → mine → journal)
automatically on a schedule so the AI Brain is always growing.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func

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
_stock_alert_cooldown: dict[str, float] = {}


def _record_breakout_alert(
    setup: dict, alert_tier: str, asset_type: str,
    scan_cycle_id: str | None = None,
    timeframe: str | None = None,
) -> None:
    """Write a BreakoutAlert row for outcome tracking."""
    import json as _json
    try:
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
    from .trading.scanner import run_crypto_breakout_scan, get_adaptive_weight
    from .trading.alerts import dispatch_alert

    logger.info("[scheduler] Running crypto breakout scanner")
    try:
        result = run_crypto_breakout_scan(max_results=20)
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
    except Exception as e:
        logger.error(f"[scheduler] Crypto breakout scan failed: {e}")


def _run_stock_breakout_job():
    """Stock breakout scanner: detect consolidation-to-breakout setups during market hours.

    Uses the same tier logic as crypto but with stock-specific thresholds.
    All thresholds are brain-adaptive.
    """
    import time as _t
    import uuid as _uuid
    from .trading.scanner import run_breakout_scan, get_adaptive_weight
    from .trading.alerts import dispatch_alert

    logger.info("[scheduler] Running stock breakout scanner")
    try:
        result = run_breakout_scan(max_results=20)
        now = _t.time()
        _cycle_id = str(_uuid.uuid4())[:12]

        stale = [k for k, v in _stock_alert_cooldown.items() if now - v > 7200]
        for k in stale:
            del _stock_alert_cooldown[k]

        t_coiled = get_adaptive_weight("stock_alert_coiled_spring_min")
        t_squeeze = get_adaptive_weight("stock_alert_squeeze_firing_min")
        t_high = get_adaptive_weight("stock_alert_high_score_min")

        all_results = result.get("results", [])
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
    except Exception as e:
        logger.error(f"[scheduler] Stock breakout scan failed: {e}")


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
    except Exception as e:
        logger.error(f"[scheduler] Momentum scanner failed: {e}")


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

        db.commit()
        logger.info(
            f"[scheduler] Breakout outcome check: {len(pending)} pending, "
            f"{updated} updated, {closed} closed"
        )
    except Exception as e:
        logger.error(f"[scheduler] Breakout outcome check failed: {e}")
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


def _run_web_pattern_research_job():
    """Executed by APScheduler: search the web for new trading patterns."""
    logger.info("[scheduler] Starting web pattern research")
    try:
        from .trading.web_pattern_researcher import run_web_pattern_research
        report = run_web_pattern_research()
        logger.info("[scheduler] Web pattern research result: %s", report)
    except Exception as e:
        logger.error("[scheduler] Web pattern research failed: %s", e)


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


def _run_pattern_backfill_job():
    """Periodically backtest patterns: new ones and recently reinforced ones.

    Skips patterns that already have 50+ linked backtests with a stable
    win rate (the engine will still pick them up during the full learning
    cycle).  Prioritizes patterns with recent evidence but few backtests.
    Insights linked to a ScanPattern that only have generic backtests get
    their old results cleared and are re-queued for pattern-aware backtesting.
    """
    from ..main import _backfill_state
    if _backfill_state.get("running"):
        logger.info("[scheduler] Pattern backfill already running, skipping")
        return

    logger.info("[scheduler] Starting smart pattern backfill")
    try:
        from ..db import SessionLocal
        from ..models.trading import TradingInsight, BacktestResult
        from .trading.backtest_engine import (
            smart_backtest_insight, _extract_context, _find_linked_pattern,
        )

        db = SessionLocal()
        try:
            bt_by_insight: dict[int, list[str]] = {}
            bt_strats_by_insight: dict[int, set[str]] = {}
            for row in (
                db.query(
                    BacktestResult.related_insight_id,
                    BacktestResult.ticker,
                    BacktestResult.strategy_name,
                )
                .filter(BacktestResult.related_insight_id.isnot(None))
                .all()
            ):
                bt_by_insight.setdefault(row[0], []).append(row[1])
                bt_strats_by_insight.setdefault(row[0], set()).add(row[2])

            candidates = db.query(TradingInsight).filter(
                TradingInsight.evidence_count > 0,
                TradingInsight.active.is_(True),
            ).order_by(TradingInsight.last_seen.desc()).all()

            need_backtest = []
            for ins in candidates:
                existing = bt_by_insight.get(ins.id, [])

                # Linked to a ScanPattern but only generic backtests? Clear & re-queue.
                linked = _find_linked_pattern(db, ins)
                if linked:
                    _, pat_name, _exit_cfg = linked
                    strat_names = bt_strats_by_insight.get(ins.id, set())
                    if pat_name not in strat_names and existing:
                        old_count = (
                            db.query(BacktestResult)
                            .filter(BacktestResult.related_insight_id == ins.id)
                            .delete()
                        )
                        ins.win_count = 0
                        ins.loss_count = 0
                        ins.evidence_count = max(1, ins.evidence_count)
                        db.commit()
                        logger.info(
                            "[scheduler] Cleared %d stale generic backtests "
                            "for insight %d (%s)",
                            old_count, ins.id, pat_name,
                        )
                        need_backtest.append(ins)
                        continue

                if len(existing) >= 50:
                    continue
                if not existing:
                    need_backtest.append(ins)
                    continue
                ctx = _extract_context(
                    ins.pattern_description or "", db=db, insight_id=ins.id,
                )
                if ctx["wants_crypto"] and not any(
                    t.endswith("-USD") for t in existing
                ):
                    need_backtest.append(ins)
                    continue
                if len(existing) < 50:
                    need_backtest.append(ins)

            if not need_backtest:
                logger.info("[scheduler] All patterns sufficiently backtested")
                return

            _backfill_state["running"] = True
            _backfill_state["total"] = len(need_backtest)
            _backfill_state["done"] = 0
            _backfill_state["filled"] = 0

            for ins in need_backtest:
                try:
                    result = smart_backtest_insight(db, ins, target_tickers=25)
                    if result["total"] > 0:
                        _backfill_state["filled"] += 1
                except Exception:
                    pass
                _backfill_state["done"] += 1

            logger.info(
                f"[scheduler] Pattern backfill complete: "
                f"{_backfill_state['filled']}/{len(need_backtest)} updated"
            )
        finally:
            _backfill_state["running"] = False
            db.close()
    except Exception as e:
        _backfill_state["running"] = False
        logger.warning(f"[scheduler] Pattern backfill failed: {e}")


def _run_exit_evolution_job():
    """Periodically fork, compare, and evolve exit-strategy variants."""
    logger.info("[scheduler] Starting exit-strategy evolution")
    try:
        from ..db import SessionLocal
        from .trading.learning import evolve_exit_strategies

        db = SessionLocal()
        try:
            stats = evolve_exit_strategies(db)
            logger.info("[scheduler] Exit evolution done: %s", stats)
        finally:
            db.close()
    except Exception as e:
        logger.warning("[scheduler] Exit evolution failed: %s", e)


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
            next_run_time=datetime.now() + timedelta(minutes=3),
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
            _check_breakout_outcomes,
            trigger=IntervalTrigger(hours=1),
            id="breakout_outcome_checker",
            name="Breakout outcome checker (hourly)",
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

        _pb_minutes = max(15, getattr(settings, "project_brain_auto_cycle_minutes", 60))
        if getattr(settings, "project_brain_enabled", True):
            _scheduler.add_job(
                _run_project_brain_job,
                trigger=IntervalTrigger(minutes=_pb_minutes),
                id="project_brain_cycle",
                name=f"Project Brain cycle (every {_pb_minutes}min)",
                replace_existing=True,
                max_instances=1,
            )

        _scheduler.add_job(
            _run_web_pattern_research_job,
            trigger=IntervalTrigger(hours=12),
            id="web_pattern_research",
            name="Web pattern research (every 12h)",
            replace_existing=True,
            max_instances=1,
            next_run_time=datetime.now() + timedelta(minutes=30),
        )

        _scheduler.add_job(
            _run_pattern_backfill_job,
            trigger=IntervalTrigger(hours=1),
            id="pattern_backfill",
            name="Backtest new untested patterns (every 1h)",
            replace_existing=True,
            max_instances=1,
        )

        _scheduler.add_job(
            _run_exit_evolution_job,
            trigger=IntervalTrigger(hours=2),
            id="exit_evolution",
            name="Exit-strategy evolution (every 2h)",
            replace_existing=True,
            max_instances=1,
            next_run_time=datetime.now() + timedelta(minutes=90),
        )

        _scheduler.start()
        logger.info(
            f"[scheduler] Trading scheduler started (learning every {_learning_hours}h, "
            f"code brain every {_code_hours}h, "
            f"reasoning brain every {_reasoning_hours}h, "
            f"project brain every {_pb_minutes}min, "
            "weekly review Sun 6PM, broker sync every 15min, price monitor every 5min, "
            "momentum scanner 9:30-11AM ET, crypto breakout scanner every 15min 24/7, "
            "stock breakout scanner market hours every 15min, web pattern research every 12h, "
            "exit-strategy evolution every 2h)"
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

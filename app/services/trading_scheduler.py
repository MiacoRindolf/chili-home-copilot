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

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func

from .trading.alert_formatter import (
    format_crypto_breakout,
    format_momentum,
    format_stock_breakout,
)

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
_lock = threading.Lock()

_VIABILITY_BRIDGE_MAX_TICKERS = 30


def run_scheduler_job_guarded(job_id: str, fn: Callable[[], None]) -> None:
    """Run a scheduler callback with structured logs; swallow exceptions after logging.

    APScheduler must not crash the process on job failure; failures are recorded
    with ``logger.exception`` and a duration field for ops triage.
    """
    import gc

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
        gc.collect()
        return
    dur_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "[scheduler_job] job_id=%s phase=ok duration_ms=%s",
        job_id,
        dur_ms,
    )
    gc.collect()


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

    if getattr(_settings, "brain_market_snapshot_defer_while_learning_running", True):
        try:
            from .trading.learning import get_learning_status

            _st = get_learning_status()
            if _st.get("running"):
                logger.info(
                    "[scheduler] brain_market_snapshots deferred: learning cycle running "
                    "(avoid parallel OHLCV with brain-worker; next interval will retry)"
                )
                return
        except Exception as _def_e:
            logger.debug("[scheduler] snapshot defer check skipped: %s", _def_e)

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

                _snap_tickers = out.get("tickers") or []
                publish_market_snapshots_refreshed(
                    db,
                    meta={
                        "daily": out.get("snapshots_taken_daily"),
                        "intraday": out.get("intraday_snapshots_taken"),
                        "tickers": _snap_tickers[:_VIABILITY_BRIDGE_MAX_TICKERS],
                    },
                )
            except Exception as _nm_e:
                logger.debug("[scheduler] neural mesh snapshot publish skipped: %s", _nm_e)
            if getattr(_settings, "brain_work_snapshots_outcome_enabled", True):
                try:
                    from .trading.brain_work.emitters import emit_market_snapshots_batch_outcome

                    emit_market_snapshots_batch_outcome(
                        db,
                        daily=int(out.get("snapshots_taken_daily") or 0),
                        intraday=int(out.get("intraday_snapshots_taken") or 0),
                        universe_size=int(out.get("universe_size") or 0),
                        job_id=str(jid) if jid is not None else None,
                        snapshot_driver=out.get("snapshot_driver"),
                    )
                except Exception as _le:
                    logger.debug("[scheduler] work ledger snapshot outcome skipped: %s", _le)
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
    """Check open paper trades for stop/target/expiry exits, plus live exit engine recommendations."""

    def _work() -> None:
        from ..db import SessionLocal
        from .trading.paper_trading import check_paper_exits

        db = SessionLocal()
        try:
            result = check_paper_exits(db)
            if result.get("closed", 0) > 0:
                logger.info("[scheduler] Paper trades: checked %d, closed %d",
                            result["checked"], result["closed"])
            # Also run the live exit engine for pattern-based exit recommendations
            try:
                from .trading.live_exit_engine import run_exit_engine
                exit_results = run_exit_engine(db)
                exits = exit_results.get("actions", [])
                if exits:
                    logger.info("[scheduler] Live exit engine: %d recommendations", len(exits))
            except Exception:
                logger.debug("[scheduler] live_exit_engine error", exc_info=True)
        finally:
            db.close()

    run_scheduler_job_guarded("paper_trade_check", _work)


def _run_momentum_paper_runner_batch_job():
    """Advance queued/active momentum *paper* automation sessions (simulated only; Phase 7).

    Same per-tick session isolation as the live runner to avoid holding a pooled
    connection across 30 ticks of quote fetches.
    """

    def _work() -> None:
        from ..config import settings as _settings
        from ..db import SessionLocal
        from .trading.momentum_neural.paper_runner import (
            list_runnable_paper_sessions,
            tick_paper_session,
        )

        if not _settings.chili_momentum_paper_runner_enabled:
            return
        if not _settings.chili_momentum_paper_runner_scheduler_enabled:
            return

        db = SessionLocal()
        try:
            session_ids = [int(s.id) for s in list_runnable_paper_sessions(db, limit=30)]
        except Exception:
            logger.warning("[scheduler] paper runner: failed to list runnable sessions", exc_info=True)
            return
        finally:
            db.close()

        if not session_ids:
            return

        ticked = 0
        for sid in session_ids:
            db = SessionLocal()
            try:
                tick_paper_session(db, sid)
                db.commit()
                ticked += 1
            except Exception:
                db.rollback()
                logger.warning("[scheduler] paper runner tick failed session=%s", sid, exc_info=True)
            finally:
                db.close()

        if ticked:
            logger.info("[scheduler] Momentum paper runner: ticked %d session(s)", ticked)

    run_scheduler_job_guarded("momentum_paper_runner_batch", _work)


def _run_momentum_live_runner_batch_job():
    """Advance queued/active momentum *live* automation sessions (real Coinbase orders — Phase 8).

    Each tick gets its own DB session so Coinbase API latency doesn't hold a
    pooled connection for the entire batch (prevents QueuePool exhaustion).
    """

    def _work() -> None:
        from ..config import settings as _settings
        from ..db import SessionLocal
        from .trading.momentum_neural.live_runner import (
            list_runnable_live_sessions,
            tick_live_session,
        )

        if not _settings.chili_momentum_live_runner_enabled:
            return
        if not _settings.chili_momentum_live_runner_scheduler_enabled:
            return

        db = SessionLocal()
        try:
            session_ids = [int(s.id) for s in list_runnable_live_sessions(db, limit=15)]
        except Exception:
            logger.warning("[scheduler] live runner: failed to list runnable sessions", exc_info=True)
            return
        finally:
            db.close()

        if not session_ids:
            return

        ticked = 0
        for sid in session_ids:
            db = SessionLocal()
            try:
                tick_live_session(db, sid)
                db.commit()
                ticked += 1
            except Exception:
                db.rollback()
                logger.warning("[scheduler] live runner tick failed session=%s", sid, exc_info=True)
            finally:
                db.close()

        if ticked:
            logger.info("[scheduler] Momentum live runner: ticked %d session(s)", ticked)

    run_scheduler_job_guarded("momentum_live_runner_batch", _work)


def _run_neural_mesh_drain_job():
    """Drain the neural-mesh activation queue.

    Without this, enqueued events (stop_eval, pattern_health, learning-cycle
    completions, brain_work_outcome, …) pile up until MAX_PENDING_QUEUE_DEPTH
    (500) is reached, at which point every subsequent enqueue is rejected and
    the mesh stops receiving signals. The runner was previously only reachable
    via POST /api/trading/brain/graph/propagate, so in practice nothing drained
    it and the queue saturated within ~36h of live traffic.
    """

    def _work() -> None:
        from sqlalchemy import text

        from ..db import SessionLocal
        from .trading.brain_neural_mesh.activation_runner import run_activation_batch

        db = SessionLocal()
        try:
            # Recover orphaned 'processing' rows (claimed by a worker that
            # crashed before marking them done). Without this, a process
            # crash permanently removes events from circulation.
            orphaned = db.execute(
                text(
                    "UPDATE brain_activation_events "
                    "SET status='pending' "
                    "WHERE status='processing' "
                    "  AND created_at < now() - interval '10 minutes'"
                )
            ).rowcount
            if orphaned:
                logger.warning(
                    "[scheduler] neural_mesh_drain: recovered %d orphaned processing rows",
                    orphaned,
                )
                db.commit()

            summary = run_activation_batch(db, time_budget_sec=3.0, max_events=32)
            db.commit()
            if summary.get("processed", 0) > 0:
                logger.info(
                    "[scheduler] neural_mesh_drain: processed=%d fires=%d inhibitions=%d "
                    "downstream=%d took_ms=%.1f",
                    summary.get("processed", 0),
                    summary.get("fires", 0),
                    summary.get("inhibitions", 0),
                    summary.get("downstream", 0),
                    float(summary.get("took_ms", 0.0)),
                )
        except Exception:
            logger.exception("[scheduler] neural_mesh_drain failed")
            try:
                db.rollback()
            except Exception:
                pass
        finally:
            db.close()

    run_scheduler_job_guarded("neural_mesh_drain", _work)


def _run_bracket_reconciliation_job():
    """Phase G - periodic shadow-mode bracket reconciliation sweep."""

    def _work() -> None:
        from ..config import settings as _cfg
        mode = (getattr(_cfg, "brain_live_brackets_mode", "off") or "off").lower()
        if mode in ("off", "authoritative"):
            if mode == "authoritative":
                logger.warning(
                    "[scheduler] bracket_reconciliation skipped: mode=authoritative "
                    "(Phase G is shadow-only; Phase G.2 required to enable)",
                )
            return
        from ..db import SessionLocal
        from .trading.bracket_reconciliation_service import (
            broker_manager_view_fn,
            run_reconciliation_sweep,
        )

        db = SessionLocal()
        try:
            summary = run_reconciliation_sweep(db, broker_view_fn=broker_manager_view_fn)
            logger.info(
                "[scheduler] bracket_reconciliation sweep done: "
                "trades=%d brackets=%d agree=%d drift=%d took_ms=%.1f",
                summary.trades_scanned,
                summary.brackets_checked,
                summary.agree,
                (
                    summary.orphan_stop + summary.missing_stop + summary.qty_drift
                    + summary.state_drift + summary.price_drift + summary.broker_down
                    + summary.unreconciled
                ),
                summary.took_ms,
            )
        except Exception:
            logger.exception("[scheduler] bracket_reconciliation sweep failed")
        finally:
            db.close()

    run_scheduler_job_guarded("bracket_reconciliation", _work)


def _run_capital_reweight_weekly_job():
    """Phase I - weekly capital re-weight sweep (shadow mode only).

    Gated by ``brain_capital_reweight_mode != off`` and hard-refused
    when the mode is ``authoritative`` (Phase I.2 will open that path).
    """

    def _work() -> None:
        from ..config import settings
        mode = (getattr(settings, "brain_capital_reweight_mode", "off") or "off").lower()
        if mode in ("off", "authoritative"):
            if mode == "authoritative":
                logger.warning(
                    "[scheduler] capital_reweight_weekly skipped: mode=authoritative "
                    "(Phase I is shadow-only; Phase I.2 required to enable)",
                )
            return
        from datetime import date as _date
        from ..db import SessionLocal
        from .trading.capital_reweight_model import BucketContext
        from .trading.capital_reweight_service import run_sweep
        from .trading.risk_dial_service import get_latest_dial

        db = SessionLocal()
        try:
            # Shadow sweep: one global row per week using active-user
            # snapshot. Per-user fan-out is Phase I.2. We still use the
            # risk-dial read path so the sweep is tilted by the current
            # dial when it is active.
            from sqlalchemy import text as _text

            rows = db.execute(_text("""
                SELECT
                    CASE
                        WHEN UPPER(ticker) LIKE '%-USD'
                          OR UPPER(ticker) LIKE '%USD'
                          OR UPPER(ticker) LIKE '%USDT'
                          OR UPPER(ticker) LIKE '%USDC'
                        THEN 'crypto:majors'
                        ELSE 'equity:default'
                    END AS bucket,
                    COALESCE(SUM(entry_price * quantity), 0) AS notional
                FROM trading_paper_trades
                WHERE status = 'open'
                GROUP BY 1
            """)).fetchall()
            buckets = tuple(
                BucketContext(
                    name=str(r[0]),
                    current_notional=float(r[1] or 0.0),
                    volatility=1.0 if str(r[0]).startswith("equity") else 2.0,
                )
                for r in rows
            )
            if not buckets:
                logger.info("[scheduler] capital_reweight_weekly: no open buckets")
                return
            total_capital = float(
                getattr(settings, "brain_capital_reweight_total_capital_default", 100_000.0)
            )
            dial = get_latest_dial(db, user_id=None, default=1.0)
            res = run_sweep(
                db,
                user_id=None,
                as_of_date=_date.today(),
                total_capital=total_capital,
                regime=None,
                dial_value=float(dial),
                buckets=buckets,
            )
            if res is None:
                logger.info("[scheduler] capital_reweight_weekly: mode=off, skipped")
                return
            logger.info(
                "[scheduler] capital_reweight_weekly sweep done: "
                "reweight_id=%s mode=%s mean_drift_bps=%.1f p90_drift_bps=%.1f",
                res.reweight_id,
                res.mode,
                res.mean_drift_bps,
                res.p90_drift_bps,
            )
        except Exception:
            logger.exception("[scheduler] capital_reweight_weekly sweep failed")
        finally:
            db.close()

    run_scheduler_job_guarded("capital_reweight_weekly", _work)


def _run_drift_monitor_daily_job():
    """Phase J - daily drift-monitor sweep (shadow mode only).

    Iterates active ``scan_patterns`` (lifecycle in ``promoted`` /
    ``live``) and, for each, reads the baseline win-probability from
    ``oos_win_rate`` (fallback ``win_rate``) and the recent closed
    paper-trade sample (bucketed by ``pnl > 0`` → 1 else 0). Writes
    one row per pattern to ``trading_pattern_drift_log`` and, when
    the row is ``red`` severity and the re-cert queue is active,
    one row to ``trading_pattern_recert_log``.

    Gated by ``brain_drift_monitor_mode != off`` and hard-refused
    when the mode is ``authoritative`` (Phase J.2 will open that path).
    """

    def _work() -> None:
        from ..config import settings
        mode = (getattr(settings, "brain_drift_monitor_mode", "off") or "off").lower()
        if mode in ("off", "authoritative"):
            if mode == "authoritative":
                logger.warning(
                    "[scheduler] drift_monitor_daily skipped: mode=authoritative "
                    "(Phase J is shadow-only; Phase J.2 required to enable)",
                )
            return
        from datetime import date as _date
        from ..db import SessionLocal
        from .trading.drift_monitor_service import (
            DriftInputBundle,
            run_sweep as _drift_run_sweep,
        )
        from .trading.recert_queue_service import (
            mode_is_active as _recert_mode_is_active,
            queue_from_drift,
        )
        from .trading.drift_monitor_model import (
            DriftMonitorInput,
            compute_drift,
        )

        db = SessionLocal()
        try:
            from sqlalchemy import text as _text
            lookback_days = int(
                getattr(settings, "brain_drift_monitor_sample_lookback_days", 30)
            )
            patterns = db.execute(_text("""
                SELECT id, name,
                       COALESCE(oos_win_rate, win_rate) AS baseline
                FROM scan_patterns
                WHERE active = TRUE
                  AND lifecycle_stage IN ('promoted', 'live')
                  AND COALESCE(oos_win_rate, win_rate) IS NOT NULL
            """)).fetchall()
            if not patterns:
                logger.info("[scheduler] drift_monitor_daily: no eligible patterns")
                return

            bundles: list[DriftInputBundle] = []
            for pid, pname, baseline in patterns:
                sample_rows = db.execute(_text("""
                    SELECT CASE WHEN COALESCE(pnl, 0) > 0 THEN 1 ELSE 0 END
                    FROM trading_paper_trades
                    WHERE scan_pattern_id = :pid
                      AND status = 'closed'
                      AND exit_date IS NOT NULL
                      AND exit_date >= NOW() - (:ld || ' days')::INTERVAL
                    ORDER BY exit_date ASC
                """), {"pid": int(pid), "ld": int(lookback_days)}).fetchall()
                outcomes = [int(r[0] or 0) for r in sample_rows]
                bundles.append(DriftInputBundle(
                    scan_pattern_id=int(pid),
                    pattern_name=pname,
                    baseline_win_prob=float(baseline) if baseline is not None else None,
                    outcomes=outcomes,
                ))

            today = _date.today()
            rows = _drift_run_sweep(
                db, bundles=bundles, as_of_date=today,
            )
            red_count = sum(1 for r in rows if r.severity == "red")
            logger.info(
                "[scheduler] drift_monitor_daily sweep done: "
                "patterns=%d rows_written=%d red=%d mode=%s",
                len(bundles), len(rows), red_count, mode,
            )

            # Fan out red rows to the re-cert queue when it is active.
            if _recert_mode_is_active():
                for r in rows:
                    if r.severity != "red":
                        continue
                    # Re-derive the pure output for the proposal path so
                    # we can pass it to queue_from_drift without re-reading
                    # the row.
                    bundle = next(
                        (b for b in bundles if b.scan_pattern_id == r.scan_pattern_id),
                        None,
                    )
                    if bundle is None:
                        continue
                    drift_out = compute_drift(DriftMonitorInput(
                        scan_pattern_id=bundle.scan_pattern_id,
                        pattern_name=bundle.pattern_name,
                        baseline_win_prob=bundle.baseline_win_prob,
                        outcomes=tuple(bundle.outcomes),
                        as_of_key=today.isoformat(),
                    ))
                    try:
                        queue_from_drift(
                            db, drift_out,
                            as_of_date=today,
                            drift_log_id=r.log_id,
                        )
                    except Exception:
                        logger.exception(
                            "[scheduler] drift_monitor_daily queue_from_drift failed",
                        )
        except Exception:
            logger.exception("[scheduler] drift_monitor_daily sweep failed")
        finally:
            db.close()

    run_scheduler_job_guarded("drift_monitor_daily", _work)


def _run_divergence_sweep_daily_job():
    """Phase K - daily divergence-panel sweep (shadow mode only).

    Discovers patterns with at least one signal in the last
    ``brain_divergence_scorer_lookback_days`` across the five substrate
    log tables (Phase A ledger parity, Phase B exit parity, Phase F
    venue truth, Phase G bracket reconciliation, Phase H position
    sizer), gathers per-layer signals for each, and writes one row to
    ``trading_pattern_divergence_log`` per pattern.

    Gated by ``brain_divergence_scorer_mode != off`` and hard-refused
    when the mode is ``authoritative`` (Phase K.2 opens that path).
    """

    def _work() -> None:
        from ..config import settings
        mode = (
            getattr(settings, "brain_divergence_scorer_mode", "off") or "off"
        ).lower()
        if mode in ("off", "authoritative"):
            if mode == "authoritative":
                logger.warning(
                    "[scheduler] divergence_sweep_daily skipped: "
                    "mode=authoritative (Phase K is shadow-only; "
                    "Phase K.2 required to enable)",
                )
            return
        from datetime import date as _date
        from ..db import SessionLocal
        from .trading.divergence_service import (
            DivergenceInputBundle,
            discover_active_patterns,
            gather_signals_for_pattern,
            run_sweep as _div_run_sweep,
        )

        db = SessionLocal()
        try:
            lookback_days = int(
                getattr(
                    settings, "brain_divergence_scorer_lookback_days", 7,
                )
            )
            patterns = discover_active_patterns(
                db, lookback_days=lookback_days,
            )
            if not patterns:
                logger.info(
                    "[scheduler] divergence_sweep_daily: no eligible patterns",
                )
                return

            bundles: list[DivergenceInputBundle] = []
            for pid, pname in patterns:
                signals = gather_signals_for_pattern(
                    db,
                    scan_pattern_id=int(pid),
                    lookback_days=lookback_days,
                )
                bundles.append(DivergenceInputBundle(
                    scan_pattern_id=int(pid),
                    pattern_name=pname,
                    signals=signals,
                ))

            today = _date.today()
            rows = _div_run_sweep(db, bundles=bundles, as_of_date=today)
            red_count = sum(1 for r in rows if r.severity == "red")
            yellow_count = sum(1 for r in rows if r.severity == "yellow")
            logger.info(
                "[scheduler] divergence_sweep_daily sweep done: "
                "patterns=%d rows_written=%d red=%d yellow=%d mode=%s",
                len(bundles), len(rows), red_count, yellow_count, mode,
            )
        except Exception:
            logger.exception(
                "[scheduler] divergence_sweep_daily sweep failed",
            )
        finally:
            db.close()

    run_scheduler_job_guarded("divergence_sweep_daily", _work)


def _run_weekly_regime_retrain_job():
    """Fit / decode 3-state HMM regimes (Q1.T2); gated by ``chili_regime_classifier_enabled``."""
    from ..config import settings as _settings

    if not getattr(_settings, "chili_regime_classifier_enabled", False):
        return

    from ..db import SessionLocal
    from .trading.regime_classifier import run_weekly_regime_retrain

    def _work() -> None:
        db = SessionLocal()
        try:
            out = run_weekly_regime_retrain(db)
            logger.info("[scheduler] regime_classifier_weekly: %s", out)
        finally:
            db.close()

    run_scheduler_job_guarded("regime_classifier_weekly", _work)


def _run_macro_regime_daily_job():
    """Phase L.17 - daily macro-regime snapshot sweep (shadow mode only).

    Fetches OHLCV trends for the rates/credit/USD ETF basket
    (IEF/SHY/TLT/HYG/LQD/UUP), combines with the existing equity regime
    composite, and writes one row to ``trading_macro_regime_snapshots``.

    Gated by ``brain_macro_regime_mode != off`` and hard-refused when
    the mode is ``authoritative`` (Phase L.17.2 opens that path).
    """

    def _work() -> None:
        from ..config import settings
        mode = (
            getattr(settings, "brain_macro_regime_mode", "off") or "off"
        ).lower()
        if mode in ("off", "authoritative"):
            if mode == "authoritative":
                logger.warning(
                    "[scheduler] macro_regime_daily skipped: "
                    "mode=authoritative (Phase L.17 is shadow-only; "
                    "Phase L.17.2 required to enable)",
                )
            return
        from ..db import SessionLocal
        from .trading.macro_regime_service import compute_and_persist

        db = SessionLocal()
        try:
            row = compute_and_persist(db)
            if row is None:
                logger.info(
                    "[scheduler] macro_regime_daily: skipped "
                    "(off / coverage_below_min)",
                )
            else:
                logger.info(
                    "[scheduler] macro_regime_daily done: "
                    "regime_id=%s label=%s coverage=%.2f mode=%s",
                    row.regime_id, row.macro_label,
                    float(row.coverage_score), mode,
                )
        except Exception:
            logger.exception(
                "[scheduler] macro_regime_daily sweep failed",
            )
        finally:
            db.close()

    run_scheduler_job_guarded("macro_regime_daily", _work)


def _run_fred_yield_curve_daily_job():
    """A4 — daily FRED DGS10/DGS2 ingestion.

    Fetches DGS10 and DGS2 from FRED's public CSV endpoint, computes the
    real yield curve slope, and updates today's macro_regime_snapshot row
    with `dgs10_real`/`dgs2_real`. The regime classifier feature pipeline
    prefers this over `yield_curve_slope_proxy` when both are present.

    Best-effort: silently no-ops on network/parse failure (proxy continues
    being used). One row per series per day in `macro_fred_fetch_log`.
    """

    def _work() -> None:
        from ..db import SessionLocal
        from .trading.fred_yield_curve import run_weekly_fred_yield_ingestion

        db = SessionLocal()
        try:
            res = run_weekly_fred_yield_ingestion(db)
            logger.info("[scheduler] fred_yield_curve_daily: %s", res)
        except Exception:
            logger.exception("[scheduler] fred_yield_curve_daily failed")
        finally:
            db.close()

    run_scheduler_job_guarded("fred_yield_curve_daily", _work)


def _run_breadth_relstr_daily_job():
    """Phase L.18 - daily breadth + RS snapshot sweep (shadow mode only).

    Fetches OHLCV trends for the fixed reference basket (11 sector SPDRs
    plus SPY / QQQ / IWM), computes the ETF-basket advance/decline proxy
    + per-sector relative strength vs SPY + size/style tilts, and writes
    one row to ``trading_breadth_relstr_snapshots``.

    Gated by ``brain_breadth_relstr_mode != off`` and hard-refused when
    the mode is ``authoritative`` (Phase L.18.2 opens that path).
    """

    def _work() -> None:
        from ..config import settings
        mode = (
            getattr(settings, "brain_breadth_relstr_mode", "off") or "off"
        ).lower()
        if mode in ("off", "authoritative"):
            if mode == "authoritative":
                logger.warning(
                    "[scheduler] breadth_relstr_daily skipped: "
                    "mode=authoritative (Phase L.18 is shadow-only; "
                    "Phase L.18.2 required to enable)",
                )
            return
        from ..db import SessionLocal
        from .trading.breadth_relstr_service import compute_and_persist

        db = SessionLocal()
        try:
            row = compute_and_persist(db)
            if row is None:
                logger.info(
                    "[scheduler] breadth_relstr_daily: skipped "
                    "(off / coverage_below_min)",
                )
            else:
                logger.info(
                    "[scheduler] breadth_relstr_daily done: "
                    "snapshot_id=%s label=%s advance_ratio=%.2f "
                    "coverage=%.2f mode=%s",
                    row.snapshot_id, row.breadth_label,
                    float(row.advance_ratio),
                    float(row.coverage_score), mode,
                )
        except Exception:
            logger.exception(
                "[scheduler] breadth_relstr_daily sweep failed",
            )
        finally:
            db.close()

    run_scheduler_job_guarded("breadth_relstr_daily", _work)


def _run_cross_asset_daily_job():
    """Phase L.19 - daily cross-asset signals sweep (shadow mode only).

    Fetches OHLCV for the fixed lead/lag basket (SPY, TLT, HYG, LQD,
    UUP, BTC-USD, ETH-USD), reads Phase L.17 macro_label and Phase L.18
    advance_ratio/breadth_label for context, pulls VIX from
    ``get_market_regime()``, computes the bond-equity / credit-equity /
    USD-crypto leads + VIX-breadth divergence + BTC-SPY beta, and
    writes one row to ``trading_cross_asset_snapshots``.

    Gated by ``brain_cross_asset_mode != off`` and hard-refused when
    the mode is ``authoritative`` (Phase L.19.2 opens that path).
    """

    def _work() -> None:
        from ..config import settings
        mode = (
            getattr(settings, "brain_cross_asset_mode", "off") or "off"
        ).lower()
        if mode in ("off", "authoritative"):
            if mode == "authoritative":
                logger.warning(
                    "[scheduler] cross_asset_daily skipped: "
                    "mode=authoritative (Phase L.19 is shadow-only; "
                    "Phase L.19.2 required to enable)",
                )
            return
        from ..db import SessionLocal
        from .trading.cross_asset_service import compute_and_persist

        db = SessionLocal()
        try:
            row = compute_and_persist(db)
            if row is None:
                logger.info(
                    "[scheduler] cross_asset_daily: skipped "
                    "(off / coverage_below_min)",
                )
            else:
                logger.info(
                    "[scheduler] cross_asset_daily done: "
                    "snapshot_id=%s label=%s coverage=%.2f mode=%s",
                    row.snapshot_id, row.cross_asset_label,
                    float(row.coverage_score), mode,
                )
        except Exception:
            logger.exception(
                "[scheduler] cross_asset_daily sweep failed",
            )
        finally:
            db.close()

    run_scheduler_job_guarded("cross_asset_daily", _work)


def _run_ticker_regime_daily_job():
    """Phase L.20 - daily per-ticker mean-reversion vs trend sweep (shadow).

    Iterates over the snapshot-universe (scan + watchlist, bounded by
    ``brain_ticker_regime_max_tickers``), fetches daily OHLCV per
    ticker, and writes one row per eligible ticker to
    ``trading_ticker_regime_snapshots``. Gated by
    ``brain_ticker_regime_mode != off`` and hard-refused when the mode
    is ``authoritative`` (Phase L.20.2 opens that path).
    """

    def _work() -> None:
        from ..config import settings
        mode = (
            getattr(settings, "brain_ticker_regime_mode", "off") or "off"
        ).lower()
        if mode in ("off", "authoritative"):
            if mode == "authoritative":
                logger.warning(
                    "[scheduler] ticker_regime_daily skipped: "
                    "mode=authoritative (Phase L.20 is shadow-only; "
                    "Phase L.20.2 required to enable)",
                )
            return
        from ..db import SessionLocal
        from .trading.ticker_regime_service import compute_and_persist_sweep

        db = SessionLocal()
        try:
            result = compute_and_persist_sweep(db)
            logger.info(
                "[scheduler] ticker_regime_daily done: "
                "attempted=%d persisted=%d skipped=%d mode=%s",
                int(result.tickers_attempted),
                int(result.tickers_persisted),
                int(result.tickers_skipped),
                mode,
            )
        except Exception:
            logger.exception(
                "[scheduler] ticker_regime_daily sweep failed",
            )
        finally:
            db.close()

    run_scheduler_job_guarded("ticker_regime_daily", _work)


def _run_vol_dispersion_daily_job():
    """Phase L.21 - daily volatility term structure + cross-sectional
    dispersion snapshot (shadow).

    Fetches VIXY/VIXM/VXZ/SPY + 11 sector SPDRs + a capped slice of
    the snapshot universe, computes the pure model, and writes one row
    to ``trading_vol_dispersion_snapshots``. Gated by
    ``brain_vol_dispersion_mode != off`` and hard-refused when the
    mode is ``authoritative`` (Phase L.21.2 opens that path).
    """

    def _work() -> None:
        from ..config import settings
        mode = (
            getattr(settings, "brain_vol_dispersion_mode", "off") or "off"
        ).lower()
        if mode in ("off", "authoritative"):
            if mode == "authoritative":
                logger.warning(
                    "[scheduler] vol_dispersion_daily skipped: "
                    "mode=authoritative (Phase L.21 is shadow-only; "
                    "Phase L.21.2 required to enable)",
                )
            return
        from ..db import SessionLocal
        from .trading.vol_dispersion_service import compute_and_persist

        db = SessionLocal()
        try:
            row = compute_and_persist(db)
            if row is None:
                logger.info(
                    "[scheduler] vol_dispersion_daily done: "
                    "no_row_persisted mode=%s", mode,
                )
            else:
                logger.info(
                    "[scheduler] vol_dispersion_daily done: "
                    "snapshot_id=%s vol=%s disp=%s corr=%s cov=%.4f mode=%s",
                    row.snapshot_id,
                    row.vol_regime_label,
                    row.dispersion_label,
                    row.correlation_label,
                    float(row.coverage_score),
                    mode,
                )
        except Exception:
            logger.exception(
                "[scheduler] vol_dispersion_daily sweep failed",
            )
        finally:
            db.close()

    run_scheduler_job_guarded("vol_dispersion_daily", _work)


def _run_intraday_session_daily_job():
    """Phase L.22 - daily intraday session regime snapshot (shadow).

    Fetches SPY 5-minute bars for the current trading day (in
    US/Eastern), computes the pure model (opening range, midday
    compression, power hour, gap dynamics, composite label), and
    writes one row to ``trading_intraday_session_snapshots``. Gated
    by ``brain_intraday_session_mode != off`` and hard-refused when
    the mode is ``authoritative`` (Phase L.22.2 opens that path).
    """

    def _work() -> None:
        from ..config import settings
        mode = (
            getattr(settings, "brain_intraday_session_mode", "off")
            or "off"
        ).lower()
        if mode in ("off", "authoritative"):
            if mode == "authoritative":
                logger.warning(
                    "[scheduler] intraday_session_daily skipped: "
                    "mode=authoritative (Phase L.22 is shadow-only; "
                    "Phase L.22.2 required to enable)",
                )
            return
        from ..db import SessionLocal
        from .trading.intraday_session_service import compute_and_persist

        db = SessionLocal()
        try:
            row = compute_and_persist(db)
            if row is None:
                logger.info(
                    "[scheduler] intraday_session_daily done: "
                    "no_row_persisted mode=%s", mode,
                )
            else:
                logger.info(
                    "[scheduler] intraday_session_daily done: "
                    "snapshot_id=%s label=%s numeric=%d cov=%.4f mode=%s",
                    row.snapshot_id,
                    row.session_label,
                    int(row.session_numeric),
                    float(row.coverage_score),
                    mode,
                )
        except Exception:
            logger.exception(
                "[scheduler] intraday_session_daily sweep failed",
            )
        finally:
            db.close()

    run_scheduler_job_guarded("intraday_session_daily", _work)


def _run_pattern_regime_perf_daily_job():
    """Phase M.1 - daily pattern x regime performance ledger (shadow).

    First consumer of the L.17-L.22 regime snapshot stack. Joins
    closed paper trades in the rolling window against the latest
    regime label per dimension at each trade's entry_date, then
    writes one aggregate row per (pattern_id, regime_dimension,
    regime_label) to ``trading_pattern_regime_performance_daily``.
    Gated by ``brain_pattern_regime_perf_mode != off`` and hard-
    refused when the mode is ``authoritative`` (Phase M.2 opens
    that path).
    """

    def _work() -> None:
        from ..config import settings
        mode = (
            getattr(settings, "brain_pattern_regime_perf_mode", "off")
            or "off"
        ).lower()
        if mode in ("off", "authoritative"):
            if mode == "authoritative":
                logger.warning(
                    "[scheduler] pattern_regime_perf_daily skipped: "
                    "mode=authoritative (Phase M.1 is shadow-only; "
                    "Phase M.2 required to enable)",
                )
            return
        from ..db import SessionLocal
        from .trading.pattern_regime_performance_service import (
            compute_and_persist,
        )

        db = SessionLocal()
        try:
            run_ref = compute_and_persist(db)
            if run_ref is None:
                logger.info(
                    "[scheduler] pattern_regime_perf_daily done: "
                    "no_cells_persisted mode=%s", mode,
                )
            else:
                logger.info(
                    "[scheduler] pattern_regime_perf_daily done: "
                    "ledger_run_id=%s cells=%d window_days=%d mode=%s",
                    run_ref.ledger_run_id,
                    run_ref.cells_persisted,
                    run_ref.window_days,
                    run_ref.mode,
                )
        except Exception:
            logger.exception(
                "[scheduler] pattern_regime_perf_daily sweep failed",
            )
        finally:
            db.close()

    run_scheduler_job_guarded("pattern_regime_perf_daily", _work)


def _run_pattern_regime_killswitch_daily_job():
    """Phase M.2.c — daily pattern x regime kill-switch / auto-quarantine sweep.

    Iterates promoted / live patterns and evaluates them against the
    M.1 ledger. Shadow / compare / authoritative gated by
    ``brain_pattern_regime_killswitch_mode``. Authoritative mode
    requires a live approval row in ``trading_governance_approvals``
    (``action_type='pattern_regime_killswitch'``); without one, the
    service emits ``killswitch_refused_authoritative`` ops lines.
    """

    def _work() -> None:
        from ..config import settings
        mode = (
            getattr(settings, "brain_pattern_regime_killswitch_mode", "off")
            or "off"
        ).lower()
        if mode == "off":
            return
        if bool(getattr(settings, "brain_pattern_regime_killswitch_kill", False)):
            logger.warning(
                "[scheduler] pattern_regime_killswitch_daily skipped: kill flag set",
            )
            return
        from ..db import SessionLocal
        from .trading.pattern_regime_killswitch_service import run_daily_sweep

        db = SessionLocal()
        try:
            out = run_daily_sweep(db)
            logger.info(
                "[scheduler] pattern_regime_killswitch_daily done: "
                "mode=%s evaluated=%s quarantined=%s refused=%s",
                out.get("mode"),
                out.get("patterns_evaluated"),
                out.get("patterns_quarantined"),
                out.get("refused_authoritative"),
            )
        except Exception:
            logger.exception(
                "[scheduler] pattern_regime_killswitch_daily sweep failed",
            )
        finally:
            db.close()

    run_scheduler_job_guarded("pattern_regime_killswitch_daily", _work)


def _run_pattern_regime_autopilot_tick_job():
    """Phase M.2-autopilot — daily advance/hold/revert tick.

    Gated by ``brain_pattern_regime_autopilot_enabled``. Writes runtime
    mode overrides into ``trading_brain_runtime_modes`` and audit rows
    into ``trading_pattern_regime_autopilot_log``. Never raises.
    """

    def _work() -> None:
        from ..config import settings as _s

        if not bool(getattr(_s, "brain_pattern_regime_autopilot_enabled", False)):
            return
        if bool(getattr(_s, "brain_pattern_regime_autopilot_kill", False)):
            logger.warning(
                "[scheduler] pattern_regime_autopilot_tick skipped: kill flag set",
            )
            return
        from ..db import SessionLocal
        from .trading.pattern_regime_autopilot_service import run_autopilot_tick

        db = SessionLocal()
        try:
            out = run_autopilot_tick(db)
            logger.info(
                "[scheduler] pattern_regime_autopilot_tick done: enabled=%s slices=%s",
                out.get("enabled"),
                list((out.get("slices") or {}).keys()),
            )
        except Exception:
            logger.exception(
                "[scheduler] pattern_regime_autopilot_tick failed",
            )
        finally:
            db.close()

    run_scheduler_job_guarded("pattern_regime_autopilot_tick", _work)


def _run_pattern_regime_autopilot_weekly_job():
    """Phase M.2-autopilot — Monday 09:00 weekly summary."""

    def _work() -> None:
        from ..config import settings as _s

        if not bool(getattr(_s, "brain_pattern_regime_autopilot_enabled", False)):
            return
        from ..db import SessionLocal
        from .trading.pattern_regime_autopilot_service import run_weekly_summary

        db = SessionLocal()
        try:
            run_weekly_summary(db)
        except Exception:
            logger.exception(
                "[scheduler] pattern_regime_autopilot_weekly failed",
            )
        finally:
            db.close()

    run_scheduler_job_guarded("pattern_regime_autopilot_weekly", _work)


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
    """Sync Robinhood + Coinbase orders and positions for the session owner.

    Previously this job iterated over ``distinct(Trade.user_id)`` of open
    trades, which self-perpetuates duplicate rows when the scheduler writes
    position copies under every user that ever had an open trade. The RH
    session is tied to a single account (``broker_sessions.username``), so
    all broker-sourced rows must be attributed to that one user.
    """
    from . import broker_service, coinbase_service

    def _resolve_rh_user_id(db) -> int | None:
        """Return the user_id that owns the live RH session, or None."""
        from sqlalchemy import text
        try:
            row = db.execute(
                text(
                    "SELECT u.id FROM broker_sessions bs "
                    "JOIN users u ON lower(u.email) = lower(bs.username) "
                    "WHERE bs.broker = 'robinhood' "
                    "ORDER BY bs.updated_at DESC LIMIT 1"
                )
            ).fetchone()
            return int(row[0]) if row else None
        except Exception:
            logger.debug("[scheduler] broker_sync: RH user lookup failed", exc_info=True)
            return None

    def _resolve_cb_user_id(db) -> int | None:
        """Return the user_id that has live Coinbase credentials, or None."""
        from sqlalchemy import text
        try:
            row = db.execute(
                text(
                    "SELECT user_id FROM broker_credentials "
                    "WHERE broker = 'coinbase' "
                    "ORDER BY updated_at DESC LIMIT 1"
                )
            ).fetchone()
            return int(row[0]) if row else None
        except Exception:
            logger.debug("[scheduler] broker_sync: CB user lookup failed", exc_info=True)
            return None

    def _work() -> None:
        from ..db import SessionLocal

        db = SessionLocal()
        try:
            if broker_service.is_connected():
                rh_uid = _resolve_rh_user_id(db)
                if rh_uid is None:
                    logger.warning(
                        "[scheduler] RH sync skipped: no broker_sessions row maps to a known user"
                    )
                else:
                    logger.info("[scheduler] RH sync for user_id=%s (session owner)", rh_uid)
                    order_result = broker_service.sync_orders_to_db(db, user_id=rh_uid)
                    logger.info("[scheduler] RH order sync (user=%s): %s", rh_uid, order_result)
                    pos_result = broker_service.sync_positions_to_db(db, user_id=rh_uid)
                    logger.info("[scheduler] RH position sync (user=%s): %s", rh_uid, pos_result)

            if coinbase_service.is_connected():
                cb_uid = _resolve_cb_user_id(db)
                if cb_uid is None:
                    logger.warning(
                        "[scheduler] CB sync skipped: no broker_credentials row for coinbase"
                    )
                else:
                    logger.info("[scheduler] CB sync for user_id=%s (credential owner)", cb_uid)
                    cb_order = coinbase_service.sync_orders_to_db(db, user_id=cb_uid)
                    logger.info("[scheduler] CB order sync (user=%s): %s", cb_uid, cb_order)
                    cb_pos = coinbase_service.sync_positions_to_db(db, user_id=cb_uid)
                    logger.info("[scheduler] CB position sync (user=%s): %s", cb_uid, cb_pos)
        finally:
            db.close()

    run_scheduler_job_guarded("broker_sync", _work)


def _run_price_monitor_job():
    """Check positions/breakouts/picks and dispatch alerts for all users with open trades.

    Also triggers event-driven pattern monitor for tickers where alerts fired.
    """
    from ..db import SessionLocal
    from .trading.alerts import run_price_monitor

    def _work() -> None:
        logger.info("[scheduler] Starting price monitor check")
        db = SessionLocal()
        alerted_tickers: list[str] = []
        try:
            from ..models.trading import Trade
            from sqlalchemy import distinct
            user_ids = [
                r[0] for r in db.query(distinct(Trade.user_id))
                .filter(Trade.status == "open", Trade.user_id.isnot(None))
                .all()
            ]
            if not user_ids:
                user_ids = [None]
            for uid in user_ids:
                try:
                    result = run_price_monitor(db, user_id=uid)
                    logger.info(f"[scheduler] Price monitor user_id={uid}: {result}")
                    if isinstance(result, dict):
                        alerted_tickers.extend(result.get("alerted_tickers", []))
                except Exception:
                    logger.warning(f"[scheduler] Price monitor failed for user_id={uid}", exc_info=True)

            # Trigger event-driven pattern monitor for all open pattern-linked tickers
            pattern_tickers = [
                r[0] for r in db.query(distinct(Trade.ticker))
                .filter(
                    Trade.status == "open",
                    Trade.related_alert_id.isnot(None),
                )
                .all()
            ]
            if pattern_tickers:
                trigger_pattern_monitor_for_tickers(pattern_tickers, reason="price_monitor")
        finally:
            db.close()

    run_scheduler_job_guarded("price_monitor", _work)


def _run_daytrade_fast_monitor_job():
    """1-minute fast check for day-trade and scalp positions (tighter exit timing)."""
    from ..db import SessionLocal
    from .trading.stop_engine import evaluate_all, dispatch_stop_alerts
    from ..models.trading import Trade
    from sqlalchemy import distinct

    def _work() -> None:
        db = SessionLocal()
        try:
            daytrade_types = ("scalp", "daytrade", "breakout", "momentum")
            user_ids = [
                r[0] for r in db.query(distinct(Trade.user_id))
                .filter(
                    Trade.status == "open",
                    Trade.user_id.isnot(None),
                    Trade.trade_type.in_(daytrade_types),
                )
                .all()
            ]
            if not user_ids:
                return
            for uid in user_ids:
                try:
                    results = evaluate_all(db, uid, staleness_secs=120)
                    dispatch_stop_alerts(db, uid, results)
                    db.commit()
                except Exception:
                    db.rollback()
                    logger.debug("[scheduler] daytrade fast monitor failed for uid=%s", uid, exc_info=True)
        finally:
            db.close()

    run_scheduler_job_guarded("daytrade_fast_monitor", _work)


def _run_crypto_stop_monitor_job():
    """24/7 stop-engine check for crypto positions only (every 2 minutes)."""
    from ..db import SessionLocal
    from .trading.stop_engine import evaluate_all, dispatch_stop_alerts
    from ..models.trading import Trade
    from sqlalchemy import distinct

    def _work() -> None:
        db = SessionLocal()
        try:
            user_ids = [
                r[0] for r in db.query(distinct(Trade.user_id))
                .filter(
                    Trade.status == "open",
                    Trade.user_id.isnot(None),
                    Trade.ticker.like("%-USD"),
                )
                .all()
            ]
            if not user_ids:
                return
            for uid in user_ids:
                try:
                    crypto_trades = db.query(Trade).filter(
                        Trade.status == "open",
                        Trade.user_id == uid,
                        Trade.ticker.like("%-USD"),
                    ).all()
                    if not crypto_trades:
                        continue
                    summary = evaluate_all(db, uid)
                    dispatched = dispatch_stop_alerts(db, uid, summary)
                    if dispatched:
                        logger.info("[scheduler] Crypto stop monitor uid=%s: %d alerts dispatched", uid, dispatched)
                except Exception:
                    logger.warning("[scheduler] Crypto stop monitor failed for uid=%s", uid, exc_info=True)
        finally:
            db.close()

    run_scheduler_job_guarded("crypto_stop_monitor", _work)


def _run_pattern_position_monitor_job():
    """Heartbeat: evaluate pattern-linked positions (event-driven mode).

    Runs less frequently (30-min heartbeat) as the primary evaluation path
    is now event-driven via the price monitor and broker sync callbacks.
    """
    from ..db import SessionLocal
    from ..models.trading import Trade
    from sqlalchemy import and_, distinct, or_

    def _work() -> None:
        db = SessionLocal()
        try:
            from .trading.pattern_position_monitor import run_pattern_position_monitor
            user_ids = [
                r[0] for r in db.query(distinct(Trade.user_id))
                .filter(
                    Trade.status == "open",
                    Trade.user_id.isnot(None),
                    or_(
                        Trade.related_alert_id.isnot(None),
                        and_(
                            Trade.related_alert_id.is_(None),
                            or_(Trade.stop_loss.isnot(None), Trade.take_profit.isnot(None)),
                        ),
                    ),
                )
                .all()
            ]
            for uid in user_ids:
                try:
                    run_pattern_position_monitor(db, uid, event_driven=True)
                except Exception:
                    logger.warning("[scheduler] pattern_position_monitor failed uid=%s", uid, exc_info=True)
        finally:
            db.close()

    run_scheduler_job_guarded("pattern_position_monitor", _work)


def trigger_pattern_monitor_for_tickers(tickers: list[str], reason: str = "event") -> None:
    """Event-driven trigger: evaluate pattern-linked positions for specific tickers.

    Called by the price monitor and broker sync when a material change is detected.
    """
    from ..db import SessionLocal
    from ..models.trading import Trade

    db = SessionLocal()
    try:
        from sqlalchemy import and_, or_

        from .trading.pattern_position_monitor import run_pattern_position_monitor_for_trades

        trades = (
            db.query(Trade)
            .filter(
                Trade.status == "open",
                Trade.ticker.in_(tickers),
                or_(
                    Trade.related_alert_id.isnot(None),
                    and_(
                        Trade.related_alert_id.is_(None),
                        or_(Trade.stop_loss.isnot(None), Trade.take_profit.isnot(None)),
                    ),
                ),
            )
            .all()
        )
        if not trades:
            return

        logger.info(
            "[scheduler] Event-driven pattern monitor for %d trades (%s): %s",
            len(trades), reason, [t.ticker for t in trades],
        )
        run_pattern_position_monitor_for_trades(db, trades, event_driven=True)
        db.commit()
    except Exception:
        logger.warning("[scheduler] Event-driven pattern monitor failed", exc_info=True)
    finally:
        db.close()


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


def _run_auto_trader_tick_job():
    from ..config import settings as _settings
    from ..db import SessionLocal
    from .trading.auto_trader import run_auto_trader_tick

    if not getattr(_settings, "chili_autotrader_enabled", False):
        return

    db = SessionLocal()
    try:
        run_auto_trader_tick(db)
    except Exception:
        logger.exception("[scheduler] auto_trader tick failed")
    finally:
        db.close()


def _run_auto_trader_monitor_job():
    from ..config import settings as _settings
    from ..db import SessionLocal
    from .trading.auto_trader_monitor import tick_auto_trader_monitor

    if not getattr(_settings, "chili_autotrader_enabled", False):
        return

    db = SessionLocal()
    try:
        tick_auto_trader_monitor(db)
    except Exception:
        logger.exception("[scheduler] auto_trader monitor failed")
    finally:
        db.close()


def _run_stuck_order_watchdog_job():
    """P0.7 — cancel orders stuck in non-terminal broker states past timeout.

    Standalone from the autotrader gates since it also covers trades from
    broker_sync / manual sources, not just AutoTrader v1.
    """
    from ..config import settings as _settings
    from ..db import SessionLocal
    from .trading.stuck_order_watchdog import tick_stuck_order_watchdog

    if not getattr(_settings, "chili_stuck_order_watchdog_enabled", True):
        return

    db = SessionLocal()
    try:
        tick_stuck_order_watchdog(db)
    except Exception:
        logger.exception("[scheduler] stuck_order_watchdog tick failed")
    finally:
        db.close()


def _run_execution_event_lag_job():
    """P0.6 — execution-event lag gauge (recorded_at - event_at P95)."""
    from ..config import settings as _settings
    from ..db import SessionLocal
    from .trading.execution_event_lag import run_execution_event_lag_tick

    if not getattr(_settings, "chili_execution_event_lag_enabled", True):
        return

    db = SessionLocal()
    try:
        run_execution_event_lag_tick(db)
    except Exception:
        logger.exception("[scheduler] execution_event_lag tick failed")
    finally:
        db.close()


def _run_drift_escalation_watchdog_job():
    """P0.8 — alert when the same intent has been in the same non-agree
    kind for N consecutive sweeps. Opt-in via feature flag."""
    from ..config import settings as _settings
    from ..db import SessionLocal
    from .trading.drift_escalation_watchdog import run_drift_escalation_watchdog

    if not getattr(_settings, "chili_drift_escalation_enabled", False):
        return

    db = SessionLocal()
    try:
        run_drift_escalation_watchdog(db)
    except Exception:
        logger.exception("[scheduler] drift_escalation_watchdog tick failed")
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
            db.flush()
            try:
                from .trading.contracts.signal_emit import emit_signal_for_breakout_alert

                emit_signal_for_breakout_alert(
                    db,
                    row,
                    scanner=f"scheduler_{alert_tier}",
                    strategy_family=str(setup.get("sector") or "breakout_scan"),
                    commit=False,
                )
            except Exception as _use:
                logger.debug(
                    "[unified_signal] scheduler breakout emit skipped: %s",
                    _use,
                    exc_info=True,
                )
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"[scheduler] Failed to record breakout alert: {e}", exc_info=True)


def _bridge_scanner_to_viability(
    db: "Session",
    results: list[dict],
    *,
    source: str = "scanner",
) -> None:
    """Run momentum neural tick directly for scanner-discovered tickers.

    Writes symbol-level viability rows synchronously so the Autopilot board sees them
    on the next poll.  Uses run_momentum_neural_tick (same path as _auto_assess_scan_only
    in opportunities.py) instead of enqueueing activation events — avoids stale pending
    events that trigger the "viability pipeline stale" warning when the brain worker is
    slow or not running.
    """
    tickers: list[str] = []
    for r in results:
        t = str(r.get("ticker") or r.get("symbol") or "").strip().upper()
        if t and t not in tickers:
            tickers.append(t)
        if len(tickers) >= _VIABILITY_BRIDGE_MAX_TICKERS:
            break
    if not tickers:
        return
    try:
        from .trading.momentum_neural.pipeline import run_momentum_neural_tick

        run_momentum_neural_tick(db, meta={"tickers": tickers})
        db.commit()
        logger.info(
            "[scheduler] viability bridge (%s): %d tickers → direct tick ok",
            source, len(tickers),
        )
    except Exception as e:
        logger.warning("[scheduler] viability bridge (%s) failed: %s", source, e)
        try:
            db.rollback()
        except Exception:
            pass


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
            msg = format_crypto_breakout(
                ticker=ticker,
                trade_label=_tc["label"],
                score=setup["score"],
                price=setup["price"],
                change_24h=setup.get("change_24h", 0),
                rvol=setup.get("rvol", 0),
                ema_alignment=setup.get("ema_alignment", "n/a"),
                flag_line=flag_line,
                entry_price=setup.get("entry_price"),
                stop_loss=setup.get("stop_loss"),
                take_profit=setup.get("take_profit"),
                duration=_tc["duration"] or "",
                sig_text=sig_text,
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

        _bridge_scanner_to_viability(db, all_results, source="crypto_breakout")
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
            msg = format_stock_breakout(
                ticker=ticker,
                trade_label=_tc["label"],
                score=setup["score"],
                price=setup["price"],
                dist_to_breakout=setup.get("dist_to_breakout", 0),
                flag_line=flag_line,
                entry_price=setup.get("entry_price"),
                stop_loss=setup.get("stop_loss"),
                take_profit=setup.get("take_profit"),
                duration=_tc["duration"] or "",
                sig_text=sig_text,
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
                msg = format_momentum(
                    ticker=setup["ticker"],
                    trade_label=_tc["label"],
                    score=setup["score"],
                    price=setup["price"],
                    vol_ratio=setup.get("vol_ratio", 0),
                    risk_reward=setup.get("risk_reward", 0),
                    duration=_tc["duration"] or "",
                    signals=", ".join(setup.get("signals", [])[:3]),
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

        _bridge_scanner_to_viability(db, res_list, source="momentum_scanner")
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


def _run_crypto_viability_refresh_job():
    """24/7 crypto viability refresh: pull latest breakout scan results and bridge to viability.

    Complements the crypto breakout scanner by ensuring viability rows stay fresh
    even between breakout scan runs. Uses the cached breakout results (no new scan).
    """
    from ..db import SessionLocal
    from .trading.scanner import get_crypto_breakout_cache

    logger.info("[scheduler] Running crypto viability refresh")
    db = SessionLocal()
    try:
        cache = get_crypto_breakout_cache()
        results = list(cache.get("results") or [])
        if results:
            _bridge_scanner_to_viability(db, results, source="crypto_viability_refresh")
        else:
            logger.debug("[scheduler] crypto viability refresh: no cached breakout results")
    except Exception as e:
        logger.warning("[scheduler] crypto viability refresh failed: %s", e)
    finally:
        db.close()


def _run_intraday_signal_sweep_job():
    """Run intraday signal sweep and optionally route into paper automation."""
    from ..config import settings as _settings
    from ..db import SessionLocal
    from .trading.intraday_signals import run_intraday_signal_sweep

    logger.info("[scheduler] Running intraday signal sweep")
    db = SessionLocal()
    try:
        uid = getattr(_settings, "brain_default_user_id", None)
        auto_paper = bool(
            getattr(_settings, "chili_momentum_paper_runner_enabled", False)
            and getattr(_settings, "chili_momentum_paper_runner_scheduler_enabled", False)
        )
        out = run_intraday_signal_sweep(db, user_id=uid, auto_paper=auto_paper)
        db.commit()
        logger.info("[scheduler] Intraday signal sweep result: %s", out)
    finally:
        db.close()


def _run_monitor_decision_review_job():
    """Hourly: fill price_after_1h/4h and was_beneficial on pattern monitor decisions."""
    from ..db import SessionLocal

    def _work() -> None:
        db = SessionLocal()
        try:
            from .trading.pattern_position_monitor import review_monitor_decisions
            result = review_monitor_decisions(db)
            if result.get("filled_1h") or result.get("filled_4h"):
                logger.info("[scheduler] monitor decision review: %s", result)
        finally:
            db.close()

    run_scheduler_job_guarded("monitor_decision_review", _work)


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


def _run_weekly_cpcv_backfill_job() -> None:
    """Sunday 04:00 America/New_York: CPCV backfill with ``--commit`` (canonical ``DATABASE_URL``).

    Gated by ``chili_cpcv_weekly_backfill_enabled`` (job is only registered when True).
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    def _work() -> None:
        root = Path(__file__).resolve().parents[2]
        script = root / "scripts" / "backfill_cpcv_metrics.py"
        logger.info("[cpcv_weekly_backfill] phase=started script=%s", script)
        proc = subprocess.run(
            [sys.executable, "-u", str(script), "--commit"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=7200,
            env=os.environ.copy(),
        )
        logger.info(
            "[cpcv_weekly_backfill] phase=summary exit_code=%s",
            proc.returncode,
        )
        if proc.stdout:
            tail_lines = proc.stdout.strip().splitlines()[-12:]
            logger.info(
                "[cpcv_weekly_backfill] log_tail=%s",
                " | ".join(tail_lines)[:2400],
            )
        if proc.stderr and proc.returncode != 0:
            logger.warning(
                "[cpcv_weekly_backfill] stderr_tail=%s",
                proc.stderr[-2400:],
            )
        logger.info(
            "[cpcv_weekly_backfill] phase=finished exit_code=%s",
            proc.returncode,
        )

    run_scheduler_job_guarded("weekly_cpcv_backfill", _work)


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

        _scheduler = BackgroundScheduler(daemon=True)
        logger.info(
            "[scheduler] Role=%s (heavy_scan_jobs=%s web_jobs=%s emit_heartbeat=%s)",
            role,
            include_heavy,
            include_web_light,
            emit_worker_heartbeat,
        )

        # Hard Rule 1/2: restore persisted kill-switch state before the first
        # job runs. Without this, a tripped breaker silently disarms on every
        # process restart — opposite of the intended safety guarantee.
        try:
            from .trading.governance import (
                get_kill_switch_status,
                restore_kill_switch_from_db,
            )
            restore_kill_switch_from_db()
            _ks = get_kill_switch_status()
            if _ks.get("active"):
                logger.warning(
                    "[scheduler] Kill switch restored ACTIVE: %s — autotrader blocked until manual reset",
                    _ks.get("reason"),
                )
            else:
                logger.info("[scheduler] Kill switch restored: inactive")
        except Exception:
            logger.warning("[scheduler] Kill-switch restore failed", exc_info=True)

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
                    hour="8-20",
                    minute="*/2",
                    timezone="US/Eastern",
                ),
                id="broker_sync",
                name="Robinhood order+position sync (ET 8am-8pm every 2min)",
                replace_existing=True,
                max_instances=1,
            )

            _scheduler.add_job(
                _run_price_monitor_job,
                trigger=CronTrigger(
                    day_of_week="mon-fri",
                    hour="8-20",
                    minute="*/5",
                    timezone="US/Eastern",
                ),
                id="price_monitor",
                name="Price monitor & alerts (ET 8am-8pm every 5min)",
                replace_existing=True,
                max_instances=1,
            )

            _scheduler.add_job(
                _run_daytrade_fast_monitor_job,
                trigger=CronTrigger(
                    day_of_week="mon-fri",
                    hour="9-16",
                    minute="*/1",
                ),
                id="daytrade_fast_monitor",
                name="Day-trade fast stop/exit monitor (market hours every 1min)",
                replace_existing=True,
                max_instances=1,
            )

            _scheduler.add_job(
                _run_crypto_stop_monitor_job,
                trigger=IntervalTrigger(minutes=2),
                id="crypto_stop_monitor",
                name="Crypto stop-loss monitor (every 2min, 24/7)",
                replace_existing=True,
                max_instances=1,
                next_run_time=datetime.now() + timedelta(seconds=30),
            )

            _scheduler.add_job(
                _run_pattern_position_monitor_job,
                trigger=IntervalTrigger(minutes=30),
                id="pattern_position_monitor",
                name="Pattern position monitor heartbeat (every 30min, event-driven primary)",
                replace_existing=True,
                max_instances=1,
                next_run_time=datetime.now() + timedelta(seconds=45),
            )

            _scheduler.add_job(
                _run_intraday_signal_sweep_job,
                trigger=IntervalTrigger(minutes=15),
                id="intraday_signal_sweep",
                name="Intraday signal sweep (every 15min)",
                replace_existing=True,
                max_instances=1,
                next_run_time=datetime.now() + timedelta(seconds=55),
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
                _run_crypto_viability_refresh_job,
                trigger=IntervalTrigger(minutes=30),
                id="crypto_viability_refresh",
                name="Crypto viability refresh (every 30min, 24/7)",
                replace_existing=True,
                max_instances=1,
                next_run_time=datetime.now() + timedelta(seconds=90),
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

        if include_heavy or include_web_light:
            _at_tick_s = max(5, int(getattr(settings, "chili_autotrader_tick_interval_seconds", 10)))
            _at_mon_s = max(5, int(getattr(settings, "chili_autotrader_monitor_interval_seconds", 30)))
            _scheduler.add_job(
                _run_auto_trader_tick_job,
                trigger=IntervalTrigger(seconds=_at_tick_s),
                id="auto_trader_tick",
                name=f"AutoTrader v1 tick (every {_at_tick_s}s)",
                replace_existing=True,
                max_instances=1,
                next_run_time=datetime.now() + timedelta(seconds=25),
            )
            _scheduler.add_job(
                _run_auto_trader_monitor_job,
                trigger=IntervalTrigger(seconds=_at_mon_s),
                id="auto_trader_monitor",
                name=f"AutoTrader v1 monitor (every {_at_mon_s}s)",
                replace_existing=True,
                max_instances=1,
                next_run_time=datetime.now() + timedelta(seconds=30),
            )

            _stuck_s = max(
                15,
                int(getattr(settings, "chili_stuck_order_watchdog_interval_seconds", 60)),
            )
            _scheduler.add_job(
                _run_stuck_order_watchdog_job,
                trigger=IntervalTrigger(seconds=_stuck_s),
                id="stuck_order_watchdog",
                name=f"Stuck-order watchdog (every {_stuck_s}s)",
                replace_existing=True,
                max_instances=1,
                next_run_time=datetime.now() + timedelta(seconds=45),
            )

            _eel_s = max(
                15,
                int(getattr(settings, "chili_execution_event_lag_interval_seconds", 60)),
            )
            _scheduler.add_job(
                _run_execution_event_lag_job,
                trigger=IntervalTrigger(seconds=_eel_s),
                id="execution_event_lag",
                name=f"Execution-event lag gauge (every {_eel_s}s)",
                replace_existing=True,
                max_instances=1,
                next_run_time=datetime.now() + timedelta(seconds=50),
            )

            _de_s = max(
                30,
                int(getattr(settings, "chili_drift_escalation_interval_seconds", 120)),
            )
            _scheduler.add_job(
                _run_drift_escalation_watchdog_job,
                trigger=IntervalTrigger(seconds=_de_s),
                id="drift_escalation_watchdog",
                name=f"Drift escalation watchdog (every {_de_s}s)",
                replace_existing=True,
                max_instances=1,
                next_run_time=datetime.now() + timedelta(seconds=90),
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

            _scheduler.add_job(
                _run_monitor_decision_review_job,
                trigger=IntervalTrigger(hours=1),
                id="monitor_decision_review",
                name="Pattern monitor decision review (hourly)",
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
            _bus_on = bool(settings.chili_autopilot_price_bus_enabled)
            _pr_m = 1 if _bus_on else max(2, int(settings.chili_momentum_paper_runner_scheduler_interval_minutes))
            _scheduler.add_job(
                _run_momentum_paper_runner_batch_job,
                trigger=IntervalTrigger(minutes=_pr_m),
                id="momentum_paper_runner_batch",
                name=f"Momentum paper automation runner (every {_pr_m}min, {'heartbeat' if _bus_on else 'simulated'})",
                replace_existing=True,
                max_instances=1,
                next_run_time=datetime.now() + timedelta(seconds=55),
            )
            if _bus_on:
                try:
                    from .trading.momentum_neural.paper_runner_loop import start_runner_loop
                    start_runner_loop()
                    logger.info("[scheduler] Event-driven paper runner loop started (price bus active)")
                except Exception as e:
                    logger.warning("[scheduler] Event-driven runner loop failed to start: %s", e)

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

        # Neural-mesh activation queue drain. The runner was previously only
        # reachable via HTTP; without this job the pending queue saturates at
        # 500 and every enqueue is rejected (stop_eval, pattern_health,
        # learning-cycle signals all silently dropped).
        try:
            if include_heavy:
                _scheduler.add_job(
                    _run_neural_mesh_drain_job,
                    trigger=IntervalTrigger(seconds=30),
                    id="neural_mesh_drain",
                    name="Neural mesh activation drain (every 30s)",
                    replace_existing=True,
                    max_instances=1,
                    next_run_time=datetime.now() + timedelta(seconds=10),
                )
        except Exception:
            logger.exception("[scheduler] failed to register neural_mesh_drain job")

        # Phase G: bracket reconciliation sweep (shadow mode only).
        # Gated by brain_live_brackets_mode != off; authoritative is blocked
        # inside the job itself (Phase G is observability-only).
        try:
            _brk_mode = (getattr(settings, "brain_live_brackets_mode", "off") or "off").lower()
            _brk_interval_s = int(getattr(settings, "brain_live_brackets_reconciliation_interval_s", 60) or 60)
            if include_web_light and _brk_mode not in ("off", "authoritative"):
                _scheduler.add_job(
                    _run_bracket_reconciliation_job,
                    trigger=IntervalTrigger(seconds=_brk_interval_s),
                    id="bracket_reconciliation",
                    name=f"Bracket reconciliation sweep (every {_brk_interval_s}s; mode={_brk_mode})",
                    replace_existing=True,
                    max_instances=1,
                    next_run_time=datetime.now() + timedelta(seconds=30),
                )
        except Exception:
            logger.exception("[scheduler] failed to register bracket_reconciliation job")

        # Phase I: weekly capital re-weight sweep (shadow mode only).
        try:
            _cr_mode = (getattr(settings, "brain_capital_reweight_mode", "off") or "off").lower()
            if include_web_light and _cr_mode not in ("off", "authoritative"):
                _cr_dow = str(getattr(settings, "brain_capital_reweight_cron_day_of_week", "sun") or "sun")
                _cr_hour = int(getattr(settings, "brain_capital_reweight_cron_hour", 18) or 18)
                _scheduler.add_job(
                    _run_capital_reweight_weekly_job,
                    trigger=CronTrigger(day_of_week=_cr_dow, hour=_cr_hour, minute=30),
                    id="capital_reweight_weekly",
                    name=f"Capital re-weight weekly ({_cr_dow} {_cr_hour:02d}:30; mode={_cr_mode})",
                    replace_existing=True,
                    max_instances=1,
                )
        except Exception:
            logger.exception("[scheduler] failed to register capital_reweight_weekly job")

        # Phase J: daily drift-monitor sweep (shadow mode only).
        try:
            _dm_mode = (getattr(settings, "brain_drift_monitor_mode", "off") or "off").lower()
            if include_web_light and _dm_mode not in ("off", "authoritative"):
                _dm_hour = int(getattr(settings, "brain_drift_monitor_cron_hour", 5) or 5)
                _dm_minute = int(getattr(settings, "brain_drift_monitor_cron_minute", 30) or 30)
                _scheduler.add_job(
                    _run_drift_monitor_daily_job,
                    trigger=CronTrigger(hour=_dm_hour, minute=_dm_minute),
                    id="drift_monitor_daily",
                    name=f"Drift monitor daily ({_dm_hour:02d}:{_dm_minute:02d}; mode={_dm_mode})",
                    replace_existing=True,
                    max_instances=1,
                )
        except Exception:
            logger.exception("[scheduler] failed to register drift_monitor_daily job")

        # Phase K: daily divergence panel sweep (shadow mode only).
        try:
            _dv_mode = (
                getattr(settings, "brain_divergence_scorer_mode", "off") or "off"
            ).lower()
            if include_web_light and _dv_mode not in ("off", "authoritative"):
                _dv_hour = int(
                    getattr(settings, "brain_divergence_scorer_cron_hour", 6) or 6
                )
                _dv_minute = int(
                    getattr(settings, "brain_divergence_scorer_cron_minute", 15) or 15
                )
                _scheduler.add_job(
                    _run_divergence_sweep_daily_job,
                    trigger=CronTrigger(hour=_dv_hour, minute=_dv_minute),
                    id="divergence_sweep_daily",
                    name=(
                        f"Divergence panel daily ({_dv_hour:02d}:{_dv_minute:02d}; "
                        f"mode={_dv_mode})"
                    ),
                    replace_existing=True,
                    max_instances=1,
                )
        except Exception:
            logger.exception(
                "[scheduler] failed to register divergence_sweep_daily job"
            )

        # Q1.T2: weekly Gaussian HMM regime retrain (gated).
        try:
            if include_web_light and getattr(
                settings, "chili_regime_classifier_enabled", False
            ):
                _rg_dow = str(
                    getattr(settings, "chili_regime_classifier_weekly_cron_dow", "sun")
                    or "sun"
                )
                _rg_hour = int(
                    getattr(settings, "chili_regime_classifier_weekly_cron_hour", 4) or 4
                )
                _rg_minute = int(
                    getattr(
                        settings, "chili_regime_classifier_weekly_cron_minute", 15
                    )
                    or 15
                )
                _scheduler.add_job(
                    _run_weekly_regime_retrain_job,
                    trigger=CronTrigger(
                        day_of_week=_rg_dow, hour=_rg_hour, minute=_rg_minute
                    ),
                    id="regime_classifier_weekly",
                    name=(
                        f"Regime HMM weekly ({_rg_dow} {_rg_hour:02d}:{_rg_minute:02d})"
                    ),
                    replace_existing=True,
                    max_instances=1,
                )
        except Exception:
            logger.exception("[scheduler] failed to register regime_classifier_weekly job")

        # Q1.T1: weekly CPCV backfill on heavy workers (Sun 04:00 ET; subprocess ``--commit``).
        try:
            if include_heavy and getattr(
                settings, "chili_cpcv_weekly_backfill_enabled", False
            ):
                _scheduler.add_job(
                    _run_weekly_cpcv_backfill_job,
                    trigger=CronTrigger(
                        day_of_week="sun",
                        hour=4,
                        minute=0,
                        timezone="America/New_York",
                    ),
                    id="weekly_cpcv_backfill",
                    name="Weekly CPCV backfill (Sun 04:00 America/New_York; --commit)",
                    replace_existing=True,
                    max_instances=1,
                )
        except Exception:
            logger.exception("[scheduler] failed to register weekly_cpcv_backfill job")

        # Phase L.17: daily macro-regime snapshot sweep (shadow mode only).
        try:
            _mr_mode = (
                getattr(settings, "brain_macro_regime_mode", "off") or "off"
            ).lower()
            if include_web_light and _mr_mode not in ("off", "authoritative"):
                _mr_hour = int(
                    getattr(settings, "brain_macro_regime_cron_hour", 6) or 6
                )
                _mr_minute = int(
                    getattr(settings, "brain_macro_regime_cron_minute", 30) or 30
                )
                _scheduler.add_job(
                    _run_macro_regime_daily_job,
                    trigger=CronTrigger(hour=_mr_hour, minute=_mr_minute),
                    id="macro_regime_daily",
                    name=(
                        f"Macro regime daily ({_mr_hour:02d}:{_mr_minute:02d}; "
                        f"mode={_mr_mode})"
                    ),
                    replace_existing=True,
                    max_instances=1,
                )
                # A4 — FRED DGS10/DGS2 ingestion 5 minutes after macro snapshot
                # so we can attach the real yield curve slope to the same row.
                _fred_minute = (_mr_minute + 5) % 60
                _fred_hour = _mr_hour + (1 if _mr_minute + 5 >= 60 else 0)
                _scheduler.add_job(
                    _run_fred_yield_curve_daily_job,
                    trigger=CronTrigger(hour=_fred_hour, minute=_fred_minute),
                    id="fred_yield_curve_daily",
                    name=(
                        f"FRED DGS10/DGS2 daily ({_fred_hour:02d}:{_fred_minute:02d})"
                    ),
                    replace_existing=True,
                    max_instances=1,
                )
        except Exception:
            logger.exception(
                "[scheduler] failed to register macro_regime_daily job"
            )

        # Phase L.18: daily breadth + RS snapshot sweep (shadow mode only).
        try:
            _br_mode = (
                getattr(settings, "brain_breadth_relstr_mode", "off") or "off"
            ).lower()
            if include_web_light and _br_mode not in ("off", "authoritative"):
                _br_hour = int(
                    getattr(settings, "brain_breadth_relstr_cron_hour", 6) or 6
                )
                _br_minute = int(
                    getattr(settings, "brain_breadth_relstr_cron_minute", 45)
                    or 45
                )
                _scheduler.add_job(
                    _run_breadth_relstr_daily_job,
                    trigger=CronTrigger(hour=_br_hour, minute=_br_minute),
                    id="breadth_relstr_daily",
                    name=(
                        f"Breadth + RS daily ({_br_hour:02d}:"
                        f"{_br_minute:02d}; mode={_br_mode})"
                    ),
                    replace_existing=True,
                    max_instances=1,
                )
        except Exception:
            logger.exception(
                "[scheduler] failed to register breadth_relstr_daily job"
            )

        # Phase L.19: daily cross-asset signals sweep (shadow mode only).
        try:
            _ca_mode = (
                getattr(settings, "brain_cross_asset_mode", "off") or "off"
            ).lower()
            if include_web_light and _ca_mode not in ("off", "authoritative"):
                _ca_hour = int(
                    getattr(settings, "brain_cross_asset_cron_hour", 7) or 7
                )
                _ca_minute = int(
                    getattr(settings, "brain_cross_asset_cron_minute", 0)
                    or 0
                )
                _scheduler.add_job(
                    _run_cross_asset_daily_job,
                    trigger=CronTrigger(hour=_ca_hour, minute=_ca_minute),
                    id="cross_asset_daily",
                    name=(
                        f"Cross-asset daily ({_ca_hour:02d}:"
                        f"{_ca_minute:02d}; mode={_ca_mode})"
                    ),
                    replace_existing=True,
                    max_instances=1,
                )
        except Exception:
            logger.exception(
                "[scheduler] failed to register cross_asset_daily job"
            )

        # Phase L.20: daily per-ticker regime sweep (shadow mode only).
        try:
            _tr_mode = (
                getattr(settings, "brain_ticker_regime_mode", "off") or "off"
            ).lower()
            if include_web_light and _tr_mode not in ("off", "authoritative"):
                _tr_hour = int(
                    getattr(settings, "brain_ticker_regime_cron_hour", 7) or 7
                )
                _tr_minute = int(
                    getattr(settings, "brain_ticker_regime_cron_minute", 15)
                    or 15
                )
                _scheduler.add_job(
                    _run_ticker_regime_daily_job,
                    trigger=CronTrigger(hour=_tr_hour, minute=_tr_minute),
                    id="ticker_regime_daily",
                    name=(
                        f"Ticker regime daily ({_tr_hour:02d}:"
                        f"{_tr_minute:02d}; mode={_tr_mode})"
                    ),
                    replace_existing=True,
                    max_instances=1,
                )
        except Exception:
            logger.exception(
                "[scheduler] failed to register ticker_regime_daily job"
            )

        # Phase L.21: daily vol term-structure + dispersion snapshot
        # (shadow mode only).
        try:
            _vd_mode = (
                getattr(settings, "brain_vol_dispersion_mode", "off") or "off"
            ).lower()
            if include_web_light and _vd_mode not in ("off", "authoritative"):
                _vd_hour = int(
                    getattr(settings, "brain_vol_dispersion_cron_hour", 7) or 7
                )
                _vd_minute = int(
                    getattr(settings, "brain_vol_dispersion_cron_minute", 30)
                    or 30
                )
                _scheduler.add_job(
                    _run_vol_dispersion_daily_job,
                    trigger=CronTrigger(hour=_vd_hour, minute=_vd_minute),
                    id="vol_dispersion_daily",
                    name=(
                        f"Vol dispersion daily ({_vd_hour:02d}:"
                        f"{_vd_minute:02d}; mode={_vd_mode})"
                    ),
                    replace_existing=True,
                    max_instances=1,
                )
        except Exception:
            logger.exception(
                "[scheduler] failed to register vol_dispersion_daily job"
            )

        # Phase L.22: daily intraday session regime snapshot
        # (shadow mode only). Runs post-close at 22:00 local.
        try:
            _is_mode = (
                getattr(settings, "brain_intraday_session_mode", "off")
                or "off"
            ).lower()
            if include_web_light and _is_mode not in ("off", "authoritative"):
                _is_hour = int(
                    getattr(
                        settings, "brain_intraday_session_cron_hour", 22,
                    )
                    or 22
                )
                _is_minute = int(
                    getattr(
                        settings, "brain_intraday_session_cron_minute", 0,
                    )
                    or 0
                )
                _scheduler.add_job(
                    _run_intraday_session_daily_job,
                    trigger=CronTrigger(hour=_is_hour, minute=_is_minute),
                    id="intraday_session_daily",
                    name=(
                        f"Intraday session daily ({_is_hour:02d}:"
                        f"{_is_minute:02d}; mode={_is_mode})"
                    ),
                    replace_existing=True,
                    max_instances=1,
                )
        except Exception:
            logger.exception(
                "[scheduler] failed to register intraday_session_daily job"
            )

        # Phase M.1: pattern x regime performance ledger (shadow-only).
        # First consumer of L.17-L.22 snapshots. Runs daily at 23:00 local,
        # after L.22 intraday session (22:00) has landed.
        try:
            _prp_mode = (
                getattr(settings, "brain_pattern_regime_perf_mode", "off")
                or "off"
            ).lower()
            if include_web_light and _prp_mode not in ("off", "authoritative"):
                _prp_hour = int(
                    getattr(
                        settings,
                        "brain_pattern_regime_perf_cron_hour",
                        23,
                    )
                    or 23
                )
                _prp_minute = int(
                    getattr(
                        settings,
                        "brain_pattern_regime_perf_cron_minute",
                        0,
                    )
                    or 0
                )
                _scheduler.add_job(
                    _run_pattern_regime_perf_daily_job,
                    trigger=CronTrigger(hour=_prp_hour, minute=_prp_minute),
                    id="pattern_regime_perf_daily",
                    name=(
                        f"Pattern x regime perf daily "
                        f"({_prp_hour:02d}:{_prp_minute:02d}; "
                        f"mode={_prp_mode})"
                    ),
                    replace_existing=True,
                    max_instances=1,
                )
        except Exception:
            logger.exception(
                "[scheduler] failed to register pattern_regime_perf_daily job"
            )

        # Phase M.2.c: pattern x regime kill-switch daily sweep (shadow-gated).
        try:
            _ks_mode = (
                getattr(settings, "brain_pattern_regime_killswitch_mode", "off")
                or "off"
            ).lower()
            if include_web_light and _ks_mode != "off":
                _ks_hour = int(
                    getattr(
                        settings,
                        "brain_pattern_regime_killswitch_cron_hour",
                        23,
                    )
                    or 23
                )
                _ks_minute = int(
                    getattr(
                        settings,
                        "brain_pattern_regime_killswitch_cron_minute",
                        5,
                    )
                    or 5
                )
                _scheduler.add_job(
                    _run_pattern_regime_killswitch_daily_job,
                    trigger=CronTrigger(hour=_ks_hour, minute=_ks_minute),
                    id="pattern_regime_killswitch_daily",
                    name=(
                        f"Pattern x regime killswitch daily "
                        f"({_ks_hour:02d}:{_ks_minute:02d}; "
                        f"mode={_ks_mode})"
                    ),
                    replace_existing=True,
                    max_instances=1,
                )
        except Exception:
            logger.exception(
                "[scheduler] failed to register pattern_regime_killswitch_daily job"
            )

        try:
            _ap_enabled = bool(
                getattr(settings, "brain_pattern_regime_autopilot_enabled", False)
            )
            if include_web_light and _ap_enabled:
                _ap_hour = int(
                    getattr(settings, "brain_pattern_regime_autopilot_cron_hour", 6) or 6
                )
                _ap_minute = int(
                    getattr(settings, "brain_pattern_regime_autopilot_cron_minute", 15) or 15
                )
                _scheduler.add_job(
                    _run_pattern_regime_autopilot_tick_job,
                    trigger=CronTrigger(hour=_ap_hour, minute=_ap_minute),
                    id="pattern_regime_autopilot_tick",
                    name=(
                        f"Pattern x regime autopilot tick "
                        f"({_ap_hour:02d}:{_ap_minute:02d})"
                    ),
                    replace_existing=True,
                    max_instances=1,
                )

                _ap_w_hour = int(
                    getattr(settings, "brain_pattern_regime_autopilot_weekly_cron_hour", 9) or 9
                )
                _ap_w_dow = str(
                    getattr(settings, "brain_pattern_regime_autopilot_weekly_cron_dow", "mon") or "mon"
                )
                _scheduler.add_job(
                    _run_pattern_regime_autopilot_weekly_job,
                    trigger=CronTrigger(day_of_week=_ap_w_dow, hour=_ap_w_hour, minute=0),
                    id="pattern_regime_autopilot_weekly",
                    name=(
                        f"Pattern x regime autopilot weekly "
                        f"({_ap_w_dow} {_ap_w_hour:02d}:00)"
                    ),
                    replace_existing=True,
                    max_instances=1,
                )
        except Exception:
            logger.exception(
                "[scheduler] failed to register pattern_regime_autopilot jobs"
            )

        # F.4-F.6 — Gateway learning loop (distiller every 15min, evolver hourly).
        # Lives on the same scheduler so the brain-worker (role=all) drives it.
        try:
            if role in ("all", "web"):
                def _run_gateway_distiller_job() -> None:
                    try:
                        from app.db import SessionLocal
                        from app.services.context_brain.distiller import (
                            distill_patterns,
                        )
                        _db = SessionLocal()
                        try:
                            res = distill_patterns(_db)
                            logger.info(
                                "[gateway-learning] distiller pass: %s", res
                            )
                        finally:
                            _db.close()
                    except Exception:
                        logger.exception("[gateway-learning] distiller failed")

                def _run_gateway_evolver_job() -> None:
                    try:
                        from app.db import SessionLocal
                        from app.services.context_brain.policy_evolver import (
                            evolve_policies,
                        )
                        _db = SessionLocal()
                        try:
                            res = evolve_policies(_db)
                            logger.info(
                                "[gateway-learning] evolver pass: %s", res
                            )
                        finally:
                            _db.close()
                    except Exception:
                        logger.exception("[gateway-learning] evolver failed")

                _scheduler.add_job(
                    _run_gateway_distiller_job,
                    trigger=IntervalTrigger(minutes=15),
                    id="gateway_distiller",
                    name="Gateway learning distiller (every 15min)",
                    replace_existing=True,
                    max_instances=1,
                    next_run_time=datetime.now() + timedelta(minutes=2),
                )
                _scheduler.add_job(
                    _run_gateway_evolver_job,
                    trigger=IntervalTrigger(hours=1),
                    id="gateway_evolver",
                    name="Gateway policy evolver (hourly)",
                    replace_existing=True,
                    max_instances=1,
                    next_run_time=datetime.now() + timedelta(minutes=10),
                )
        except Exception:
            logger.exception(
                "[scheduler] failed to register gateway learning jobs"
            )

        # Q1.T4 — Strategy parameter learning pass (every 6 hours).
        try:
            if role in ("all", "web"):
                def _run_strategy_param_learning_job() -> None:
                    try:
                        from app.db import SessionLocal
                        from app.services.trading.strategy_parameter import (
                            run_parameter_learning_pass,
                        )
                        _db = SessionLocal()
                        try:
                            res = run_parameter_learning_pass(_db)
                            logger.info(
                                "[strategy-param] learning pass: %s", res
                            )
                        finally:
                            _db.close()
                    except Exception:
                        logger.exception(
                            "[strategy-param] learning pass failed"
                        )

                _scheduler.add_job(
                    _run_strategy_param_learning_job,
                    trigger=IntervalTrigger(hours=6),
                    id="strategy_parameter_learning",
                    name="Strategy parameter learning pass (every 6h)",
                    replace_existing=True,
                    max_instances=1,
                    next_run_time=datetime.now() + timedelta(minutes=20),
                )
        except Exception:
            logger.exception(
                "[scheduler] failed to register strategy parameter learning job"
            )

        # Q2 Task K (Phase 1) — daily pattern-survival feature snapshot.
        # Runs once a day at 03:30 America/Los_Angeles (after the macro
        # regime daily at 02:00 has settled). Flag-gated; the job itself
        # also re-checks the flag and skips when off, so flipping the flag
        # at runtime takes effect on the next tick without a restart.
        try:
            if role in ("all", "web"):
                def _run_pattern_survival_snapshot_job() -> None:
                    try:
                        from app.db import SessionLocal
                        from app.services.trading.pattern_survival import (
                            run_pattern_survival_snapshot_job,
                        )
                        _db = SessionLocal()
                        try:
                            res = run_pattern_survival_snapshot_job(_db)
                            logger.info(
                                "[pattern-survival] daily snapshot: %s", res
                            )
                        finally:
                            _db.close()
                    except Exception:
                        logger.exception(
                            "[pattern-survival] daily snapshot failed"
                        )

                _scheduler.add_job(
                    _run_pattern_survival_snapshot_job,
                    trigger=CronTrigger(
                        hour=3, minute=30,
                        timezone="America/Los_Angeles",
                    ),
                    id="pattern_survival_snapshot",
                    name="Pattern-survival daily feature snapshot (03:30 PT)",
                    replace_existing=True,
                    max_instances=1,
                )
        except Exception:
            logger.exception(
                "[scheduler] failed to register pattern_survival snapshot job"
            )

        # Q2 Task R — pattern-survival training pass (weekly Sun 04:30 PT).
        # Runs after the regime classifier weekly retrain (Sun 04:15) and
        # the daily snapshot job. Order matters: the snapshot must have
        # populated features for the day before the training pass tries
        # to score them, and the regime retrain must have run before
        # snapshot so regime_at_snapshot is filled.
        try:
            if role in ("all", "web"):
                def _run_pattern_survival_training_job() -> None:
                    try:
                        from app.db import SessionLocal
                        from app.services.trading.pattern_survival import (
                            run_pattern_survival_training_pass,
                        )
                        _db = SessionLocal()
                        try:
                            res = run_pattern_survival_training_pass(_db)
                            logger.info(
                                "[pattern-survival] training pass: %s", res
                            )
                        finally:
                            _db.close()
                    except Exception:
                        logger.exception(
                            "[pattern-survival] training pass failed"
                        )

                _scheduler.add_job(
                    _run_pattern_survival_training_job,
                    trigger=CronTrigger(
                        day_of_week="sun", hour=4, minute=30,
                        timezone="America/Los_Angeles",
                    ),
                    id="pattern_survival_training",
                    name=(
                        "Pattern-survival weekly training "
                        "(label backfill + train + score, Sun 04:30 PT)"
                    ),
                    replace_existing=True,
                    max_instances=1,
                )
        except Exception:
            logger.exception(
                "[scheduler] failed to register pattern_survival training job"
            )

        # Q2 Task L — perps ingestion (every hour).
        # Flag-gated by chili_perps_lane_enabled. Iterates over the seeded
        # perp_contracts and writes premium/funding/OI rows to perp_quotes,
        # perp_funding, perp_oi, perp_basis. Continues to no-op if the flag
        # is off — useful so seed contract data accumulates silently before
        # any strategy ever consumes it (warm cache for funding_carry /
        # oi_divergence backtests).
        try:
            if role in ("all", "web"):
                def _run_perps_ingestion_job() -> None:
                    try:
                        from app.config import settings
                        if not getattr(
                            settings, "chili_perps_lane_enabled", False
                        ):
                            return
                        from app.db import SessionLocal
                        from app.services.trading.perps.ingestion import (
                            run_perps_ingestion_pass,
                        )
                        _db = SessionLocal()
                        try:
                            res = run_perps_ingestion_pass(_db)
                            logger.info("[perps] ingestion: %s", res)
                        finally:
                            _db.close()
                    except Exception:
                        logger.exception("[perps] ingestion failed")

                _scheduler.add_job(
                    _run_perps_ingestion_job,
                    trigger=IntervalTrigger(hours=1),
                    id="perps_ingestion",
                    name="Perps premium/funding/OI ingestion (hourly)",
                    replace_existing=True,
                    max_instances=1,
                    next_run_time=datetime.now() + timedelta(minutes=2),
                )
        except Exception:
            logger.exception(
                "[scheduler] failed to register perps ingestion job"
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
            "crypto viability refresh every 30min 24/7, "
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


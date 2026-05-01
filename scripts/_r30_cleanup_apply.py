"""R30 cleanup: remove dead _try_auto_execute_stop call + rename misleading
_run_crypto_stop_monitor_job. Pulls clean HEAD copies, applies surgical
string-replace edits, ast.parse-validates, writes back.
"""
import subprocess, ast, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# ---------- Edit 1: stop_engine.py ----------

stop_engine = ROOT / "app" / "services" / "trading" / "stop_engine.py"
head_se = subprocess.check_output(
    ["git", "show", "HEAD:app/services/trading/stop_engine.py"],
    cwd=str(ROOT),
).decode("utf-8")


SE_OLD = """        # Critical fast-path: direct Telegram for events that demand immediate action
        if event == "STOP_HIT" or event == "TIME_EXIT":
            _fmt = format_time_exit if event == "TIME_EXIT" else format_stop_hit
            msg = _fmt(ticker, price, reason, **_fmt_kw)
            dispatch_alert(db, user_id, STOP_HIT, ticker, msg, skip_throttle=True)
            dispatched += 1
            _try_auto_execute_stop(db, user_id, alert)"""

SE_NEW = """        # Critical fast-path: direct Telegram for events that demand immediate action
        if event == "STOP_HIT" or event == "TIME_EXIT":
            _fmt = format_time_exit if event == "TIME_EXIT" else format_stop_hit
            msg = _fmt(ticker, price, reason, **_fmt_kw)
            dispatch_alert(db, user_id, STOP_HIT, ticker, msg, skip_throttle=True)
            dispatched += 1
            # R30 cleanup (2026-04-30): _try_auto_execute_stop call REMOVED.
            # Single source of truth for crypto exit execution is now
            # ``run_crypto_exit_pass`` (called every 30s from
            # ``tick_auto_trader_monitor``); equity exits run through
            # ``submit_robinhood_trade_exit`` from the same monitor.
            # Leaving the call here was dead code (gated by
            # ``chili_auto_execute_stops=False``) but would have raced
            # the autotrader execution path if anyone ever flipped the
            # flag. dispatch_stop_alerts now does what its name says:
            # dispatches alerts (Telegram + neural mesh), no execution."""


# ---------- Edit 2: trading_scheduler.py ----------

scheduler = ROOT / "app" / "services" / "trading_scheduler.py"
head_sc = subprocess.check_output(
    ["git", "show", "HEAD:app/services/trading_scheduler.py"],
    cwd=str(ROOT),
).decode("utf-8")


SC_OLD_FUNC = """def _run_crypto_stop_monitor_job():
    \"\"\"24/7 stop-engine check for crypto positions only (every 2 minutes).\"\"\"
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
                    Trade.status == \"open\",
                    Trade.user_id.isnot(None),
                    Trade.ticker.like(\"%-USD\"),
                )
                .all()
            ]
            if not user_ids:
                return
            for uid in user_ids:
                try:
                    crypto_trades = db.query(Trade).filter(
                        Trade.status == \"open\",
                        Trade.user_id == uid,
                        Trade.ticker.like(\"%-USD\"),
                    ).all()
                    if not crypto_trades:
                        continue
                    summary = evaluate_all(db, uid)
                    dispatched = dispatch_stop_alerts(db, uid, summary)
                    if dispatched:
                        logger.info(\"[scheduler] Crypto stop monitor uid=%s: %d alerts dispatched\", uid, dispatched)
                except Exception:
                    logger.warning(\"[scheduler] Crypto stop monitor failed for uid=%s\", uid, exc_info=True)
        finally:
            db.close()

    run_scheduler_job_guarded(\"crypto_stop_monitor\", _work)"""


SC_NEW_FUNC = """def _run_stop_alert_dispatch_job():
    \"\"\"Stop-alert dispatch for crypto positions (every 2 minutes, 24/7).

    R30 cleanup (2026-04-30): renamed from ``_run_crypto_stop_monitor_job``.
    The original name implied this job acts on stops (places sells when
    triggered), but it actually only DISPATCHES alerts -- Telegram +
    neural-mesh sensor events. Real crypto exit execution lives in
    ``run_crypto_exit_pass`` called every 30s from
    ``tick_auto_trader_monitor``. The two paths used to share an
    auto-execute path via ``_try_auto_execute_stop`` (gated by
    ``chili_auto_execute_stops=False``); that has been removed in R30
    so the autotrader monitor is the single source of truth for exit
    execution.

    Job ID is intentionally kept as ``crypto_stop_monitor`` so
    ``brain_batch_jobs`` history continuity is preserved.
    \"\"\"
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
                    Trade.status == \"open\",
                    Trade.user_id.isnot(None),
                    Trade.ticker.like(\"%-USD\"),
                )
                .all()
            ]
            if not user_ids:
                return
            for uid in user_ids:
                try:
                    crypto_trades = db.query(Trade).filter(
                        Trade.status == \"open\",
                        Trade.user_id == uid,
                        Trade.ticker.like(\"%-USD\"),
                    ).all()
                    if not crypto_trades:
                        continue
                    summary = evaluate_all(db, uid)
                    dispatched = dispatch_stop_alerts(db, uid, summary)
                    if dispatched:
                        logger.info(\"[scheduler] stop_alert_dispatch uid=%s: %d alerts dispatched\", uid, dispatched)
                except Exception:
                    logger.warning(\"[scheduler] stop_alert_dispatch failed for uid=%s\", uid, exc_info=True)
        finally:
            db.close()

    run_scheduler_job_guarded(\"crypto_stop_monitor\", _work)"""


SC_OLD_REGISTRATION = """                    _run_crypto_stop_monitor_job,"""

SC_NEW_REGISTRATION = """                    _run_stop_alert_dispatch_job,"""


def apply():
    # ---- stop_engine.py ----
    if SE_OLD not in head_se:
        print("SE_OLD not found in HEAD"); sys.exit(1)
    se_new = head_se.replace(SE_OLD, SE_NEW)
    try:
        ast.parse(se_new)
    except SyntaxError as e:
        print(f"stop_engine SYNTAX line {e.lineno}: {e.msg}"); sys.exit(1)
    stop_engine.write_text(se_new, encoding="utf-8", newline="\n")
    print(f"wrote {len(se_new.splitlines())} lines to {stop_engine}")

    # ---- trading_scheduler.py ----
    if SC_OLD_FUNC not in head_sc:
        print("SC_OLD_FUNC not found in HEAD"); sys.exit(1)
    sc_new = head_sc.replace(SC_OLD_FUNC, SC_NEW_FUNC)

    if SC_OLD_REGISTRATION not in sc_new:
        print("SC_OLD_REGISTRATION not found"); sys.exit(1)
    sc_new = sc_new.replace(SC_OLD_REGISTRATION, SC_NEW_REGISTRATION)

    try:
        ast.parse(sc_new)
    except SyntaxError as e:
        print(f"scheduler SYNTAX line {e.lineno}: {e.msg}"); sys.exit(1)
    scheduler.write_text(sc_new, encoding="utf-8", newline="\n")
    print(f"wrote {len(sc_new.splitlines())} lines to {scheduler}")


if __name__ == "__main__":
    apply()

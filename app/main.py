"""CHILI Home Copilot - FastAPI application entry point."""
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)

# Install chili.* handler (trace_id formatter + default filter) before any chili.dedup / lifespan logs
from . import logger as _chili_log_setup  # noqa: F401

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse as StarletteJSONResponse
from sqlalchemy.exc import OperationalError
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader

from .db import Base, SessionLocal, engine
from .migrations import run_migrations
from .routers import admin, auth, brain, brain_project, brain_v1_compat, chat, dev_terminal, health_routes, jobs, pages, marketplace, mobile, trading
from .modules import get_nav_modules, load_enabled_modules, load_third_party_module
from .models import (  # noqa: F401 — register ORM tables
    BrainWorkerControl,
    LearningCycleAiReport,
    MarketplaceModule,
    PatternEvidenceHypothesis,
    PatternTradeRow,
)
from .models.coding_task import PlanTaskCodingProfile, CodingExecutionIteration  # noqa: F401
from .services.trading_scheduler import start_scheduler, stop_scheduler

# Suppress noisy WinError 10054 tracebacks from asyncio on Windows
if sys.platform == "win32":
    class _WinErrorFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            msg = record.getMessage()
            if "WinError 10054" in msg or "_call_connection_lost" in msg:
                return False
            return True
    logging.getLogger("asyncio").addFilter(_WinErrorFilter())

# Under pytest, conftest sets CHILI_PYTEST=1 before importing this module. Skip all DB work
# here so ``app.main`` import does not open connections or take locks; the ``db`` fixture
# runs create_all + migrations, then truncates — avoiding self-deadlock with TRUNCATE.
_under_pytest = (os.environ.get("CHILI_PYTEST") or "").strip().lower() in ("1", "true", "yes")
if not _under_pytest:
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)

# Builtin pattern seeding queries scan_patterns; can block if another process holds locks.
# Tests truncate app tables per case; callers that need ScanPattern rows insert them.
if not _under_pytest:
    try:
        from .services.trading.pattern_engine import (
            seed_builtin_patterns as _seed_builtin,
            seed_community_patterns as _seed_community,
        )
        _seed_db = SessionLocal()
        _seed_builtin(_seed_db)
        _seed_community(_seed_db)
        _seed_db.close()
    except Exception:
        pass

_backfill_state: dict = {"running": False, "total": 0, "done": 0, "filled": 0}


def _dedup_backtests():
    """One-time cleanup: remove duplicate BacktestResults per
    (related_insight_id, ticker, strategy_name), keeping only the newest
    record in each group. Then recompute win/loss counters on TradingInsight.
    """
    _log = logging.getLogger("chili.dedup")
    try:
        from .db import SessionLocal as _SL
        from .models.trading import TradingInsight, BacktestResult as _BT

        db = _SL()
        try:
            from sqlalchemy import func

            groups = (
                db.query(
                    _BT.related_insight_id,
                    _BT.ticker,
                    _BT.strategy_name,
                    func.count(_BT.id).label("cnt"),
                    func.max(_BT.id).label("keep_id"),
                )
                .filter(_BT.related_insight_id.isnot(None))
                .group_by(_BT.related_insight_id, _BT.ticker, _BT.strategy_name)
                .having(func.count(_BT.id) > 1)
                .all()
            )
            if not groups:
                _log.info("[dedup] No duplicate backtests found")
                return

            total_deleted = 0
            insight_ids: set[int] = set()
            for row in groups:
                ins_id, ticker, strat, cnt, keep_id = row
                deleted = (
                    db.query(_BT)
                    .filter(
                        _BT.related_insight_id == ins_id,
                        _BT.ticker == ticker,
                        _BT.strategy_name == strat,
                        _BT.id != keep_id,
                    )
                    .delete(synchronize_session=False)
                )
                total_deleted += deleted
                insight_ids.add(ins_id)

            db.flush()

            from .services.trading.insight_backtest_panel_sync import (
                sync_insight_backtest_tallies_from_evidence_panel,
            )

            for ins_id in insight_ids:
                ins = db.get(TradingInsight, ins_id)
                if not ins:
                    continue
                try:
                    sync_insight_backtest_tallies_from_evidence_panel(db, ins)
                except Exception:
                    _log.exception("[dedup] panel sync failed for insight %s", ins_id)
                ins.evidence_count = max(1, ins.evidence_count or 1)

            db.commit()
            _log.info(
                "[dedup] Removed %d duplicate backtests across %d insights",
                total_deleted, len(insight_ids),
            )
        finally:
            db.close()
    except Exception:
        _log.exception("[dedup] Failed to clean up duplicate backtests")


def _repair_wrongly_deactivated():
    """Re-activate user-seeded patterns and their variants that were
    incorrectly deactivated by the old aggressive pruning logic."""
    _log = logging.getLogger("chili.repair")
    try:
        from .db import SessionLocal as _SL
        from .models.trading import ScanPattern, TradingInsight
        db = _SL()
        try:
            _PROTECTED = {"user_seeded", "seed", "user"}
            roots = (
                db.query(ScanPattern)
                .filter(
                    ScanPattern.active.is_(False),
                    ScanPattern.origin.in_(list(_PROTECTED)),
                )
                .all()
            )
            reactivated = 0
            for rp in roots:
                rp.active = True
                reactivated += 1
                children = db.query(ScanPattern).filter(ScanPattern.parent_id == rp.id).all()
                for ch in children:
                    if not ch.active:
                        ch.active = True
                        reactivated += 1
                ins_list = db.query(TradingInsight).filter(
                    TradingInsight.scan_pattern_id == rp.id,
                    TradingInsight.active.is_(False),
                ).all()
                for ins in ins_list:
                    ins.active = True
                    reactivated += 1
                for ch in children:
                    ch_ins = db.query(TradingInsight).filter(
                        TradingInsight.scan_pattern_id == ch.id,
                        TradingInsight.active.is_(False),
                    ).all()
                    for ins in ch_ins:
                        ins.active = True
                        reactivated += 1
            if reactivated:
                db.commit()
                _log.info("[repair] Reactivated %d wrongly-deactivated patterns/insights", reactivated)
        finally:
            db.close()
    except Exception:
        _log.exception("[repair] Failed to repair deactivated patterns")


def _reinfer_pattern_timeframes():
    """One-time migration: re-infer timeframe for ALL existing patterns using
    the latest dynamic inference logic.  Only updates rows whose inferred
    timeframe differs from the current value."""
    _log = logging.getLogger("chili.reinfer_tf")
    try:
        from .db import SessionLocal as _SL
        from .models.trading import ScanPattern
        from .services.backtest_service import infer_pattern_timeframe
        import json as _json
        from collections import Counter

        db = _SL()
        try:
            patterns = db.query(ScanPattern).all()
            updated = 0
            changes: Counter = Counter()
            for p in patterns:
                conditions: list[dict] = []
                try:
                    rules = _json.loads(p.rules_json) if p.rules_json else {}
                    conditions = rules.get("conditions", [])
                except Exception:
                    pass
                new_tf = infer_pattern_timeframe(
                    conditions,
                    name=p.name or "",
                    asset_class=p.asset_class or "all",
                    description=p.description or "",
                )
                old_tf = p.timeframe or "1d"
                if new_tf != old_tf:
                    changes[f"{old_tf}->{new_tf}"] += 1
                    p.timeframe = new_tf
                    updated += 1
            if updated:
                db.commit()
                summary = ", ".join(f"{k}: {v}" for k, v in changes.most_common())
                _log.info("[reinfer_tf] Updated %d of %d patterns: %s",
                          updated, len(patterns), summary)
            else:
                _log.info("[reinfer_tf] All %d patterns already have correct timeframes",
                          len(patterns))
        finally:
            db.close()
    except Exception:
        _log.exception("[reinfer_tf] Failed to re-infer pattern timeframes")


def _ensure_ticker_scope_columns():
    """Add ticker_scope and scope_tickers columns if they don't exist yet."""
    _log = logging.getLogger("chili.migrate")
    try:
        from .db import engine as _engine
        from sqlalchemy import text, inspect as sa_inspect
        insp = sa_inspect(_engine)
        existing = {c["name"] for c in insp.get_columns("scan_patterns")}
        with _engine.begin() as conn:
            if "ticker_scope" not in existing:
                conn.execute(text(
                    "ALTER TABLE scan_patterns ADD COLUMN ticker_scope VARCHAR(20) NOT NULL DEFAULT 'universal'"
                ))
                _log.info("[migrate] Added ticker_scope column to scan_patterns")
            if "scope_tickers" not in existing:
                conn.execute(text(
                    "ALTER TABLE scan_patterns ADD COLUMN scope_tickers TEXT"
                ))
                _log.info("[migrate] Added scope_tickers column to scan_patterns")
    except Exception:
        _log.exception("[migrate] Failed to add ticker_scope columns")


def _cleanup_cross_asset_backtests():
    """Backfill ScanPattern.asset_class from hints, remove stock rows on crypto patterns (and vice versa), recompute insights/scopes."""
    _log = logging.getLogger("chili.asset_cleanup")
    try:
        from .db import SessionLocal as _SL
        from .services.trading.backtest_asset_cleanup import run_cross_asset_backtest_cleanup

        db = _SL()
        try:
            stats = run_cross_asset_backtest_cleanup(db)
            if (
                stats.get("patterns_asset_backfilled")
                or stats.get("backtests_deleted")
                or stats.get("scopes_recomputed")
            ):
                _log.info(
                    "[asset_cleanup] backfilled=%d deleted_bts=%d insights=%d scopes=%d",
                    stats.get("patterns_asset_backfilled", 0),
                    stats.get("backtests_deleted", 0),
                    stats.get("insights_recomputed", 0),
                    stats.get("scopes_recomputed", 0),
                )
        finally:
            db.close()
    except Exception:
        _log.exception("[asset_cleanup] failed")


def _recompute_all_ticker_scopes():
    """One-time: classify ticker_scope for all patterns with enough backtest data."""
    _log = logging.getLogger("chili.scope_recompute")
    try:
        from .db import SessionLocal as _SL
        from .models.trading import ScanPattern

        db = _SL()
        try:
            from .services.trading.learning import recompute_ticker_scope
            patterns = db.query(ScanPattern).filter(ScanPattern.active.is_(True)).all()
            updated = 0
            for p in patterns:
                old_scope = p.ticker_scope or "universal"
                new_scope = recompute_ticker_scope(db, p.id)
                if new_scope and new_scope != old_scope:
                    updated += 1
            if updated:
                db.commit()
                _log.info("[scope_recompute] Updated ticker_scope for %d of %d active patterns",
                          updated, len(patterns))
            else:
                _log.info("[scope_recompute] All %d active patterns already have correct scopes",
                          len(patterns))
        finally:
            db.close()
    except Exception:
        _log.exception("[scope_recompute] Failed to recompute ticker scopes")


def _backfill_backtests():
    """Background: run smart backtests for insights that lack results or
    still have old generic-strategy backtests that need re-running with
    the unified DynamicPatternStrategy engine."""
    _log = logging.getLogger("chili.backfill")
    try:
        from .db import SessionLocal as _SL
        from .models.trading import TradingInsight, BacktestResult as _BT, ScanPattern
        from .services.trading.backtest_engine import (
            smart_backtest_insight, _extract_context, _GENERIC_STRATEGY_NAMES,
        )

        db = _SL()
        try:
            bt_by_insight: dict[int, list[str]] = {}
            bt_has_generic: dict[int, bool] = {}
            for row in (
                db.query(
                    _BT.related_insight_id, _BT.ticker, _BT.strategy_name,
                )
                .filter(_BT.related_insight_id.isnot(None))
                .all()
            ):
                bt_by_insight.setdefault(row[0], []).append(row[1])
                if row[2] in _GENERIC_STRATEGY_NAMES:
                    bt_has_generic[row[0]] = True

            all_candidates = (
                db.query(TradingInsight)
                .filter(TradingInsight.active.is_(True))
                .all()
            )

            stale: list = []
            seen_ids: set[int] = set()
            for ins in all_candidates:
                existing = bt_by_insight.get(ins.id, [])

                if bt_has_generic.get(ins.id):
                    old_count = (
                        db.query(_BT)
                        .filter(_BT.related_insight_id == ins.id)
                        .delete()
                    )
                    ins.win_count = 0
                    ins.loss_count = 0
                    ins.evidence_count = max(1, ins.evidence_count)
                    db.commit()
                    _log.info(
                        "Cleared %d generic backtests for insight %d — "
                        "will re-run with unified dynamic engine",
                        old_count, ins.id,
                    )
                    stale.append(ins)
                    seen_ids.add(ins.id)
                    continue

                if not existing:
                    stale.append(ins)
                    seen_ids.add(ins.id)
                    continue

                ctx = _extract_context(
                    ins.pattern_description or "", db=db, insight_id=ins.id,
                )
                if ctx["wants_crypto"]:
                    has_crypto_bt = any(t.endswith("-USD") for t in existing)
                    if not has_crypto_bt:
                        stale.append(ins)
                        seen_ids.add(ins.id)

            for ins in all_candidates:
                if ins.id in seen_ids:
                    continue
                sp_id = getattr(ins, "scan_pattern_id", None)
                if not sp_id:
                    continue
                sp = db.query(ScanPattern).get(sp_id)
                if sp and sp.win_rate is not None and sp.win_rate > 0:
                    bt_count = len(bt_by_insight.get(ins.id, []))
                    if bt_count == 0:
                        _log.info(
                            "Pattern %d (%s) has WR=%.0f%% but 0 BacktestResults — queuing backfill",
                            sp.id, sp.name[:40], sp.win_rate if sp.win_rate <= 100 else sp.win_rate,
                        )
                        stale.append(ins)
                        seen_ids.add(ins.id)

            linked_sp_ids = {
                getattr(ins, "scan_pattern_id", None)
                for ins in all_candidates
                if getattr(ins, "scan_pattern_id", None)
            }
            orphan_patterns = (
                db.query(ScanPattern)
                .filter(
                    ScanPattern.win_rate.isnot(None),
                    ScanPattern.win_rate > 0,
                    ~ScanPattern.id.in_(linked_sp_ids) if linked_sp_ids else ScanPattern.id.isnot(None),
                )
                .all()
            )
            if orphan_patterns:
                from .models.core import User
                user_ids = [
                    r[0] for r in db.query(User.id).all()
                ] or [None]
                for sp in orphan_patterns:
                    _log.info(
                        "Orphan pattern %d (%s) has WR but no TradingInsight — creating for %d user(s)",
                        sp.id, sp.name[:40], len(user_ids),
                    )
                    first_ins = None
                    for uid in user_ids:
                        new_ins = TradingInsight(
                            user_id=uid,
                            scan_pattern_id=sp.id,
                            pattern_description=f"{sp.name} — {sp.description or ''}",
                            confidence=sp.confidence or 0.5,
                            evidence_count=1,
                            active=True,
                        )
                        db.add(new_ins)
                        db.flush()
                        seen_ids.add(new_ins.id)
                        if first_ins is None:
                            first_ins = new_ins
                    if first_ins is not None:
                        stale.append(first_ins)
                db.commit()

            if not stale:
                _log.info("No insights need backtest backfill")
                return

            _backfill_state["running"] = True
            _backfill_state["total"] = len(stale)
            _backfill_state["done"] = 0
            _backfill_state["filled"] = 0

            _log.info(f"Running smart backtest backfill for {len(stale)} insights...")
            for ins in stale:
                try:
                    result = smart_backtest_insight(db, ins, target_tickers=25)
                    if result["total"] > 0:
                        _backfill_state["filled"] += 1
                except Exception:
                    pass
                _backfill_state["done"] += 1

            _log.info(
                f"Backtest backfill complete: "
                f"{_backfill_state['filled']}/{len(stale)} insights updated"
            )
        finally:
            _backfill_state["running"] = False
            db.close()
    except Exception as e:
        _backfill_state["running"] = False
        _log.warning(f"Backtest backfill failed: {e}")


def _run_deferred_startup() -> None:
    """Heavy startup work (runs in a daemon thread).

    Uvicorn binds the listening socket only *after* FastAPI lifespan yields.
    Running this work synchronously before yield blocked the port for several
    seconds, causing browsers to show connection timeouts during reload.
    Order: DB maintenance first, then WS, prewarm, backfill thread, scheduler.
    """
    import threading

    _log = logging.getLogger("chili.startup")
    if _under_pytest:
        _log.info(
            "[startup] CHILI_PYTEST: skipping deferred startup (broker restore, DB maintenance, scheduler)"
        )
        return
    try:
        from .config import settings as _settings

        _sched_role = (getattr(_settings, "chili_scheduler_role", None) or "all").strip().lower()
        # Robinhood restore can block on device approval / MFA — must not run in lifespan
        # before yield or HTTP never becomes ready (empty reply / TLS handshake failure).
        _restore_broker_sessions()
        # Hard Rule 1/2: kill-switch state must survive restarts. Without this,
        # a tripped breaker silently disarms on every redeploy.
        try:
            from .services.trading.governance import (
                get_kill_switch_status,
                restore_kill_switch_from_db,
            )
            restore_kill_switch_from_db()
            _ks = get_kill_switch_status()
            if _ks.get("active"):
                _log.warning(
                    "[startup] Kill switch restored ACTIVE: %s", _ks.get("reason")
                )
        except Exception:
            _log.debug("[startup] Kill-switch restore failed", exc_info=True)
        if _sched_role != "none":
            _dedup_backtests()
        _repair_wrongly_deactivated()
        _ensure_ticker_scope_columns()
        if _sched_role != "none":
            _cleanup_cross_asset_backtests()
        if _sched_role != "none":
            _reinfer_pattern_timeframes()
        _start_massive_ws()
        _start_price_bus()
        if _sched_role != "none":
            _recompute_all_ticker_scopes()
        if _sched_role != "none":
            _prewarm_market_context()
            threading.Thread(target=_backfill_backtests, daemon=True).start()
        else:
            _log.info(
                "[startup] CHILI_SCHEDULER_ROLE=none: skipping dedup_backtests, cross-asset cleanup, reinfer, "
                "scope recompute, Massive WebSocket (REST quotes), market prewarm, backtest backfill thread"
            )
        start_scheduler()
    except Exception:
        _log.exception("[startup] Deferred startup failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    import threading
    # Load ML model first (doesn't need DB)
    try:
        from .services.trading.ml_engine import load_model
        load_model()
    except Exception:
        pass
    # Defer heavy DB + scheduler startup so lifespan yields immediately; uvicorn
    # binds the socket only after yield (see uvicorn.server.Server.startup).
    # Broker restore runs inside _run_deferred_startup (not here) so Robinhood login
    # cannot block application startup.
    threading.Thread(target=_run_deferred_startup, daemon=True, name="chili-deferred-startup").start()
    yield
    _stop_massive_ws()
    stop_scheduler()


def _restore_broker_sessions():
    """Try to restore persisted Robinhood + Coinbase sessions on startup."""
    _log = logging.getLogger("chili.startup")
    try:
        from .services import broker_service
        broker_service.try_restore_session()
    except Exception:
        pass

    try:
        from .db import SessionLocal
        from .models.core import BrokerCredential
        from .services.credential_vault import decrypt_credentials
        from .services import coinbase_service, broker_manager

        db = SessionLocal()
        try:
            cb_creds = (
                db.query(BrokerCredential)
                .filter(BrokerCredential.broker == "coinbase")
                .all()
            )
            if not cb_creds:
                _log.debug("[startup] No Coinbase credentials in vault")
            else:
                connected = False
                for cb_cred in cb_creds:
                    creds = decrypt_credentials(cb_cred.encrypted_data)
                    if not (creds and creds.get("api_key") and creds.get("api_secret")):
                        continue
                    if not connected:
                        result = coinbase_service.connect_with_credentials(
                            creds["api_key"], creds["api_secret"],
                        )
                        _log.info("[startup] Coinbase auto-reconnect: %s", result.get("status"))
                        connected = result.get("status") == "connected"
                    if connected:
                        uid = cb_cred.user_id
                        sync_result = broker_manager.sync_all(db, uid)
                        _log.info("[startup] Broker sync user_id=%s: cb=%s, rh=%s, wl=%s",
                            uid,
                            sync_result.get("coinbase_positions"),
                            sync_result.get("robinhood_positions"),
                            sync_result.get("watchlist_added"),
                        )
        finally:
            db.close()
    except Exception:
        _log.debug("[startup] Coinbase auto-reconnect failed", exc_info=True)


def _start_massive_ws():
    """Start the Massive WebSocket client if configured (not in API-only ``none`` role)."""
    try:
        from .config import settings
        _role = (getattr(settings, "chili_scheduler_role", None) or "all").strip().lower()
        if _role == "none":
            return
        if settings.massive_api_key and settings.massive_use_websocket:
            from .services.massive_client import get_ws_client
            ws = get_ws_client()
            ws.start()
    except Exception:
        pass


def _start_price_bus():
    """Start the unified price bus if configured — bridges Massive + Coinbase WS."""
    _log = logging.getLogger("chili.startup")
    try:
        from .config import settings
        if not settings.chili_autopilot_price_bus_enabled:
            return
        from .services.trading.price_bus import get_price_bus
        bus = get_price_bus()
        bus.bridge_massive_ws()
        if settings.chili_coinbase_ws_enabled:
            from .services.trading.venue.coinbase_spot import get_coinbase_ws
            cb_ws = get_coinbase_ws()
            if not cb_ws._running:
                result = cb_ws.start()
                _log.info("[startup] Coinbase WS started for price bus: %s", result)
            bus.bridge_coinbase_ws()
        _log.info("[startup] Price bus started: %s", bus.describe())
    except Exception:
        _log.debug("[startup] Price bus start failed", exc_info=True)


def _stop_massive_ws():
    """Gracefully stop the Massive WebSocket client."""
    try:
        from .services.massive_client import get_ws_client
        ws = get_ws_client()
        ws.stop()
    except Exception:
        pass


def _prewarm_market_context():
    """Warm the market context cache in a background thread so the first
    AI Analyze call doesn't block on a cold 20-ticker scoring run."""
    import threading

    def _warm():
        try:
            from .services.trading.ai_context import build_market_context
            build_market_context(None, None)
        except Exception:
            pass

    threading.Thread(target=_warm, daemon=True).start()


app = FastAPI(title="CHILI Home Copilot", lifespan=lifespan)

_db_exc_log = logging.getLogger("chili.database")


@app.exception_handler(OperationalError)
async def _sqlalchemy_operational_error_handler(request: Request, exc: OperationalError):
    """Map DB saturation / transient errors to 503 JSON instead of opaque 500 text."""
    orig = getattr(exc, "orig", None)
    msg = str(orig) if orig is not None else str(exc)
    low = msg.lower()
    if "too many clients" in low:
        _db_exc_log.warning("postgres too_many_clients path=%s", request.url.path)
        return StarletteJSONResponse(
            status_code=503,
            content={
                "ok": False,
                "error": "postgres_too_many_clients",
                "detail": (
                    "PostgreSQL rejected a new connection. Close idle clients, "
                    "restart PostgreSQL, or raise max_connections."
                ),
            },
        )
    _db_exc_log.warning("database operational error path=%s msg=%s", request.url.path, msg[:400])
    return StarletteJSONResponse(
        status_code=503,
        content={"ok": False, "error": "database_unavailable", "detail": msg[:500]},
    )


# Session middleware (authlib OAuth stores nonce/state here)
from .config import settings as _cfg
app.add_middleware(SessionMiddleware, secret_key=_cfg.session_secret)

# CORS for web and mobile clients (development-friendly defaults).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

_uploads_dir = Path(__file__).resolve().parent.parent / "data" / "uploads"
_uploads_dir.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=_uploads_dir), name="uploads")

_projects_dir = Path(__file__).resolve().parent.parent / "data" / "projects"
_projects_dir.mkdir(parents=True, exist_ok=True)
app.mount("/project-files", StaticFiles(directory=_projects_dir), name="project-files")

_voice_dir = Path(__file__).resolve().parent.parent / "data" / "voice"
_voice_dir.mkdir(parents=True, exist_ok=True)
app.mount("/voice-files", StaticFiles(directory=_voice_dir), name="voice-files")

_templates_dir = Path(__file__).parent / "templates"
enabled_modules = load_enabled_modules()

base_loader = Jinja2Templates(directory=_templates_dir)

# If modules contribute their own template directories in the future we can
# extend this list; for now they re-use the core templates directory.
loader_list = [base_loader.env.loader]
app.state.templates = Jinja2Templates(directory=_templates_dir)
app.state.templates.env.loader = ChoiceLoader(loader_list)

# Navigation entries for optional modules (used by templates)
app.state.nav_modules = get_nav_modules()

app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(admin.router)
app.include_router(brain_project.router)
app.include_router(dev_terminal.router)
app.include_router(brain.router)
app.include_router(brain_v1_compat.router)
app.include_router(pages.router)
app.include_router(jobs.router)
app.include_router(health_routes.router)
app.include_router(marketplace.router)
app.include_router(mobile.router)
app.include_router(trading.router)

# Optional feature modules (planner, intercom, voice, projects, ...)
for mod in enabled_modules:
    if mod.router:
        app.include_router(mod.router)

# Third-party marketplace modules (installed under data/modules/).
# Same lock-contention issue as pattern seed: importing app while another process
# holds DB locks can block here indefinitely. Tests do not need dynamic modules.
if not _under_pytest:
    try:
        db = SessionLocal()
        try:
            enabled_third_party = (
                db.query(MarketplaceModule)
                .filter(MarketplaceModule.enabled.is_(True))
                .all()
            )
            for m in enabled_third_party:
                root = Path(m.local_path)
                if root.exists() and root.is_dir():
                    load_third_party_module(app, root)
        finally:
            db.close()
    except Exception:
        # Fail-soft on marketplace load; core app must still boot.
        pass

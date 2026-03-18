"""CHILI Home Copilot - FastAPI application entry point."""
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader

from .db import Base, SessionLocal, engine
from .migrations import run_migrations
from .routers import admin, auth, brain, chat, health_routes, pages, marketplace, mobile, trading
from .modules import get_nav_modules, load_enabled_modules, load_third_party_module
from .models import MarketplaceModule
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

Base.metadata.create_all(bind=engine)
run_migrations(engine)

try:
    from .services.trading.pattern_engine import seed_builtin_patterns as _seed_patterns
    _seed_db = SessionLocal()
    _seed_patterns(_seed_db)
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

            for ins_id in insight_ids:
                ins = db.get(TradingInsight, ins_id)
                if not ins:
                    continue
                bts = (
                    db.query(_BT)
                    .filter(_BT.related_insight_id == ins_id)
                    .all()
                )
                with_trades = [b for b in bts if (b.trade_count or 0) > 0]
                ins.win_count = sum(1 for b in with_trades if (b.return_pct or 0) > 0)
                ins.loss_count = len(with_trades) - ins.win_count
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


def _backfill_backtests():
    """Background: run smart backtests for insights that lack results or
    still have old generic-strategy backtests that need re-running with
    the unified DynamicPatternStrategy engine."""
    _log = logging.getLogger("chili.backfill")
    try:
        from .db import SessionLocal as _SL
        from .models.trading import TradingInsight, BacktestResult as _BT
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
                    continue

                if not existing:
                    stale.append(ins)
                    continue

                ctx = _extract_context(
                    ins.pattern_description or "", db=db, insight_id=ins.id,
                )
                if ctx["wants_crypto"]:
                    has_crypto_bt = any(t.endswith("-USD") for t in existing)
                    if not has_crypto_bt:
                        stale.append(ins)

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    import threading
    start_scheduler()
    try:
        from .services.trading.ml_engine import load_model
        load_model()
    except Exception:
        pass
    _restore_broker_sessions()
    _start_massive_ws()
    _prewarm_market_context()
    _dedup_backtests()
    _repair_wrongly_deactivated()
    threading.Thread(target=_backfill_backtests, daemon=True).start()
    yield
    _stop_massive_ws()
    stop_scheduler()


def _restore_broker_sessions():
    """Try to restore persisted Robinhood session on startup."""
    try:
        from .services import broker_service
        broker_service.try_restore_session()
    except Exception:
        pass


def _start_massive_ws():
    """Start the Massive WebSocket client if configured."""
    try:
        from .config import settings
        if settings.massive_api_key and settings.massive_use_websocket:
            from .services.massive_client import get_ws_client
            ws = get_ws_client()
            ws.start()
    except Exception:
        pass


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
app.include_router(brain.router)
app.include_router(pages.router)
app.include_router(health_routes.router)
app.include_router(marketplace.router)
app.include_router(mobile.router)
app.include_router(trading.router)

# Optional feature modules (planner, intercom, voice, projects, ...)
for mod in enabled_modules:
    if mod.router:
        app.include_router(mod.router)

# Third-party marketplace modules (installed under data/modules/).
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

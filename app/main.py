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


def _backfill_backtests():
    """Background: run real backtests for insights that have no win/loss data yet."""
    _log = logging.getLogger("chili.backfill")
    try:
        from .db import SessionLocal as _SL
        from .models.trading import TradingInsight
        from .services.backtest_service import run_backtest, save_backtest
        from .services.trading.market_data import DEFAULT_SCAN_TICKERS, DEFAULT_CRYPTO_TICKERS

        _STRATEGY_MAP = {
            "rsi": "rsi_reversal", "macd": "macd", "bollinger": "bb_bounce",
            "ema": "ema_cross", "sma": "sma_cross",
            "trend": "trend_follow", "momentum": "trend_follow",
        }
        tickers = list(DEFAULT_SCAN_TICKERS[:4]) + list(DEFAULT_CRYPTO_TICKERS[:1])

        from .models.trading import BacktestResult as _BT

        db = _SL()
        try:
            ids_with_linked_bts = {
                r[0] for r in db.query(_BT.related_insight_id).filter(
                    _BT.related_insight_id.isnot(None)
                ).distinct().all()
            }
            all_candidates = db.query(TradingInsight).filter(
                TradingInsight.evidence_count > 0,
            ).all()
            stale = [ins for ins in all_candidates if ins.id not in ids_with_linked_bts]

            if not stale:
                _log.info("No insights need backtest backfill")
                return

            _backfill_state["running"] = True
            _backfill_state["total"] = len(stale)
            _backfill_state["done"] = 0
            _backfill_state["filled"] = 0

            _log.info(f"Running backtest backfill for {len(stale)} insights...")
            for ins in stale:
                desc = ins.pattern_description.lower()
                strategy = "trend_follow"
                for kw, strat in _STRATEGY_MAP.items():
                    if kw in desc:
                        strategy = strat
                        break

                wins, losses = 0, 0
                for t in tickers:
                    try:
                        result = run_backtest(t, strategy_id=strategy, period="1y")
                        if result.get("ok") and result.get("trade_count", 0) > 0:
                            save_backtest(db, ins.user_id, result, insight_id=ins.id)
                            if result.get("return_pct", 0) > 0:
                                wins += 1
                            else:
                                losses += 1
                    except Exception:
                        continue

                if wins + losses > 0:
                    ins.win_count = (ins.win_count or 0) + wins
                    ins.loss_count = (ins.loss_count or 0) + losses
                    ins.evidence_count = (ins.evidence_count or 0) + wins + losses
                    db.commit()
                    _backfill_state["filled"] += 1

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

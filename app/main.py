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

# Backfill win_count/loss_count for existing insights that have evidence but no win/loss data
try:
    _bf_db = SessionLocal()
    from .models.trading import TradingInsight as _TI
    import re as _re
    _stale = _bf_db.query(_TI).filter(
        _TI.evidence_count > 0,
        (_TI.win_count == None) | (_TI.win_count == 0),  # noqa: E711
        (_TI.loss_count == None) | (_TI.loss_count == 0),  # noqa: E711
    ).all()
    _backfilled = 0
    for _ins in _stale:
        _m = _re.search(r"(\d+(?:\.\d+)?)%\s*win", _ins.pattern_description or "")
        if _m:
            _parsed_wr = float(_m.group(1))
            _n_match = _re.search(r"\bn[=:]?\s*(\d+)", _ins.pattern_description or "")
            _samples_match = _re.search(r"(\d+)\s*samples", _ins.pattern_description or "")
            if _n_match:
                _n = int(_n_match.group(1))
            elif _samples_match:
                _n = int(_samples_match.group(1))
            else:
                _n = min(_ins.evidence_count or 1, 20)
            _n = max(_n, 1)
            _ins.win_count = round(_parsed_wr / 100 * _n)
            _ins.loss_count = _n - _ins.win_count
            _backfilled += 1
    if _backfilled:
        _bf_db.commit()
        logging.getLogger("chili").info(f"Backfilled win/loss counts for {_backfilled} insights")
    _bf_db.close()
except Exception:
    pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    try:
        from .services.trading.ml_engine import load_model
        load_model()
    except Exception:
        pass
    _restore_broker_sessions()
    _start_massive_ws()
    _prewarm_market_context()
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

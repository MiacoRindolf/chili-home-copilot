"""CHILI Home Copilot - FastAPI application entry point."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader

from .db import Base, SessionLocal, engine
from .migrations import run_migrations
from .routers import admin, chat, health_routes, pages, marketplace, mobile, trading
from .modules import get_nav_modules, load_enabled_modules, load_third_party_module
from .models import MarketplaceModule
from .services.trading_scheduler import start_scheduler, stop_scheduler

Base.metadata.create_all(bind=engine)
run_migrations(engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    try:
        from .services.trading.ml_engine import load_model
        load_model()
    except Exception:
        pass
    yield
    stop_scheduler()


app = FastAPI(title="CHILI Home Copilot", lifespan=lifespan)

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

app.include_router(chat.router)
app.include_router(admin.router)
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

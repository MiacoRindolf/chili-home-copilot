"""CHILI Home Copilot - FastAPI application entry point."""
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .db import Base, engine
from .migrations import run_migrations
from .routers import chat, admin, pages, health_routes, intercom, projects, voice, planner

Base.metadata.create_all(bind=engine)
run_migrations(engine)

app = FastAPI(title="CHILI Home Copilot")

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

app.state.templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

app.include_router(chat.router)
app.include_router(admin.router)
app.include_router(pages.router)
app.include_router(health_routes.router)
app.include_router(intercom.router)
app.include_router(projects.router)
app.include_router(voice.router)
app.include_router(planner.router)

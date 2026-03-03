"""CHILI Home Copilot - FastAPI application entry point."""
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from .db import Base, engine
from .routers import chat, admin, pages, health_routes, intercom, projects

Base.metadata.create_all(bind=engine)

# Lightweight migration: add columns that may be missing on existing DBs
from sqlalchemy import inspect as sa_inspect, text
with engine.connect() as conn:
    user_cols = {c["name"] for c in sa_inspect(engine).get_columns("users")}
    if "email" not in user_cols:
        conn.execute(text("ALTER TABLE users ADD COLUMN email TEXT"))
        conn.commit()

    msg_cols = {c["name"] for c in sa_inspect(engine).get_columns("chat_messages")}
    if "image_path" not in msg_cols:
        conn.execute(text("ALTER TABLE chat_messages ADD COLUMN image_path TEXT"))
        conn.commit()

    existing_tables = set(sa_inspect(engine).get_table_names())
    if "conversations" in existing_tables:
        convo_cols = {c["name"] for c in sa_inspect(engine).get_columns("conversations")}
        if "project_id" not in convo_cols:
            conn.execute(text("ALTER TABLE conversations ADD COLUMN project_id INTEGER REFERENCES projects(id)"))
            conn.commit()

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
app.mount("/voice", StaticFiles(directory=_voice_dir), name="voice")

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

chat.init_templates(templates)
admin.init_templates(templates)
pages.init_templates(templates)
health_routes.init_templates(templates)
intercom.init_templates(templates)

app.include_router(chat.router)
app.include_router(admin.router)
app.include_router(pages.router)
app.include_router(health_routes.router)
app.include_router(intercom.router)
app.include_router(projects.router)

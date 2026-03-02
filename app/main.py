"""CHILI Home Copilot - FastAPI application entry point."""
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from .db import Base, engine
from .routers import chat, admin, pages, health_routes

Base.metadata.create_all(bind=engine)

app = FastAPI(title="CHILI Home Copilot")

app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

chat.init_templates(templates)
admin.init_templates(templates)
pages.init_templates(templates)

app.include_router(chat.router)
app.include_router(admin.router)
app.include_router(pages.router)
app.include_router(health_routes.router)

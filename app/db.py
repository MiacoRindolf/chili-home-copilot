import os
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from .config import settings

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DATA_DIR.mkdir(exist_ok=True)

# PostgreSQL only — DATABASE_URL validated in config (see .env.example).
DATABASE_URL = settings.database_url

# Process-pool queue workers set CHILI_MP_BACKTEST_CHILD before first db import (see backtest_queue_worker).
_mp_child = os.environ.get("CHILI_MP_BACKTEST_CHILD", "").strip().lower() in ("1", "true", "yes")
_pool_size = (
    settings.brain_mp_child_database_pool_size
    if _mp_child
    else settings.database_pool_size
)
_max_overflow = (
    settings.brain_mp_child_database_max_overflow
    if _mp_child
    else settings.database_max_overflow
)

engine = create_engine(
    DATABASE_URL,
    pool_size=_pool_size,
    max_overflow=_max_overflow,
    pool_pre_ping=True,  # detect stale connections
    pool_recycle=3600,  # avoid stale server-side disconnects on long-lived CHILI + worker
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

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
    pool_pre_ping=True,  # detect stale connections at checkout
    pool_recycle=3600,  # recycle at checkout if older than 1h
    # FIX 13+14 (deep audit 2026-04-28): keep-alives at the TCP level so a
    # long-running brain-worker learning cycle (~34min hold) can't have its
    # connection silently closed by the server. The brain-worker grabs a
    # session, runs through 20+ steps, and the snapshot SELECT (LIMIT 5000)
    # is the long pole — without keepalives, postgres closes the idle TCP
    # socket and the next query fails with 'server closed the connection
    # unexpectedly'. The cycle then rolls back, the predictions cache fails
    # to emit ('Promoted prediction cache at cycle end failed'), and the
    # next consumer reads cached_result_count=0.
    #
    # 30s keepalive_idle is well below typical net.ipv4.tcp_keepalive_time
    # (default 7200s on Linux) so we don't depend on OS defaults; 5s
    # keepalives_interval gives 6 keepalives before tcp_keepalives_count
    # gives up — the connection stays alive even when fully idle.
    connect_args={
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 5,
        "keepalives_count": 5,
    },
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

if _mp_child:
    import atexit

    def _dispose_mp_child_engine() -> None:
        # Return pooled connections to Postgres when a queue worker process exits (spawn child).
        try:
            engine.dispose()
        except Exception:
            pass

    atexit.register(_dispose_mp_child_engine)

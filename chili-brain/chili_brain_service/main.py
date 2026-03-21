"""Brain HTTP service — delegates to `app.services.trading.learning` until code is extracted."""
from __future__ import annotations

import os
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

INTERNAL_SECRET = os.environ.get("CHILI_BRAIN_INTERNAL_SECRET", "").strip()

app = FastAPI(
    title="CHILI Brain Service",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)


def _internal_auth(authorization: str | None = Header(None)) -> None:
    if not INTERNAL_SECRET:
        return
    if (authorization or "").strip() != f"Bearer {INTERNAL_SECRET}":
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "chili-brain"}


@app.post("/v1/run-learning-cycle", dependencies=[Depends(_internal_auth)])
def run_learning_cycle_http() -> dict[str, Any]:
    """Run the full trading learning cycle (same as brain worker in-process)."""
    from app.db import SessionLocal
    from app.services.trading.learning import run_learning_cycle

    db = SessionLocal()
    try:
        return run_learning_cycle(db, user_id=None, full_universe=True)
    finally:
        db.close()


@app.get("/v1/capabilities")
def capabilities() -> dict[str, Any]:
    return {
        "implemented": ["run_learning_cycle"],
        "planned": [
            "code_learning_cycle",
            "reasoning_cycle",
            "project_brain_cycle",
        ],
    }


# --- Placeholder routes for future extraction (explicit 501) ---


@app.post("/v1/run-code-learning-cycle", dependencies=[Depends(_internal_auth)])
def code_learning_not_implemented() -> JSONResponse:
    return JSONResponse(
        status_code=501,
        content={
            "ok": False,
            "detail": "Not moved to Brain service yet — still served by CHILI scheduler",
        },
    )


@app.post("/v1/run-reasoning-cycle", dependencies=[Depends(_internal_auth)])
def reasoning_not_implemented() -> JSONResponse:
    return JSONResponse(
        status_code=501,
        content={
            "ok": False,
            "detail": "Not moved to Brain service yet — still served by CHILI scheduler",
        },
    )


@app.post("/v1/run-project-brain-cycle", dependencies=[Depends(_internal_auth)])
def project_brain_not_implemented() -> JSONResponse:
    return JSONResponse(
        status_code=501,
        content={
            "ok": False,
            "detail": "Not moved to Brain service yet — still served by CHILI scheduler",
        },
    )

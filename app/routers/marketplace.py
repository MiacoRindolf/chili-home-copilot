"""Marketplace routes: page + REST APIs for module install/enable/disable/uninstall."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..deps import get_db, get_identity_ctx
from ..logger import log_error, log_info
from ..services import marketplace_service

router = APIRouter(tags=["marketplace"])


class ModuleSlugBody(BaseModel):
    slug: str = Field(..., min_length=1, max_length=255, pattern=r"^[a-z0-9_-]+$")


# ── Page ────────────────────────────────────────────────────────────────


@router.get("/marketplace", response_class=HTMLResponse)
def marketplace_page(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    if ctx["is_guest"]:
        return request.app.state.templates.TemplateResponse(
            request, "pair_required.html",
            {"title": "Marketplace"},
        )

    return request.app.state.templates.TemplateResponse(
        request, "marketplace.html",
        {"title": "Marketplace", "is_guest": ctx["is_guest"]},
    )


# ── List ────────────────────────────────────────────────────────────────


@router.get("/api/marketplace/modules", response_class=JSONResponse)
def api_list_modules(request: Request, db: Session = Depends(get_db)):
    trace_id = request.headers.get("x-trace-id", "marketplace_list")
    items = marketplace_service.list_registry_with_status(db, trace_id)
    return items


# ── Install ─────────────────────────────────────────────────────────────


@router.post("/api/marketplace/install", response_class=JSONResponse)
def api_install_module(
    body: ModuleSlugBody,
    request: Request,
    db: Session = Depends(get_db),
):
    ctx = get_identity_ctx(request, db)
    if ctx["is_guest"]:
        raise HTTPException(status_code=403, detail="Pairing required")

    trace_id = request.headers.get("x-trace-id", "marketplace_install")

    try:
        mod, installed_now = marketplace_service.install_from_registry(
            db, body.slug, trace_id,
        )
        log_info(trace_id, f"module_installed slug={body.slug} installed_now={installed_now}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        log_error(trace_id, f"module_install_failed slug={body.slug} error={exc}")
        raise HTTPException(status_code=500, detail="Installation failed. Check server logs.")

    return {
        "slug": mod.slug,
        "name": mod.name,
        "summary": mod.summary,
        "version": mod.version,
        "enabled": bool(mod.enabled),
        "installed": True,
        "local_path": mod.local_path,
    }


# ── Enable / Disable ───────────────────────────────────────────────────


@router.post("/api/marketplace/enable", response_class=JSONResponse)
def api_enable_module(
    body: ModuleSlugBody,
    request: Request,
    db: Session = Depends(get_db),
):
    ctx = get_identity_ctx(request, db)
    if ctx["is_guest"]:
        raise HTTPException(status_code=403, detail="Pairing required")

    trace_id = request.headers.get("x-trace-id", "marketplace_enable")
    try:
        mod = marketplace_service.set_enabled(db, body.slug, True)
        log_info(trace_id, f"module_enabled slug={body.slug}")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        log_error(trace_id, f"module_enable_failed slug={body.slug} error={exc}")
        raise HTTPException(status_code=500, detail="Could not enable module.")

    return {"slug": mod.slug, "enabled": bool(mod.enabled)}


@router.post("/api/marketplace/disable", response_class=JSONResponse)
def api_disable_module(
    body: ModuleSlugBody,
    request: Request,
    db: Session = Depends(get_db),
):
    ctx = get_identity_ctx(request, db)
    if ctx["is_guest"]:
        raise HTTPException(status_code=403, detail="Pairing required")

    trace_id = request.headers.get("x-trace-id", "marketplace_disable")
    try:
        mod = marketplace_service.set_enabled(db, body.slug, False)
        log_info(trace_id, f"module_disabled slug={body.slug}")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        log_error(trace_id, f"module_disable_failed slug={body.slug} error={exc}")
        raise HTTPException(status_code=500, detail="Could not disable module.")

    return {"slug": mod.slug, "enabled": bool(mod.enabled)}


# ── Uninstall ───────────────────────────────────────────────────────────


@router.delete("/api/marketplace/modules/{slug}", status_code=204)
def api_uninstall_module(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
):
    ctx = get_identity_ctx(request, db)
    if ctx["is_guest"]:
        raise HTTPException(status_code=403, detail="Pairing required")

    trace_id = request.headers.get("x-trace-id", "marketplace_uninstall")
    try:
        marketplace_service.uninstall(db, slug)
        log_info(trace_id, f"module_uninstalled slug={slug}")
    except Exception as exc:
        log_error(trace_id, f"module_uninstall_failed slug={slug} error={exc}")

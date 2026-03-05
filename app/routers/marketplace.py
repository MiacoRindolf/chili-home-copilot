from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from ..deps import get_db, get_identity_ctx
from ..logger import log_error, log_info
from ..services import marketplace_service

router = APIRouter(tags=["marketplace"])


@router.get("/marketplace", response_class=HTMLResponse)
def marketplace_page(request: Request, db: Session = Depends(get_db)):
    ctx = get_identity_ctx(request, db)
    if ctx["is_guest"]:
        return request.app.state.templates.TemplateResponse(
            "pair_required.html",
            {
                "request": request,
                "title": "Marketplace",
            },
        )

    return request.app.state.templates.TemplateResponse(
        "marketplace.html",
        {
            "request": request,
            "title": "Marketplace",
            "is_guest": ctx["is_guest"],
        },
    )


@router.get("/api/marketplace/modules", response_class=JSONResponse)
def api_list_modules(request: Request, db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    trace_id = request.headers.get("x-trace-id", "marketplace_list")
    items = marketplace_service.list_registry_with_status(db, trace_id)
    return items


@router.post("/api/marketplace/install", response_class=JSONResponse)
def api_install_module(
    request: Request,
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    trace_id = request.headers.get("x-trace-id", "marketplace_install")
    slug = str(payload.get("slug", "")).strip()
    if not slug:
        raise HTTPException(status_code=400, detail="Missing slug")

    try:
        mod, installed_now = marketplace_service.install_from_registry(db, slug, trace_id)
        log_info(trace_id, f"module_installed slug={slug} installed_now={installed_now}")
    except Exception as exc:
        log_error(trace_id, f"module_install_failed slug={slug} error={exc}")
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "slug": mod.slug,
        "name": mod.name,
        "summary": mod.summary,
        "version": mod.version,
        "enabled": bool(mod.enabled),
        "installed": True,
        "local_path": mod.local_path,
    }


@router.post("/api/marketplace/enable", response_class=JSONResponse)
def api_enable_module(
    request: Request,
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    trace_id = request.headers.get("x-trace-id", "marketplace_enable")
    slug = str(payload.get("slug", "")).strip()
    if not slug:
        raise HTTPException(status_code=400, detail="Missing slug")

    try:
        mod = marketplace_service.set_enabled(db, slug, True)
        log_info(trace_id, f"module_enabled slug={slug}")
    except Exception as exc:
        log_error(trace_id, f"module_enable_failed slug={slug} error={exc}")
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "slug": mod.slug,
        "enabled": bool(mod.enabled),
    }


@router.post("/api/marketplace/disable", response_class=JSONResponse)
def api_disable_module(
    request: Request,
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    trace_id = request.headers.get("x-trace-id", "marketplace_disable")
    slug = str(payload.get("slug", "")).strip()
    if not slug:
        raise HTTPException(status_code=400, detail="Missing slug")

    try:
        mod = marketplace_service.set_enabled(db, slug, False)
        log_info(trace_id, f"module_disabled slug={slug}")
    except Exception as exc:
        log_error(trace_id, f"module_disable_failed slug={slug} error={exc}")
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "slug": mod.slug,
        "enabled": bool(mod.enabled),
    }


@router.delete("/api/marketplace/modules/{slug}", status_code=204)
def api_uninstall_module(
    request: Request,
    slug: str,
    db: Session = Depends(get_db),
) -> None:
    trace_id = request.headers.get("x-trace-id", "marketplace_uninstall")
    try:
        marketplace_service.uninstall(db, slug)
        log_info(trace_id, f"module_uninstalled slug={slug}")
    except Exception as exc:
        log_error(trace_id, f"module_uninstall_failed slug={slug} error={exc}")
        # Best-effort uninstall; return 204 regardless to keep UX simple.
        return


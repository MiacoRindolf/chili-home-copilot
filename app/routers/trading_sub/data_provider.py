"""Data-provider status API (split from monolithic ``trading`` router)."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(tags=["trading"])


@router.get("/api/trading/data-provider/status")
def api_data_provider_status():
    """Return data-provider usage metrics and feature-flag status."""
    from ...config import settings

    massive_enabled = bool(settings.massive_api_key)
    massive_metrics = {}
    if massive_enabled:
        try:
            from ...services.massive_client import get_metrics as get_massive_metrics
            massive_metrics = get_massive_metrics()
        except Exception:
            pass

    polygon_enabled = settings.use_polygon and bool(settings.polygon_api_key)
    polygon_metrics = {}
    if polygon_enabled:
        try:
            from ...services.polygon_client import get_metrics
            polygon_metrics = get_metrics()
        except Exception:
            pass

    return JSONResponse({
        "massive_enabled": massive_enabled,
        "massive_base_url": settings.massive_base_url if massive_enabled else None,
        "massive_websocket": settings.massive_use_websocket if massive_enabled else False,
        "massive_metrics": massive_metrics,
        "polygon_enabled": polygon_enabled,
        "polygon_base_url": settings.polygon_base_url if polygon_enabled else None,
        "polygon_metrics": polygon_metrics,
        "provider_order": [
            p for p, enabled in [
                ("massive", massive_enabled),
                ("polygon", polygon_enabled),
                ("yfinance", True),
            ] if enabled
        ],
    })

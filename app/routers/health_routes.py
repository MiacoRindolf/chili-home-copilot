"""Health and metrics routes."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, HTMLResponse
from sqlalchemy.orm import Session

from ..deps import get_db
from ..health import check_db, check_ollama
from ..metrics import (
    latency_stats, latency_history, get_counts, model_stats,
    total_stats, messages_per_day, hourly_activity, action_type_stats,
    feature_usage, response_time_trend, conversation_stats, top_users,
    rag_stats,
)
from .. import openai_client

router = APIRouter()


@router.get("/health", response_class=JSONResponse)
def health(db: Session = Depends(get_db)):
    db_status = check_db(db)
    ollama_status = check_ollama()
    overall_ok = db_status.get("ok") and ollama_status.get("ok")
    return {"ok": bool(overall_ok), "db": db_status, "ollama": ollama_status}


@router.get("/metrics", response_class=HTMLResponse)
def metrics_page(request: Request, db: Session = Depends(get_db)):
    """Serve the metrics dashboard."""
    return request.app.state.templates.TemplateResponse(request, "metrics.html", {})


@router.get("/api/metrics", response_class=JSONResponse)
def metrics_api(db: Session = Depends(get_db)):
    """JSON API for all metrics data — consumed by the dashboard via AJAX."""
    db_status = check_db(db)
    ollama_status = check_ollama()

    ms = model_stats(db)
    ts = total_stats(db)
    lat = latency_stats()
    counts = get_counts(db)
    features = feature_usage(db)
    convos = conversation_stats(db)
    top = top_users(db, limit=5)
    daily = messages_per_day(db, days=14)
    hourly = hourly_activity(db, days=7)
    lat_history = latency_history()
    actions = action_type_stats(db)
    rs = rag_stats()

    return {
        "system": {
            "db": db_status,
            "ollama": ollama_status,
            "openai_configured": openai_client.is_configured(),
            "openai_model": openai_client.LLM_MODEL,
            "premium_model": openai_client.PREMIUM_MODEL,
        },
        "totals": ts,
        "counts": counts,
        "latency": lat,
        "latency_history": lat_history,
        "model_usage": ms,
        "action_types": actions,
        "features": features,
        "conversations": convos,
        "top_users": top,
        "messages_per_day": daily,
        "hourly_activity": hourly,
        "rag": rs,
    }

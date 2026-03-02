"""Health and metrics routes."""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..deps import get_db
from ..health import check_db, check_ollama
from ..metrics import record_latency, latency_stats, get_counts, model_stats

router = APIRouter()


@router.get("/health", response_class=JSONResponse)
def health(db: Session = Depends(get_db)):
    db_status = check_db(db)
    ollama_status = check_ollama()
    overall_ok = db_status.get("ok") and ollama_status.get("ok")
    return {"ok": bool(overall_ok), "db": db_status, "ollama": ollama_status}


@router.get("/metrics", response_class=JSONResponse)
def metrics(db: Session = Depends(get_db)):
    return {
        "counts": get_counts(db),
        "llm_chat_latency": latency_stats(),
        "model_usage": model_stats(db),
    }

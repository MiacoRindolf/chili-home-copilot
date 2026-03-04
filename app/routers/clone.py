"""Clone data-collection routes: Q&A page, answer API, progress API, export.

TEMPORARY — this module exists only for collecting training data and will be
removed once the data has been exported to the standalone clone project.
"""
from fastapi import APIRouter, Depends, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
import io, csv, json

from ..deps import get_db
from ..pairing import DEVICE_COOKIE_NAME, get_identity_record
from ..services import clone_service

router = APIRouter()
templates = None


def init_templates(t: Jinja2Templates):
    global templates
    templates = t


def _require_user(request: Request, db: Session) -> dict | None:
    token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, token)
    if identity["is_guest"] or not identity.get("user_id"):
        return None
    return identity


@router.get("/clone", response_class=HTMLResponse)
def clone_page(request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    user_name = identity["user_name"] if identity else "Guest"
    is_guest = identity is None
    return templates.TemplateResponse(request, "clone.html", {
        "user_name": user_name,
        "is_guest": is_guest,
    })


@router.get("/api/clone/questions", response_class=JSONResponse)
def list_questions(request: Request, db: Session = Depends(get_db)):
    """Return the full question bank with the user's existing answers."""
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Pair your device first"}, status_code=403)

    profile = clone_service.get_or_create_profile(db, identity["user_id"])
    progress = clone_service.get_progress(db, profile.id)

    questions = []
    for q in clone_service.QUESTION_BANK:
        existing = clone_service.get_answer_for_key(db, profile.id, q["key"])
        questions.append({
            **q,
            "answered": existing is not None,
            "existing_answer": existing,
        })

    return {
        "questions": questions,
        "progress": progress,
    }


@router.post("/api/clone/answer", response_class=JSONResponse)
async def submit_answer(request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Pair your device first"}, status_code=403)

    body = await request.json()
    question_key = body.get("question_key", "")
    selected = body.get("selected_options", [])
    freeform = body.get("freeform", "")

    if not question_key:
        return JSONResponse({"error": "question_key required"}, status_code=400)
    if not selected and not freeform.strip():
        return JSONResponse({"error": "Select at least one option or type an answer"}, status_code=400)

    profile = clone_service.get_or_create_profile(db, identity["user_id"])

    try:
        clone_service.save_answer(db, profile.id, question_key, selected, freeform)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    progress = clone_service.get_progress(db, profile.id)
    next_q = clone_service.get_next_unanswered(db, profile.id, after_key=question_key)

    return {
        "ok": True,
        "progress": progress,
        "next_question_key": next_q["key"] if next_q else None,
    }


@router.get("/api/clone/progress", response_class=JSONResponse)
def progress_api(request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Pair your device first"}, status_code=403)

    profile = clone_service.get_or_create_profile(db, identity["user_id"])
    return clone_service.get_progress(db, profile.id)


@router.get("/api/clone/answers", response_class=JSONResponse)
def all_answers(request: Request, db: Session = Depends(get_db)):
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Pair your device first"}, status_code=403)

    profile = clone_service.get_or_create_profile(db, identity["user_id"])
    return {"answers": clone_service.get_all_answers(db, profile.id)}


@router.get("/api/clone/export")
def export_data(
    request: Request,
    fmt: str = Query("json", pattern="^(json|csv)$"),
    db: Session = Depends(get_db),
):
    """Export all Q&A data for use in the standalone clone project."""
    identity = _require_user(request, db)
    if not identity:
        return JSONResponse({"error": "Pair your device first"}, status_code=403)

    profile = clone_service.get_or_create_profile(db, identity["user_id"])
    answers = clone_service.get_all_answers(db, profile.id)
    user_name = identity["user_name"]

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["category", "question", "selected_options", "freeform", "updated_at"])
        for a in answers:
            writer.writerow([
                a["category"],
                a["question"],
                "; ".join(a["selected_labels"]),
                a["freeform"],
                a["updated_at"],
            ])
        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=clone-data-{user_name}.csv"},
        )

    payload = {
        "user": user_name,
        "exported_at": __import__("datetime").datetime.utcnow().isoformat(),
        "total_answers": len(answers),
        "answers": answers,
    }
    content = json.dumps(payload, indent=2)
    return StreamingResponse(
        iter([content]),
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename=clone-data-{user_name}.json"},
    )

"""Project Space routes: CRUD for projects, file management, conversation assignment."""
from fastapi import APIRouter, Depends, Request, File, UploadFile, Form
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from typing import Optional

from ..deps import get_db
from ..models import Project, ProjectFile, Conversation
from ..pairing import DEVICE_COOKIE_NAME, get_identity_record
from ..deps import get_convo_key
from ..logger import new_trace_id, log_info
from ..services import project_file_service as pfs

router = APIRouter()


def _get_user_id(request: Request, db: Session) -> Optional[int]:
    """Extract user_id from cookie. Returns None for guests."""
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)
    if identity["is_guest"]:
        return None
    return identity.get("user_id")


def _require_user(request: Request, db: Session):
    """Return user_id or raise a 403."""
    uid = _get_user_id(request, db)
    if uid is None:
        return None
    return uid


# ── Project CRUD ─────────────────────────────────────────────────────────────

@router.get("/api/projects", response_class=JSONResponse)
def list_projects(request: Request, db: Session = Depends(get_db)):
    uid = _get_user_id(request, db)
    if uid is None:
        return {"projects": []}

    projects = (
        db.query(Project)
        .filter(Project.user_id == uid)
        .order_by(Project.updated_at.desc())
        .all()
    )
    result = []
    for p in projects:
        file_count = db.query(ProjectFile).filter(ProjectFile.project_id == p.id).count()
        convo_count = db.query(Conversation).filter(Conversation.project_id == p.id).count()
        result.append({
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "color": p.color,
            "file_count": file_count,
            "convo_count": convo_count,
            "created_at": str(p.created_at),
            "updated_at": str(p.updated_at),
        })
    return {"projects": result}


@router.post("/api/projects", response_class=JSONResponse)
def create_project(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    color: str = Form("#6366f1"),
    db: Session = Depends(get_db),
):
    uid = _get_user_id(request, db)
    if uid is None:
        return JSONResponse({"error": "Guests cannot create projects"}, status_code=403)

    project = Project(user_id=uid, name=name.strip(), description=description.strip() or None, color=color)
    db.add(project)
    db.commit()
    db.refresh(project)
    log_info("project", f"created project_id={project.id} user={uid} name={project.name!r}")
    return {
        "id": project.id, "name": project.name, "description": project.description,
        "color": project.color, "file_count": 0, "convo_count": 0,
        "created_at": str(project.created_at), "updated_at": str(project.updated_at),
    }


@router.put("/api/projects/{project_id}", response_class=JSONResponse)
def update_project(
    project_id: int,
    request: Request,
    name: str = Form(None),
    description: str = Form(None),
    color: str = Form(None),
    db: Session = Depends(get_db),
):
    uid = _get_user_id(request, db)
    if uid is None:
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    project = db.query(Project).filter(Project.id == project_id, Project.user_id == uid).first()
    if not project:
        return JSONResponse({"error": "Not found"}, status_code=404)

    if name is not None:
        project.name = name.strip()
    if description is not None:
        project.description = description.strip() or None
    if color is not None:
        project.color = color
    db.commit()
    db.refresh(project)
    return {"ok": True, "name": project.name, "description": project.description, "color": project.color}


@router.delete("/api/projects/{project_id}", response_class=JSONResponse)
def delete_project(project_id: int, request: Request, db: Session = Depends(get_db)):
    uid = _get_user_id(request, db)
    if uid is None:
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    project = db.query(Project).filter(Project.id == project_id, Project.user_id == uid).first()
    if not project:
        return JSONResponse({"error": "Not found"}, status_code=404)

    # Unlink conversations (don't delete them)
    db.query(Conversation).filter(Conversation.project_id == project_id).update({"project_id": None})

    trace_id = new_trace_id()
    pfs.remove_project_collection(project_id, trace_id)
    db.delete(project)
    db.commit()
    log_info(trace_id, f"deleted project_id={project_id} user={uid}")
    return {"ok": True}


# ── Project Files ────────────────────────────────────────────────────────────

@router.get("/api/projects/{project_id}/files", response_class=JSONResponse)
def list_project_files(project_id: int, request: Request, db: Session = Depends(get_db)):
    uid = _get_user_id(request, db)
    if uid is None:
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    project = db.query(Project).filter(Project.id == project_id, Project.user_id == uid).first()
    if not project:
        return JSONResponse({"error": "Not found"}, status_code=404)

    files = db.query(ProjectFile).filter(ProjectFile.project_id == project_id).order_by(ProjectFile.created_at.desc()).all()
    return {
        "files": [
            {
                "id": f.id,
                "original_name": f.original_name,
                "content_type": f.content_type,
                "file_size": f.file_size,
                "created_at": str(f.created_at),
            }
            for f in files
        ]
    }


@router.post("/api/projects/{project_id}/files", response_class=JSONResponse)
async def upload_project_file(
    project_id: int,
    request: Request,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    uid = _get_user_id(request, db)
    if uid is None:
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    project = db.query(Project).filter(Project.id == project_id, Project.user_id == uid).first()
    if not project:
        return JSONResponse({"error": "Not found"}, status_code=404)

    trace_id = new_trace_id()
    results = []

    for file in files:
        if not file or not file.filename:
            continue

        file_bytes = await file.read()
        err = pfs.validate_file(file.filename, len(file_bytes))
        if err:
            results.append({"name": file.filename, "ok": False, "error": err})
            continue

        pf = pfs.save_file(project_id, file_bytes, file.filename, file.content_type or "application/octet-stream", db)
        ingest_result = pfs.ingest_file(project_id, pf, trace_id)
        results.append({
            "name": file.filename,
            "ok": True,
            "id": pf.id,
            "chunks": ingest_result.get("chunks", 0),
        })
        log_info(trace_id, f"uploaded file={file.filename} project={project_id} size={len(file_bytes)}")

    return {"results": results}


@router.delete("/api/projects/{project_id}/files/{file_id}", response_class=JSONResponse)
def delete_project_file(project_id: int, file_id: int, request: Request, db: Session = Depends(get_db)):
    uid = _get_user_id(request, db)
    if uid is None:
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    project = db.query(Project).filter(Project.id == project_id, Project.user_id == uid).first()
    if not project:
        return JSONResponse({"error": "Not found"}, status_code=404)

    pf = db.query(ProjectFile).filter(ProjectFile.id == file_id, ProjectFile.project_id == project_id).first()
    if not pf:
        return JSONResponse({"error": "File not found"}, status_code=404)

    trace_id = new_trace_id()
    pfs.remove_file(project_id, pf, db, trace_id)
    return {"ok": True}


# ── Conversation Assignment ──────────────────────────────────────────────────

@router.post("/api/projects/{project_id}/conversations/{convo_id}", response_class=JSONResponse)
def assign_conversation(project_id: int, convo_id: int, request: Request, db: Session = Depends(get_db)):
    uid = _get_user_id(request, db)
    if uid is None:
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    project = db.query(Project).filter(Project.id == project_id, Project.user_id == uid).first()
    if not project:
        return JSONResponse({"error": "Project not found"}, status_code=404)

    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)
    convo_key = get_convo_key(identity, device_token, request.client.host)

    convo = db.query(Conversation).filter(Conversation.id == convo_id, Conversation.convo_key == convo_key).first()
    if not convo:
        return JSONResponse({"error": "Conversation not found"}, status_code=404)

    convo.project_id = project_id
    db.commit()
    return {"ok": True}


@router.delete("/api/projects/{project_id}/conversations/{convo_id}", response_class=JSONResponse)
def unassign_conversation(project_id: int, convo_id: int, request: Request, db: Session = Depends(get_db)):
    uid = _get_user_id(request, db)
    if uid is None:
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)
    convo_key = get_convo_key(identity, device_token, request.client.host)

    convo = db.query(Conversation).filter(
        Conversation.id == convo_id,
        Conversation.convo_key == convo_key,
        Conversation.project_id == project_id,
    ).first()
    if not convo:
        return JSONResponse({"error": "Not found"}, status_code=404)

    convo.project_id = None
    db.commit()
    return {"ok": True}

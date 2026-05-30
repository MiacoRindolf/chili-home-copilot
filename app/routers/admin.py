"""Admin routes: dashboard, user management, pairing, exports."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from sqlalchemy.orm import Session
import csv
import io
import json

from ..deps import get_db, require_paired
from ..models import Chore, Birthday, ChatLog, User, Device, HousemateProfile, UserMemory
from ..health import check_db, check_ollama
from ..metrics import (
    latency_stats, get_counts, model_stats, total_stats, user_stats, rag_stats,
    admin_dashboard_json, messages_per_day, hourly_activity, feature_usage,
    per_user_chore_stats, system_alerts, response_time_trend, top_users,
    conversation_stats, action_type_stats, latency_history,
)
from .. import openai_client
from ..pairing import generate_pair_code

router = APIRouter()


def _guard(ctx):
    """Return a redirect response if the user is a guest, else None."""
    if ctx is None:
        return RedirectResponse("/chat", status_code=303)
    return None


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@router.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, ctx=Depends(require_paired)):
    redirect = _guard(ctx)
    if redirect:
        return redirect

    db: Session = ctx["db"]
    dashboard = admin_dashboard_json(db)

    logs = db.query(ChatLog).order_by(ChatLog.id.desc()).limit(200).all()
    log_rows = [
        {
            "time": l.created_at.strftime("%Y-%m-%d %H:%M:%S") if l.created_at else "—",
            "ip": l.client_ip,
            "action": l.action_type,
            "trace_id": l.trace_id,
            "message": (l.message[:80] + "...") if len(l.message) > 80 else l.message,
        }
        for l in logs
    ]
    action_types = sorted(set(r["action"] for r in log_rows))

    return request.app.state.templates.TemplateResponse(request, "admin.html", {
        "user_name": ctx["user_name"],
        "dashboard_json": json.dumps(dashboard),
        "log_rows": log_rows,
        "action_types": action_types,
    })


@router.get("/api/admin/dashboard")
def api_admin_dashboard(ctx=Depends(require_paired)):
    redirect = _guard(ctx)
    if redirect:
        return redirect
    db: Session = ctx["db"]
    return JSONResponse(admin_dashboard_json(db))


@router.get("/api/admin/alerts")
def api_admin_alerts(ctx=Depends(require_paired)):
    redirect = _guard(ctx)
    if redirect:
        return redirect
    db: Session = ctx["db"]
    return JSONResponse(system_alerts(db))


@router.get("/api/admin/logs")
def api_admin_logs(ctx=Depends(require_paired), limit: int = 200):
    redirect = _guard(ctx)
    if redirect:
        return redirect
    db: Session = ctx["db"]
    logs = db.query(ChatLog).order_by(ChatLog.id.desc()).limit(min(limit, 500)).all()
    return JSONResponse([
        {
            "time": l.created_at.strftime("%Y-%m-%d %H:%M:%S") if l.created_at else "—",
            "ip": l.client_ip,
            "action": l.action_type,
            "trace_id": l.trace_id,
            "message": (l.message[:80] + "...") if len(l.message) > 80 else l.message,
        }
        for l in logs
    ])


# ---------------------------------------------------------------------------
# Bracket pending-decision admin endpoint (bracket-writer-respect-upside-targets, 2026-05-04)
# ---------------------------------------------------------------------------
#
# Operator-input mechanism for pending bracket decisions surfaced by the
# bracket writer's covered-by-existing-sell branch. The writer persists a
# pending_decision row into trading_bracket_intents.payload_json with an
# options list (keep_target / replace_with_stop / [convert_to_trailing_stop
# if helper exists]). The reconciler sees operator_choice on next sweep
# and routes to the corresponding resolution path.
#
# Request body shape:
#   {"choice": "keep_target" | "replace_with_stop" | "convert_to_trailing_stop"}
#
# Response: 200 with the updated bracket_intent JSON (subset). 4xx on
# unknown intent_id, 4xx on choice not in current options list.
#
# This is the data-only stub. UI (autopilot settings page) is Phase 7 of
# the broader initiative; not in this task.

@router.post("/api/admin/bracket-decisions/{bracket_intent_id}")
async def api_admin_bracket_decision(
    bracket_intent_id: int,
    request: Request,
    ctx=Depends(require_paired),
):
    redirect = _guard(ctx)
    if redirect:
        return redirect
    db: Session = ctx["db"]

    from sqlalchemy import text as _sql_text
    import json as _json

    # Async body read so FastAPI's running loop isn't violated.
    try:
        body_bytes = await request.body()
    except Exception:
        body_bytes = b""
    try:
        body = _json.loads(body_bytes.decode("utf-8") or "{}")
    except Exception:
        body = {}

    choice = (body.get("choice") or "").strip()
    if not choice:
        return JSONResponse(
            {"ok": False, "error": "missing_choice"}, status_code=400,
        )

    row = db.execute(_sql_text(
        "SELECT id, trade_id, ticker, intent_state, payload_json "
        "FROM trading_bracket_intents WHERE id = :iid"
    ), {"iid": int(bracket_intent_id)}).first()
    if row is None:
        return JSONResponse(
            {"ok": False, "error": "intent_not_found"}, status_code=404,
        )

    payload = row[4] if row[4] is not None else {}
    pending = payload.get("pending_decision") if isinstance(payload, dict) else None
    if not isinstance(pending, dict):
        return JSONResponse(
            {"ok": False, "error": "no_pending_decision"}, status_code=400,
        )

    valid_choices = {
        opt.get("choice") for opt in (pending.get("options") or [])
        if isinstance(opt, dict)
    }
    if choice not in valid_choices:
        return JSONResponse(
            {
                "ok": False,
                "error": "invalid_choice",
                "valid_choices": sorted(valid_choices),
            },
            status_code=400,
        )

    db.execute(_sql_text(
        "UPDATE trading_bracket_intents "
        "SET payload_json = jsonb_set("
        "  COALESCE(payload_json, '{}'::jsonb), "
        "  '{pending_decision,operator_choice}', "
        "  to_jsonb(CAST(:choice AS TEXT))"
        "), "
        "updated_at = NOW() "
        "WHERE id = :iid"
    ), {"iid": int(bracket_intent_id), "choice": choice})
    db.commit()

    updated = db.execute(_sql_text(
        "SELECT id, trade_id, ticker, intent_state, payload_json, updated_at "
        "FROM trading_bracket_intents WHERE id = :iid"
    ), {"iid": int(bracket_intent_id)}).first()

    return JSONResponse({
        "ok": True,
        "intent_id": int(updated[0]),
        "trade_id": int(updated[1]) if updated[1] is not None else None,
        "ticker": updated[2],
        "intent_state": updated[3],
        "pending_decision": (updated[4] or {}).get("pending_decision"),
        "updated_at": updated[5].isoformat() if updated[5] else None,
    })


# ---------------------------------------------------------------------------
# Bracket cover-policy snapshot (bracket-writer-cover-policy-clarify, 2026-05-03)
# ---------------------------------------------------------------------------
#
# Read-only snapshot of intent rows that hit the writer's
# ``covered_by_existing_sell`` branch — i.e. open positions where the
# broker has working sell coverage but no stop-typed order. With the
# DEFAULT writer policy these rows are NOT downside-protected; the
# limit-sell only locks upside. Operator endpoint to scan exposure at
# a glance.

@router.get("/api/admin/bracket/cover-policy-snapshot")
def api_admin_bracket_cover_policy_snapshot(ctx=Depends(require_paired)):
    redirect = _guard(ctx)
    if redirect:
        return redirect
    db: Session = ctx["db"]
    from sqlalchemy import text as _sql_text
    from datetime import datetime, timezone
    from ..config import settings as _settings

    rows = db.execute(_sql_text(
        "SELECT bi.id AS intent_id, bi.trade_id, t.ticker, "
        "       bi.intent_state, bi.last_diff_reason, "
        "       bi.stop_price, bi.quantity AS local_qty, "
        "       bi.broker_stop_order_id, t.status AS trade_status, "
        "       bi.updated_at "
        "FROM trading_bracket_intents bi "
        "JOIN trading_management_envelopes t ON t.id = bi.trade_id "
        "WHERE bi.last_diff_reason LIKE 'covered_by_existing_sell%' "
        "  AND t.status = 'open' "
        "ORDER BY bi.updated_at DESC NULLS LAST"
    )).fetchall()

    flags = {
        "chili_bracket_missing_stop_repair_enabled": bool(
            getattr(_settings, "chili_bracket_missing_stop_repair_enabled", False)
        ),
        "chili_bracket_writer_cancel_covering_sell": bool(
            getattr(_settings, "chili_bracket_writer_cancel_covering_sell", False)
        ),
        "chili_bracket_intent_mirror_enabled": bool(
            getattr(_settings, "chili_bracket_intent_mirror_enabled", False)
        ),
    }

    advisory = (
        "no downside protection; broker has limit-sell only — set "
        "CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1 for cancel-and-place-stop"
    )

    return JSONResponse({
        "as_of": datetime.now(timezone.utc).isoformat(),
        "flags": flags,
        "row_count": len(rows),
        "rows": [
            {
                "intent_id": int(r[0]),
                "trade_id": int(r[1]) if r[1] is not None else None,
                "ticker": r[2],
                "intent_state": r[3],
                "last_diff_reason": r[4],
                "stop_price_local": float(r[5]) if r[5] is not None else None,
                "local_qty": float(r[6]) if r[6] is not None else None,
                "broker_stop_order_id": r[7],
                "trade_status": r[8],
                "updated_at": r[9].isoformat() if r[9] is not None else None,
                "advisory": advisory,
            }
            for r in rows
        ],
    })


# ---------------------------------------------------------------------------
# User Management
# ---------------------------------------------------------------------------

@router.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request, ctx=Depends(require_paired)):
    redirect = _guard(ctx)
    if redirect:
        return redirect

    db: Session = ctx["db"]
    users = user_stats(db)
    all_users = db.query(User).order_by(User.name.asc()).all()

    return request.app.state.templates.TemplateResponse(request, "admin_users.html", {
        "user_name": ctx["user_name"],
        "users": users,
        "all_users": all_users,
    })


@router.post("/admin/users")
def admin_create_user(
    name: str = Form(...),
    email: str = Form(""),
    ctx=Depends(require_paired),
):
    redirect = _guard(ctx)
    if redirect:
        return redirect

    db: Session = ctx["db"]
    user = User(name=name.strip())
    if email.strip():
        user.email = email.strip().lower()
    db.add(user)
    db.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/delete")
def admin_delete_user(user_id: int, ctx=Depends(require_paired)):
    redirect = _guard(ctx)
    if redirect:
        return redirect

    db: Session = ctx["db"]
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return RedirectResponse("/admin/users", status_code=303)

    db.query(Device).filter(Device.user_id == user_id).delete()
    db.query(HousemateProfile).filter(HousemateProfile.user_id == user_id).delete()
    db.delete(user)
    db.commit()
    return RedirectResponse("/admin/users", status_code=303)


# ---------------------------------------------------------------------------
# User Memories API
# ---------------------------------------------------------------------------

@router.get("/api/admin/user/{user_id}/memories")
def api_user_memories(user_id: int, ctx=Depends(require_paired)):
    redirect = _guard(ctx)
    if redirect:
        return redirect
    db: Session = ctx["db"]
    memories = (
        db.query(UserMemory)
        .filter(UserMemory.user_id == user_id, UserMemory.superseded == False)
        .order_by(UserMemory.created_at.desc())
        .limit(50)
        .all()
    )
    return JSONResponse([
        {
            "id": m.id,
            "category": m.category,
            "content": m.content,
            "created_at": m.created_at.strftime("%Y-%m-%d %H:%M") if m.created_at else "",
        }
        for m in memories
    ])


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------

@router.post("/admin/pair-code")
def admin_pair_code(request: Request, user_id: int = Form(...), ctx=Depends(require_paired)):
    redirect = _guard(ctx)
    if redirect:
        return redirect

    db: Session = ctx["db"]
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return JSONResponse({"ok": False, "error": "User not found"}, status_code=404)

    code = generate_pair_code(db, user_id=user_id, minutes_valid=10)

    if request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse({"ok": True, "code": code, "user_name": user.name})

    return JSONResponse({"ok": True, "code": code, "user_name": user.name})


# ---------------------------------------------------------------------------
# RAG Management
# ---------------------------------------------------------------------------

@router.post("/admin/rag/ingest")
def admin_rag_ingest(ctx=Depends(require_paired)):
    redirect = _guard(ctx)
    if redirect:
        return redirect

    from ..rag import ingest_documents
    result = ingest_documents(trace_id="admin-ingest")
    return JSONResponse(result)


@router.get("/admin/rag/status")
def admin_rag_status(ctx=Depends(require_paired)):
    redirect = _guard(ctx)
    if redirect:
        return redirect

    return JSONResponse(rag_stats())


# ---------------------------------------------------------------------------
# CSV Exports
# ---------------------------------------------------------------------------

@router.get("/export/chores.csv")
def export_chores_csv(db: Session = Depends(get_db)):
    chores = db.query(Chore).order_by(Chore.id.asc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "title", "done", "priority", "due_date", "recurrence", "assigned_to", "created_at", "completed_at"])
    for c in chores:
        writer.writerow([
            c.id, c.title, c.done, c.priority or "", 
            c.due_date.isoformat() if c.due_date else "",
            c.recurrence or "", c.assigned_to or "",
            c.created_at.isoformat() if c.created_at else "",
            c.completed_at.isoformat() if c.completed_at else "",
        ])
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=chores.csv"},
    )


@router.get("/export/birthdays.csv")
def export_birthdays_csv(db: Session = Depends(get_db)):
    birthdays = db.query(Birthday).order_by(Birthday.date.asc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "name", "date"])
    for b in birthdays:
        writer.writerow([b.id, b.name, b.date.isoformat()])
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=birthdays.csv"},
    )

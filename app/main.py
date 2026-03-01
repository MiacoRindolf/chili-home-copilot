from fastapi import FastAPI, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from sqlalchemy.orm import Session
from datetime import date

from .db import Base, engine, SessionLocal
from .chili_nlu import parse_message
from .models import Chore, Birthday
from .llm_planner import plan_action
from .logger import new_trace_id, log_info
from .health import check_db, check_ollama
from .metrics import record_latency, latency_stats, get_counts
from .health import reset_demo_data
import time
import csv
import io

Base.metadata.create_all(bind=engine)

app = FastAPI(title="CHILI Home Copilot")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.get("/health", response_class=JSONResponse)
def health(db: Session = Depends(get_db)):
    db_status = check_db(db)
    ollama_status = check_ollama()

    overall_ok = db_status.get("ok") and ollama_status.get("ok")

    return {
        "ok": bool(overall_ok),
        "db": db_status,
        "ollama": ollama_status,
    }

@app.get("/", response_class=HTMLResponse)
def home(db: Session = Depends(get_db)):
    chores = db.query(Chore).order_by(Chore.id.desc()).all()
    birthdays = db.query(Birthday).order_by(Birthday.date.asc()).all()

    chore_items = "".join(
        f"<li>{'✅' if c.done else '⬜'} {c.title} "
        f"<a href='/chores/{c.id}/done'>mark done</a></li>"
        for c in chores
    ) or "<li>No chores yet.</li>"

    bday_items = "".join(
        f"<li>🎂 {b.name} — {b.date.isoformat()}</li>"
        for b in birthdays
    ) or "<li>No birthdays yet.</li>"

    return f"""
    <html>
      <head>
        <title>CHILI Home Copilot</title>
        <meta charset="utf-8"/>
      </head>
      <body style="font-family: Arial; max-width: 800px; margin: 40px auto;">
        <h1>🌶️ CHILI — Home Copilot</h1>
        <p><b>Conversational Home Interface & Life Intelligence</b></p>

        <h2>Chores</h2>
        <form method="post" action="/chores">
          <input name="title" placeholder="Add a chore..." style="width: 60%;" required />
          <button type="submit">Add</button>
        </form>
        <ul>{chore_items}</ul>

        <h2>Birthday reminders</h2>
        <form method="post" action="/birthdays">
          <input name="name" placeholder="Name" required />
          <input name="date" type="date" required />
          <button type="submit">Add</button>
        </form>
        <ul>{bday_items}</ul>

        <hr/>
        <p style="color: #666;">
          Local-first prototype. Next: LLM chat + tool-use.
        </p>
      </body>
    </html>
    """

@app.get("/chat", response_class=HTMLResponse)
def chat_page():
    return """
    <html>
      <head>
        <title>CHILI Chat</title>
        <meta charset="utf-8"/>
      </head>
      <body style="font-family: Arial; max-width: 800px; margin: 40px auto;">
        <h1>🌶️ CHILI Chat</h1>
        <p><a href="/">← Back to Home</a></p>

        <form method="post" action="/chat">
          <input name="message" placeholder="Type a request..." style="width: 70%;" required />
          <button type="submit">Send</button>
        </form>

        <div style="margin-top: 20px; padding: 12px; background: #f5f5f5;">
          <p><b>CHILI:</b> Chat is wired. Next we’ll make it understand chores & birthdays.</p>
        </div>
      </body>
    </html>
    """

@app.post("/chat", response_class=HTMLResponse)
def chat_submit(message: str = Form(...), db: Session = Depends(get_db)):
    trace_id = new_trace_id()
    t0 = time.time()
    log_info(trace_id, f"chat_message={message!r}")
    # Try LLM planner first, fallback to rules
    try:
        planned = plan_action(message)
        log_info(trace_id, f"planned={planned}")
        print("PLANNED_ACTION:", planned)
        llm_reply = planned.get("reply") if isinstance(planned, dict) else None
    except Exception as e:
        # fallback to rule-based parser if LLM is unavailable
        fallback = parse_message(message)
        planned = {"type": fallback.type, "data": fallback.data, "llm_error": str(e)}

    if isinstance(planned, dict) and "type" in planned and "data" in planned:
        action_type = planned["type"]
        action_data = planned["data"]
        llm_reply = planned.get("reply") if isinstance(planned, dict) else None
    else:
        # fallback
        fallback = parse_message(message)
        action_type = fallback.type
        action_data = fallback.data
        llm_reply = planned.get("reply") if isinstance(planned, dict) else None

    reply_lines = []
    if llm_reply:
      reply_lines.append(llm_reply)
    safe_msg = message.replace("<", "&lt;").replace(">", "&gt;")

    if action_type == "add_chore":
        title = action_data["title"]
        db.add(Chore(title=title, done=False))
        db.commit()
        reply_lines.append(f"Added chore: <b>{title}</b> ✅")
        log_info(trace_id, "executed=add_chore")

    elif action_type == "list_chores":
        chores = db.query(Chore).order_by(Chore.id.desc()).all()
        log_info(trace_id, "executed=list_chores")
        if chores:
            items = "".join([f"<li>#{c.id} {'✅' if c.done else '⬜'} {c.title}</li>" for c in chores])
            reply_lines.append(f"<ul>{items}</ul>")
        else:
            reply_lines.append("No chores found.")

    elif action_type == "list_chores_pending":
        chores = db.query(Chore).filter(Chore.done == False).order_by(Chore.id.desc()).all()
        log_info(trace_id, "executed=list_chores_pending")
        if chores:
            items = "".join([f"<li>#{c.id} ⬜ {c.title}</li>" for c in chores])
            reply_lines.append(f"Pending chores:<ul>{items}</ul>")
        else:
            reply_lines.append("No pending chores. Nice! ✅")

    elif action_type == "mark_chore_done":
        chore_id = action_data["id"]
        chore = db.query(Chore).filter(Chore.id == chore_id).first()
        log_info(trace_id, "executed=mark_chore_done")
        if chore:
            chore.done = True
            db.commit()
            reply_lines.append(f"Marked chore #{chore_id} as done ✅")
        else:
            reply_lines.append(f"Couldn't find chore #{chore_id}.")

    elif action_type == "add_birthday":
        name = action_data["name"]
        bday_str = action_data["date"]              # e.g. "1994-07-18"
        bday = date.fromisoformat(bday_str)         # convert to datetime.date

        db.add(Birthday(name=name, date=bday))
        db.commit()
        reply_lines.append(f"Added birthday: <b>{name}</b> on <b>{bday.isoformat()}</b> 🎂")
        log_info(trace_id, "executed=add_birthday")

    elif action_type == "list_birthdays":
        birthdays = db.query(Birthday).order_by(Birthday.date.asc()).all()
        log_info(trace_id, "executed=list_birthdays")
        if birthdays:
            items = "".join([f"<li>🎂 {b.name} — {b.date.isoformat()}</li>" for b in birthdays])
            reply_lines.append(f"<ul>{items}</ul>")
        else:
            reply_lines.append("No birthdays found.")

    else:
        log_info(trace_id, "executed=unknown")
        reply_lines.append(
            "Try commands like:<br>"
            "<code>add chore take out trash</code><br>"
            "<code>list chores</code><br>"
            "<code>list pending chores</code><br>"
            "<code>done 1</code><br>"
            "<code>add birthday Mom 2026-05-12</code><br>"
            "<code>list birthdays</code>"
        )

    reply_html = "<br>".join(reply_lines)

    ms = int((time.time() - t0) * 1000)
    log_info(trace_id, f"latency_ms={ms}")
    record_latency(ms)

    return f"""
    <html>
      <head>
        <title>CHILI Chat</title>
        <meta charset="utf-8"/>
      </head>
      <body style="font-family: Arial; max-width: 800px; margin: 40px auto;">
        <h1>🌶️ CHILI Chat</h1>
        <p><a href="/">← Home</a> | <a href="/chat">New message</a></p>

        <div style="margin-top: 20px; padding: 12px; background: #f5f5f5;">
          <p><b>You:</b> {safe_msg}</p>
          <p><b>CHILI:</b><br>{reply_html}</p>
        </div>

        <p style="color:#888; font-size:12px; margin-top:16px;">
          trace_id: <code>{trace_id}</code>
        </p>
      </body>
    </html>
    """

@app.post("/chores")
def add_chore(title: str = Form(...), db: Session = Depends(get_db)):
    db.add(Chore(title=title, done=False))
    db.commit()
    return RedirectResponse("/", status_code=303)

@app.get("/chores/{chore_id}/done")
def mark_chore_done(chore_id: int, db: Session = Depends(get_db)):
    chore = db.query(Chore).filter(Chore.id == chore_id).first()
    if chore:
        chore.done = True
        db.commit()
    return RedirectResponse("/", status_code=303)

@app.post("/birthdays")
def add_birthday(
    name: str = Form(...),
    date: date = Form(...),
    db: Session = Depends(get_db),
):
    db.add(Birthday(name=name, date=date))
    db.commit()
    return RedirectResponse("/", status_code=303)

@app.get("/metrics", response_class=JSONResponse)
def metrics(db: Session = Depends(get_db)):
    return {
        "counts": get_counts(db),
        "llm_chat_latency": latency_stats(),
    }

@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(db: Session = Depends(get_db)):
    # Reuse existing health + metrics logic
    db_status = check_db(db)
    ollama_status = check_ollama()

    counts = get_counts(db)
    lat = latency_stats()

    ok = bool(db_status.get("ok") and ollama_status.get("ok"))

    return f"""
    <html>
      <head>
        <title>CHILI Admin</title>
        <meta charset="utf-8"/>
      </head>
      <body style="font-family: Arial; max-width: 900px; margin: 40px auto;">
        <h1>🛠️ CHILI Admin</h1>
        <p><a href="/">Home</a> | <a href="/chat">Chat</a> | <a href="/health">/health</a> | <a href="/metrics">/metrics</a></p>

        <h2>Status</h2>
        <div style="padding: 12px; background: {'#e8f5e9' if ok else '#ffebee'}; border: 1px solid #ddd;">
          <p><b>Overall:</b> {'✅ OK' if ok else '❌ Issues detected'}</p>
          <p><b>DB:</b> {'✅ OK' if db_status.get('ok') else '❌ ' + db_status.get('error','')}</p>
          <p><b>Ollama:</b> {'✅ OK' if ollama_status.get('ok') else '❌ ' + ollama_status.get('error','')}</p>
          <p><b>Models:</b> {', '.join(ollama_status.get('models', [])) if ollama_status.get('ok') else 'N/A'}</p>
          <form method="post" action="/admin/reset" style="margin-top: 12px;">
            <button type="submit" onclick="return confirm('Reset demo data (delete all chores and birthdays)?')">
              Reset demo data
            </button>
          </form>
        </div>

        <h2>Counts</h2>
        <div style="padding: 12px; background: #f5f5f5; border: 1px solid #ddd;">
          <p><b>Chores:</b> total={counts['chores']['total']}, pending={counts['chores']['pending']}, done={counts['chores']['done']}</p>
          <p><b>Birthdays:</b> total={counts['birthdays']['total']}</p>
        </div>

        <h2>LLM Chat Latency (last {lat['count']} requests)</h2>
        <div style="padding: 12px; background: #f5f5f5; border: 1px solid #ddd;">
          <p><b>Avg:</b> {lat['avg_ms']} ms</p>
          <p><b>P95:</b> {lat['p95_ms']} ms</p>
        </div>

        <h2>Exports</h2>
        <ul>
          <li><a href="/export/chores.csv">Download chores.csv</a></li>
          <li><a href="/export/birthdays.csv">Download birthdays.csv</a></li>
        </ul>

        <hr/>
        <p style="color:#888; font-size:12px;">CHILI Admin — local dashboard</p>
      </body>
    </html>
    """

@app.post("/admin/reset")
def admin_reset(db: Session = Depends(get_db)):
    result = reset_demo_data(db)
    # Redirect back to admin page (even if failed)
    return RedirectResponse("/admin", status_code=303)

@app.get("/export/chores.csv")
def export_chores_csv(db: Session = Depends(get_db)):
    chores = db.query(Chore).order_by(Chore.id.asc()).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "title", "done"])
    for c in chores:
        writer.writerow([c.id, c.title, c.done])

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=chores.csv"},
    )


@app.get("/export/birthdays.csv")
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
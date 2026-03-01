from fastapi import FastAPI, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from datetime import date

from .db import Base, engine, SessionLocal
from .chili_nlu import parse_message
from .models import Chore, Birthday

Base.metadata.create_all(bind=engine)

app = FastAPI(title="CHILI Home Copilot")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

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
    action = parse_message(message)

    reply_lines = []
    safe_msg = message.replace("<", "&lt;").replace(">", "&gt;")

    if action.type == "add_chore":
        title = action.data["title"]
        db.add(Chore(title=title, done=False))
        db.commit()
        reply_lines.append(f"Added chore: <b>{title}</b> ✅")

    elif action.type == "list_chores":
        chores = db.query(Chore).order_by(Chore.id.desc()).all()
        if chores:
            items = "".join([f"<li>#{c.id} {'✅' if c.done else '⬜'} {c.title}</li>" for c in chores])
            reply_lines.append(f"<ul>{items}</ul>")
        else:
            reply_lines.append("No chores found.")

    elif action.type == "list_chores_pending":
        chores = db.query(Chore).filter(Chore.done == False).order_by(Chore.id.desc()).all()
        if chores:
            items = "".join([f"<li>#{c.id} ⬜ {c.title}</li>" for c in chores])
            reply_lines.append(f"Pending chores:<ul>{items}</ul>")
        else:
            reply_lines.append("No pending chores. Nice! ✅")

    elif action.type == "mark_chore_done":
        chore_id = action.data["id"]
        chore = db.query(Chore).filter(Chore.id == chore_id).first()
        if chore:
            chore.done = True
            db.commit()
            reply_lines.append(f"Marked chore #{chore_id} as done ✅")
        else:
            reply_lines.append(f"Couldn't find chore #{chore_id}.")

    elif action.type == "add_birthday":
        name = action.data["name"]
        bday = action.data["date"]
        db.add(Birthday(name=name, date=bday))
        db.commit()
        reply_lines.append(f"Added birthday: <b>{name}</b> on <b>{bday.isoformat()}</b> 🎂")

    elif action.type == "list_birthdays":
        birthdays = db.query(Birthday).order_by(Birthday.date.asc()).all()
        if birthdays:
            items = "".join([f"<li>🎂 {b.name} — {b.date.isoformat()}</li>" for b in birthdays])
            reply_lines.append(f"<ul>{items}</ul>")
        else:
            reply_lines.append("No birthdays found.")

    else:
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
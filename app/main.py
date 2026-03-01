from fastapi import FastAPI, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from datetime import date

from .db import Base, engine, SessionLocal
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
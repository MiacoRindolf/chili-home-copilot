from fastapi import FastAPI, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from sqlalchemy.orm import Session
from datetime import date

from .db import Base, engine, SessionLocal
from .chili_nlu import parse_message
from .models import Chore, Birthday, ChatLog, User, ChatMessage
from .pairing import DEVICE_COOKIE_NAME, redeem_pair_code, register_device, get_identity, generate_pair_code, get_identity_record
from .llm_planner import plan_action
from .logger import new_trace_id, log_info
from . import rag as rag_module
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

def get_convo_key(identity: dict, device_token: str | None, client_ip: str) -> str:
    if not identity["is_guest"] and identity["user_id"] is not None:
        return f"user:{identity['user_id']}"
    # guests: stable enough for LAN
    return f"guest:{device_token or client_ip}"

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
        <meta name="viewport" content="width=device-width, initial-scale=1" />
      </head>
      <body style="font-family: Arial; max-width: 800px; margin: 40px auto;">
        <h1>🌶️ CHILI — Home Copilot</h1>
        <p><b>Conversational Home Interface & Life Intelligence</b></p>

        <h2>Chores</h2>
        <form method="post" action="/chores">
          <input name="title" placeholder="Add a chore..." style="width: 100%; max-width: 520px; padding: 10px; font-size: 16px;" required />
          <button style="padding: 10px 14px; font-size: 16px; margin-top: 8px;" type="submit">Add</button>
        </form>
        <ul>{chore_items}</ul>

        <h2>Birthday reminders</h2>
        <form method="post" action="/birthdays">
          <input name="name" placeholder="Name" required />
          <input name="date" type="date" required />
          <button style="padding: 10px 14px; font-size: 16px; margin-top: 8px;" type="submit">Add</button>
        </form>
        <ul>{bday_items}</ul>

        <hr/>
        <p style="color: #666;">
          Local-first household copilot. <a href="/chat">Open Chat</a> | <a href="/admin">Admin</a>
        </p>
      </body>
    </html>
    """

@app.get("/chat", response_class=HTMLResponse)
def chat_page():
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <title>CHILI Chat</title>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root {
      --bg: #f9fafb; --bg-header: #fff; --bg-input-bar: #fff;
      --bg-msg-assist: #fff; --border: #e5e7eb; --text: #1f2937;
      --text-secondary: #6b7280; --text-muted: #9ca3af;
      --input-border: #d1d5db; --input-bg: #fff;
      --code-bg: #f3f4f6;
      --accent: #2563eb; --accent-hover: #1d4ed8; --accent-disabled: #93c5fd;
    }
    [data-theme="dark"] {
      --bg: #111827; --bg-header: #1f2937; --bg-input-bar: #1f2937;
      --bg-msg-assist: #1f2937; --border: #374151; --text: #f3f4f6;
      --text-secondary: #9ca3af; --text-muted: #6b7280;
      --input-border: #4b5563; --input-bg: #111827;
      --code-bg: #374151;
      --accent: #3b82f6; --accent-hover: #2563eb; --accent-disabled: #1e40af;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      height: 100vh; display: flex; flex-direction: column;
      background: var(--bg); color: var(--text);
    }
    header {
      background: var(--bg-header); border-bottom: 1px solid var(--border);
      padding: 12px 20px; flex-shrink: 0;
      display: flex; justify-content: space-between; align-items: center;
    }
    header h1 { font-size: 20px; }
    header nav { font-size: 13px; margin-top: 4px; }
    header nav a { color: var(--text-secondary); text-decoration: none; }
    header nav a:hover { text-decoration: underline; }
    #theme-toggle {
      background: none; border: 1px solid var(--border); border-radius: 6px;
      padding: 4px 10px; font-size: 18px; cursor: pointer; line-height: 1;
    }

    #chat-area {
      flex: 1; overflow-y: auto; padding: 16px 20px;
    }
    #chat-log {
      display: flex; flex-direction: column; gap: 10px;
      max-width: 800px; width: 100%; margin: 0 auto;
    }

    .msg {
      padding: 10px 14px; border-radius: 12px;
      max-width: 80%; word-wrap: break-word;
      line-height: 1.45; font-size: 15px;
    }
    .msg.user {
      background: var(--accent); color: #fff;
      align-self: flex-end;
      border-bottom-right-radius: 4px;
    }
    .msg.assistant {
      background: var(--bg-msg-assist); color: var(--text);
      align-self: flex-start;
      border: 1px solid var(--border);
      border-bottom-left-radius: 4px;
    }
    .msg .role { font-weight: 600; font-size: 12px; margin-bottom: 3px; opacity: 0.7; }
    .msg.user .role { color: rgba(255,255,255,0.8); }
    .msg .ts { font-size: 11px; margin-top: 4px; opacity: 0.5; }

    #welcome {
      text-align: center; margin-top: 20vh; color: var(--text-muted);
      max-width: 800px; width: 100%; margin-left: auto; margin-right: auto;
    }
    #welcome h2 { font-size: 28px; margin-bottom: 8px; color: var(--text-secondary); }
    #welcome p { font-size: 14px; line-height: 1.6; }
    #welcome code { background: var(--code-bg); padding: 2px 6px; border-radius: 4px; font-size: 13px; }

    #input-bar {
      flex-shrink: 0; background: var(--bg-input-bar);
      border-top: 1px solid var(--border); padding: 12px 20px;
    }
    #chat-form {
      display: flex; gap: 8px;
      max-width: 800px; margin: 0 auto;
    }
    #msg-input {
      flex: 1; padding: 10px 14px; font-size: 15px;
      border: 1px solid var(--input-border); border-radius: 8px;
      outline: none; transition: border-color 0.15s;
      background: var(--input-bg); color: var(--text);
    }
    #msg-input:focus { border-color: var(--accent); }
    #chat-form button {
      padding: 10px 20px; font-size: 15px; font-weight: 600;
      background: var(--accent); color: #fff; border: none;
      border-radius: 8px; cursor: pointer; transition: background 0.15s;
    }
    #chat-form button:hover { background: var(--accent-hover); }
    #chat-form button:disabled { background: var(--accent-disabled); cursor: not-allowed; }
    #status {
      font-size: 12px; color: var(--text-muted);
      max-width: 800px; margin: 6px auto 0; text-align: center;
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>&#x1F336;&#xFE0F; CHILI Chat</h1>
      <nav><a href="/">Home</a> &#xB7; <a href="/admin">Admin</a></nav>
    </div>
    <button id="theme-toggle" title="Toggle dark mode">&#x1F319;</button>
  </header>

  <div id="chat-area">
    <div id="welcome">
      <h2>&#x1F336;&#xFE0F;</h2>
      <p>
        Ask me anything about your household.<br/>
        Try: <code>add chore take out trash</code>
        or <code>list chores</code><br/>
        or <code>add birthday Mom 2026-05-12</code>
      </p>
    </div>
    <div id="chat-log"></div>
  </div>

  <div id="input-bar">
    <form id="chat-form">
      <input id="msg-input" name="message" placeholder="Type a message..." autocomplete="off" required />
      <button type="submit">Send</button>
    </form>
    <div id="status"></div>
  </div>

  <script>
    const chatArea  = document.getElementById('chat-area');
    const chatLog   = document.getElementById('chat-log');
    const welcome   = document.getElementById('welcome');
    const form      = document.getElementById('chat-form');
    const input     = document.getElementById('msg-input');
    const status    = document.getElementById('status');
    const btn       = form.querySelector('button');
    const themeBtn  = document.getElementById('theme-toggle');

    // --- Dark mode ---
    function setTheme(t) {
      document.documentElement.setAttribute('data-theme', t);
      themeBtn.textContent = t === 'dark' ? String.fromCodePoint(0x2600) : String.fromCodePoint(0x1F319);
      localStorage.setItem('chili-theme', t);
    }
    themeBtn.addEventListener('click', function() {
      const cur = document.documentElement.getAttribute('data-theme');
      setTheme(cur === 'dark' ? 'light' : 'dark');
    });
    setTheme(localStorage.getItem('chili-theme') || 'light');

    // --- Chat helpers ---
    function esc(s) {
      const d = document.createElement('div');
      d.textContent = s;
      return d.innerHTML;
    }

    function fmtTime(raw) {
      if (!raw) return '';
      try {
        const d = new Date(raw.replace(' ', 'T'));
        if (isNaN(d)) return '';
        const now = new Date();
        const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
        if (d.toDateString() === now.toDateString()) return time;
        return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ', ' + time;
      } catch(e) { return ''; }
    }

    function addMsg(role, content, ts) {
      welcome.style.display = 'none';
      const div = document.createElement('div');
      div.className = 'msg ' + role;
      const label = role === 'user' ? 'You' : 'CHILI';
      const timeStr = fmtTime(ts);
      const body = esc(content).split(String.fromCharCode(10)).join('<br>');
      div.innerHTML = '<div class="role">' + esc(label) + '</div>'
                    + '<div>' + body + '</div>'
                    + (timeStr ? '<div class="ts">' + esc(timeStr) + '</div>' : '');
      chatLog.appendChild(div);
      chatArea.scrollTop = chatArea.scrollHeight;
    }

    async function loadHistory() {
      try {
        const res = await fetch('/api/chat/history');
        const data = await res.json();
        status.textContent = data.is_guest
          ? 'Guest mode (read-only for writes)'
          : 'Signed in as ' + data.user;
        if (data.messages.length > 0) {
          data.messages.forEach(function(m) { addMsg(m.role, m.content, m.created_at); });
        }
      } catch (e) {
        status.textContent = 'Could not load history.';
      }
    }

    form.addEventListener('submit', async function(e) {
      e.preventDefault();
      const msg = input.value.trim();
      if (!msg) return;

      addMsg('user', msg, '');
      input.value = '';
      btn.disabled = true;
      status.textContent = 'Thinking...';

      try {
        const body = new FormData();
        body.append('message', msg);
        const res = await fetch('/api/chat', { method: 'POST', body: body });
        const data = await res.json();
        addMsg('assistant', data.reply || '(no reply)', '');
        status.textContent = '';
      } catch (e) {
        addMsg('assistant', 'Error: could not reach CHILI backend.', '');
        status.textContent = '';
      } finally {
        btn.disabled = false;
        input.focus();
      }
    });

    loadHistory();
    input.focus();
  </script>
</body>
</html>
"""

@app.post("/chat", response_class=HTMLResponse)
def chat_submit(request: Request, message: str = Form(...), db: Session = Depends(get_db)):
    trace_id = new_trace_id()
    t0 = time.time()

    client_ip = request.client.host
    log_info(trace_id, f"client_ip={client_ip}")
    log_info(trace_id, f"chat_message={message!r}")

    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    user_name, is_guest = get_identity(db, device_token)
    log_info(trace_id, f"user={user_name} guest={is_guest} client_ip={client_ip}")
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

    WRITE_ACTIONS = {"add_chore", "mark_chore_done", "add_birthday"}

    if is_guest and action_type in WRITE_ACTIONS:
        # log the attempt (still useful)
        db.add(ChatLog(
            client_ip=client_ip,
            trace_id=trace_id,
            message=message,
            action_type=f"BLOCKED_{action_type}",
        ))
        db.commit()

        ms = int((time.time() - t0) * 1000)
        log_info(trace_id, f"latency_ms={ms}")
        record_latency(ms)
        log_info(trace_id, f"blocked_guest_write action={action_type}")

        safe_msg = message.replace("<", "&lt;").replace(">", "&gt;")
        reply_html = "Guest mode is read-only. Ask the admin to pair your device at /pair."

        return f"""
        <html>
          <head>
            <title>CHILI Chat</title>
            <meta charset="utf-8"/>
            <meta name="viewport" content="width=device-width, initial-scale=1" />
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
            <p style="color:#888; font-size:12px;">
              user: <code>{user_name}</code> | guest: <code>{is_guest}</code>
            </p>
            <p style="color:#888; font-size:12px;">
              from: <code>{client_ip}</code>
            </p>
          </body>
        </html>
        """

    db.add(ChatLog(
        client_ip=client_ip,
        trace_id=trace_id,
        message=message,
        action_type=action_type,
    ))
    db.commit()

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
        <meta name="viewport" content="width=device-width, initial-scale=1" />
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
        <p style="color:#888; font-size:12px;">
          user: <code>{user_name}</code> | guest: <code>{is_guest}</code>
        </p>
        <p style="color:#888; font-size:12px;">
          from: <code>{client_ip}</code>
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

    logs = db.query(ChatLog).order_by(ChatLog.id.desc()).limit(20).all()
    logs_html = "".join(
        f"<li>{l.created_at} | {l.client_ip} | {l.action_type} | <code>{l.trace_id}</code> | {l.message}</li>"
        for l in logs
    ) or "<li>No chat logs yet.</li>"

    return f"""
    <html>
      <head>
        <title>CHILI Admin</title>
        <meta charset="utf-8"/>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
      </head>
      <body style="font-family: Arial; max-width: 900px; margin: 40px auto;">
        <h1>🛠️ CHILI Admin</h1>
        <p><a href="/">Home</a> | <a href="/chat">Chat</a> | <a href="/health">/health</a> | <a href="/metrics">/metrics</a> | <a href="/admin/users">Manage users & pairing</a> </p>

        <h2>Status</h2>
        <div style="padding: 12px; background: {'#e8f5e9' if ok else '#ffebee'}; border: 1px solid #ddd;">
          <p><b>Overall:</b> {'✅ OK' if ok else '❌ Issues detected'}</p>
          <p><b>DB:</b> {'✅ OK' if db_status.get('ok') else '❌ ' + db_status.get('error','')}</p>
          <p><b>Ollama:</b> {'✅ OK' if ollama_status.get('ok') else '❌ ' + ollama_status.get('error','')}</p>
          <p><b>Models:</b> {', '.join(ollama_status.get('models', [])) if ollama_status.get('ok') else 'N/A'}</p>
          <form method="post" action="/admin/reset" style="margin-top: 12px;">
            <button style="width: 100%; max-width: 520px; padding: 10px; font-size: 16px;" type="submit" onclick="return confirm('Reset demo data (delete all chores and birthdays)?')">
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

        <h2>Recent Chat Activity</h2>
        <ul>{logs_html}</ul>

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

@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(db: Session = Depends(get_db)):
    users = db.query(User).order_by(User.name.asc()).all()

    items = "".join([f"<li>#{u.id} {u.name}</li>" for u in users]) or "<li>No users yet.</li>"

    return f"""
    <html><head>
      <title>CHILI Admin - Users</title>
      <meta name="viewport" content="width=device-width, initial-scale=1" />
    </head><body style="font-family:Arial;max-width:900px;margin:40px auto;">
      <h1>👥 CHILI Admin - Users</h1>
      <p><a href="/admin">← Admin</a></p>

      <h2>Create housemate</h2>
      <form method="post" action="/admin/users">
        <input name="name" placeholder="Name (e.g., Alex)" required style="width:100%;max-width:520px;padding:10px;font-size:16px;" />
        <button type="submit" style="padding:10px 14px;font-size:16px;margin-top:8px;">Create</button>
      </form>

      <h2>Existing users</h2>
      <ul>{items}</ul>

      <h2>Generate pairing code</h2>
      <form method="post" action="/admin/pair-code">
        <input name="user_id" placeholder="User ID (e.g., 1)" required style="width:100%;max-width:520px;padding:10px;font-size:16px;" />
        <button type="submit" style="padding:10px 14px;font-size:16px;margin-top:8px;">Generate code</button>
      </form>

      <p>Housemate pairs at: <code>/pair</code></p>
    </body></html>
    """

@app.post("/admin/users")
def admin_create_user(name: str = Form(...), db: Session = Depends(get_db)):
    db.add(User(name=name.strip()))
    db.commit()
    return RedirectResponse("/admin/users", status_code=303)

@app.post("/admin/pair-code", response_class=HTMLResponse)
def admin_pair_code(user_id: int = Form(...), db: Session = Depends(get_db)):
    code = generate_pair_code(db, user_id=user_id, minutes_valid=10)
    return HTMLResponse(
        f"<p>Pairing code (valid 10 min): <b>{code}</b></p><p>Go to <code>/pair</code> on the device.</p><p><a href='/admin/users'>Back</a></p>"
    )

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

@app.get("/pair", response_class=HTMLResponse)
def pair_page():
    return """
    <html><head>
      <title>Pair Device</title>
      <meta name="viewport" content="width=device-width, initial-scale=1" />
    </head><body style="font-family:Arial;max-width:800px;margin:40px auto;">
      <h1>🔗 Pair this device to CHILI</h1>
      <p>Ask the admin for a pairing code.</p>
      <form method="post" action="/pair">
        <input name="code" placeholder="Pairing code" required style="width:100%;max-width:520px;padding:10px;font-size:16px;" />
        <input name="label" placeholder="Device label (e.g., Alex iPhone)" required style="width:100%;max-width:520px;padding:10px;font-size:16px;margin-top:8px;" />
        <button type="submit" style="padding:10px 14px;font-size:16px;margin-top:8px;">Pair</button>
      </form>
      <p><a href="/chat">Go to Chat</a></p>
    </body></html>
    """

@app.post("/pair")
def pair_submit(request: Request, code: str = Form(...), label: str = Form(...), db: Session = Depends(get_db)):
    client_ip = request.client.host
    pc = redeem_pair_code(db, code.strip())
    if not pc:
        return HTMLResponse("<p>Invalid/expired code. Ask admin for a new one.</p><p><a href='/pair'>Back</a></p>", status_code=400)

    token = register_device(db, user_id=pc.user_id, label=label.strip(), client_ip=client_ip)
    resp = RedirectResponse("/chat", status_code=303)
    resp.set_cookie(DEVICE_COOKIE_NAME, token, httponly=True, samesite="lax")
    return resp

@app.get("/api/chat/history", response_class=JSONResponse)
def chat_history(request: Request, db: Session = Depends(get_db)):
    client_ip = request.client.host
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)

    identity = get_identity_record(db, device_token)
    convo_key = get_convo_key(identity, device_token, client_ip)

    msgs = (
        db.query(ChatMessage)
        .filter(ChatMessage.convo_key == convo_key)
        .filter(ChatMessage.content != "")
        .order_by(ChatMessage.id.asc())
        .limit(50)
        .all()
    )

    return {
        "convo_key": convo_key,
        "user": identity["user_name"],
        "is_guest": identity["is_guest"],
        "messages": [
            {"role": m.role, "content": m.content, "created_at": str(m.created_at)}
            for m in msgs
        ],
    }

@app.post("/api/chat", response_class=JSONResponse)
def chat_api(request: Request, message: str = Form(...), db: Session = Depends(get_db)):
    trace_id = new_trace_id()
    t0 = time.time()

    client_ip = request.client.host
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)

    identity = get_identity_record(db, device_token)
    convo_key = get_convo_key(identity, device_token, client_ip)

    user_name = identity["user_name"]
    is_guest = identity["is_guest"]

    log_info(trace_id, f"client_ip={client_ip} user={user_name} guest={is_guest} convo={convo_key}")
    log_info(trace_id, f"chat_message={message!r}")

    # store user message
    db.add(ChatMessage(convo_key=convo_key, role="user", content=message, trace_id=trace_id))
    db.commit()

    # load memory window (last 12 messages)
    recent = (
        db.query(ChatMessage)
        .filter(ChatMessage.convo_key == convo_key)
        .order_by(ChatMessage.id.desc())
        .limit(12)
        .all()
    )
    recent = list(reversed(recent))

    # Build a context string for planner (simple)
    context = "\n".join([f"{m.role.upper()}: {m.content}" for m in recent])

    # RAG: search household documents for relevant context
    rag_context = None
    rag_hits = rag_module.search(message, n_results=3, trace_id=trace_id)
    if rag_hits and rag_hits[0]["distance"] < 1.5:
        rag_context = "\n---\n".join(
            f"[{h['source']}]: {h['text']}" for h in rag_hits
        )
        log_info(trace_id, f"rag_context_injected sources={[h['source'] for h in rag_hits]}")

    try:
        planned = plan_action(
            f"Conversation so far:\n{context}\n\nNew user message: {message}",
            rag_context=rag_context,
        )
    except Exception as e:
        log_info(trace_id, f"llm_error={e}")
        llm_reply = (
            "CHILI's brain is offline. "
            "Start Ollama to use chat: ollama serve"
        )
        db.add(ChatMessage(convo_key=convo_key, role="assistant", content=llm_reply, trace_id=trace_id, action_type="llm_offline"))
        db.commit()
        db.add(ChatLog(client_ip=client_ip, trace_id=trace_id, message=message, action_type="llm_offline"))
        db.commit()
        ms = int((time.time() - t0) * 1000)
        record_latency(ms)
        log_info(trace_id, f"latency_ms={ms} action=llm_offline")
        return {"trace_id": trace_id, "user": user_name, "is_guest": is_guest, "action_type": "llm_offline", "executed": False, "reply": llm_reply}

    action_type = planned.get("type", "unknown")
    action_data = planned.get("data", {})
    llm_reply = planned.get("reply") or ""

    WRITE_ACTIONS = {"add_chore", "mark_chore_done", "add_birthday"}

    # Guest read-only enforcement
    if is_guest and action_type in WRITE_ACTIONS:
        llm_reply = "Guest mode is read-only. Ask the admin to pair your device at /pair."
        action_type = "unknown"
        action_data = {"reason": "guest_read_only"}

    # Execute action and build reply if the LLM didn't provide one
    executed = False

    if action_type == "add_chore":
        title = action_data["title"]
        db.add(Chore(title=title, done=False))
        db.commit()
        executed = True
        if not llm_reply:
            llm_reply = f"Added chore: {title}"

    elif action_type == "list_chores":
        chores = db.query(Chore).order_by(Chore.id.desc()).all()
        executed = True
        if not llm_reply:
            if chores:
                lines = [f"#{c.id} {'[done]' if c.done else '[todo]'} {c.title}" for c in chores]
                llm_reply = "Chores:\n" + "\n".join(lines)
            else:
                llm_reply = "No chores yet."

    elif action_type == "list_chores_pending":
        chores = db.query(Chore).filter(Chore.done == False).order_by(Chore.id.desc()).all()
        executed = True
        if not llm_reply:
            if chores:
                lines = [f"#{c.id} {c.title}" for c in chores]
                llm_reply = "Pending chores:\n" + "\n".join(lines)
            else:
                llm_reply = "No pending chores. Nice!"

    elif action_type == "mark_chore_done":
        chore_id = action_data["id"]
        chore = db.query(Chore).filter(Chore.id == chore_id).first()
        if chore:
            chore.done = True
            db.commit()
            executed = True
            if not llm_reply:
                llm_reply = f"Marked chore #{chore_id} as done."
        else:
            if not llm_reply:
                llm_reply = f"Couldn't find chore #{chore_id}."

    elif action_type == "add_birthday":
        name = action_data["name"]
        bday = date.fromisoformat(action_data["date"])
        db.add(Birthday(name=name, date=bday))
        db.commit()
        executed = True
        if not llm_reply:
            llm_reply = f"Added birthday: {name} on {bday.isoformat()}"

    elif action_type == "list_birthdays":
        birthdays = db.query(Birthday).order_by(Birthday.date.asc()).all()
        executed = True
        if not llm_reply:
            if birthdays:
                lines = [f"{b.name} - {b.date.isoformat()}" for b in birthdays]
                llm_reply = "Birthdays:\n" + "\n".join(lines)
            else:
                llm_reply = "No birthdays yet."

    elif action_type == "answer_from_docs":
        executed = True
        source = action_data.get("source", "")
        if source and llm_reply:
            llm_reply = f"{llm_reply}\n(source: {source})"

    if not llm_reply:
        llm_reply = "I'm not sure what to do with that. Try: add chore, list chores, add birthday, list birthdays."

    # store assistant response
    db.add(ChatMessage(
        convo_key=convo_key,
        role="assistant",
        content=llm_reply,
        trace_id=trace_id,
        action_type=action_type
    ))
    db.commit()

    # audit log
    db.add(ChatLog(
        client_ip=client_ip,
        trace_id=trace_id,
        message=message,
        action_type=action_type
    ))
    db.commit()

    ms = int((time.time() - t0) * 1000)
    record_latency(ms)
    log_info(trace_id, f"latency_ms={ms} action={action_type} executed={executed}")

    return {
        "trace_id": trace_id,
        "user": user_name,
        "is_guest": is_guest,
        "action_type": action_type,
        "executed": executed,
        "reply": llm_reply,
    }
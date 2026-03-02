from fastapi import FastAPI, Depends, Form, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from typing import Optional
from pathlib import Path
import json as json_mod
from sqlalchemy.orm import Session
from datetime import date

from .db import Base, engine, SessionLocal
from .chili_nlu import parse_message
from .models import Chore, Birthday, ChatLog, User, ChatMessage, HousemateProfile, Conversation
from .pairing import DEVICE_COOKIE_NAME, redeem_pair_code, register_device, get_identity, generate_pair_code, get_identity_record
from .llm_planner import plan_action
from .logger import new_trace_id, log_info
from . import rag as rag_module
from . import openai_client
from . import personality as personality_module
from .health import check_db, check_ollama
from .metrics import record_latency, latency_stats, get_counts
from .health import reset_demo_data
import time
import csv
import io

Base.metadata.create_all(bind=engine)

app = FastAPI(title="CHILI Home Copilot")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

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

def _execute_tool(db, action_type, action_data, llm_reply, is_guest):
    """Shared tool execution logic for /api/chat and /api/chat/stream."""
    WRITE_ACTIONS = {"add_chore", "mark_chore_done", "add_birthday"}
    if is_guest and action_type in WRITE_ACTIONS:
        return "Guest mode is read-only. Ask the admin to pair your device at /pair.", False, "guest_blocked"

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

    return llm_reply, executed, action_type


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

@app.get("/chat")
def chat_page():
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <title>CHILI Chat</title>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <meta name="theme-color" content="#2563eb" />
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
  <link rel="manifest" href="/static/manifest.json" />
  <link rel="apple-touch-icon" href="/static/icon-192.png" />
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
      color: var(--text);
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
      resize: none; overflow-y: auto; min-height: 44px; max-height: 160px;
      font-family: inherit; line-height: 1.4;
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

    /* Markdown content in assistant messages */
    .msg.assistant .md-body p { margin: 0.4em 0; }
    .msg.assistant .md-body p:first-child { margin-top: 0; }
    .msg.assistant .md-body p:last-child { margin-bottom: 0; }
    .msg.assistant .md-body ul, .msg.assistant .md-body ol { margin: 0.4em 0; padding-left: 1.5em; }
    .msg.assistant .md-body li { margin: 0.2em 0; }
    .msg.assistant .md-body code {
      background: var(--code-bg); padding: 2px 6px; border-radius: 4px; font-size: 0.9em;
    }
    .msg.assistant .md-body pre {
      background: var(--code-bg); padding: 12px; border-radius: 8px;
      overflow-x: auto; margin: 0.6em 0; position: relative;
    }
    .msg.assistant .md-body pre code {
      background: none; padding: 0; font-size: 0.85em; line-height: 1.5;
    }
    .msg.assistant .md-body pre code.hljs { background: transparent; }
    .msg.assistant .md-body blockquote {
      border-left: 3px solid var(--accent); padding-left: 12px;
      margin: 0.6em 0; color: var(--text-secondary);
    }
    .msg.assistant .md-body table { border-collapse: collapse; margin: 0.6em 0; width: 100%; }
    .msg.assistant .md-body th, .msg.assistant .md-body td {
      border: 1px solid var(--border); padding: 6px 10px; text-align: left;
    }
    .msg.assistant .md-body th { background: var(--code-bg); font-weight: 600; }
    .msg.assistant .md-body h1, .msg.assistant .md-body h2, .msg.assistant .md-body h3 {
      margin: 0.6em 0 0.3em; line-height: 1.3;
    }
    .msg.assistant .md-body h1 { font-size: 1.3em; }
    .msg.assistant .md-body h2 { font-size: 1.15em; }
    .msg.assistant .md-body h3 { font-size: 1.05em; }
    .msg.assistant .md-body a { color: var(--accent); }
    .msg.assistant .md-body hr { border: none; border-top: 1px solid var(--border); margin: 0.8em 0; }

    /* Message footer: model badge + copy */
    .msg-footer {
      display: flex; align-items: center; gap: 8px;
      margin-top: 6px; font-size: 11px;
    }
    .msg-footer .ts { margin-top: 0; }
    .model-badge {
      background: var(--code-bg); color: var(--text-secondary);
      padding: 2px 8px; border-radius: 4px; font-weight: 500;
    }
    .copy-btn {
      background: none; border: 1px solid var(--border); border-radius: 4px;
      padding: 2px 8px; cursor: pointer; color: var(--text-secondary);
      font-size: 11px; transition: all 0.15s;
    }
    .copy-btn:hover { background: var(--code-bg); color: var(--text); }
    .code-copy-btn {
      position: absolute; top: 6px; right: 6px; background: var(--bg-msg-assist);
      border: 1px solid var(--border); border-radius: 4px; padding: 2px 8px;
      cursor: pointer; color: var(--text-secondary); font-size: 11px;
      opacity: 0; transition: opacity 0.15s;
    }
    .msg.assistant .md-body pre:hover .code-copy-btn { opacity: 1; }

    /* Sidebar */
    #app-layout { display: flex; flex: 1; overflow: hidden; }
    #sidebar {
      width: 260px; flex-shrink: 0; background: var(--bg-header);
      border-right: 1px solid var(--border); display: flex; flex-direction: column;
      transition: margin-left 0.2s;
    }
    #sidebar.hidden { margin-left: -260px; }
    #sidebar-header {
      padding: 12px; border-bottom: 1px solid var(--border);
      display: flex; gap: 8px;
    }
    #sidebar-header button {
      flex: 1; padding: 8px; font-size: 13px; font-weight: 600;
      border: 1px solid var(--border); border-radius: 6px; cursor: pointer;
      background: var(--accent); color: #fff; transition: background 0.15s;
    }
    #sidebar-header button:hover { background: var(--accent-hover); }
    #sidebar-toggle {
      background: none; border: 1px solid var(--border); border-radius: 6px;
      padding: 4px 10px; cursor: pointer; font-size: 16px; color: var(--text-secondary);
      flex-shrink: 0;
    }
    #convo-list {
      flex: 1; overflow-y: auto; padding: 8px;
    }
    .convo-item {
      padding: 10px 12px; border-radius: 8px; cursor: pointer;
      font-size: 13px; color: var(--text); margin-bottom: 2px;
      display: flex; justify-content: space-between; align-items: center;
      transition: background 0.1s;
    }
    .convo-item:hover { background: var(--code-bg); }
    .convo-item.active { background: var(--accent); color: #fff; }
    .convo-item .convo-title {
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1;
    }
    .convo-item .convo-delete {
      opacity: 0; background: none; border: none; cursor: pointer;
      color: inherit; font-size: 14px; padding: 0 4px; flex-shrink: 0;
    }
    .convo-item:hover .convo-delete { opacity: 0.6; }
    .convo-item .convo-delete:hover { opacity: 1; }

    #main-col { display: flex; flex-direction: column; flex: 1; min-width: 0; }

    #sidebar-backdrop {
      display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.4);
      z-index: 99;
    }
    #sidebar-backdrop.visible { display: block; }

    @keyframes pulse {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.6; }
    }

    .search-snippet {
      font-size: 11px; color: var(--text-muted); margin-top: 2px;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }

    @media (max-width: 700px) {
      header { padding: 8px 12px; }
      header h1 { font-size: 17px; }
      header nav { font-size: 12px; }
      #sidebar {
        position: fixed; left: 0; top: 0; bottom: 0; z-index: 100;
        width: 280px;
      }
      #sidebar.hidden { margin-left: -280px; }
      #chat-area { padding: 10px 8px; }
      .msg { max-width: 95%; font-size: 14px; padding: 8px 12px; }
      #input-bar { padding: 8px 8px calc(8px + env(safe-area-inset-bottom)); }
      #chat-form { gap: 6px; }
      #msg-input { font-size: 16px; padding: 10px 12px; }
      #chat-form button, #mic-btn { min-height: 44px; min-width: 44px; }
      .convo-item { padding: 12px; min-height: 44px; }
      #welcome { margin-top: 12vh; }
      #welcome h2 { font-size: 24px; }
    }
  </style>
  <script>
    (function(){var t=localStorage.getItem('chili-theme');if(t)document.documentElement.setAttribute('data-theme',t);})();
  </script>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.9.0/build/styles/github-dark.min.css"/>
  <script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></script>
  <script src="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.9.0/build/highlight.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/dompurify@3.0.6/dist/purify.min.js"></script>
</head>
<body>
  <header>
    <div style="display:flex;align-items:center;gap:10px;">
      <button id="sidebar-toggle-btn" title="Toggle sidebar" style="background:none;border:1px solid var(--border);border-radius:6px;padding:4px 10px;cursor:pointer;font-size:16px;color:var(--text-secondary);">&#x2630;</button>
      <div>
        <h1>&#x1F336;&#xFE0F; CHILI Chat</h1>
        <nav><a href="/">Home</a> &#xB7; <a href="/profile">Profile</a> &#xB7; <a href="/admin">Admin</a></nav>
      </div>
    </div>
    <div style="display:flex;align-items:center;gap:8px;">
      <button id="install-btn" style="display:none;background:var(--accent);color:#fff;border:none;border-radius:6px;padding:6px 12px;font-size:12px;font-weight:600;cursor:pointer;">Install App</button>
      <button id="theme-toggle" title="Toggle dark mode">&#x1F319;</button>
    </div>
  </header>

  <div id="app-layout">
    <div id="sidebar-backdrop"></div>
    <aside id="sidebar" class="hidden">
      <div id="sidebar-header">
        <button id="new-chat-btn">+ New Chat</button>
      </div>
      <div id="sidebar-search-wrap" style="padding:0 12px 8px;">
        <input id="convo-search" type="text" placeholder="Search conversations..." style="width:100%;padding:8px 10px;font-size:13px;border:1px solid var(--input-border);border-radius:6px;background:var(--input-bg);color:var(--text);outline:none;" />
      </div>
      <div id="convo-list"></div>
    </aside>

    <div id="main-col">
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
          <textarea id="msg-input" name="message" placeholder="Type a message..." autocomplete="off" required rows="1"></textarea>
          <button type="button" id="mic-btn" title="Voice input" style="background:none;border:1px solid var(--input-border);border-radius:8px;padding:6px 10px;cursor:pointer;font-size:18px;color:var(--text-secondary);transition:all 0.15s;display:none;">&#x1F3A4;</button>
          <button type="submit">Send</button>
        </form>
        <div id="status"></div>
      </div>
    </div>
  </div>

  <script>
    var chatArea  = document.getElementById('chat-area');
    var chatLog   = document.getElementById('chat-log');
    var welcome   = document.getElementById('welcome');
    var form      = document.getElementById('chat-form');
    var input     = document.getElementById('msg-input');
    var statusEl  = document.getElementById('status');
    var btn       = form.querySelector('button[type="submit"]');
    var themeBtn  = document.getElementById('theme-toggle');
    var sidebar   = document.getElementById('sidebar');
    var convoList = document.getElementById('convo-list');
    var newChatBtn = document.getElementById('new-chat-btn');
    var sidebarToggleBtn = document.getElementById('sidebar-toggle-btn');
    var sidebarBackdrop = document.getElementById('sidebar-backdrop');
    var micBtn    = document.getElementById('mic-btn');
    var convoSearch = document.getElementById('convo-search');

    var currentConvoId = null;
    var isGuest = true;

    var _marked = (typeof marked !== 'undefined') ? marked : null;
    var _DOMPurify = (typeof DOMPurify !== 'undefined') ? DOMPurify : null;
    var _hljs = (typeof hljs !== 'undefined') ? hljs : null;

    if (_marked && _marked.use) { try { _marked.use({ breaks: true, gfm: true }); } catch(e) {} }

    function renderMd(text) {
      if (!text) return '';
      try {
        var html = _marked ? _marked.parse(text) : esc(text).split(String.fromCharCode(10)).join('<br>');
        return _DOMPurify ? _DOMPurify.sanitize(html) : html;
      } catch(e) {
        return esc(text).split(String.fromCharCode(10)).join('<br>');
      }
    }

    function setTheme(t) {
      document.documentElement.setAttribute('data-theme', t);
      themeBtn.innerHTML = (t === 'dark') ? '&#9728;&#65039;' : '&#127769;';
      localStorage.setItem('chili-theme', t);
    }
    themeBtn.onclick = function() {
      var cur = document.documentElement.getAttribute('data-theme');
      setTheme(cur === 'dark' ? 'light' : 'dark');
    };
    setTheme(localStorage.getItem('chili-theme') || 'light');

    var SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
    var speechRec = null;
    var _wantMic = false;
    if (SpeechRec) {
      micBtn.style.display = '';
      function micOn() {
        micBtn.style.background = '#ef4444';
        micBtn.style.color = '#fff';
        micBtn.style.animation = 'pulse 1s infinite';
        statusEl.textContent = 'Listening... tap mic to stop';
      }
      function micOff() {
        micBtn.style.background = 'none';
        micBtn.style.color = 'var(--text-secondary)';
        micBtn.style.animation = '';
        if (statusEl.textContent.indexOf('Listen') === 0) statusEl.textContent = '';
      }
      function micStart() {
        speechRec = new SpeechRec();
        speechRec.continuous = true;
        speechRec.interimResults = true;
        speechRec.lang = 'en-US';
        speechRec.onresult = function(e) {
          var transcript = '';
          for (var i = 0; i < e.results.length; i++) {
            transcript += e.results[i][0].transcript;
          }
          input.value = transcript;
          input.style.height = 'auto';
          input.style.height = Math.min(input.scrollHeight, 160) + 'px';
        };
        speechRec.onend = function() {
          if (!_wantMic) { micOff(); input.focus(); return; }
          setTimeout(function() {
            if (!_wantMic) { micOff(); input.focus(); return; }
            try { micStart(); } catch(ex) { _wantMic = false; micOff(); }
          }, 800);
        };
        speechRec.onerror = function(e) {
          if (e.error === 'aborted' || e.error === 'no-speech') return;
          _wantMic = false;
          micOff();
          var msgs = { network: 'Voice needs internet connection', 'not-allowed': 'Mic permission denied (check browser settings)' };
          statusEl.textContent = msgs[e.error] || ('Mic error: ' + e.error);
          setTimeout(function() { var t = statusEl.textContent; if (t.indexOf('Mic') === 0 || t.indexOf('Voice') === 0) statusEl.textContent = ''; }, 4000);
        };
        speechRec.start();
      }
      function micToggle(e) {
        e.preventDefault();
        if (_wantMic) {
          _wantMic = false;
          try { speechRec.abort(); } catch(ex) {}
          micOff();
          input.focus();
          return;
        }
        _wantMic = true;
        micOn();
        try { micStart(); } catch(ex) { _wantMic = false; micOff(); }
      }
      micBtn.addEventListener('mousedown', micToggle);
      micBtn.addEventListener('touchstart', micToggle, { passive: false });
    }

    function esc(s) {
      var d = document.createElement('div');
      d.textContent = s;
      return d.innerHTML;
    }

    function fmtTime(raw) {
      if (!raw) return '';
      try {
        var d = new Date(raw.replace(' ', 'T'));
        if (isNaN(d)) return '';
        var now = new Date();
        var time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
        if (d.toDateString() === now.toDateString()) return time;
        return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ', ' + time;
      } catch(e) { return ''; }
    }

    function copyText(text, el) {
      navigator.clipboard.writeText(text).then(function() {
        var orig = el.textContent;
        el.textContent = 'Copied!';
        setTimeout(function() { el.textContent = orig; }, 1500);
      });
    }

    function addMsg(role, content, ts, meta) {
      welcome.style.display = 'none';
      var div = document.createElement('div');
      div.className = 'msg ' + role;
      var label = role === 'user' ? 'You' : 'CHILI';
      var timeStr = fmtTime(ts);

      var bodyHTML;
      if (role === 'assistant') {
        bodyHTML = '<div class="md-body">' + renderMd(content) + '<\/div>';
      } else {
        bodyHTML = '<div>' + esc(content).split(String.fromCharCode(10)).join('<br>') + '<\/div>';
      }

      var footerHTML = '';
      if (role === 'assistant') {
        footerHTML = '<div class="msg-footer">';
        if (meta && meta.model_used) {
          footerHTML += '<span class="model-badge">' + esc(meta.model_used) + '<\/span>';
        }
        footerHTML += '<button class="copy-btn" title="Copy message">Copy<\/button>';
        if (timeStr) footerHTML += '<span class="ts">' + esc(timeStr) + '<\/span>';
        footerHTML += '<\/div>';
      } else if (timeStr) {
        footerHTML = '<div class="ts">' + esc(timeStr) + '<\/div>';
      }

      div.innerHTML = '<div class="role">' + esc(label) + '<\/div>' + bodyHTML + footerHTML;

      if (_hljs) { div.querySelectorAll('pre code').forEach(function(el) { try { _hljs.highlightElement(el); } catch(e){} }); }
      div.querySelectorAll('.md-body pre').forEach(function(pre) {
        var cbtn = document.createElement('button');
        cbtn.className = 'code-copy-btn';
        cbtn.textContent = 'Copy';
        cbtn.onclick = function() { copyText(pre.querySelector('code').textContent, cbtn); };
        pre.appendChild(cbtn);
      });

      var msgCopyBtn = div.querySelector('.copy-btn');
      if (msgCopyBtn) {
        msgCopyBtn.onclick = function() {
          var md = div.querySelector('.md-body');
          copyText(md ? md.textContent : content, msgCopyBtn);
        };
      }

      chatLog.appendChild(div);
      chatArea.scrollTop = chatArea.scrollHeight;
    }

    input.addEventListener('input', function() {
      this.style.height = 'auto';
      this.style.height = Math.min(this.scrollHeight, 160) + 'px';
    });
    input.addEventListener('keydown', function(e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        form.dispatchEvent(new Event('submit', { cancelable: true }));
      }
    });

    function toggleSidebar() {
      var willShow = sidebar.classList.contains('hidden');
      sidebar.classList.toggle('hidden');
      sidebarBackdrop.classList.toggle('visible', willShow);
    }
    sidebarToggleBtn.addEventListener('click', toggleSidebar);
    sidebarBackdrop.addEventListener('click', function() {
      sidebar.classList.add('hidden');
      sidebarBackdrop.classList.remove('visible');
    });

    async function loadConversations() {
      await loadConversationsData();
    }

    function renderConvoList(convos) {
      convoList.innerHTML = '';
      convos.forEach(function(c) {
        var item = document.createElement('div');
        item.className = 'convo-item' + (c.id === currentConvoId ? ' active' : '');
        item.innerHTML = '<span class="convo-title">' + esc(c.title) + '<\/span>'
          + '<button class="convo-delete" title="Delete">&times;<\/button>';
        item.querySelector('.convo-title').onclick = function() { switchConvo(c.id); };
        item.querySelector('.convo-delete').onclick = function(e) {
          e.stopPropagation();
          deleteConvo(c.id);
        };
        convoList.appendChild(item);
      });
    }

    async function switchConvo(id) {
      currentConvoId = id;
      chatLog.innerHTML = '';
      welcome.style.display = 'none';
      await loadHistory(id);
      await loadConversations();
    }

    async function deleteConvo(id) {
      await fetch('/api/conversations/' + id, { method: 'DELETE' });
      if (currentConvoId === id) {
        currentConvoId = null;
        chatLog.innerHTML = '';
        welcome.style.display = '';
      }
      await loadConversations();
    }

    newChatBtn.addEventListener('click', async function() {
      currentConvoId = null;
      chatLog.innerHTML = '';
      welcome.style.display = '';
      var items = convoList.querySelectorAll('.convo-item');
      items.forEach(function(i) { i.classList.remove('active'); });
      input.focus();
    });

    async function loadHistory(convoId) {
      try {
        var url = '/api/chat/history';
        if (convoId != null) url += '?conversation_id=' + convoId;
        var res = await fetch(url);
        var data = await res.json();
        statusEl.textContent = data.is_guest
          ? 'Guest mode (read-only for writes)'
          : 'Signed in as ' + data.user;
        if (data.messages && data.messages.length > 0) {
          data.messages.forEach(function(m) {
            addMsg(m.role, m.content, m.created_at, { model_used: m.model_used });
          });
        }
      } catch(e) {
        statusEl.textContent = 'Could not load history.';
      }
    }

    function createStreamingMsg() {
      welcome.style.display = 'none';
      var div = document.createElement('div');
      div.className = 'msg assistant';
      div.innerHTML = '<div class="role">CHILI<\/div><div class="md-body"><\/div>';
      chatLog.appendChild(div);
      return div;
    }

    function finalizeStreamingMsg(div, fullText, meta) {
      var mdBody = div.querySelector('.md-body');
      mdBody.innerHTML = renderMd(fullText);

      if (_hljs) { div.querySelectorAll('pre code').forEach(function(el) { try { _hljs.highlightElement(el); } catch(e){} }); }
      div.querySelectorAll('.md-body pre').forEach(function(pre) {
        var cbtn = document.createElement('button');
        cbtn.className = 'code-copy-btn';
        cbtn.textContent = 'Copy';
        cbtn.onclick = function() { copyText(pre.querySelector('code').textContent, cbtn); };
        pre.appendChild(cbtn);
      });

      var footer = document.createElement('div');
      footer.className = 'msg-footer';
      if (meta && meta.model_used) {
        footer.innerHTML += '<span class="model-badge">' + esc(meta.model_used) + '<\/span>';
      }
      var copyBtn = document.createElement('button');
      copyBtn.className = 'copy-btn';
      copyBtn.textContent = 'Copy';
      copyBtn.title = 'Copy message';
      copyBtn.onclick = function() { copyText(mdBody.textContent, copyBtn); };
      footer.appendChild(copyBtn);
      div.appendChild(footer);
    }

    form.addEventListener('submit', async function(e) {
      e.preventDefault();
      var msg = input.value.trim();
      if (!msg) return;

      addMsg('user', msg, '');
      input.value = '';
      input.style.height = 'auto';
      btn.disabled = true;
      statusEl.textContent = 'Thinking...';

      try {
        var body = new FormData();
        body.append('message', msg);
        if (currentConvoId != null) body.append('conversation_id', currentConvoId);

        var res = await fetch('/api/chat/stream', { method: 'POST', body: body });

        if (!res.ok || !res.body) {
          var fbody = new FormData();
          fbody.append('message', msg);
          if (currentConvoId != null) fbody.append('conversation_id', currentConvoId);
          var fres = await fetch('/api/chat', { method: 'POST', body: fbody });
          var fdata = await fres.json();
          if (fdata.conversation_id && currentConvoId == null) currentConvoId = fdata.conversation_id;
          addMsg('assistant', fdata.reply || '(no reply)', '', { model_used: fdata.model_used });
          statusEl.textContent = '';
          if (!isGuest) loadConversations();
          return;
        }

        var streamDiv = createStreamingMsg();
        var fullText = '';
        var reader = res.body.getReader();
        var decoder = new TextDecoder();
        var buffer = '';
        var meta = {};

        while (true) {
          var result = await reader.read();
          if (result.done) break;
          buffer += decoder.decode(result.value, { stream: true });

          var lines = buffer.split(String.fromCharCode(10));
          buffer = lines.pop();

          for (var i = 0; i < lines.length; i++) {
            var line = lines[i];
            if (line.startsWith('data: ')) {
              try {
                var evt = JSON.parse(line.slice(6));
                if (evt.done) {
                  meta = evt;
                  if (evt.conversation_id && currentConvoId == null) {
                    currentConvoId = evt.conversation_id;
                  }
                } else if (evt.token) {
                  fullText += evt.token;
                  var mdBody = streamDiv.querySelector('.md-body');
                  mdBody.innerHTML = renderMd(fullText);
                  chatArea.scrollTop = chatArea.scrollHeight;
                }
              } catch(pe) {}
            }
          }
        }

        finalizeStreamingMsg(streamDiv, fullText, meta);
        statusEl.textContent = '';
        if (!isGuest) loadConversations();
      } catch(e) {
        addMsg('assistant', 'Error: could not reach CHILI backend.', '');
        statusEl.textContent = '';
      } finally {
        btn.disabled = false;
        input.focus();
      }
    });

    var _allConvos = [];

    async function loadConversationsData() {
      try {
        var res = await fetch('/api/conversations');
        var data = await res.json();
        isGuest = data.is_guest;
        if (isGuest) {
          sidebar.classList.add('hidden');
          sidebarToggleBtn.style.display = 'none';
          return [];
        }
        sidebar.classList.remove('hidden');
        sidebarToggleBtn.style.display = '';
        _allConvos = data.conversations || [];
        renderConvoList(_allConvos);
        return _allConvos;
      } catch(e) { return []; }
    }

    var _searchTimer = null;
    convoSearch.addEventListener('input', function() {
      clearTimeout(_searchTimer);
      var q = convoSearch.value.trim();
      if (!q) { renderConvoList(_allConvos); return; }
      _searchTimer = setTimeout(async function() {
        try {
          var res = await fetch('/api/conversations/search?q=' + encodeURIComponent(q));
          var data = await res.json();
          renderSearchResults(data.results || []);
        } catch(e) { renderConvoList(_allConvos); }
      }, 300);
    });

    function renderSearchResults(results) {
      convoList.innerHTML = '';
      if (results.length === 0) {
        convoList.innerHTML = '<div style="padding:12px;font-size:13px;color:var(--text-muted);text-align:center;">No matches<\/div>';
        return;
      }
      results.forEach(function(r) {
        var item = document.createElement('div');
        item.className = 'convo-item' + (r.id === currentConvoId ? ' active' : '');
        var html = '<div style="flex:1;min-width:0;"><div class="convo-title">' + esc(r.title) + '<\/div>';
        if (r.snippet) html += '<div class="search-snippet">' + esc(r.snippet) + '<\/div>';
        html += '<\/div>';
        item.innerHTML = html;
        item.onclick = function() { convoSearch.value = ''; switchConvo(r.id); };
        convoList.appendChild(item);
      });
    }

    var _installPrompt = null;
    var installBtn = document.getElementById('install-btn');
    window.addEventListener('beforeinstallprompt', function(e) {
      e.preventDefault();
      _installPrompt = e;
      installBtn.style.display = '';
    });
    installBtn.onclick = function() {
      if (!_installPrompt) return;
      _installPrompt.prompt();
      _installPrompt.userChoice.then(function() { installBtn.style.display = 'none'; _installPrompt = null; });
    };
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register('/static/sw.js').catch(function() {});
    }

    (async function() {
      var convos = await loadConversationsData();
      if (!isGuest && convos.length > 0) {
        await switchConvo(convos[0].id);
      } else if (isGuest) {
        await loadHistory();
      }
      input.focus();
    })();
  </script>
</body>
</html>
"""
    return HTMLResponse(content=html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

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

@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, db: Session = Depends(get_db)):
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)

    if identity["is_guest"] or not identity["user_id"]:
        return HTMLResponse(
            "<html><body style='font-family:Arial;max-width:800px;margin:40px auto;'>"
            "<h1>Profile</h1><p>You need to be a paired housemate to view your profile.</p>"
            "<p><a href='/pair'>Pair your device</a> | <a href='/chat'>Back to Chat</a></p>"
            "</body></html>"
        )

    user_id = identity["user_id"]
    user_name = identity["user_name"]
    profile = db.query(HousemateProfile).filter(HousemateProfile.user_id == user_id).first()

    import json as _json
    interests_str = ""
    if profile and profile.interests:
        try:
            interests_list = _json.loads(profile.interests)
            interests_str = ", ".join(interests_list)
        except _json.JSONDecodeError:
            interests_str = profile.interests

    dietary = profile.dietary if profile else ""
    tone = profile.tone if profile else ""
    notes = profile.notes if profile else ""
    last_updated = profile.last_extracted_at.strftime("%B %d, %Y %H:%M") if profile and profile.last_extracted_at else "Never"

    return f"""
    <html><head>
      <title>CHILI Profile - {user_name}</title>
      <meta charset="utf-8"/>
      <meta name="viewport" content="width=device-width, initial-scale=1" />
    </head><body style="font-family:Arial;max-width:800px;margin:40px auto;">
      <h1>Your Profile</h1>
      <p><a href="/chat">Chat</a> | <a href="/">Home</a></p>
      <p>Hi <b>{user_name}</b>! CHILI learns about you from your conversations to personalize responses.</p>

      <form method="post" action="/profile">
        <div style="margin:16px 0;">
          <label><b>Interests</b> (comma-separated)</label><br/>
          <input name="interests" value="{interests_str}" style="width:100%;max-width:520px;padding:10px;font-size:15px;" />
        </div>
        <div style="margin:16px 0;">
          <label><b>Dietary preferences</b></label><br/>
          <input name="dietary" value="{dietary}" placeholder="e.g., vegetarian, no dairy" style="width:100%;max-width:520px;padding:10px;font-size:15px;" />
        </div>
        <div style="margin:16px 0;">
          <label><b>Communication style</b></label><br/>
          <input name="tone" value="{tone}" placeholder="e.g., casual, brief, formal" style="width:100%;max-width:520px;padding:10px;font-size:15px;" />
        </div>
        <div style="margin:16px 0;">
          <label><b>Notes</b> (anything CHILI should know)</label><br/>
          <textarea name="notes" rows="3" style="width:100%;max-width:520px;padding:10px;font-size:15px;">{notes}</textarea>
        </div>
        <button type="submit" style="padding:10px 20px;font-size:16px;">Save Profile</button>
      </form>

      <p style="color:#888;font-size:12px;margin-top:24px;">
        Last auto-updated: {last_updated}
      </p>
    </body></html>
    """


@app.post("/profile")
def profile_save(
    request: Request,
    interests: str = Form(""),
    dietary: str = Form(""),
    tone: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)

    if identity["is_guest"] or not identity["user_id"]:
        return RedirectResponse("/profile", status_code=303)

    user_id = identity["user_id"]
    import json as _json

    interests_list = [i.strip() for i in interests.split(",") if i.strip()]
    interests_json = _json.dumps(interests_list)

    profile = db.query(HousemateProfile).filter(HousemateProfile.user_id == user_id).first()
    if profile:
        profile.interests = interests_json
        profile.dietary = dietary.strip()
        profile.tone = tone.strip()
        profile.notes = notes.strip()
    else:
        db.add(HousemateProfile(
            user_id=user_id,
            interests=interests_json,
            dietary=dietary.strip(),
            tone=tone.strip(),
            notes=notes.strip(),
        ))
    db.commit()

    return RedirectResponse("/profile", status_code=303)


def _model_stats(db: Session) -> dict:
    """Count assistant messages by model_used."""
    from sqlalchemy import func
    rows = (
        db.query(ChatMessage.model_used, func.count(ChatMessage.id))
        .filter(ChatMessage.role == "assistant")
        .group_by(ChatMessage.model_used)
        .all()
    )
    return {model or "unknown": count for model, count in rows}


@app.get("/metrics", response_class=JSONResponse)
def metrics(db: Session = Depends(get_db)):
    return {
        "counts": get_counts(db),
        "llm_chat_latency": latency_stats(),
        "model_usage": _model_stats(db),
    }

@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(db: Session = Depends(get_db)):
    # Reuse existing health + metrics logic
    db_status = check_db(db)
    ollama_status = check_ollama()

    counts = get_counts(db)
    lat = latency_stats()

    ok = bool(db_status.get("ok") and ollama_status.get("ok"))

    model_stats = _model_stats(db)
    openai_configured = openai_client.is_configured()

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

        <h2>Model Usage</h2>
        <div style="padding: 12px; background: #f5f5f5; border: 1px solid #ddd;">
          <p><b>OpenAI:</b> {'Configured (' + openai_client.OPENAI_MODEL + ')' if openai_configured else 'Not configured (set OPENAI_API_KEY in .env)'}</p>
          {''.join(f'<p><b>{model}:</b> {count} messages</p>' for model, count in model_stats.items()) or '<p>No messages yet.</p>'}
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

@app.get("/api/conversations", response_class=JSONResponse)
def list_conversations(request: Request, db: Session = Depends(get_db)):
    client_ip = request.client.host
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)
    convo_key = get_convo_key(identity, device_token, client_ip)

    if identity["is_guest"]:
        return {"conversations": [], "is_guest": True}

    convos = (
        db.query(Conversation)
        .filter(Conversation.convo_key == convo_key)
        .order_by(Conversation.created_at.desc())
        .all()
    )
    return {
        "is_guest": False,
        "conversations": [
            {"id": c.id, "title": c.title, "created_at": str(c.created_at)}
            for c in convos
        ],
    }


@app.post("/api/conversations", response_class=JSONResponse)
def create_conversation(request: Request, db: Session = Depends(get_db)):
    client_ip = request.client.host
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)
    convo_key = get_convo_key(identity, device_token, client_ip)

    if identity["is_guest"]:
        return JSONResponse({"error": "Guests cannot create conversations"}, status_code=403)

    convo = Conversation(convo_key=convo_key, title="New Chat")
    db.add(convo)
    db.commit()
    db.refresh(convo)
    return {"id": convo.id, "title": convo.title, "created_at": str(convo.created_at)}


@app.delete("/api/conversations/{convo_id}", response_class=JSONResponse)
def delete_conversation(convo_id: int, request: Request, db: Session = Depends(get_db)):
    client_ip = request.client.host
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)
    convo_key = get_convo_key(identity, device_token, client_ip)

    convo = db.query(Conversation).filter(
        Conversation.id == convo_id,
        Conversation.convo_key == convo_key,
    ).first()

    if not convo:
        return JSONResponse({"error": "Not found"}, status_code=404)

    db.delete(convo)
    db.commit()
    return {"ok": True}


@app.get("/api/conversations/search", response_class=JSONResponse)
def search_conversations(request: Request, q: str = Query(""), db: Session = Depends(get_db)):
    client_ip = request.client.host
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)
    convo_key = get_convo_key(identity, device_token, client_ip)

    if not q.strip() or identity.get("is_guest"):
        return {"results": []}

    pattern = f"%{q.strip()}%"
    matches = (
        db.query(ChatMessage.conversation_id, ChatMessage.content)
        .filter(
            ChatMessage.convo_key == convo_key,
            ChatMessage.conversation_id.isnot(None),
            ChatMessage.content.ilike(pattern),
        )
        .order_by(ChatMessage.id.desc())
        .limit(100)
        .all()
    )

    seen = {}
    for convo_id, content in matches:
        if convo_id not in seen:
            snippet = content[:80] + ("..." if len(content) > 80 else "")
            seen[convo_id] = snippet

    convo_ids = list(seen.keys())[:20]
    convos = (
        db.query(Conversation)
        .filter(Conversation.id.in_(convo_ids))
        .all()
    )
    convo_map = {c.id: c.title for c in convos}

    results = [
        {"id": cid, "title": convo_map.get(cid, "Untitled"), "snippet": seen[cid]}
        for cid in convo_ids
        if cid in convo_map
    ]
    return {"results": results}


@app.get("/api/chat/history", response_class=JSONResponse)
def chat_history(
    request: Request,
    conversation_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
):
    client_ip = request.client.host
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)

    identity = get_identity_record(db, device_token)
    convo_key = get_convo_key(identity, device_token, client_ip)

    q = db.query(ChatMessage).filter(
        ChatMessage.convo_key == convo_key,
        ChatMessage.content != "",
    )

    if conversation_id is not None:
        q = q.filter(ChatMessage.conversation_id == conversation_id)
    elif not identity["is_guest"]:
        latest = (
            db.query(Conversation)
            .filter(Conversation.convo_key == convo_key)
            .order_by(Conversation.created_at.desc())
            .first()
        )
        if latest:
            q = q.filter(ChatMessage.conversation_id == latest.id)
            conversation_id = latest.id
        else:
            q = q.filter(ChatMessage.conversation_id == None)

    msgs = q.order_by(ChatMessage.id.asc()).limit(50).all()

    return {
        "convo_key": convo_key,
        "user": identity["user_name"],
        "is_guest": identity["is_guest"],
        "messages": [
            {"role": m.role, "content": m.content, "created_at": str(m.created_at), "model_used": m.model_used}
            for m in msgs
        ],
    }


@app.post("/api/chat", response_class=JSONResponse)
def chat_api(
    request: Request,
    message: str = Form(...),
    conversation_id: Optional[int] = Form(None),
    db: Session = Depends(get_db),
):
    trace_id = new_trace_id()
    t0 = time.time()

    client_ip = request.client.host
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)

    identity = get_identity_record(db, device_token)
    convo_key = get_convo_key(identity, device_token, client_ip)

    user_name = identity["user_name"]
    is_guest = identity["is_guest"]

    # For paired users: auto-create conversation if needed
    if not is_guest and conversation_id is None:
        convo = Conversation(convo_key=convo_key, title="New Chat")
        db.add(convo)
        db.commit()
        db.refresh(convo)
        conversation_id = convo.id

    log_info(trace_id, f"client_ip={client_ip} user={user_name} guest={is_guest} convo={convo_key} conversation_id={conversation_id}")
    log_info(trace_id, f"chat_message={message!r}")

    db.add(ChatMessage(convo_key=convo_key, conversation_id=conversation_id, role="user", content=message, trace_id=trace_id))
    db.commit()

    # load memory window (last 12 messages for this conversation)
    mem_filter = ChatMessage.conversation_id == conversation_id if conversation_id else ChatMessage.convo_key == convo_key
    recent = (
        db.query(ChatMessage)
        .filter(mem_filter)
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

    # Personality: load profile context for paired users
    personality_context = None
    user_id = identity.get("user_id")
    if user_id and not identity["is_guest"]:
        personality_context = personality_module.get_profile_context(user_id, db)
        if personality_context:
            log_info(trace_id, f"personality_injected user_id={user_id}")

    try:
        planned = plan_action(
            f"Conversation so far:\n{context}\n\nNew user message: {message}",
            rag_context=rag_context,
            personality_context=personality_context,
        )
    except Exception as e:
        log_info(trace_id, f"llm_error={e}")
        llm_reply = (
            "CHILI's brain is offline. "
            "Start Ollama to use chat: ollama serve"
        )
        db.add(ChatMessage(convo_key=convo_key, conversation_id=conversation_id, role="assistant", content=llm_reply, trace_id=trace_id, action_type="llm_offline", model_used="offline"))
        db.commit()
        db.add(ChatLog(client_ip=client_ip, trace_id=trace_id, message=message, action_type="llm_offline"))
        db.commit()
        ms = int((time.time() - t0) * 1000)
        record_latency(ms)
        log_info(trace_id, f"latency_ms={ms} action=llm_offline")
        return {"trace_id": trace_id, "user": user_name, "is_guest": is_guest, "action_type": "llm_offline", "executed": False, "reply": llm_reply, "conversation_id": conversation_id}

    action_type = planned.get("type", "unknown")
    action_data = planned.get("data", {})
    llm_reply = planned.get("reply") or ""

    llm_reply, executed, action_type = _execute_tool(db, action_type, action_data, llm_reply, is_guest)

    # OpenAI fallback: if planner returned unknown and OpenAI is configured,
    # route to OpenAI for a full conversational response
    model_used = "llama3"
    if action_type == "unknown" and openai_client.is_configured():
        openai_messages = [
            {"role": m.role, "content": m.content} for m in recent
        ]
        openai_system = openai_client.SYSTEM_PROMPT
        openai_system += f"\n\nYou are talking to: {user_name}."
        if personality_context:
            openai_system += f"\n\n{personality_context}"
        if rag_context:
            openai_system += f"\n\nHousehold document context:\n{rag_context}"

        result = openai_client.chat(
            messages=openai_messages,
            system_prompt=openai_system,
            trace_id=trace_id,
        )
        if result["reply"]:
            llm_reply = result["reply"]
            action_type = "general_chat"
            model_used = result["model"]
            executed = True
            log_info(trace_id, f"openai_fallback tokens={result['tokens_used']} model={model_used}")

    if not llm_reply:
        llm_reply = "I'm not sure what to do with that. Try: add chore, list chores, add birthday, list birthdays."

    db.add(ChatMessage(
        convo_key=convo_key,
        conversation_id=conversation_id,
        role="assistant",
        content=llm_reply,
        trace_id=trace_id,
        action_type=action_type,
        model_used=model_used,
    ))
    db.commit()

    # Auto-title: set conversation title from first user message
    if conversation_id:
        convo_obj = db.query(Conversation).filter(Conversation.id == conversation_id).first()
        if convo_obj and convo_obj.title == "New Chat":
            convo_obj.title = message[:40].strip() + ("..." if len(message) > 40 else "")
            db.commit()

    db.add(ChatLog(
        client_ip=client_ip,
        trace_id=trace_id,
        message=message,
        action_type=action_type
    ))
    db.commit()

    if user_id and not is_guest:
        try:
            if personality_module.should_update(user_id, db):
                personality_module.extract_profile(user_id, db, trace_id=trace_id)
        except Exception as e:
            log_info(trace_id, f"personality_extraction_error={e}")

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
        "model_used": model_used,
        "conversation_id": conversation_id,
    }


def _sse_event(data: dict) -> str:
    return f"data: {json_mod.dumps(data)}\n\n"


def _store_and_title(convo_key, conversation_id, content, trace_id, action_type, model_used, client_ip, message):
    """Store assistant message and auto-title in a fresh DB session (safe for generators)."""
    s = SessionLocal()
    try:
        s.add(ChatMessage(
            convo_key=convo_key, conversation_id=conversation_id,
            role="assistant", content=content, trace_id=trace_id,
            action_type=action_type, model_used=model_used,
        ))
        if conversation_id:
            c = s.query(Conversation).filter(Conversation.id == conversation_id).first()
            if c and c.title == "New Chat":
                c.title = message[:40].strip() + ("..." if len(message) > 40 else "")
        s.add(ChatLog(client_ip=client_ip, trace_id=trace_id, message=message, action_type=action_type))
        s.commit()
    finally:
        s.close()


@app.post("/api/chat/stream")
def chat_stream_api(
    request: Request,
    message: str = Form(...),
    conversation_id: Optional[int] = Form(None),
    db: Session = Depends(get_db),
):
    trace_id = new_trace_id()
    t0 = time.time()

    client_ip = request.client.host
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)
    convo_key = get_convo_key(identity, device_token, client_ip)
    user_name = identity["user_name"]
    is_guest = identity["is_guest"]
    user_id = identity.get("user_id")

    if not is_guest and conversation_id is None:
        convo = Conversation(convo_key=convo_key, title="New Chat")
        db.add(convo)
        db.commit()
        db.refresh(convo)
        conversation_id = convo.id

    db.add(ChatMessage(convo_key=convo_key, conversation_id=conversation_id, role="user", content=message, trace_id=trace_id))
    db.commit()

    mem_filter = ChatMessage.conversation_id == conversation_id if conversation_id else ChatMessage.convo_key == convo_key
    recent = list(reversed(
        db.query(ChatMessage).filter(mem_filter).order_by(ChatMessage.id.desc()).limit(12).all()
    ))
    context = "\n".join([f"{m.role.upper()}: {m.content}" for m in recent])

    rag_context = None
    rag_hits = rag_module.search(message, n_results=3, trace_id=trace_id)
    if rag_hits and rag_hits[0]["distance"] < 1.5:
        rag_context = "\n---\n".join(f"[{h['source']}]: {h['text']}" for h in rag_hits)

    personality_context = None
    if user_id and not is_guest:
        personality_context = personality_module.get_profile_context(user_id, db)

    # Plan
    try:
        planned = plan_action(
            f"Conversation so far:\n{context}\n\nNew user message: {message}",
            rag_context=rag_context,
            personality_context=personality_context,
        )
    except Exception:
        reply = "CHILI's brain is offline. Start Ollama to use chat: ollama serve"
        def offline_gen():
            yield _sse_event({"token": reply, "done": False})
            yield _sse_event({"token": "", "done": True, "action_type": "llm_offline", "model_used": "offline", "conversation_id": conversation_id})
            _store_and_title(convo_key, conversation_id, reply, trace_id, "llm_offline", "offline", client_ip, message)
        return StreamingResponse(offline_gen(), media_type="text/event-stream")

    action_type = planned.get("type", "unknown")
    action_data = planned.get("data", {})
    llm_reply = planned.get("reply") or ""

    llm_reply, executed, action_type = _execute_tool(db, action_type, action_data, llm_reply, is_guest)

    # For tool actions or when OpenAI is not available: send full reply as one chunk
    if action_type != "unknown" or not openai_client.is_configured():
        if not llm_reply:
            llm_reply = "I'm not sure what to do with that. Try: add chore, list chores, add birthday, list birthdays."
        model_used = "llama3"

        def tool_gen():
            yield _sse_event({"token": llm_reply, "done": False})
            yield _sse_event({"token": "", "done": True, "action_type": action_type, "model_used": model_used, "conversation_id": conversation_id})
            _store_and_title(convo_key, conversation_id, llm_reply, trace_id, action_type, model_used, client_ip, message)
        return StreamingResponse(tool_gen(), media_type="text/event-stream")

    # OpenAI streaming fallback
    openai_messages = [{"role": m.role, "content": m.content} for m in recent]
    openai_system = openai_client.SYSTEM_PROMPT
    openai_system += f"\n\nYou are talking to: {user_name}."
    if personality_context:
        openai_system += f"\n\n{personality_context}"
    if rag_context:
        openai_system += f"\n\nHousehold document context:\n{rag_context}"

    def stream_gen():
        full_reply = []
        for token in openai_client.chat_stream(messages=openai_messages, system_prompt=openai_system, trace_id=trace_id):
            full_reply.append(token)
            yield _sse_event({"token": token, "done": False})

        complete = "".join(full_reply)
        if not complete:
            complete = "I'm not sure what to do with that."
            yield _sse_event({"token": complete, "done": False})

        yield _sse_event({"token": "", "done": True, "action_type": "general_chat", "model_used": openai_client.OPENAI_MODEL, "conversation_id": conversation_id})
        _store_and_title(convo_key, conversation_id, complete, trace_id, "general_chat", openai_client.OPENAI_MODEL, client_ip, message)

    return StreamingResponse(stream_gen(), media_type="text/event-stream")
# CHILI Home Copilot

**Conversational Home Interface & Life Intelligence**

A local-first household assistant powered by a local LLM (Ollama llama3) with optional OpenAI fallback. Housemates interact through natural language to manage chores, birthday reminders, ask questions about household documents, and have general conversations -- all running on your own hardware with cloud AI as an optional upgrade.

Built as a production-style LLM application showcasing multi-model routing, RAG (Retrieval-Augmented Generation), personality profiling, tool-calling architecture, strict validation, identity/auth, observability, and guardrails.

## Architecture

```
 Browser (/chat)                    Admin (/admin)
      │  Sidebar + Markdown + SSE          │
      ▼                                    ▼
 ┌──────────────────────────────────────────────┐
 │              FastAPI (Python)                 │
 │                                              │
 │  /api/chat/stream ──► RAG ──► LLM Planner    │
 │   (SSE streaming)   (ChromaDB)   (Ollama)    │
 │         │               │    +       │       │
 │         │            context  personality  Validator│
 │         │            injected  profile   (Pydantic)│
 │         │                               │    │
 │         │                     tool action?   │
 │         │                    ╱     ╲         │
 │         │                 yes       no       │
 │         │                  │         │       │
 │         │             Tool Exec  OpenAI SSE  │
 │         │                  │    (streaming)  │
 │         ▼                  ▼         │       │
 │       PostgreSQL ◄──────────┘─────────┘      │
 │  (conversations, chat_messages, …)          │
 │                                              │
 │  Identity: cookie ──► Device ──► User        │
 │  Conversations: sidebar, auto-title, CRUD    │
 │  Observability: trace_id, model_used, logs   │
 │  Guardrails: guest read-only, schema reject  │
 └──────────────────────────────────────────────┘
```

**Key design principle: "LLM plans, code executes."** The model chooses an action via strict JSON; application code validates the schema and performs the database operation. The LLM never touches state directly.

## Features

### Trading & AI Analysis
- **Full-featured trading terminal** at `/trading` with interactive charts (LightweightCharts), watchlist, portfolio, and journal
- **AI Analyze**: One-click streaming AI analysis of any ticker with rich context (indicators, fundamentals, patterns, market pulse) — powered by free-tier LLM cascade (Groq → Gemini → OpenAI last resort)
- **AI Brain**: Self-learning pattern discovery engine that mines market snapshots, tests hypotheses, and evolves scoring weights over time
  - 30+ technical indicators (RSI, MACD, EMA stack, Bollinger Bands, Stochastic, ADX, ATR, etc.)
  - **News sentiment analysis** via VADER — aggregated sentiment scored per ticker and used in pattern mining
  - **Fundamental data integration** — P/E ratio, market cap fed into ML features and pattern discovery
  - Machine learning predictions (GradientBoosting) with technical + sentiment + fundamental features
  - Novel pattern discovery, hypothesis testing, adaptive weight evolution, and confidence decay
- **Scanner & Screener**: Batch scoring of 100+ tickers with parallel processing, breakout detection, and momentum scanning
- **CHILI's Top Picks**: Cached market-wide recommendations with stale-while-revalidate for instant load
- **News cards** with sentiment badges (bullish/bearish/neutral), embedded article reader modal, and horizontal scroll
- **Strategy proposals**: AI-generated trade setups with approve/reject workflow
- **Alerts**: Price target and percent-change alerts with SMS delivery (Twilio) and DB logging
- **Performance optimizations**: Batch yfinance downloads, parallel ticker scoring, 5-minute market-context cache, ThreadPoolExecutor throughout

### LLM Tool Calling
- Natural language mapped to structured actions (`add_chore`, `mark_chore_done`, `add_birthday`, `list_chores`, `answer_from_docs`, etc.)
- Planner returns strict JSON `{type, data, reply}` validated by Pydantic discriminated-union schemas
- Invalid schemas, bad dates, unknown IDs, and extra fields are all rejected to safe fallback
- Ambiguous requests return `type=unknown` with a clarifying question -- no guessing

### RAG (Retrieval-Augmented Generation)
- Drop `.txt` files into `docs/` (house info, rules, recipes, manuals) and run `python -m app.ingest`
- Documents are chunked by paragraph, embedded with Ollama `nomic-embed-text`, and stored in ChromaDB
- Every chat message triggers a vector similarity search; relevant chunks are injected into the LLM prompt
- The LLM uses `answer_from_docs` action type to cite which document it used
- Graceful degradation: if ChromaDB is empty or embeddings are unavailable, CHILI falls back to normal tool-calling

### Smart Multi-Model Routing
- **Free-tier LLM cascade**: Groq (primary) → secondary Groq model → Gemini (free) → OpenAI (paid last resort)
- Local-first: tool actions (chores, birthdays, RAG) always use llama3 locally (free, private)
- **Streaming responses**: all LLM tiers stream tokens via SSE for real-time ChatGPT-like typing experience
- `model_used` tracked on every message for observability (`/admin`, `/metrics`)

### Housemate Personality Profiles
- CHILI automatically learns each housemate's personality from their conversations
- Profiles extracted every 20 messages via OpenAI (interests, dietary preferences, tone, notes)
- Personality context injected into both llama3 and OpenAI prompts for personalized responses
- `/profile` page where housemates can view and edit what CHILI has learned
- Privacy: only your own profile visible. Admin can see all profiles

### ChatGPT-Style Chat UI
- **Markdown rendering** with syntax-highlighted code blocks (marked.js + highlight.js)
- **Streaming responses** via Server-Sent Events -- tokens appear in real-time like ChatGPT
- **Conversation sidebar** for paired users: create, switch, and delete named conversations
- **Multi-line input** with auto-resize textarea (Enter sends, Shift+Enter for newline)
- **Model badge** on each assistant message showing which LLM responded (llama3, gpt-4o-mini, etc.)
- **Copy buttons** on messages and code blocks
- Dark mode with persistent theme toggle
- Guests see a single continuous thread; paired users get full conversation management

### Chat Memory
- Persistent conversation history per user stored in PostgreSQL (`chat_messages` + `conversations` tables)
- Memory scoped by `convo_key`: paired users get named conversations, guests get isolated threads
- Last 12 messages sent as context window to the planner for multi-turn conversations

### Identity & Device Pairing
- Admin registers housemates with name + email at `/admin/users`
- **Self-service pairing**: guests click "Link your device" in `/chat`, enter their email, receive a 6-digit code via Gmail SMTP, and verify to pair
- When email isn't configured, the code is shown directly (dev mode)
- Manual fallback via `/pair` with admin-generated codes still available
- Cookie-based device tokens map browsers to users; unpaired devices are Guest (read-only)

### Guardrails & Safety
- **Guest read-only enforcement**: write actions (`add_chore`, `mark_chore_done`, `add_birthday`) are blocked *before* any DB mutation
- **Strict Pydantic validation**: every LLM output is validated against typed schemas with `extra="forbid"` -- no prompt injection can add unexpected fields
- **Fallback parser**: if Ollama is unavailable, a rule-based regex parser keeps the app functional
- **Temperature 0**: deterministic LLM output for predictable tool calling

### Observability
- `trace_id` generated per request, threaded through logs and shown in the UI
- Structured log format: `timestamp | level | trace=<id> | message`
- `/health` endpoint checks DB connectivity and Ollama availability
- `/metrics` returns chore/birthday counts plus LLM latency stats (avg, p95)
- `/admin` dashboard with live health status, counts, latency, recent audit logs, and data exports

### Admin Tooling
- Dashboard at `/admin` with system health, metrics, and recent chat activity
- User management and pairing code generation at `/admin/users`
- CSV exports for chores and birthdays
- Demo data reset

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11, FastAPI |
| Database | PostgreSQL via SQLAlchemy |
| LLM (local) | Ollama (llama3 for planning, nomic-embed-text for embeddings) |
| LLM (cloud) | Tiered cascade: Groq (free) → Gemini (free) → OpenAI (paid last resort) |
| Vector Store | ChromaDB (local, persistent) |
| ML / NLP | scikit-learn (GradientBoosting), ta (technical analysis), vaderSentiment |
| Charts | LightweightCharts (TradingView), yfinance for market data |
| Validation | Pydantic v2 (strict schemas, discriminated unions) |
| Frontend | Server-rendered HTML + vanilla JS (`fetch()` for chat) |
| Container | Docker + Docker Compose (PostgreSQL + Ollama + app) |
| Environment | Conda (`chili-env`) or Docker |

## Quick Start

### PostgreSQL (required)

- **Docker Compose** (`scripts/docker-setup.sh`): includes a **PostgreSQL 16** service; the `chili` container gets `DATABASE_URL=postgresql://chili:chili@postgres:5432/chili` automatically. The DB is also published on the host as **`localhost:5433`** (`chili` / `chili` / `chili`).
- **Local / Conda**: set **`DATABASE_URL`** in `.env` to **`postgresql://chili:chili@localhost:5433/chili`** so the host app uses the same database as Compose (see `.env.example`). The app applies the schema on startup (`create_all` + versioned migrations).

- More: **[docs/DATABASE_POSTGRES.md](docs/DATABASE_POSTGRES.md)** (`pytest`, legacy import, Compose details).
- Pattern trade analytics (per-trade rows, evidence hypotheses, Brain UI): **[docs/PATTERN_TRADE_ANALYTICS.md](docs/PATTERN_TRADE_ANALYTICS.md)**.

### Option A: Docker (recommended)

The fastest way to get CHILI running: **PostgreSQL**, Ollama, model pulls, the app, and RAG ingest.

```bash
git clone https://github.com/MiacoRindolf/chili-home-copilot.git
cd chili-home-copilot

cp .env.example .env
# Optional: edit .env for API keys, etc. DATABASE_URL is set inside Compose for the app container.

# Start everything (postgres + ollama + chili)
bash scripts/docker-setup.sh
```

Open **[https://localhost:8000/chat](https://localhost:8000/chat)** to start chatting (Docker serves **HTTPS** with a container-generated certificate; your browser will show a warning — use **Advanced → Continue** for local dev).

To stop: `docker compose down`. To stop and wipe **all** data (Postgres, Ollama models cache volume, app data): `docker compose down -v`.

To use **plain HTTP** inside the container only (e.g. debugging), run with `-e CHILI_TLS=0` or add a `docker-compose.override.yml` that sets `CHILI_TLS=0` on the `chili` service (the default in `docker-compose.yml` is **HTTPS**, `CHILI_TLS=1`).

### Option B: Local (Conda)

#### Prerequisites
- Python 3.11+ (via Conda or system Python)
- [Ollama](https://ollama.com/) installed with `llama3` pulled

#### Setup

```bash
git clone https://github.com/MiacoRindolf/chili-home-copilot.git
cd chili-home-copilot

# Create and activate conda environment
conda create -n chili-env python=3.11 -y
conda activate chili-env

# Install dependencies
pip install -r requirements.txt

cp .env.example .env
# Edit .env: set DATABASE_URL (PostgreSQL) and optionally OPENAI_API_KEY

# Pull the LLM and embedding models
ollama pull llama3
ollama pull nomic-embed-text

# (Optional) Ingest household documents for RAG
python -m app.ingest

# Start the server (HTTPS; frees port 8000 first on Windows to avoid WinError 10048)
.\scripts\start-https.ps1
```

Open [https://localhost:8000/chat](https://localhost:8000/chat) to start chatting. If port 8000 is already in use, see [Port 8000 already in use](#port-8000-already-in-use-windows-winerror-10048) below.

> **RAG setup**: Drop `.txt` files into the `docs/` folder (house rules, WiFi info, recipes, etc.) and run `python -m app.ingest` to make them searchable via chat. Example files are included.

### LAN Access

CHILI is designed for household use. When using `.\scripts\start-https.ps1`, the server binds `0.0.0.0`; access from any device on your local network using your machine's LAN IP (e.g., `https://192.168.1.x:8000`).

### Port 8000 already in use (Windows `WinError 10048`)

Another `uvicorn`/Python process is still listening (often an old terminal, a second Cursor terminal, or a **reload supervisor** that respawns workers). Free the port, then start again:

```powershell
.\scripts\free-port.ps1 -Port 8000
```

This script **does not require Administrator** for normal dev: it stops listeners first, targets the **parent** of the process holding the socket (fixes `uvicorn --reload`), retries a few times, and only pops **UAC** if something is still bound after that.

`scripts\start-https.ps1` runs `free-port.ps1` twice (with a short delay between) before starting the server so the port is reliably free and WinError 10048 is avoided.

Avoid relying on `Stop-Process` on the raw listener PID alone — with `--reload`, that PID can come back immediately; use `free-port.ps1` instead.

**Still 10048 after `free-port`?** Windows may have **reserved** the port (Hyper-V / WSL excluded range). The script exits with an error and suggests another port. Quick fix:

```powershell
$env:CHILI_PORT = '8010'
.\scripts\start-https.ps1
```

Or let the repo pick the first free port among 8000, 8010, 8020, …:

```powershell
.\scripts\start-dev.ps1
```

**Brain worker won’t stop / port still stuck:** run a full reset **in Windows PowerShell** (Cursor’s sandbox cannot always kill your processes):

```powershell
cd C:\dev\chili-home-copilot
.\scripts\reset-chili-dev.ps1
# optional: free port + open new window with HTTPS uvicorn
.\scripts\reset-chili-dev.ps1 -StartUvicorn
```

**CHILI running as a Windows service on 8000:** If you run the app as a service (e.g. NSSM) bound to port 8000, it will conflict with a manual dev server. Either **stop the service** before starting local dev, or use a different port for dev: `$env:CHILI_PORT='8010'; .\scripts\start-https.ps1`.

## Project Structure

```
app/
├── main.py              # FastAPI app creation, router mounting, migrations
├── deps.py              # Shared FastAPI dependencies (get_db, identity resolution)
├── config.py            # App settings (env vars, module flags)
├── routers/
│   ├── chat.py          # Chat page, /api/chat, streaming, conversations
│   ├── trading.py       # Trading page + AI analysis/smart-pick endpoints
│   ├── trading_sub/     # Trading sub-routers (ai, scanner, alerts, broker)
│   ├── admin.py         # Admin dashboard, user management, exports
│   ├── pages.py         # Home, profile, pair pages + form handlers
│   └── health_routes.py # /health and /metrics endpoints
├── services/
│   ├── chat_service.py  # Unified chat logic: tool execution, planning, SSE
│   ├── yf_session.py    # Yahoo Finance wrapper (rate-limited, cached, sentiment-enriched)
│   └── trading/         # Trading business logic
│       ├── ai_context.py    # Rich AI context assembly (parallel, cached market pulse)
│       ├── learning.py      # AI Brain: pattern mining, snapshots, ML predictions
│       ├── ml_engine.py     # GradientBoosting model (tech + sentiment + fundamentals)
│       ├── scanner.py       # Ticker scoring, screener, momentum scanner, smart pick
│       ├── sentiment.py     # VADER news sentiment scoring
│       ├── market_data.py   # Indicators, quotes, regime detection
│       ├── portfolio.py     # Watchlist, trades, insights
│       ├── journal.py       # Trading journal entries
│       └── alerts.py        # Price alert monitoring and dispatch
├── models/
│   └── trading.py       # Trading SQLAlchemy models (MarketSnapshot, Trade, etc.)
├── templates/
│   ├── base.html        # Shared layout (PWA meta, theme vars, dark mode)
│   ├── chat.html        # Chat UI (sidebar, streaming, voice, search)
│   ├── trading.html     # Trading terminal (charts, brain dashboard, news)
│   ├── home.html        # Home page (chores + birthdays)
│   ├── admin.html       # Admin dashboard
│   └── ...              # profile, pair, admin_users, marketplace
├── migrations.py        # Lightweight SQLite migrations
├── openai_client.py     # Tiered LLM cascade (Groq → Gemini → OpenAI)
├── llm_planner.py       # Ollama planner (accepts RAG + personality context)
├── rag.py               # RAG module: chunking, embedding, ChromaDB search
├── personality.py       # Personality profiling: extraction, context injection
├── planner_schema.py    # Pydantic validation schemas (incl. answer_from_docs)
├── db.py                # Database engine and session setup
├── models.py            # Core SQLAlchemy models (User, Chore, Birthday, etc.)
├── pairing.py           # Device pairing and identity resolution
├── logger.py            # Structured logging with trace_id
├── static/              # PWA assets (manifest, service worker, icons)
├── prompts/             # LLM system prompts (planner, trading analyst)
docs/
├── momentum-trading-strategy.txt  # Trading strategy (evolving, brain-tested)
├── modules.md           # Module system & marketplace docs
├── TOOLING.md           # Tool-calling architecture deep dive
├── house-info.txt       # Example: WiFi, landlord, trash, parking
├── house-rules.txt      # Example: quiet hours, kitchen, guests
├── recipes.txt          # Example: household favorite recipes
data/                    # ChromaDB, uploads, ticker cache, optional legacy .db (gitignored)
tests/                   # pytest (PostgreSQL; see docs/DATABASE_POSTGRES.md)
chili_mobile/            # Flutter mobile app (Android/iOS)
Dockerfile               # Container image for CHILI app
docker-compose.yml       # Full stack: PostgreSQL + Ollama + CHILI
scripts/
├── docker-setup.sh      # One-command Docker bootstrap
├── start-https.ps1      # HTTPS setup for LAN (mkcert)
requirements.txt         # Pinned Python dependencies
.env.example             # Template for environment variables
```

## Design Decisions

| Decision | Rationale |
|----------|-----------|
| **LLM plans, code executes** | Prevents the model from directly mutating state. Makes actions auditable, testable, and safe to retry. |
| **Strict Pydantic schemas with `extra="forbid"`** | Rejects any unexpected fields from the LLM, preventing prompt injection from smuggling extra parameters. |
| **Discriminated union validation** | Each action type has its own schema with typed fields. A `mark_chore_done` with `id=0` or a birthday with an invalid date is caught before execution. |
| **Guest enforcement before execution** | Write-action blocking happens before any DB call, not after. An early bug taught us this -- the fix is architecturally important. |
| **`convo_key` scoping** | Paired users get `user:<id>` so conversations follow the person across devices. Guests get `guest:<token>` for isolated threads. |
| **Rule-based fallback** | If Ollama is down, CHILI still works via regex parsing. Graceful degradation over hard failure. |
| **Temperature 0** | Tool-calling needs deterministic output. Creative variation in action planning causes schema validation failures. |
| **PostgreSQL** | Required relational store (concurrent web + worker). `DATABASE_URL` in `.env`; migrations in `app/migrations.py`. Legacy `chili.db` import: `scripts/migrate_legacy_sqlite_to_postgres.py`. |
| **RAG with local embeddings** | Ollama `nomic-embed-text` keeps embeddings local (no cloud API keys). ChromaDB provides persistent vector storage with zero infrastructure. |
| **RAG is additive** | Document search enhances the planner without replacing tool-calling. Existing chore/birthday actions work identically whether RAG context is present or not. |
| **Simple paragraph chunking** | Household docs are short and structured. Splitting by paragraph with a 500-char cap is sufficient without complex chunking libraries. |
| **Local-first, cloud-optional** | Tool actions stay on llama3 (free, private). OpenAI only handles general conversation when the local planner can't. No API key required to use core features. |
| **gpt-4o-mini default** | Cheapest OpenAI model with strong quality. ~$0.15/1M input tokens makes it viable for household use without budget worries. |
| **Personality via extraction** | Every 20 messages, OpenAI summarizes the user's traits from conversation history. Cheaper and simpler than per-message analysis, and quality improves with more data. |
| **User-editable profiles** | Auto-extraction can be wrong. The `/profile` page lets housemates correct CHILI's understanding, building trust and improving personalization. |
| **Docker Compose stack** | `docker-compose.yml` runs PostgreSQL (with health check), Ollama, and CHILI. The app container gets `DATABASE_URL` pointed at the `postgres` service; `OLLAMA_HOST` targets Ollama on the Compose network. |
| **Pinned requirements.txt** | Exact versions for reproducible builds. Conda is great for local dev, but `pip install -r requirements.txt` works everywhere -- Docker, CI, VMs. |
| **SSE streaming** | Server-Sent Events let the frontend display tokens as they arrive from OpenAI, matching the ChatGPT typing experience. Tool actions (instant) send the full reply as one chunk. |
| **Conversation sidebar for users only** | Paired users get multi-conversation management (create, switch, delete). Guests see a single shared thread -- no sidebar clutter, simpler UX for casual visitors. |
| **CDN for frontend libs** | marked.js, highlight.js, and DOMPurify loaded from CDN. Zero build step, instant updates, keeps the repo lean. |
| **NLU fallback parser** | When Ollama is offline, a rule-based regex parser handles common commands (add/list chores, mark done, add/list birthdays) without any LLM. OpenAI picks up general chat. Full graceful degradation. |
| **RAG + personality badges** | Assistant messages show badges indicating when RAG context was used (with source filenames) and when personality profiling personalized the response. Trace IDs are clickable for debugging. |
| **Conversation export** | Download any conversation as Markdown or JSON for archiving, sharing, or debugging. |
| **Guest chat visibility** | Housemates can view and reply to guest conversations from a dedicated sidebar section, enabling support for visitors without requiring them to pair devices. |

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Home page (chores + birthdays) |
| `GET` | `/chat` | Chat UI (single-page, fetch-based) |
| `POST` | `/api/chat` | Send a message, get JSON response |
| `POST` | `/api/chat/stream` | Send a message, get SSE streaming response |
| `GET` | `/api/chat/history` | Retrieve conversation history (optional `?conversation_id=`) |
| `GET` | `/api/chat/guest-history` | View a guest's chat history (housemates only, `?guest_convo_key=`) |
| `POST` | `/api/chat/guest-reply` | Reply to a guest's conversation (housemates only) |
| `GET` | `/api/conversations` | List conversations for current user |
| `POST` | `/api/conversations` | Create a new conversation |
| `DELETE` | `/api/conversations/{id}` | Delete a conversation and its messages |
| `GET` | `/api/conversations/search` | Search conversations by message content |
| `GET` | `/api/conversations/guests` | List guest conversations (housemates only) |
| `GET` | `/api/conversations/{id}/export` | Export conversation as Markdown or JSON (`?fmt=md\|json`) |
| `GET` | `/health` | DB + Ollama health check |
| `GET` | `/metrics` | Counts + LLM latency stats |
| `GET` | `/admin` | Admin dashboard |
| `GET` | `/admin/users` | User management + pairing |
| `GET` | `/profile` | Housemate personality profile (view + edit) |
| `GET` | `/pair` | Device pairing page (manual fallback) |
| `POST` | `/api/pair/request` | Request email pairing code (guest) |
| `POST` | `/api/pair/verify` | Verify code and pair device (guest) |
| `GET` | `/export/chores.csv` | CSV export of chores |
| `GET` | `/export/birthdays.csv` | CSV export of birthdays |

## What's Next

- [x] Pytest suite for guardrails, planner validation, and guest enforcement
- [x] RAG over household documents (manuals, recipes, notes)
- [x] Smart multi-model routing (local llama3 + OpenAI fallback)
- [x] Housemate personality profiles (auto-extracted, editable)
- [x] NLU fallback parser (rule-based, works when Ollama is offline)
- [x] RAG source badges, personality indicator, and trace ID display in chat
- [x] Conversation export (Markdown / JSON)
- [x] Guest chat visibility and housemate replies
- [x] Streaming LLM responses via SSE + ChatGPT-style UI (markdown, sidebar, conversations)
- [x] Docker containerization (`docker compose up` one-liner)
- [x] Trading terminal with interactive charts, watchlist, portfolio, and journal
- [x] AI Brain: self-learning pattern mining with hypothesis testing and adaptive weights
- [x] News sentiment analysis (VADER) integrated into Brain learning and ML features
- [x] Fundamental data (P/E, market cap) as ML features for predictions
- [x] Parallel AI context assembly with cached market pulse (5-min stale-while-revalidate)
- [x] Free-tier LLM cascade (Groq → Gemini → OpenAI last resort)
- [x] Flutter mobile app with voice chat
- [ ] Expanded tool actions (shopping list, edit/delete, chore assignment)
- [ ] Image & file understanding (GPT-4o vision, PDF ingestion)
- [ ] Scheduled reminders & push notifications
- [ ] LLM evaluation & observability dashboard
- [ ] Real-time collaboration (WebSockets, live updates)
- [ ] Multi-household support

## Further Reading

- [TOOLING.md](docs/TOOLING.md) — Tool-calling architecture, guardrail layers, and observability design
- [momentum-trading-strategy.txt](docs/momentum-trading-strategy.txt) — CHILI's evolving trading strategy (brain-tested)
- [modules.md](docs/modules.md) — Module system & marketplace documentation

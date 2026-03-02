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
 │       SQLite DB ◄──────────┘─────────┘       │
 │  (conversations, chat_messages)              │
 │                                              │
 │  Identity: cookie ──► Device ──► User        │
 │  Conversations: sidebar, auto-title, CRUD    │
 │  Observability: trace_id, model_used, logs   │
 │  Guardrails: guest read-only, schema reject  │
 └──────────────────────────────────────────────┘
```

**Key design principle: "LLM plans, code executes."** The model chooses an action via strict JSON; application code validates the schema and performs the database operation. The LLM never touches state directly.

## Features

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
- Local-first: tool actions (chores, birthdays, RAG) always use llama3 locally (free, private)
- OpenAI fallback: general conversation routes to OpenAI API when the local planner returns `type=unknown`
- **Streaming responses**: OpenAI fallback streams tokens via SSE for real-time ChatGPT-like typing experience
- Cost-aware: defaults to `gpt-4o-mini` (~$0.15/1M tokens). Configurable via `.env`
- Graceful: works without an API key (existing help message). Zero breaking changes
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
- Persistent conversation history per user stored in SQLite (`chat_messages` + `conversations` tables)
- Memory scoped by `convo_key`: paired users get named conversations, guests get isolated threads
- Last 12 messages sent as context window to the planner for multi-turn conversations

### Identity & Device Pairing
- Admin creates housemate accounts and generates one-time pairing codes
- Devices pair via `/pair` using the code; a cookie-based device token maps the browser to a user
- Unpaired devices are treated as Guest (read-only)

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
| Database | SQLite via SQLAlchemy |
| LLM (local) | Ollama (llama3 for planning, nomic-embed-text for embeddings) |
| LLM (cloud) | OpenAI API (gpt-4o-mini for general chat + personality extraction) |
| Vector Store | ChromaDB (local, persistent) |
| Validation | Pydantic v2 (strict schemas, discriminated unions) |
| Frontend | Server-rendered HTML + vanilla JS (`fetch()` for chat) |
| Container | Docker + Docker Compose (Ollama sidecar) |
| Environment | Conda (`chili-env`) or Docker |

## Quick Start

### Option A: Docker (recommended)

The fastest way to get CHILI running. One command pulls Ollama + models, starts the app, and ingests documents.

```bash
git clone https://github.com/MiacoRindolf/chili-home-copilot.git
cd chili-home-copilot

# (Optional) Set up OpenAI for general chat
cp .env.example .env
# Edit .env and add your OPENAI_API_KEY

# Start everything
bash scripts/docker-setup.sh
```

Open [http://localhost:8000/chat](http://localhost:8000/chat) to start chatting.

To stop: `docker compose down`. To stop and wipe data: `docker compose down -v`.

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

# (Optional) Set up OpenAI for general chat
cp .env.example .env
# Edit .env and add your OPENAI_API_KEY

# Pull the LLM and embedding models
ollama pull llama3
ollama pull nomic-embed-text

# (Optional) Ingest household documents for RAG
python -m app.ingest

# Start the server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open [http://localhost:8000/chat](http://localhost:8000/chat) to start chatting.

> **RAG setup**: Drop `.txt` files into the `docs/` folder (house rules, WiFi info, recipes, etc.) and run `python -m app.ingest` to make them searchable via chat. Example files are included.

### LAN Access

CHILI is designed for household use. Run with `--host 0.0.0.0` and access from any device on your local network using your machine's LAN IP (e.g., `http://192.168.1.x:8000`).

## Project Structure

```
app/
├── main.py            # FastAPI routes, chat UI, /profile page
├── models.py          # SQLAlchemy models (incl. HousemateProfile)
├── db.py              # Database engine and session setup
├── llm_planner.py     # Ollama planner (accepts RAG + personality context)
├── openai_client.py   # OpenAI API wrapper for general chat fallback
├── personality.py     # Personality profiling: extraction, context injection
├── planner_schema.py  # Pydantic validation schemas (incl. answer_from_docs)
├── rag.py             # RAG module: chunking, embedding, ChromaDB search
├── ingest.py          # CLI script: python -m app.ingest
├── chili_nlu.py       # Rule-based fallback parser
├── pairing.py         # Device pairing and identity resolution
├── schemas.py         # API-level Pydantic schemas
├── logger.py          # Structured logging with trace_id
├── health.py          # Health checks (DB + Ollama) and demo reset
├── metrics.py         # Latency tracking and count aggregation
docs/
├── house-info.txt     # Example: WiFi, landlord, trash, parking
├── house-rules.txt    # Example: quiet hours, kitchen, guests
├── recipes.txt        # Example: household favorite recipes
data/
├── chili.db           # SQLite database (auto-created, gitignored)
├── chroma/            # ChromaDB vector store (auto-created, gitignored)
tests/
├── test_rag.py             # Tests for chunking, search, and ingestion
├── test_planner_schema.py  # Schema validation tests (incl. answer_from_docs)
├── test_api.py             # API integration tests
├── test_openai_routing.py  # OpenAI fallback routing + model tracking tests
├── test_personality.py     # Personality extraction and profile tests
├── test_conversations.py   # Conversation CRUD, streaming, and sidebar tests
├── test_fallback_parser.py # Fallback parser tests
Dockerfile             # Container image for CHILI app
docker-compose.yml     # Full stack: CHILI + Ollama
scripts/
├── docker-setup.sh    # One-command Docker bootstrap
requirements.txt       # Pinned Python dependencies
.env.example           # Template for environment variables
TOOLING.md             # Deep dive into guardrails and tool-calling design
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
| **SQLite** | Zero-config, single-file database. Perfect for a household app that runs on one machine. Easily swappable via SQLAlchemy. |
| **RAG with local embeddings** | Ollama `nomic-embed-text` keeps embeddings local (no cloud API keys). ChromaDB provides persistent vector storage with zero infrastructure. |
| **RAG is additive** | Document search enhances the planner without replacing tool-calling. Existing chore/birthday actions work identically whether RAG context is present or not. |
| **Simple paragraph chunking** | Household docs are short and structured. Splitting by paragraph with a 500-char cap is sufficient without complex chunking libraries. |
| **Local-first, cloud-optional** | Tool actions stay on llama3 (free, private). OpenAI only handles general conversation when the local planner can't. No API key required to use core features. |
| **gpt-4o-mini default** | Cheapest OpenAI model with strong quality. ~$0.15/1M input tokens makes it viable for household use without budget worries. |
| **Personality via extraction** | Every 20 messages, OpenAI summarizes the user's traits from conversation history. Cheaper and simpler than per-message analysis, and quality improves with more data. |
| **User-editable profiles** | Auto-extraction can be wrong. The `/profile` page lets housemates correct CHILI's understanding, building trust and improving personalization. |
| **Docker with Ollama sidecar** | `docker-compose.yml` runs Ollama as a service container with a health check. The app uses `OLLAMA_HOST` env var so the same code works locally and in Docker without config changes. |
| **Pinned requirements.txt** | Exact versions for reproducible builds. Conda is great for local dev, but `pip install -r requirements.txt` works everywhere -- Docker, CI, VMs. |
| **SSE streaming** | Server-Sent Events let the frontend display tokens as they arrive from OpenAI, matching the ChatGPT typing experience. Tool actions (instant) send the full reply as one chunk. |
| **Conversation sidebar for users only** | Paired users get multi-conversation management (create, switch, delete). Guests see a single shared thread -- no sidebar clutter, simpler UX for casual visitors. |
| **CDN for frontend libs** | marked.js, highlight.js, and DOMPurify loaded from CDN. Zero build step, instant updates, keeps the repo lean. |

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Home page (chores + birthdays) |
| `GET` | `/chat` | Chat UI (single-page, fetch-based) |
| `POST` | `/api/chat` | Send a message, get JSON response |
| `POST` | `/api/chat/stream` | Send a message, get SSE streaming response |
| `GET` | `/api/chat/history` | Retrieve conversation history (optional `?conversation_id=`) |
| `GET` | `/api/conversations` | List conversations for current user |
| `POST` | `/api/conversations` | Create a new conversation |
| `DELETE` | `/api/conversations/{id}` | Delete a conversation and its messages |
| `GET` | `/health` | DB + Ollama health check |
| `GET` | `/metrics` | Counts + LLM latency stats |
| `GET` | `/admin` | Admin dashboard |
| `GET` | `/admin/users` | User management + pairing |
| `GET` | `/profile` | Housemate personality profile (view + edit) |
| `GET` | `/pair` | Device pairing page |
| `GET` | `/export/chores.csv` | CSV export of chores |
| `GET` | `/export/birthdays.csv` | CSV export of birthdays |

## What's Next

- [x] Pytest suite for guardrails, planner validation, and guest enforcement
- [x] RAG over household documents (manuals, recipes, notes)
- [x] Smart multi-model routing (local llama3 + OpenAI fallback)
- [x] Housemate personality profiles (auto-extracted, editable)
- [ ] Scheduled reminders (e.g., "remind me to take out trash every Tuesday")
- [x] Streaming LLM responses via SSE + ChatGPT-style UI (markdown, sidebar, conversations)
- [x] Docker containerization (`docker compose up` one-liner)
- [ ] Multi-household support

## Further Reading

See [TOOLING.md](TOOLING.md) for a detailed walkthrough of the tool-calling architecture, guardrail layers, and observability design.

# CHILI Tooling & Guardrails

CHILI uses a production-style LLM architecture:

**LLM = planner** (chooses an action)  
**Code = executor** (performs the action against PostgreSQL via SQLAlchemy)

This design prevents the model from directly modifying state and makes the system safer and testable.

---

## High-level flow

1. User sends a message in `/chat`
2. CHILI calls the **local LLM planner** (Ollama `llama3`)
3. Planner returns **strict JSON**:
   - `type` (action name)
   - `data` (parameters)
   - `reply` (short user-facing sentence)
4. JSON is validated with **strict Pydantic schemas**
5. If valid → execute action (DB writes/reads)
6. If invalid/ambiguous → fallback to safe `unknown` behavior
7. CHILI returns the `reply` and any results (e.g., lists)

---

## Action schema (tool contract)

All planner outputs must match:

```json
{
  "type": "<action>",
  "data": { },
  "reply": "<short sentence>"
}
```

Supported actions:

* `add_chore` → `{"title": "..." }`
* `list_chores` → `{ }`
* `list_chores_pending` → `{ }`
* `mark_chore_done` → `{"id": 1}`
* `add_birthday` → `{"name": "...", "date": "YYYY-MM-DD"}`
* `list_birthdays` → `{ }`
* `unknown` → `{"reason": "..."}`

---

## Guardrails

### 1) Strict JSON-only output

The planner prompt requires **JSON only** (no markdown, no extra text).

### 2) Strict schema validation (Pydantic)

Plans are validated with discriminated union schemas:

* invalid types/fields → rejected
* incorrect data types (e.g., `id=0`, bad date) → rejected
* extra keys → rejected

Rejected plans become:

* `type="unknown"` with safe fallback reply

### 3) Ambiguity handling (no guessing)

If the user request is unclear, the planner must return:

* `type="unknown"` + a clarifying question in `reply`

Example:
User: "do the thing"
→ `unknown` + "What would you like me to do—add a chore, list chores, or add a birthday reminder?"

### 4) Fallback logic

If Ollama is unavailable or errors occur:

* CHILI falls back to a rule-based parser (`parse_message`)
* This keeps the app usable even without the LLM running.

---

## Observability

CHILI emits:

* **trace_id** for each `/chat` request
* structured logs containing:

  * user message
  * planned JSON
  * executed action
  * latency

The trace_id is also displayed in the UI for debugging.

---

## Health & Metrics

* `/health`: checks DB + Ollama status
* `/metrics`: counts + LLM latency stats
* `/admin`: dashboard view of health/metrics + reset + exports

---

## Why this design

This is a common production LLM pattern:

* **separate planning from execution**
* keep state changes in code, not in the model
* validate outputs
* log decisions

It makes the system safer, more predictable, and easier to test.

---

## Trading AI Architecture

CHILI's trading module (`app/services/trading/`) extends the platform's typed-contract architecture into an AI-native trading system:

- models analyze, recommend, decide, and adapt inside typed, policy-bound lanes
- deterministic services validate, constrain, execute, audit, and can veto
- Brain owns learning, thesis formation, and recommendation quality
- Autopilot owns runtime reading and inspection, not strategy logic

### AI Analyze Flow

1. User clicks "AI Analyze" on a ticker
2. Backend assembles rich context via `build_ai_context()`:
   - **Parallel phase** (ThreadPoolExecutor, 6 workers): technical indicators, quote, fundamentals, scanner score, market pulse, portfolio context — all concurrently
   - **Market pulse** is cached for 5 minutes (stale-while-revalidate at 10 min) to avoid re-scoring 20 tickers on every call
   - DB queries: backtests, trades, stats, learned patterns, journal notes
3. Context + user message sent to **free-tier LLM cascade** (Groq → Gemini → OpenAI) via SSE streaming
4. Tokens stream to the frontend in real-time

### AI Brain (Self-Learning)

The brain runs periodic learning cycles (`run_learning_cycle()`) that:

1. **Take market snapshots** — parallel capture of 100+ tickers with technical indicators, news sentiment (VADER), and fundamentals (P/E, market cap)
2. **Mine patterns** — scan snapshots for technical + sentiment + fundamental confluences, tag them, and store as `TradingInsight` records
3. **Test hypotheses** — validate assumptions (e.g. "MACD negative = bad") against actual returns
4. **Discover novel patterns** — find combinations no strategy taught
5. **Train ML model** — GradientBoosting classifier with 15+ features (RSI, MACD, EMA, ADX, Stochastic, news sentiment, news count, P/E ratio)
6. **Adapt weights** — scoring weights evolve based on pattern confidence
7. **Decay & prune** — stale or underperforming patterns lose confidence

### Performance Optimizations

| Optimization | Effect |
|-------------|--------|
| `batch_download()` pre-warming | 20 tickers fetched in 1 HTTP call instead of 20 |
| Parallel `_score_ticker()` | 10 concurrent threads instead of serial loop |
| Market context cache (5-min TTL) | Repeat Analyze calls return instantly |
| Stale-while-revalidate | Serves stale data while background thread refreshes |
| `ThreadPoolExecutor(max_workers=6)` in `build_ai_context` | All 6 data sources fetched concurrently |
| yfinance rate-limiter + multi-tier cache | 30s quotes, 30min history, 24h fundamentals |

---

## Intercom: Tier 2 native wrapper (future)

**Goal:** PTT audio plays on mobile even when the screen is locked, with push notifications for incoming messages. The PWA cannot do this (browsers suspend JS and close WebSockets when the screen locks).

**Approach:**

* **Capacitor** (recommended): Wrap the existing PWA in a native Android/iOS shell. Add a background plugin to keep the WebSocket connection alive.
* **Foreground Service** (Android): Keep the app alive with a persistent notification (e.g. "CHILI Intercom active"). Play incoming PTT via native audio APIs.
* **Push Notifications**: Use Firebase Cloud Messaging (FCM) to wake the app when a PTT is incoming, even if the app is in the background or closed.
* **Scope:** Separate project (e.g. new repo or `/native` folder). Estimated 2–3 days. Document detailed steps in `docs/native-intercom.md` when starting.

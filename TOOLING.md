
# CHILI Tooling & Guardrails

CHILI uses a production-style LLM architecture:

**LLM = planner** (chooses an action)  
**Code = executor** (performs the action against SQLite)

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
````

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
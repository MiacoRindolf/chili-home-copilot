# CC_REPORT: f-teacher-escalation-live-hook

**Type:** operator-directed, out-of-band ("go for everything", 2026-06-01;
commit→push→PR→merge per change). Second "full send" deliverable; implements the
deferred brief `docs/STRATEGY/QUEUED/f-teacher-escalation-live-hook.md`.
`NEXT_TASK.md` (phase-5i soak) untouched.

## What shipped

Wires the dormant P4 learning loop onto a real failed chat turn — the riskiest of
the three, so it is **flag-gated, fire-and-forget, and fully dormant by default**
(I implemented it myself, not via subagent).

- **`app/services/teacher_hook.py`** (new) — the CHILI-specific bridge:
  - `maybe_fire_teacher_escalation(user_request, tool_results, agent_reply, trace_id)`
    — checks `teacher_escalation_enabled` (default False → cheap no-op) then
    `teacher_escalation.should_escalate(...)`; on a detected failure it spawns the
    escalation. NEVER raises.
  - `_spawn_escalation` runs `escalate_and_learn` in a **dedicated daemon thread**
    with its own asyncio loop — so the BLOCKING strong-model call can never stall
    the FastAPI event loop (the key reason not to use `asyncio.create_task` on the
    request loop).
  - `_make_strong_llm_caller` adapts CHILI's `context_brain.llm_gateway.gateway_chat`
    to the injectable `llm_caller(prompt)->str|None` the pure module expects;
    best-effort (returns None on any error). Keeps `teacher_escalation.py` pure.

- **`app/services/chat_service.py`** — ONE guarded call right after
  `execute_tool_with_client_action` (the turn-completion point where user
  request + tool result + reply are all in scope), wrapped in try/except. Adapts
  the tool result to `[{"tool", "output", "error"}]` (error set when
  `executed=False`). When the flag is off this is a single getattr; it can never
  block or raise into the response.

No new config (the `teacher_escalation_enabled` flag already exists from P4).

## Verification

- `tests/test_teacher_hook.py` (10 cases): flag-off no-op; clean turn → no fire;
  failed turn → spawn (args forwarded); never-raises (should_escalate blowing up
  is swallowed); strong-caller returns reply / None-on-error / None-on-empty;
  `_spawn_escalation` actually drives `escalate_and_learn` with the right args
  (threading patched to run synchronously). + `test_teacher_escalation.py` (29).
  **37 passed.**
- `chat_service` import smoke green.
- Direct chat smoke (mocked planner, `POST /api/chat`, flag off): **GREEN** —
  STATUS 200 with the reply, exercising the full `resolve_response` path incl. the
  hook. Three earlier smoke attempts failed *environmentally* (no LLM backend; a
  transient Postgres connection abort; a db-fixture-truncate deadlock from this
  session's accumulated boot load) — none related to the hook, which is
  exception-isolated and flag-off by default regardless.

## Surprises / deviations

- A test initially used `patch("app.config.settings")` which *replaces* the
  object; `teacher_escalation.py` holds a module-level `from .config import
  settings` reference, so its `should_escalate` still read the real (disabled)
  flag → no fire. Fixed by patching the attribute on the real singleton
  (`patch.object(settings, ...)`), which both modules share. In production both
  `from ..config import settings` bindings resolve to the same singleton, so the
  double flag-check is consistent.

## Deferred

- `gateway_chat(purpose="teacher_escalation")` — if that purpose isn't in the
  gateway's policy table it may route to a default or error; the adapter swallows
  errors (no skill saved), so it's safe, but a dedicated high-tier policy entry
  for this purpose would make activation cleaner.
- Saved skills land in the JSONL `FileSkillStore`; surfacing them to the brain
  (RAG-indexed retrieval by the weak model) is the natural next step.
- Activation soak: enable `teacher_escalation_enabled` and drive a deliberately
  failed tool turn to confirm a skill is written (fire-and-forget verified).

## Open questions for Cowork

1. Add a `teacher_escalation` purpose to the gateway policy (preferred high tier),
   or keep best-effort routing?
2. Index saved skills into `app/rag.py` for retrieval, or keep as an audit log?

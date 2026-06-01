# QUEUED: teacher-escalation live hook (the wiring half of P4)

**Context:** P4 shipped `app/teacher_escalation.py` — a dormant, injectable
learning loop: on a detected failure, a strong teacher model is called and its
output distilled into a reusable skill (saved only if the teacher itself passes
the failure check; untrusted-trace guarded). What's NOT done is hooking it onto a
real failed turn in CHILI. This brief is that work.

## Why it was deferred (not forced)

`escalate_and_learn` needs three inputs from a failed turn: the user request, the
**tool_results** list, and the **agent_reply**. CHILI's current LLM paths don't
cleanly produce all three:

- `services/tool_handlers.py` + `llm_planner.py` are **single-shot** (plan →
  execute one tool → return) — there's no iterative agent turn with a captured
  tool-result/reply transcript to evaluate.
- `reasoning_brain` / `project_brain` research is **summarization**, not
  tool-using agency, so the failure signals (`Unknown action`, `I don't have a
  tool`) mostly don't apply.

Forcing a hook onto one of these would be a low-value, shallow integration. The
honest move is to hook it where a real tool-using turn exists.

## Options for the hook point (pick one with Cowork)

1. **Chat/planner path** — capture `{tool, output, error}` per executed action and
   the assistant reply for a turn, then call `should_escalate(...)`; on True, fire
   `escalate_and_learn(...)` as a background task with CHILI's strong-model
   gateway as the injected `llm_caller`. Lowest user-facing risk if done as
   fire-and-forget (never blocks the response).
2. **A future iterative agent loop** — if CHILI grows a ReAct loop (currently
   ABSENT per the salvage assessment), that's the natural home.

## Integration details

- Inject `llm_caller`: wrap `context_brain.llm_gateway.gateway_chat` (or
  `llm_router`) to call a STRONG tier and return text. Keep a timeout.
- `skill_saver`: start with the default `FileSkillStore`; a follow-up can index
  saved skills into `app/rag.py` so the weak model retrieves them semantically.
- Gate on `settings.teacher_escalation_enabled` (default off) — already wired into
  `should_escalate`.
- Fire-and-forget: escalation must never add latency to or block the user turn.

## Constraints / safety

- Keep the `<<<UNTRUSTED_TRACE>>>` guard — do not pass raw tool output to the
  teacher without it (second-order prompt-injection risk into persisted skills).
- Research/chat side only; nothing about this touches trading execution.

## Success criteria

- With `teacher_escalation_enabled=1`, a deliberately-failed tool turn triggers an
  async teacher call; a valid skill lands in the store; a turn where the teacher
  also fails saves nothing; the user response latency is unchanged (fire-and-forget
  verified).

## Reference

- `app/teacher_escalation.py` (P4) — `should_escalate`, `escalate_and_learn`,
  `FileSkillStore`, `evaluate_turn_regex`.

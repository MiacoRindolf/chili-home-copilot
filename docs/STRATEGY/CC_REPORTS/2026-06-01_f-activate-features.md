# CC_REPORT: f-activate-features

**Type:** operator-directed ("these are dormant?? take the flags out and test them
yourself", 2026-06-01). The operator rejected the reflexive dormant-by-default
flagging; this validates the features by exercising them and turns them on.
Branched from latest `origin/main`. `NEXT_TASK.md` (phase-5i soak) untouched.

## Validation first (the operator's ask: "test them yourself")

Exercised both features with real output and judged quality against my own
standard:

- **Daily brief** — generated from realistic data (585/537 patterns, GRT-USD/
  EKSO/ACHC closes, payoff ratios). Output: accurate, clean GFM tables, correct
  signed-money formatting, and it surfaces **payoff ratio** (537 Reclaim 29.6:1,
  585 Wedge 4.97:1). Reads like something I'd write. Tightened one rough edge:
  the empty "Unrealized" column on open positions now drops when no position
  carries a value.
- **Teacher escalation** — ran the full pipeline with me acting as the strong
  teacher on a sample failed turn ("open my X chat" → `Unknown action 'switch'`).
  The pipeline built a guarded prompt, my teacher response passed the confidence
  gate, and a well-formed, portable skill (`open-named-chat-session`) was saved.
  Genuinely useful.

## Bug that activation surfaced (why testing > flag-gating)

The chat-path adapter signaled `error="tool_execution_failed"` whenever
`executed=False` — but that's true for **every ordinary conversational turn**
(`action_type="unknown"`, no tool attempted). With the flag flipped on, the
teacher would have fired on **every chat message** (cost disaster). Fixed:

- Extracted `teacher_hook.maybe_fire_for_turn(message, action_type, llm_reply,
  executed, is_guest, trace_id)` — skips guests, treats conversational turns
  (`unknown`/`general_chat`) as non-failures, flags a tool error only when a real
  tool action was planned but didn't execute; verbal give-ups still caught by the
  regex. `chat_service` now makes one clean call.

## What changed (flags → ON)

- `search_fetch_sources: True` (research enriches with full article text)
- `teacher_escalation_enabled: True` (fires ONLY on genuine failures — bounded)
- `chili_daily_trading_brief_enabled: True` (daily HTML brief job)
- `mcp_enabled: True`, but the supervisor now starts only when
  `mcp_servers_json` lists a server — so it's a no-op idle until you add one (no
  redundant second flip). Kept as a kill switch.

Each flag kept as a kill switch (CHILI convention: `*_enabled: bool = True`).

## Verification

- `tests/test_teacher_hook.py` (+5 scoping cases: guest never fires; conversational
  passes no error and does not fire end-to-end with the flag ON; real tool failure
  flags error; executed passes no error) + skill_memory + trading_brief =
  **48 passed**. Compile green on main/config/chat_service/teacher_hook.
- Activation smoke (`POST /api/chat`, flags on, conversational turn): **GREEN** —
  flags confirmed on (teacher/brief/fetch/mcp = True), STATUS 200 with reply, and
  `TEACHER_FIRED_ON_CONVERSATIONAL = 0` (the scoping fix holds end-to-end).

## Cost note for the operator

Teacher escalation now makes a strong-model (gateway) call per **genuinely-failed**
non-guest turn. It's bounded to real failures (not every turn), runs
fire-and-forget, and `teacher_escalation_enabled=False` kills it instantly if you
want to cap paid-LLM spend (note codex is concurrently reducing trading LLM
spend — this is a separate, research/chat-side cost).

## Open questions for Cowork

1. Comfortable with teacher escalation's per-failed-turn cost, or want it back off
   until a budgeted soak?
2. Wire `skill_memory.retrieve_skills` into the weak model's prompt next (closes
   the full loop)?

# CC_REPORT: f-odysseus-salvage-teacher-escalation (P4)

**Type:** operator-directed, out-of-band (operator chose "Build both P3 and P4",
2026-06-01, commit→push→PR→merge per change). `NEXT_TASK.md` (phase-5i soak)
untouched. Final unit of the odysseus-salvage series.

## What shipped

- **New `app/teacher_escalation.py`** — the learning loop CHILI's `llm_router`
  lacked: it escalates weak→strong on confidence but throws the lesson away; this
  persists it. On a detected failure, a strong "teacher" model is called with the
  failed trace and emits a portable reusable skill; the skill is saved ONLY if
  the teacher's own response passes the same failure check (no persisting a
  procedure the teacher itself wasn't confident about).
  - **Injectable** `llm_caller` (async) and `skill_saver` — the core is decoupled
    from CHILI's specific LLM/storage and unit-testable without network or DB.
  - **`FileSkillStore`** — bounded (`max_skills`), dedup-by-name, thread-safe
    JSONL store under `settings.teacher_skill_dir`; the default skill saver. No
    migration needed.
  - Pure helpers reused from odysseus: `is_self_hosted` (SOTA-host detection),
    `evaluate_turn_regex` (tool-error + give-up failure patterns),
    `_extract_skill_json`, `build_teacher_prompt`, `should_escalate` gate.

- **Security — untrusted-trace guard preserved.** The failed trace is captured
  execution output and may carry prompt-injection payloads. The teacher prompt
  fences it in `<<<UNTRUSTED_TRACE>>>` markers with an explicit
  data-not-instructions guard, so a payload can't be distilled into a persisted
  skill the weak model later follows (a second-order injection). This is salvaged
  verbatim in intent.

- **Dormant by default.** `teacher_escalation_enabled: bool = False`,
  `teacher_skill_dir: str = "data/skills"`. Ready utility; not wired into the live
  LLM path.

- **Dropped** odysseus's inline-SSE teacher-takeover (`run_teacher_inline`) and
  agent-loop recursion — deeply coupled to odysseus's streaming agent loop. The
  injectable `escalate_and_learn` core captures the value without the coupling.

Files: 1 added (`app/teacher_escalation.py`), 1 test added
(`tests/test_teacher_escalation.py`), `config.py` modified, backlog updated. No
schema, no migrations, no trading code touched.

## Verification

- `tests/test_teacher_escalation.py` (29 cases): SOTA vs self-hosted detection;
  failure detection (tool error field, tool-output error pattern, reply give-up,
  clean turn); **untrusted-trace fencing + guard text present in the prompt**;
  skill JSON extraction (present/absent/malformed); `escalate_and_learn` saves on
  success, no-save when teacher emits no JSON, **no-save when the teacher's own
  response trips the give-up regex** (proves the sketchy-skill gate), caller
  exception / empty response → None; `should_escalate` gate (disabled / failure /
  clean); `FileSkillStore` save+dedup+bound+evict-oldest+reject-unnamed (using
  tempdirs — no repo pollution). **All 29 pass.**
- Full salvage-suite regression (search + web_search + visual_report + mcp_client
  + teacher_escalation): **161 passed, 0 failures**; no stray `data/skills`
  directory created in the repo.

## Surprises / deviations

- None. Async core exercised from sync tests via `asyncio.run()` (consistent with
  P3; no `asyncio_mode` configured in the repo).

## Deferred

- Not wired into the live LLM path. A future task would inject CHILI's
  strong-model gateway (e.g. `context_brain.llm_gateway` / `llm_router`) as the
  `llm_caller`, and call `should_escalate` + `escalate_and_learn` at the end of a
  failed reasoning/agent turn. Kept out so this is a pure, inert addition.
- Skills currently land in a JSONL file. If CHILI wants them surfaced to the brain
  (RAG-indexed, or shown in an admin view), that's a follow-up — the store is
  intentionally simple to start.

## Open questions for Cowork

1. Where should escalation hook in first — the reasoning_brain background loop, or
   the interactive chat/agent path? (Background is lower-risk to start.)
2. Should persisted skills feed `app/rag.py` so the weak model retrieves them
   semantically, rather than just sitting in JSONL?

---

## Series wrap — odysseus salvage complete

All four backlog items shipped to main this session, each as its own
commit→push→PR→merge, all off the live-trading path, no schema/migrations, frozen
contracts untouched:

| Unit | PR | Merge |
|------|----|----|
| Win#1 + P1 (resilient search + SSRF fetcher + research wiring) | #172 | 63906f9 |
| P2 (visual report generator) | #173 | fc63f28 |
| P3 (read-only safety-gated MCP client) | #174 | a2064df |
| P4 (teacher-escalation skill learning) | (this) | (this) |

Rejected as duplicate/too-coupled: deep-research, vector RAG, llm_core, agent
loop, hwfit (see backlog). All shipped modules are dormant/ready utilities except
the search upgrade, which is live behind DDG-compatible defaults.

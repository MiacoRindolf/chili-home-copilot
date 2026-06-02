# CC_REPORT: f-teacher-skill-memory

**Type:** operator-directed, out-of-band ("continue", 2026-06-01;
commit→push→PR→merge per change). Branched from latest `origin/main` (`b58c4da`);
codex not active on `rag.py`/teacher files. `NEXT_TASK.md` (phase-5i soak)
untouched.

## What shipped

Closes the storage half of the P4 teacher-escalation loop: learned skills are now
**indexed for semantic recall**, not just appended to a JSONL audit log.

- **`app/services/skill_memory.py`** (new) — `index_skill(skill)` upserts a
  flattened skill doc into a dedicated `teacher_skills` ChromaDB collection
  (reusing `app/rag.py`'s Chroma client + Ollama embedding fn);
  `retrieve_skills(query, k)` returns the most similar learned skills. Fully
  guarded/best-effort: if Chroma/Ollama is unavailable, both degrade to no-ops
  (False / []) and never raise.
- **`app/services/teacher_hook.py`** — the escalation now uses a
  `_combined_skill_saver` that persists to the authoritative `FileSkillStore`
  **and** best-effort indexes via `skill_memory` (an index failure can't drop the
  skill — the file save's success is what's returned).

## Verification

- `tests/test_skill_memory.py` (12, Chroma mocked): doc flattening; upsert on
  index; unnamed/empty/non-dict rejected before touching the store; name-only
  indexes; no-op when store unavailable; never-raises on upsert/query error; query
  result mapping; empty-query → []; combined saver does file-save + index, and an
  index failure doesn't block the file save. + `test_teacher_hook` (11, updated to
  the saver-injecting call) + `test_teacher_escalation` (29). **50 passed.**
- Import smoke green.

## Surprises / deviations

- A self-written test wrongly expected a name-only skill to be rejected; in fact
  the doc = the name, so it indexes. Fixed the test to match (and added the
  not-a-dict guard case).

## Deferred

- **Retrieval-into-prompt** — `retrieve_skills` is a ready API, but injecting
  retrieved procedures into the weak model's context touches the LLM prompt
  assembly (near codex's active LLM-spend work), so that wiring is deliberately
  deferred. With it, the loop fully closes: weak model fails → teacher writes a
  skill → indexed → retrieved + injected next time.
- The whole path is dormant (rides the `teacher_escalation_enabled` flag).

## Open questions for Cowork

1. Wire `retrieve_skills` into the chat prompt (gated), or keep as an API until
   teacher-escalation is enabled in a soak?
2. Backfill: index existing `FileSkillStore` JSONL skills on first use?

# QUEUED: odysseus salvage backlog

**Source:** operator-directed review (2026-06-01) of
[`pewdiepie-archdaemon/odysseus`](https://github.com/pewdiepie-archdaemon/odysseus)
— an MIT-licensed self-hosted AI workspace (~44k LOC Python). odysseus itself
adapts opencode (MIT), Tongyi DeepResearch (Apache-2.0), and llmfit (MIT); all
permissive. License is clean to salvage with attribution.

**Method:** mapped odysseus's strongest modules against what CHILI already has,
to salvage *gaps*, not duplicates. Modules where CHILI already has an equivalent
(multi-step research via `context_brain`; vector RAG via `app/rag.py`; LLM tier
escalation via `app/services/llm_router/router.py`) were explicitly rejected to
avoid a second parallel stack.

**Shipped from this review (2026-06-01):** Win #1 — resilient multi-provider
web/news search + SSRF-safe page-content fetcher. See
`docs/STRATEGY/CC_REPORTS/2026-06-01_f-odysseus-salvage-resilient-search.md`.
New module `app/search_providers.py`; `app/web_search.py` rewired with DDG
backstop; additive `fetch_source()` / `search_with_sources()` APIs.

The items below are the **remaining** candidates, ranked by impact × safety for
a trading brain. Each is sized for a single Cowork → Claude Code NEXT_TASK.

---

## P1 — Wire source-content into the research consumers — ✅ SHIPPED 2026-06-01

**Shipped:** new flag-gated `web_search.research_search()` centralizes the opt-in.
`reasoning_brain/web_researcher.py` and `project_brain/web_research.py` now call
it; `reasoning`'s mechanical (non-LLM) summary also prefers fetched content.
Config: `search_fetch_sources: bool = False` (default off) + `search_max_fetch:
int = 3`. With the flag off, behavior is identical to before. Tests:
`TestResearchSearch` (flag off = no fetch; on = enrich up to cap; failed fetch
leaves result unenriched). 72 search tests + 21 research-consumer tests pass.
See `docs/STRATEGY/CC_REPORTS/2026-06-01_f-odysseus-salvage-research-content.md`.

**To activate:** set `CHILI_SEARCH_FETCH_SOURCES=1` (and optionally tune
`CHILI_SEARCH_MAX_FETCH`). Recommended to measure summary-quality lift on a
held-out set of ticker-catalyst topics before leaving it on.

---

## P2 — Standalone visual report generator — ✅ SHIPPED 2026-06-01

**Shipped:** new `app/visual_report.py` — `generate_report(title, body_markdown,
*, subtitle, label, sources, stats)` → complete self-contained HTML (no external
assets, no backend calls). Editorial CSS (dark/light via prefers-color-scheme,
aurora bg, reduced-motion aware), auto TOC sidebar with scroll-spy, optional
stats bar, collapsible sources, Print / Download-HTML toolbar. Trimmed from
odysseus `src/visual_report.py` (~1.87k LOC → ~480) — dropped the backend-coupled
machinery (OG-image reroll/hide → /api/research, chat-spinoff CTA, session ids,
per-category palettes). `markdown` declared as a dep (degrades to a regex
renderer if absent); `bs4` reused. 15 tests in `tests/test_visual_report.py`.
See `docs/STRATEGY/CC_REPORTS/2026-06-01_f-odysseus-salvage-visual-report.md`.

**Wiring (W1) SHIPPED 2026-06-01:** `GET /api/brain/reasoning/research/report`
(in `app/routers/brain.py`) renders the user's non-stale `ReasoningResearch` rows
into one self-contained HTML digest via `generate_report` (guests get an empty
digest; `?download=1` forces attachment). 5 integration tests in
`tests/test_reasoning_research_report.py`. See
`docs/STRATEGY/CC_REPORTS/2026-06-01_f-odysseus-salvage-wiring-w1-research-report.md`.
A daily-trading-brief export remains a future hook.

---

## P3 — MCP client — ✅ SHIPPED 2026-06-01

**Shipped:** new `app/mcp_client.py` — config-driven, CHILI-native external MCP
client (stdio + sse transports). **Read-only by policy with two independent
gates:** a per-server allowlist AND a hard in-code denylist of dangerous
tool-name patterns (order/trade/buy/sell/withdraw/transfer/...) that blocks a
tool even if allowlisted — applied at discovery AND re-applied at call time.
DORMANT by default (`mcp_enabled=False`, `mcp_servers_json=""`); guarded SDK
import. `mcp>=1.0` declared + installed. 50 tests in `tests/test_mcp_client.py`,
heavy on the safety gate. See
`docs/STRATEGY/CC_REPORTS/2026-06-01_f-odysseus-salvage-mcp-client.md`.

**Wiring (W2) SHIPPED 2026-06-01:** read-only `GET /api/brain/mcp/status` reports
the enabled flag, SDK presence, parsed server registry (ids/names/transport/
allowlist — never URLs/secrets), and a config-sanity flag listing any allowlisted
tool the denylist blocks. No live connections. See
`docs/STRATEGY/CC_REPORTS/2026-06-01_f-odysseus-salvage-wiring-w2-mcp-status.md`.

**Live connection lifecycle (W3) SHIPPED 2026-06-01:** `MCPSupervisor` in
`app/mcp_client.py` runs connect→serve-queue→disconnect in ONE task (anyio-safe);
wired into `app/main.py` lifespan gated on `mcp_enabled` (inert by default,
skipped under pytest); `GET /api/brain/mcp/status` now reports
`supervisor_running`. 9 supervisor tests (lifecycle, idempotent start,
not-running guards, connect-failure survival, timeout). See
`docs/STRATEGY/CC_REPORTS/2026-06-01_f-mcp-connection-lifecycle.md`. Brief at
`docs/STRATEGY/QUEUED/f-mcp-connection-lifecycle.md` (end-to-end stub-server test
remains a stretch goal).

---

## P4 — Teacher → skill-learning escalation — ✅ SHIPPED 2026-06-01

**Shipped:** new `app/teacher_escalation.py`. On a detected failure, a strong
"teacher" model is called with the failed trace and emits a portable reusable
skill; the skill is saved ONLY if the teacher's own response passes the same
failure check. The LLM caller and skill saver are **injectable** (decoupled,
unit-testable); a bounded JSONL `FileSkillStore` is the default (no migration).
The teacher prompt wraps the trace in `<<<UNTRUSTED_TRACE>>>` markers with a
data-not-instructions guard to prevent second-order prompt injection into
persisted skills. DORMANT by default (`teacher_escalation_enabled=False`).
29 tests in `tests/test_teacher_escalation.py`. See
`docs/STRATEGY/CC_REPORTS/2026-06-01_f-odysseus-salvage-teacher-escalation.md`.

**Live hook DEFERRED** to a dedicated brief —
`docs/STRATEGY/QUEUED/f-teacher-escalation-live-hook.md`. A meaningful hook needs
a failed *agent-with-tools* turn (user request + tool_results + agent_reply);
CHILI's single-shot planner doesn't cleanly produce that, so forcing a hook would
be shallow. The brief lays out the chat/planner-path option with a fire-and-forget
teacher call. `escalate_and_learn(...)` / `should_escalate(...)` stay ready.

---

## Explicitly REJECTED (CHILI already has an equivalent — do not import)

- **Deep research pipeline** (`src/deep_research.py`) — CHILI has the
  `context_brain` tree pipeline (decompose → execute → compile → synthesize).
  Importing odysseus's IterResearch loop would create a second parallel stack.
- **Vector RAG / memory** (`src/rag_vector.py`, `services/memory/`) — CHILI has
  `app/rag.py` (ChromaDB + Ollama embeddings). Duplicate.
- **LLM core / multi-provider abstraction** (`src/llm_core.py`) — CHILI has the
  `llm_router` tier cascade (Ollama → Groq → gpt-4o-mini → gpt-4o/Claude).
  Duplicate.
- **Agent loop + tool framework** (`src/agent_loop.py`) — only ~40% extractable;
  deeply coupled to odysseus's 40-tool ecosystem. Use as a *design reference* if
  CHILI ever needs a true iterative ReAct loop, but do not lift wholesale.
- **Hardware-fit / cookbook** (`services/hwfit/`) — local-model-serving advisor;
  not relevant to the trading brain's priorities.

---

## Notes for Cowork

- All P-items are off the live-trading execution path and respect the frozen
  prediction-mirror contract and live-placement safety belts.
- Attribution: any salvaged file must carry an MIT attribution header pointing at
  odysseus (and Apache-2.0 note for DeepResearch-derived bits), matching the
  header already added to `app/search_providers.py`.
- Suggested sequence: **P1 first** (it activates value already shipped in Win #1
  for near-zero cost), then **P2** (cheap, visible), then P3/P4 as strategic.

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

**Not yet wired into a route/consumer** — it's a ready util. Natural next hooks:
a "download brief as HTML" button on a research report, and an export for the
daily trading brief / CC-style summaries. Left as a small follow-up so this
commit stays a pure, side-effect-free addition.

---

## P3 — MCP client (extensibility)

**Gap:** CHILI is not an MCP client (confirmed absent). It cannot consume
external MCP servers (e.g. a broker-docs server, an SEC-filings server).

**Task:** salvage odysseus `src/mcp_manager.py` patterns to add a minimal MCP
client: connect to configured stdio/HTTP MCP servers, list tools, dispatch
calls. Keep it read-only/allowlisted initially; do **not** expose any tool that
can place orders.

**Why it matters:** clean extension point for future data sources without
bloating the core. Strategic, not urgent.

**Blast radius:** medium — new surface area; must be allowlisted and kept off any
order-placement authority. Coupled in odysseus (≈40% extractable per assessment)
— treat as reference, build CHILI-native.

---

## P4 — Teacher → skill-learning escalation (niche)

**Gap:** CHILI's `llm_router` escalates weak→strong on a confidence heuristic but
does **not** persist a reusable procedure when a strong model rescues a failure.

**Task:** salvage odysseus `src/teacher_escalation.py` (75% self-contained). When
the local model fails a task and a stronger model succeeds, have the teacher emit
a portable SKILL.md-style procedure (paths/hosts/model-names stripped) stored for
later reuse. Inject CHILI's LLM caller + a skill-saver.

**Why it matters:** compounding improvement of the local-first agent over time.
Niche — only pays off where weak+strong model pairs run regularly.

**Blast radius:** low-medium — touches the LLM layer, not trading. Needs a place
to persist skills (CHILI has `code_brain` / planner stores to evaluate).

**Salvage ref:** odysseus `src/teacher_escalation.py` — read in full; the
escalation gate + untrusted-trace guard + portable-skill generation are the
valuable parts.

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

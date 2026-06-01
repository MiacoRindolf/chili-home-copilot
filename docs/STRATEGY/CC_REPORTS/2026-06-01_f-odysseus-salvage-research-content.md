# CC_REPORT: f-odysseus-salvage-research-content (P1)

**Type:** operator-directed, out-of-band (operator authorized proceeding outside
the Cowork NEXT_TASK queue, 2026-06-01). Follows
`2026-06-01_f-odysseus-salvage-resilient-search.md`. The active
`NEXT_TASK.md` (phase-5i soak) remains untouched and open.

## What shipped

Activated the source-content fetching that Win #1 shipped but left dormant —
research now summarizes from full article text instead of search snippets alone,
**behind a default-off flag** so the live research cadence is unchanged until
enabled.

- **`web_search.research_search()`** (new) — centralizes the opt-in: it is exactly
  `search()` when `settings.search_fetch_sources` is False; when True it enriches
  up to `settings.search_max_fetch` results with fetched page content (truncated)
  via the existing SSRF-safe `fetch_source()`.
- **`reasoning_brain/web_researcher.py`** — `_search_topic` now calls
  `research_search`; `_snippet_from_result` prefers a leading slice of fetched
  `content` when present, so the mechanical (non-LLM) summary path also benefits.
- **`project_brain/web_research.py`** — `research_topics` now calls
  `research_search`; fetched content flows into the raw JSON the LLM summarizes.
- **Config** (`app/config.py`): `search_fetch_sources: bool = False`,
  `search_max_fetch: int = 3`.

Files touched: 4 modified (`app/config.py`, `app/web_search.py`,
`app/services/reasoning_brain/web_researcher.py`,
`app/services/project_brain/web_research.py`), 1 test file extended
(`tests/test_web_search.py`), backlog doc updated. No migrations.

## Verification

- New `TestResearchSearch`: flag-off makes no fetch call (identical to `search()`);
  flag-on enriches exactly up to the cap and leaves the rest unenriched; a failed
  fetch leaves the result unenriched. **Pass.**
- `tests/test_web_search.py` + `tests/test_search_providers.py` +
  `tests/test_reasoning_web_research_mechanics.py`: **72 passed.**
- Consumer regression: `test_research_integrity.py`,
  `test_phase2_research_hygiene.py`, `test_project_brain_*`: **21 passed.**
- Total this task: **0 regressions.**

## Surprises / deviations

- A first-run test failure was test-isolation, not a code bug: the mock's return
  value reused shared dict objects and `research_search` enriches in place, so one
  test leaked `content` into the next. Fixed the fixture to mint fresh dicts per
  call. (The in-place enrichment is intended — callers pass freshly-searched
  lists.)

## Deferred

- No production measurement of summary-quality lift yet — recommend a held-out
  ticker-catalyst A/B before leaving `search_fetch_sources` on in prod.
- P2 (visual report), P3 (MCP client), P4 (teacher-skill escalation) remain in
  `docs/STRATEGY/QUEUED/f-odysseus-salvage-backlog.md`.

## Open questions for Cowork

1. Flip `CHILI_SEARCH_FETCH_SOURCES=1` in a soak window to measure lift, or hold
   until a provider key (Brave/SearXNG) is provisioned so the cascade isn't
   DDG-only first?
2. Proceed to P2 (visual report generator) next, or pause salvage here?

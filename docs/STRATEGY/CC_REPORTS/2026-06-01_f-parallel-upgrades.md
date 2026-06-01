# CC_REPORT: f-parallel-upgrades (3 agents)

**Type:** operator-directed, out-of-band ("spawn 3 more agents to help upgrade in
parallel", 2026-06-01; commit→push→PR→merge per change). `NEXT_TASK.md`
(phase-5i soak) untouched.

## Approach

Spawned 3 background subagents, each constrained to a **disjoint file surface**
with **DB-free tests** (so no merge conflicts and no shared test-DB truncation
races), none touching trading/frozen code, `config.py`, `main.py`, or
`requirements.txt`. I reviewed every diff (not blind trust), fixed one
cross-module defect, and shipped each surface as its own PR.

## What shipped (3 PRs)

**U1 — Daily Trading Brief builder** (`app/services/trading_brief.py` + tests)
- Pure `build_brief(summary) -> {title, subtitle, label, markdown, stats,
  sources}`; turns a pre-fetched trading summary into a markdown brief shaped for
  `visual_report.generate_report`. Every input key optional; degrades gracefully.
- Money/percent/payoff formatting, GFM tables with pipe-escaping.
- **Defect I caught + fixed in review:** the builder emitted markdown starting
  with `## Performance` and no top-level `#` title, so `generate_report` would
  steal "Performance" as the hero title and drop that section. Fixed by leading
  with `# Daily Trading Brief`; added an end-to-end integration test
  (`test_brief_composes_with_visual_report`) pinning it. (This is the issue the
  brief-builder agent flagged as a "visual_report bug" — it's actually a
  brief-side contract fix.)

**U2 — visual_report theming + plaintext** (`app/visual_report.py` + tests)
- `generate_report(..., category="")` re-tints the accent palette for
  `brief`/`research`/`audit`/`alert` (light + dark); unknown/empty → default,
  byte-compatible (`body_class=""`, empty `category_css`).
- New `to_plaintext(markdown)` export. 26 tests (10 prior + 16 new) incl. the
  no-unfilled-placeholder guard extended to the new template slots.

**U3 — search dedup + concurrent fetch** (`app/search_providers.py`,
`app/web_search.py` + tests)
- `_normalize_url` + `_dedupe_results`; `resilient_search` now dedupes the
  winning provider's results (by normalized URL) before the count cap.
- `fetch_many(urls, max_workers=4)` — bounded ThreadPoolExecutor (≤8 workers),
  input de-dupe, order-preserving, never raises; `web_search.fetch_sources(urls)`
  thin wrapper. 82 search tests pass.

## Verification

- Per-surface agent runs: U1 builder tests, U2 26 pass, U3 82 pass.
- **Combined suite** (trading_brief + visual_report + search_providers +
  web_search + mcp_client + teacher_escalation): **214 passed** — confirms the
  three surfaces compose. After the U1 title fix + integration test: trading_brief
  **22 passed**.

## Surprises / deviations

- The cross-module title-stealing defect (above) — exactly the kind of thing
  parallel agents miss when each only sees its own surface; caught in integration
  review.
- The brief-builder agent opened a spawned-task chip for a "visual_report bug";
  it was the brief-side H1 issue, now fixed — that chip can be dismissed.

## Deferred

- Wiring a `GET /api/brain/trading/brief` route (fetch real trading summary →
  build_brief → generate_report). U1 ships the pure builder; the route + DB-fetch
  is a small follow-up (kept out to avoid touching brain.py/main.py in this batch).
- `category=` is available but no caller passes it yet; the research-digest route
  (W1) could pass `category="research"`.

## Open questions for Cowork

1. Wire the daily-brief route + a scheduled artifact, or leave `build_brief` as a
   library for now?
2. Adopt `fetch_sources`/dedup in the research path (pairs with `search_fetch_
   sources`)?

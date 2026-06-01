# CC_REPORT: f-odysseus-salvage-wiring-w1-research-report

**Type:** operator-directed, out-of-band ("yes continue" → wire the dormant
salvage utilities, 2026-06-01; commit→push→PR→merge per change). `NEXT_TASK.md`
(phase-5i soak) untouched. First of the wiring follow-ups.

## What shipped

- **`GET /api/brain/reasoning/research/report`** (new, `app/routers/brain.py`) —
  renders the caller's non-stale `ReasoningResearch` rows into one self-contained
  HTML digest via `app/visual_report.py` (`generate_report`). Read-only.
  - Aggregates up to 50 rows ordered by relevance then recency; one `##` section
    per topic, deduped sources collected into the report's sources panel, a stats
    bar (Topics / Sources).
  - Guests (no `user_id`) get a friendly empty digest, not an error.
  - `?download=1` sets `Content-Disposition: attachment` so the browser saves the
    file; default is inline view.
  - Identity via `get_identity_ctx`; no client-supplied ids trusted.

This activates the P2 visual-report util on a real consumer (the first wiring).

Files: `app/routers/brain.py` modified (+1 route), 1 test added
(`tests/test_reasoning_research_report.py`). No schema, no migrations, no trading
code, no LLM calls.

## Verification

- `tests/test_reasoning_research_report.py` (5 cases): guest → empty digest;
  paired user sees their topic/summary/source; **stale rows excluded**; download
  sets the attachment header; **malformed `sources` JSON doesn't crash**. All 5
  pass (full-app boot integration test; ~7.5 min incl. migrations on the test DB).

## Surprises / deviations

- None. The endpoint reuses the existing `brain` router, its already-imported
  `ReasoningResearch`/`HTMLResponse`/`get_identity_ctx`, and the P2 generator.

## Deferred

- A daily-trading-brief / CC-summary export using the same generator (separate
  data source) — future hook.

## Open questions for Cowork

- Should this digest be linked from the Brain UI (`brain.html`), or stay an
  API/download-only endpoint for now?

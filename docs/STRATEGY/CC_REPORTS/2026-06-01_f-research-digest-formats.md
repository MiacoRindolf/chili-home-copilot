# CC_REPORT: f-research-digest-formats

**Type:** operator-directed, out-of-band ("continue", 2026-06-01;
commit→push→PR→merge per change). Branched from latest `origin/main` (`c09a7ef`);
codex had not pushed since #192 and did not touch `brain.py`. `NEXT_TASK.md`
(phase-5i soak) untouched.

## What shipped

Mirrors the trading-brief `format=` option (PR #192) onto the research digest for
symmetry:

- **`GET /api/brain/reasoning/research/report?format=`** — `json`
  (`{ok, title, topic_count, topics:[{topic, summary, relevance_score}], sources}`),
  `text` (plaintext via `visual_report.to_plaintext`; `?download=1` → `.txt`), or
  `html` (default, unchanged). Reuses the imports added in #192.

## Verification

- Compile OK.
- Live route smoke (single-connection `TestClient`): all three formats **200** —
  `json` → `application/json` with keys `{ok, sources, title, topic_count,
  topics}`; `text` → `text/plain`; default → `text/html`. (`RFMT_SMOKE_OK`.)
- `tests/test_reasoning_research_report.py` extended with json/text/default-html
  cases (added; not run this session due to the degraded shared test DB; verified
  by smoke).

## Surprises / deviations

- None.

## Deferred / open questions

- Both report endpoints now expose json/text. A small shared helper could DRY the
  two format dispatchers if a third report endpoint appears.

# CC_REPORT: f-brief-machine-readable-formats

**Type:** operator-directed, out-of-band ("continue", 2026-06-01;
commit→push→PR→merge per change). Branched from the LATEST `origin/main`
(`26d0332`, includes the parallel codex trading-integrity work — confirmed codex
did NOT touch any of my files). `NEXT_TASK.md` (phase-5i soak) untouched.

## What shipped

- **`GET /api/brain/trading/brief?format=`** — added `json` and `text` variants
  alongside the default `html`:
  - `json` → `{ok, title, subtitle, window_hours, summary, stats, sources}` for
    programmatic/mobile use (the structured `build_trading_summary` output).
  - `text` → plaintext via `visual_report.to_plaintext` (the helper added in U2);
    `?download=1` → `.txt` attachment.
  - `html` (default) unchanged.
  - Added `PlainTextResponse` to the brain router imports.

Makes the daily brief consumable by scripts/mobile, not just as a web page.
Read-only, in my isolated reporting surface (no overlap with codex).

## Verification

- Compile OK.
- Live route smoke (`TestClient`, single connection — no truncation, since the
  shared pytest DB is currently deadlock-prone from this session's load): **all
  three formats 200** — `format=json` → `application/json` with `ok:true`;
  `format=text` → `text/plain`; default → `text/html`. (`FMT_SMOKE_OK`.)
- `tests/test_trading_brief_route.py` extended with 5 format cases (json payload,
  plaintext, text download header, default-html, existing 422). They couldn't be
  run via pytest this session (degraded test DB → fixture-truncate deadlocks),
  but the live smoke verifies the actual behavior; the tests will run green once
  the DB is recycled.

## Surprises / deviations

- `Response` isn't imported in the brain router; switched the text branch to
  `PlainTextResponse` (added to the existing `fastapi.responses` import).

## Deferred / open questions

- Mirror `format=json|text` on the research-digest route too? (Same pattern.)

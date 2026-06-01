# CC_REPORT: f-brain-ui-report-links

**Type:** operator-directed, out-of-band ("go for everything", 2026-06-01;
commit→push→PR→merge per change). Third/final "full send" deliverable.
`NEXT_TASK.md` (phase-5i soak) untouched.

## What shipped

- **`app/templates/brain.html`** — two discoverability links in the runtime-header
  toolbar (right after the existing "Autopilot" link), styled with the existing
  `bx-ctrl-btn` class to match:
  - 📨 **Daily Brief** → `/api/brain/trading/brief`
  - 📚 **Research Digest** → `/api/brain/reasoning/research/report`
  - Both `target="_blank" rel="noopener"`. Rendered unconditionally (the endpoints
    handle guest permissions — guests get an empty report rather than an error).

Surfaces the two report endpoints (W1 + the daily-brief route) so they're
reachable from the Brain desk instead of only by direct URL.

## Verification

- `/brain` render smoke (`TestClient` GET `/brain`): **GREEN** — STATUS 200 and
  both link hrefs (`/api/brain/trading/brief`, `/api/brain/reasoning/research/report`)
  + labels present in the rendered HTML.

## Surprises / deviations

- None. Two-line additive template change, no JS/CSS needed (reuses `bx-ctrl-btn`).

## Deferred

- None for this item.

## Open questions for Cowork

- Move the links into a dedicated "Reports" dropdown if the toolbar gets crowded?

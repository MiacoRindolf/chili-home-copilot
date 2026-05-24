# CC_REPORT: f-brain-runtime-drawer-reduced-motion

**Date:** 2026-05-23
**Brief:** `docs/STRATEGY/NEXT_TASK.md` (slug: `f-brain-runtime-drawer-reduced-motion`)
**Plan:** `scripts/_claude_session_consult/f-brain-runtime-drawer-reduced-motion/plan.request.md`
**Source:** Open Question §1 of `2026-05-23_f-brain-runtime-tab-redesign.md`, ACK'd in matching COWORK_REVIEW.

## What shipped

One commit on `main`:

- **`e19b4f3`** — `style(brain-runtime): honor prefers-reduced-motion for diagnostics drawer`
  - Added an 8-line `@media (prefers-reduced-motion: reduce)` block at the end
    of the "Runtime tab redesign 2026-05-23" section in
    `app/static/css/brain-trading.css` (lines 378–384), neutralizing the
    `.bx-diagnostics-drawer` (`transition: transform .2s ease`, line 267) and
    `.bx-diagnostics-drawer-backdrop` (`transition: opacity .15s ease`, line 254)
    transitions with `transition: none`. The `.open` modifiers — `transform:
    translateX(0)` and `opacity: 1` — stay intact, so the drawer still opens
    and closes; it just snaps rather than slides.

**Files touched:** 1 modified (`app/static/css/brain-trading.css`, +8 lines, 0 deletions).
**No JS edits. No template edits. No DOM-id changes. No migrations.**

Spot-check confirmed no `.bx-*` selector currently uses the `animation`
property (the deleted `brainPanelIn` keyframe was removed in Phase D), so the
block targets only the two drawer transitions per the brief's §1.

## Verification

- **`grep -nA5 'prefers-reduced-motion' app/static/css/brain-trading.css`**:
  ```
  379:@media (prefers-reduced-motion: reduce) {
  380-  .bx-diagnostics-drawer,
  381-  .bx-diagnostics-drawer-backdrop {
  382-    transition: none;
  383-  }
  384-}
  ```
- **`git diff --stat`**: `app/static/css/brain-trading.css | 8 ++++++++` — exactly
  one file touched, additive only.
- **Service smoke**: `docker compose up -d --force-recreate chili`; after 10s,
  `curl -fks https://localhost:8000/brain?domain=trading` → **HTTP 200**. No
  startup errors in container logs related to migrations or template render.
- **Playwright reduced-motion screenshot**: captured
  `docs/STRATEGY/CC_REPORTS/2026-05-23_runtime-tab-redesign-screens/05_drawer_reduced_motion.png`
  via `scripts/_brain_screenshots_reduced_motion.py` using a context with
  `reduced_motion="reduce"`. The drawer is fully at `translateX(0)` only
  ~200ms after invoking `openBxDiagnosticsDrawer('patterns')` — without the
  carve-out the `.2s ease` transition would leave it mid-slide at that tick.
  The backdrop is at full opacity (hard cut, no fade). Both Pattern Gate
  Status and Dispatch Queue Health subpanels render inside the drawer.

## Surprises / deviations

- **Helper script added alongside the CC report**:
  `scripts/_brain_screenshots_reduced_motion.py` mirrors the existing
  `_brain_screenshots.py` but uses `reduced_motion="reduce"` in the
  `browser.new_context(...)` options. Including it with the report commit
  (not the style commit) so the style commit stays strictly the 8-line CSS
  edit the brief described.
- **No `*[class^="bx-"] { animation: none }` defensive rule added.** The
  brief's §3 said "spot-check; if any other animations survive, neutralize
  them in the same media block". The grep found zero `.bx-*` selectors with
  an `animation:` property, so adding a wildcard would target rules that
  don't exist — out of scope per the "drawer-only" constraint on §"Out of
  scope" line 36–38.

## Deferred

- **Global reduced-motion pass on `/brain?domain=trading`** — explicitly
  flagged out-of-scope in the brief (lines 36–38). Animations on `.brain-pulse`,
  `.b-domain-dot.learning`, `.tbn-edge-pulse`, `.tbn-neural-*`, `.bw-status-dot.running`,
  and the `brainModalIn .2s ease` modal entry remain unmuted under
  `prefers-reduced-motion: reduce`. Worth a single bundled brief later if an
  a11y sweep is queued — there are ~7 `animation:` properties in the file.
- **Reduced-motion variant for `_runtime_help_modal.html` content swap** —
  the brief noted this overlay has no animation today, so nothing was changed.
  The shared `#brain-modal-overlay` shell does animate (line 892:
  `animation: brainModalIn .2s ease`); a future a11y brief should fold that
  into the reduced-motion carve-out.

## Open questions for Cowork

None for this brief — it was a single-file additive CSS change matching the
spec verbatim. The two "deferred" items above are the only candidates for a
future a11y-pass brief; flagged for your queue rather than my next session.

## How to re-run the verification screenshot

```
conda run -n chili-env --no-capture-output python scripts/_brain_screenshots_reduced_motion.py
```

Expects the chili web service at `https://localhost:8000/brain?domain=trading`.
Writes `05_drawer_reduced_motion.png` next to the four phase screenshots.

# NEXT_TASK: f-brain-runtime-drawer-reduced-motion

STATUS: DONE   <!-- shipped 2026-05-23 in commit e19b4f3; CC_REPORT: docs/STRATEGY/CC_REPORTS/2026-05-23_f-brain-runtime-drawer-reduced-motion.md -->

**Source:** Open Question §1 in `docs/STRATEGY/CC_REPORTS/2026-05-23_f-brain-runtime-tab-redesign.md`, ACK'd in `docs/STRATEGY/COWORK_REVIEWS/2026-05-23_f-brain-runtime-tab-redesign.md`.
**Scope:** Single CSS edit. ~5 minutes of CC time. No JS, no template, no DOM-id change.
**Risk:** Trivial. Reversible.

## Goal

Honor `prefers-reduced-motion: reduce` for the new diagnostics drawer so users with vestibular-sensitivity settings don't get the 200ms slide-in animation.

## What to change

`app/static/css/brain-trading.css` — add a `@media (prefers-reduced-motion: reduce)` block that:

1. Sets `transition: none` on `.bx-diagnostics-drawer` and `.bx-diagnostics-drawer-backdrop`.
2. Keeps the `transform: translateX(0)` / `opacity` end-state — the drawer still opens/closes, just instantly.
3. While there: also disable any other entry-animation keyframes on `bx-*` selectors (the `brainPanelIn` keyframe is already gone from Phase D, but spot-check; if any other animations survive, neutralize them in the same media block).

## Acceptance

- `grep -nA3 'prefers-reduced-motion' app/static/css/brain-trading.css` shows the new block.
- Visually verify (Playwright headless with `--force-prefers-reduced-motion` OR Chrome devtools "Emulate CSS prefers-reduced-motion: reduce"): drawer opens instantly with no slide; closes instantly. Backdrop fade-in becomes a hard cut.
- No JS changes. No DOM-id changes. No other CSS touched (`git diff --stat` shows exactly one file: `brain-trading.css`).
- `git grep` for `prefers-reduced-motion` returns at least the new block.

## Commit message

```
style(brain-runtime): honor prefers-reduced-motion for diagnostics drawer
```

## Out of scope

- Any other animation on the page (network mesh activation, sidebar toasts, etc.). Address those in a global a11y pass if one is queued; this brief is drawer-only.
- Other a11y items (color contrast, focus ring tuning, keyboard order). Separate briefs.
- Reduced-motion variants for the `_runtime_help_modal.html` overlay — that one currently has no animation, so nothing to do.

## Verification (no commit beyond the one in the commit message)

1. After the CSS edit, restart the chili web service: `docker compose up -d --force-recreate chili`. Confirm HTTP 200 at `https://localhost:8000/brain?domain=trading`.
2. Re-run the existing screenshot helper to capture a "reduced-motion drawer open" snapshot:
   - If Playwright supports `forced_colors`/`prefers_reduced_motion` context options, capture `05_drawer_reduced_motion.png` into `docs/STRATEGY/CC_REPORTS/2026-05-23_runtime-tab-redesign-screens/`.
   - If not, skip and note in the CC report.
3. Write CC_REPORT at `docs/STRATEGY/CC_REPORTS/$(Get-Date -Format yyyy-MM-dd)_f-brain-runtime-drawer-reduced-motion.md`.
4. Mark NEXT_TASK STATUS DONE with the commit hash.

## Rollback

`git revert <commit>` — the CSS block is additive; reverting just removes the new `@media` block.

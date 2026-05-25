# QUEUED: f-brain-runtime-drawer-reduced-motion

**Source:** Open Question §1 in `docs/STRATEGY/CC_REPORTS/2026-05-23_f-brain-runtime-tab-redesign.md`, ACK'd in `docs/STRATEGY/COWORK_REVIEWS/2026-05-23_f-brain-runtime-tab-redesign.md`.
**Scope:** Single CSS edit. ~5 minutes of CC time. No JS, no template, no DOM-id change.
**Risk:** Trivial. Reversible.

## Goal

Honor `prefers-reduced-motion: reduce` for the new diagnostics drawer so users with vestibular-sensitivity settings don't get the 200ms slide-in animation.

## What to change

`app/static/css/brain-trading.css` — add a `@media (prefers-reduced-motion: reduce)` block that:

1. Sets `transition: none` on `.bx-diagnostics-drawer` and `.bx-diagnostics-drawer-backdrop`.
2. Keeps the `transform: translateX(0)` / `opacity` end-state — the drawer still opens/closes, just instantly.
3. While there: also disable the `brainPanelIn` keyframe (it's already gone from Phase D, but check; if any other entry-animation keyframes survive on `bx-*` selectors, neutralize them in the same media block).

## Acceptance

- `grep -A3 'prefers-reduced-motion' app/static/css/brain-trading.css` shows the new block.
- Manual smoke (Chrome devtools "Emulate CSS prefers-reduced-motion: reduce"): drawer opens instantly with no slide; closes instantly. Backdrop fade-in becomes a hard cut.
- No JS changes. No DOM-id changes. No other CSS touched.

## Commit message

```
style(brain-runtime): honor prefers-reduced-motion for diagnostics drawer
```

## Out of scope

- Any other animation on the page (network mesh activation, sidebar toasts, etc.). Address those in a global a11y pass if one is queued; this brief is drawer-only.

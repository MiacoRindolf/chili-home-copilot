# QUEUED: f-brain-global-a11y-reduced-motion-pass

**Source:** Deferred items §1 + §2 of `docs/STRATEGY/CC_REPORTS/2026-05-23_f-brain-runtime-drawer-reduced-motion.md`, ACK'd in the matching COWORK_REVIEW (§"Answers to CC's deferred items").
**Scope:** Single CSS edit in `brain-trading.css`. Bundles seven `animation:` properties + one `transition`-driven modal overlay into one `@media (prefers-reduced-motion: reduce)` block.
**Risk:** Trivial. Reversible. No JS, no template, no DOM-id change.
**Priority:** Low — drawer was the only large-screen 200ms transform animation; remaining items are small pulses/rotations less likely to trigger vestibular issues. Pick up between higher-priority briefs.

## Goal

Extend the `prefers-reduced-motion: reduce` carve-out shipped in commit `e19b4f3` to the other animated surfaces on `/brain?domain=trading`, so users with vestibular-sensitivity settings get a uniformly still UI rather than one drawer-and-everything-else-still-pulsing.

## Targets

Run `grep -nE '(animation:|@keyframes)' app/static/css/brain-*.css app/static/css/brain-trading.css` before editing to capture the live list. Expected matches (from 2026-05-23 audit):

1. `.brain-pulse` — small loading-state pulse animation.
2. `.b-domain-dot.learning` — domain-card learning indicator pulse.
3. `.tbn-edge-pulse` — neural-mesh edge propagation pulse.
4. `.tbn-neural-*` — any neural-mesh layer animation (ring fade-ins, activation halos, ripples).
5. `.bw-status-dot.running` — worker-status dot pulse.
6. `#brain-modal-overlay` / `.brain-modal-card` — `animation: brainModalIn .2s ease` on the shared modal shell (this is the one `_runtime_help_modal.html` rides).
7. Any `bx-*` selector that has gained an `animation` property since 2026-05-23 (none at audit time, but spot-check before editing).

## What to change

`app/static/css/brain-trading.css` — extend the existing `@media (prefers-reduced-motion: reduce)` block (lines 379–384) to include the additional selectors. Two acceptable shapes:

**Shape A (explicit list, preferred):**

```css
@media (prefers-reduced-motion: reduce) {
  .bx-diagnostics-drawer,
  .bx-diagnostics-drawer-backdrop {
    transition: none;
  }
  .brain-pulse,
  .b-domain-dot.learning,
  .tbn-edge-pulse,
  .tbn-neural-ring,
  .bw-status-dot.running,
  #brain-modal-overlay,
  .brain-modal-card {
    animation: none !important;
  }
}
```

**Shape B (wildcard fallback, only if Shape A misses a runtime-discovered class):** add a defensive `*[class*="-pulse"], *[class*="brain-pulse"] { animation: none; }`. Avoid if Shape A covers everything live.

`!important` is acceptable here because the rule is INSIDE an `@media` block that the user explicitly opted into; it's not a global override.

## Acceptance

- `grep -nA15 'prefers-reduced-motion' app/static/css/brain-trading.css` shows the extended block.
- `git diff --stat` shows exactly one file changed: `app/static/css/brain-trading.css`. Additive only (no existing rules modified outside the `@media` block).
- Playwright reduced-motion capture (reuse `scripts/_brain_screenshots_reduced_motion.py`): worker-status dot is static (no pulse), neural-mesh nodes don't propagate (no edges pulsing), help-modal opens with no scale/fade — just shows.
- No JS edits, no template edits, no DOM-id changes.

## Commit message

```
style(brain-runtime): extend prefers-reduced-motion carve-out across all brain animations
```

## Out of scope

- Refactoring animations themselves (durations, easing curves). This brief is reduced-motion-only.
- Forced-colors / high-contrast `@media` support — separate brief.
- Reduced-motion handling outside `brain-trading.css` (e.g., `chat.html`, `home.html` if they have animations). This brief is brain-page-only.
- The Network sub-tab's SVG-based neural-mesh activation visuals if they are driven by JS (SMIL or JS-driven CSS-variable interpolation). If `grep` shows the mesh animation lives in JS, raise an Open Question in the CC_REPORT for a follow-up brief; do not refactor JS in this one.

## Rollback

`git revert <commit>` — additive-only; reverting just shrinks the `@media` block back to the drawer-only state from `e19b4f3`.

## Reference

- Drawer-only precursor: commit `e19b4f3`, CC_REPORT `2026-05-23_f-brain-runtime-drawer-reduced-motion.md`.
- File: `app/static/css/brain-trading.css` lines 379–384 (existing block to extend).

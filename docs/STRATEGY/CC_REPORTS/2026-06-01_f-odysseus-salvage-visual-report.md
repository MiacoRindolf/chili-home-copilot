# CC_REPORT: f-odysseus-salvage-visual-report (P2)

**Type:** operator-directed, out-of-band (operator authorized proceeding outside
the Cowork NEXT_TASK queue, 2026-06-01, and asked to commit→push→PR→merge per
change). Third in the odysseus-salvage series. `NEXT_TASK.md` (phase-5i soak)
remains untouched and open.

## What shipped

- **New `app/visual_report.py`** — `generate_report(title, body_markdown, *,
  subtitle="", label="CHILI Report", sources=None, stats=None) -> str` renders a
  complete, **self-contained** HTML document from markdown: no external assets,
  no backend calls, openable/shareable offline.
  - Editorial CSS: dark/light via `prefers-color-scheme`, animated aurora
    background (disabled under `prefers-reduced-motion`), serif display + system
    body typography.
  - Auto table-of-contents sidebar from h2/h3 with IntersectionObserver
    scroll-spy; smooth-scroll anchors.
  - Optional stats bar (`{label: value}`), collapsible sources list (domain
    extraction, www-stripped), Print / Download-HTML toolbar (pure client-side).
  - Title is taken from the report's first non-generic heading (stripped from the
    body to avoid duplication); all user content is `html.escape`d.
- **Trimmed from odysseus** `src/visual_report.py` (~1,870 LOC → ~480). Dropped
  the backend-coupled machinery that has no CHILI equivalent: OG-image
  reroll/hide wired to `/api/research/*`, chat-spinoff CTA, `session_id`
  plumbing, and the per-category palette/structural-CSS system. Kept the
  self-contained renderer + CSS.
- **requirements.txt:** declared `markdown>=3.5` (installed into chili-env, v3.10
  verified). The module guards the import and degrades to a minimal regex
  renderer if `markdown`/`bs4` are ever absent.

Files: 1 added (`app/visual_report.py`), 1 test added
(`tests/test_visual_report.py`), `requirements.txt` modified, backlog updated.
No schema changes, no migrations, no trading code touched.

## Verification

- `tests/test_visual_report.py` (15 cases): full-document structure; **no
  unfilled `.format()` placeholders** (guards the brace-escaping in the large
  CSS/JS template); title-from-heading; TOC anchors; sources + stats rendering;
  None-valued stat skipped; HTML escaping of title/subtitle; empty-body safety;
  bold-line→heading promotion; helper units (`_strip_thinking`,
  `_extract_headings` slug dedup, generic-heading skip, external-link new-tab,
  bare-URL autolink). **All 15 pass.**
- Generated a realistic daily-trading-brief sample (15.6 KB single file) and
  delivered it to the operator for visual confirmation.

## Surprises / deviations

- One test failure on first run was a wrong test assumption, not a bug: a
  malicious *title* was correctly overridden by the body's first heading (so it
  was never rendered), making the escape assertion moot. Reworked the test to use
  a heading-less body so the fallback title is actually exercised. Escaping is
  correct.

## Deferred

- Not yet wired into a route or consumer — `generate_report` is a ready util.
  Natural follow-ups: a "download brief as HTML" export on a research report, and
  a daily-trading-brief / CC-summary export. Kept out so this commit is a pure,
  side-effect-free addition.

## Open questions for Cowork

1. Where should the first wiring land — research report download, or a scheduled
   daily-trading-brief artifact?
2. Continue salvage to P3 (MCP client) / P4 (teacher-skill escalation), or pause
   here? P3/P4 are higher blast radius and touch the LLM/extensibility surface,
   so they warrant a deliberate go/no-go.

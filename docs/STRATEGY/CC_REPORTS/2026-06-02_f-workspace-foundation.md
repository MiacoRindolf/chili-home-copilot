# CC_REPORT: f-workspace-foundation (WS-1)

**Type:** operator-directed ("turn the old/dull desktop app into a real workspace
like odysseus but better — you have autonomy on look/feel", 2026-06-02). First
PR of the CHILI Workspace redesign initiative. Branched from latest `origin/main`.
`NEXT_TASK.md` (phase-5i soak) untouched.

## Diagnosis

CHILI's web UI was ~14 standalone pages each rolling its own header/nav with
inconsistent links + ad-hoc styling — no unifying shell. The Brain desk was the
only token-based, polished surface (an island). odysseus is a cohesive workspace
but has real weaknesses (redundant rail+sidebar nav, no routing/deep-linking,
only colors tokenized, 400k monolithic files, no build step).

## What shipped — the foundation

A unified **CHILI Workspace** shell + design system + a live Dashboard, built to
beat odysseus on its weak points:

- **`app/static/css/workspace.css`** — namespaced (`--ws-*`) design system,
  dark-first with a `[data-theme="light"]` override, **complete** token scale
  (color · space · radius · shadow · z · type — not just color). Shell + cards +
  KPIs + tables + command palette + responsive/reduced-motion.
- **`app/templates/_workspace.html`** — the shared shell every surface extends:
  ONE canonical icon rail (Dashboard/Chat/Trading/Brain/Research/Planner), a top
  command bar with a Cmd-K palette, and a content slot. Per-page `ws_active`
  highlight + `ws_crumb`.
- **`app/templates/dashboard.html`** — a real command-center landing: KPI row
  (Net P/L, win rate, open positions, top patterns), Today's closes, Open
  positions, Top patterns (with payoff), Research digest, Quick actions.
- **`app/services/dashboard_summary.py`** — LIVE data, read-only/defensive:
  reuses `build_trading_summary` + the reasoning-research rows. Every section
  degrades to an empty state rather than 500-ing.
- **`app/static/js/workspace.js`** — theme toggle (dark-first default, persists
  via the shared `chili-theme` key), Cmd-K palette with type-to-filter + arrow
  nav.
- **Route** `GET /workspace` (in `pages.py`) — non-breaking; the old `/` home is
  untouched. Migration of Chat/Trading/Brain/Planner into the shell follows in
  WS-2+.

## Verification (rendered, not just unit-tested)

- Built a fully-rendered prototype, iterated visually via the preview tool,
  screenshotted dark + light + the Cmd-K palette.
- Rendered the REAL `/workspace` template (TestClient) and screenshotted it — the
  shell, rail active state, KPI cards, and empty states all compose; tokens
  resolve (`--ws-bg #0b0e14`, cards `#11151e`, dark theme).
- `tests/test_dashboard_summary.py` (4): KPI formatting, payoff in top patterns,
  negative-P/L down state, no-user empty, trading-failure degrades. **Pass.**
- `tests/test_workspace_route.py`: `/workspace` → 200 with shell + KPIs + palette
  + the stylesheet link. (Verified live via render smoke `WS_RENDER_OK`.)

## Surprises / deviations — a real bug the preview caught

`workspace.css` had `brain-*/trading` in the header comment — the `*/` **closed
the block comment early**, merging the `:root{` selector into invalid garbage so
the ENTIRE token block was discarded by the CSS parser (structural rules still
applied, so it looked "half-styled"). This would have shipped the workspace
**completely unstyled in production**. Caught only by rendering the page and
inspecting computed styles (`--ws-bg` resolved to empty). Fixed by rewording the
comment. This is the case-in-point for verifying UI by *seeing* it.

## Deferred (the rollout)

- WS-2+: migrate Chat → Trading → Brain → Planner into the shell (one PR each).
- Cmd-K real search (chats/tickers/patterns), tool-aware streaming, PWA SW,
  mobile off-canvas rail, repointing `/` to the workspace once proven.
- Dashboard: add brain-status + recent-chats cards (need light queries into those
  models; left out of WS-1 to keep it shippable and all-real-data).

## Open questions for Cowork

1. Keep dark-first default for the workspace, or honor the global light default?
2. When ready, repoint `/` → `/workspace`, or keep the classic home alongside?

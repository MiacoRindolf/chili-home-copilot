# CC_REPORT: f-workspace-embed-frontdoor (WS-3)

**Type:** operator-directed ("yes keep going", 2026-06-02). Third PR of the CHILI
OS initiative. Branched from latest `origin/main`. `NEXT_TASK.md` (phase-5i soak)
untouched.

## What shipped

Makes the OS windows seamless and turns CHILI OS into the front door.

### Seamless windows (embed mode)
- When a page is opened inside an OS window, os.js requests it with `?embed=1`.
  Now the pages **honor it** by hiding their own header/nav (the OS window
  provides the chrome), so windowed apps are clean — no redundant inner header.
- Implemented **template-only, zero route changes**: each page gates its header
  with `{% if request.query_params.get('embed') != '1' %}` (the `request` object
  is already in the Jinja context). Applied to **Chat, Trading, Brain, Planner**
  (the Research/brief reports were already chrome-less).

### Front door
- `/` now renders **CHILI OS** (the dashboard). The classic household home moved
  to **`/home`** (still fully functional); the chore/birthday form handlers
  redirect to `/home`. `/workspace` remains an alias for `/`.

## Verification (TestClient smoke `WS3_OK`)

- `/` → OS dashboard (`ws-app`); `/home` → classic home (`CHILI Home`).
- `/chat` shows its header; `/chat?embed=1` **omits** it (content intact).
- `/trading?embed=1`, `/brain?embed=1` → 200; `/planner?embed=1` omits
  `planner-header`.
- `tests/test_workspace_route.py` extended: front-door (OS at `/`, classic at
  `/home`) + embed-strips-chrome (chat header gone when embedded, planner header
  gated). Compile green.

## Surprises / deviations

- Reading `request.query_params` directly in the template avoided touching any
  route for embed mode — cleaner and lower-risk than threading an `embed` flag
  through four route handlers.

## Deferred

- Dashboard hero/quick-action links (`New chat`, `Open desk`) still navigate
  full-page; a small os.js `[data-os-open]` hook would make them open windows
  too (WS-4 polish).
- Taskbar for minimized windows, keyboard window management, saved layouts.
- `/home` classic page's own "Home" nav link points at `/` (the OS now) — fine,
  but could relabel to "Workspace" later.

## Open questions for Cowork

1. Good with `/` = CHILI OS as the default front door, or prefer a redirect so the
   URL stays `/workspace`?
2. WS-4 next: make dashboard links open windows + a minimized-window taskbar?

# CC_REPORT: f-workspace-os-windowing (WS-2)

**Type:** operator-directed ("basically like a chili operating system", 2026-06-02).
Second PR of the CHILI Workspace → **CHILI OS** initiative. Branched from latest
`origin/main`. `NEXT_TASK.md` (phase-5i soak) untouched.

## What shipped — the window manager

Turns the workspace shell into an OS-like desktop: the dock opens CHILI surfaces
as draggable / tiling / minimizable **windows** over the dashboard desktop. This
is odysseus's single best idea (its windowing), rebuilt better — managed z-order,
URL-hash deep-linking, clean component layer, no monolith.

- **`app/static/js/os.js`** — vanilla window manager: open an app as a window
  (iframe of its real route), **drag** (title bar) with **edge-snap tiling**
  (left / right / maximize, with a ghost preview), **resize** (corner),
  **minimize** / **maximize-restore** / **close**, **focus** (z-order), a
  pop-out-to-tab control, and dock open-indicators. Restores a deep-linked app
  from `#app=chat` on load; exposes `window.ChiliOS.open(app)`.
- **`app/static/css/os.css`** — desktop, window chrome, snap ghost, dock dots,
  mobile full-window fallback.
- **`app/templates/_workspace.html`** — the rail is now a **dock**: items carry
  `data-app`/`data-src` so os.js opens them as windows (the `href` stays as a
  no-JS fallback → progressive enhancement). The dashboard content is the desktop
  home (dims when windows are open); the command palette opens apps as windows
  too. Apps wired: Chat, Trading Desk, Brain, Research, Planner.

## Verification (rendered)

- The window manager was first proven in a standalone prototype (Chat + Trading
  tiled side-by-side, drag/resize/snap/minimize/focus all working).
- Rendered the REAL `/workspace` with the OS layer and screenshotted it: opened
  Trading + Chat via `ChiliOS.open(...)`, tiled them — full window chrome (bars,
  controls, dock indicators) renders on the real integration. (The iframe bodies
  show 404 only because the static preview server has no CHILI routes; the live
  app loads the real pages.)
- `WS_OS_OK` route smoke: `/workspace` 200 with `os-desktop`/`os-home`/`os.js` +
  dock `data-src` routes.
- `tests/test_workspace_route.py` extended: asserts the OS desktop/home, os.css +
  os.js, and the chat/trading dock-as-window wiring.

## Surprises / deviations

- None. Progressive enhancement keeps the rail working as plain links if JS fails.

## Deferred

- **`?embed=1` clean mode** (WS-3): windowed apps currently iframe the full
  routes, so each shows its own page header inside the window. Add an embed mode
  to Chat/Trading/Brain that hides the page chrome when `embed=1`, for seamless
  windows. (os.js already appends `?embed=1`; the routes just need to honor it.)
- Taskbar for minimized windows (currently restored via the dock), keyboard
  window management (move/resize/cycle), per-session window layout persistence.

## Open questions for Cowork

1. Approve WS-3 (embed mode) next so windowed apps are chrome-less?
2. Repoint `/` → `/workspace` to make CHILI OS the front door?

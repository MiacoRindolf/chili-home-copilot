# CHILI OS — the workspace layer

CHILI OS is the desktop web app's home: a **local-first, windowed "operating
system"** that opens CHILI's real surfaces (Chat, Trading Desk, Brain, Research,
Planner) as draggable, tiling, minimizable windows over a live cockpit desktop.
It was built incrementally (branches `ws/1-…` through `ws/23-…`, all merged) and
this is its first consolidated architecture doc.

This doc is engineer-facing and tracks the source. Every API name, localStorage
key, endpoint, and shortcut below is copied from the code, not assumed.

---

## 1. Overview & philosophy

- **Windows are iframes of real routes.** A window is just an `<iframe>` pointed
  at the same route you'd hit directly (`/chat`, `/trading`, …) with an
  `?embed=1` flag appended. The OS does not reimplement any surface — it frames
  them. This keeps every app a normal, independently-navigable page.
- **Progressive enhancement / degrades without JS.** The dock items in the rail
  are plain `<a href>` links (`/chat`, `/trading`, …). With JS the click is
  intercepted and opens a window; with JS off (or `os.js` failing to load) the
  link still navigates. The window manager bails immediately if the desktop
  container is missing: `os.js` starts with `if (!desktop) return;`.
- **Local-first state.** Window layout, named Spaces, theme, and palette recents
  all live in `localStorage` (see the table in §7). The only server calls are
  two read-only JSON polls (`/api/workspace/search`, `/api/workspace/desktop`).
- **Read-only / defensive backend.** Both backend services wrap every DB query
  so a failure degrades one widget/result-group to empty or "unknown" rather
  than 500-ing the poll. There are no writes and no new schema.

### Front door routes

| Route | Serves | Notes |
|-------|--------|-------|
| `/` and `/workspace` | `dashboard.html` (extends `_workspace.html`) | The OS home. `workspace()` in `app/routers/pages.py`. |
| `/home` | `home.html` | The **classic** household home (chores/birthdays/calendar). Still reachable; no longer the front door. |

Child surfaces opened as windows honor `?embed=1` to strip their own chrome
(see §8).

---

## 2. The shell (`_workspace.html`) + design system

`app/templates/_workspace.html` is the OS frame **every workspace surface
extends** (it itself extends `base.html`). It renders:

- **Rail / dock** (`nav.ws-rail`) — the CHILI logo plus one `.ws-rb` button per
  app. Each app button carries `data-app`, `data-src`, `data-title`, `data-icon`
  (the dashboard button has only `data-app="dashboard"`, no `data-src`, because
  "Dashboard" is the desktop home, not a window). Items keep their `href` for
  no-JS fallback. A spacer pushes Profile + avatar to the bottom.
- **Topbar** (`header.ws-topbar`) — breadcrumb (`ws-crumb`), the ⌘K search
  trigger (`#ws-cmdk-trigger`), and a right cluster: notifications bell
  (`#ws-notif`), Spaces button (`#ws-spaces`), and theme toggle
  (`#ws-theme-toggle`).
- **Desktop** (`#os-desktop`) — contains the home content (`#os-home` →
  `.ws-wrap` → `ws_content` block) and the tiling drop-preview ghost
  (`#os-ghost`). The window manager appends windows and the taskbar here.
- **Command palette scrim** (`#ws-scrim` → `.ws-palette` with `#ws-palette-in`
  and `#ws-palette-results`).
- **Toast container** (`#ws-toasts`, fixed, `aria-live="polite"`).

It loads `workspace.css`, `os.css`, `os-notify.css` and the three scripts
`workspace.js`, `os.js`, `notifications.js`.

### Jinja blocks a child template can fill

| Block | Purpose |
|-------|---------|
| `ws_content` | The desktop home body (inside `#os-home > .ws-wrap`). |
| `ws_crumb` | The breadcrumb sub-label (defaults to `Dashboard`). |
| `ws_topbar_extra` | Extra controls injected into the topbar-right cluster (e.g. the today-P/L pill). |
| `ws_head` | Extra `<head>` content (after the workspace stylesheets). |
| `ws_title` | The `<title>` (suffixed with `· CHILI`). |

A child also sets the context var `ws_active` (e.g. `"dashboard"`) so the rail
highlights the current app.

### Design system (`workspace.css` + `os.css`)

`workspace.css` defines the `--ws-*` token system on `:root` and is **namespaced
to the `--ws-*` prefix and `.ws-*` classes so it never collides with `base.html`,
brain, or trading CSS** (per the file's own header comment). Tokens cover fonts
(`--ws-font-ui`, `--ws-font-mono`), a type scale (`--ws-fs-11`…`--ws-fs-34`),
weights, spacing (`--ws-s1`…`--ws-s12`), radii (`--ws-r-sm`…`--ws-r-pill`),
z-index layers (`--ws-z-rail`/`-topbar`/`-drawer`/`-palette`/`-toast`),
motion (`--ws-ease`, `--ws-dur`), surfaces, borders, text colors, semantic
colors (`--ws-accent`, `--ws-chili`, `--ws-up`, `--ws-down`, `--ws-gold`,
`--ws-violet`), and shadows.

The system is **dark-first**: the base `:root` block is the dark palette, and
**`:root[data-theme="light"]` overrides** the relevant tokens for light mode.

`os.css` layers the desktop/window-manager visuals on top of those tokens
(`.os-desktop`, `.os-win`, `.os-bar`, taskbar, the snap ghost, and the
entrance/minimize/close keyframes). `os-notify.css` styles the toast stack and
notification center.

---

## 3. Window manager (`os.js`)

A single IIFE, vanilla JS, no dependencies. State: `wins` (app → window element),
`order` (focus order, last = top), and a z-index counter `Z`.

### Lifecycle

| Function | Behavior |
|----------|----------|
| `openApp(cfg, geom)` | Creates the window element, sets initial geometry (cascade offset, or the `geom` from a restore), builds the title bar + body + `<iframe src=withEmbed(cfg.src)>`, wires controls, focuses it, and saves layout. If the app is **already open**, it re-shows/focuses it instead — and if `cfg.deep` (a deep-link) it re-points the existing iframe to the new src. |
| `closeApp(app)` | Plays the `.closing` animation, removes the element, drops it from `order`, clears the dock `os-open` state and taskbar chip, re-syncs the home dim, updates the hash, saves layout. |
| `minimizeApp(app)` | Adds a taskbar chip, plays the `os-out` shrink (skipped under reduced-motion or while restoring), hides the element (`display:none`), updates hash + layout. |
| `focusWin(app)` | Marks it `.active`, bumps z-index, moves it to the end of `order`, updates the URL hash. |

### The `?embed=1` convention

`withEmbed(src)` appends `embed=1` (`?` or `&` as needed). The iframe loads the
embedded src; the **title-bar "open in new tab" link and the Copy-link button use
the *raw* (non-embed) URL** so a copied/opened link is the normal page.

### Drag + edge/corner tiling

`dragify(el, app)` moves the window on title-bar drag (ignoring clicks on the
controls or the pop-out link). While dragging, the pointer position within the
desktop selects a **snap zone**, previewed by the ghost (`#os-ghost`); on
mouseup the window snaps to that zone. Zones (corners take priority over edges,
then the top strip):

| Zone | Trigger (within desktop) | Result |
|------|--------------------------|--------|
| `tl` / `tr` / `bl` / `br` | within a `130 × 110px` corner hot-zone | quarter tile |
| `max` | `y < 16px` (top strip) | full desktop |
| `left` / `right` | `x < 22px` / `x > W-22px` | left / right half |

`snap(el, zone)` computes the geometry from `desktop.clientWidth/Height` and
applies a brief `.snapping` transition. The title-bar **Tile/restore** button
(`.max`) toggles `max` against a saved restore-rect.

### Resize

`resizify(el)` drags the bottom-right handle (`.os-rs`), clamped to a
`340 × 220px` minimum.

### Taskbar (minimized windows)

A `.os-taskbar` is appended to the desktop. `addChip` / `removeChip` manage one
`.os-chip` per minimized app; clicking a chip re-shows and focuses the window.
`#os-home` gets `.dimmed` whenever any window is open (`syncHome`).

### Title-bar controls

Reload (re-sets the iframe `src` to re-trigger load, re-showing the loading
placeholder), Copy-link (writes the raw URL via `navigator.clipboard`, falling
back to a hidden `<textarea>` + `execCommand`), Minimize, Tile/restore, Close,
plus the pop-out "open in new tab" link.

### Animations & reduced motion

`animIn(el)` re-triggers the `os-in` entrance keyframe on open / restore-from-
taskbar; minimize uses `os-out`; close uses `.closing`. All entrance/minimize
motion is **skipped when `prefers-reduced-motion: reduce`** (`_reduceMotion`),
and the minimize shrink is also skipped while replaying a saved layout
(`restoring`).

### Keyboard shortcuts

Only fire while the OS chrome has focus (keydown inside an app iframe stays with
that iframe). See the reference table in §7.

---

## 4. Session restore & named Spaces

A **layout** is the set of open windows with their geometry, minimized state,
and focus order. `captureLayout()` serializes it; `applyLayout(data)` replays it
by re-opening each app's window via its dock button (`openApp(cfgFromEl(b), …)`)
in saved focus order.

- **Session restore.** Every mutating action calls `saveLayout()`, persisting to
  `localStorage["chili-os-layout"]`. On load, `restoreLayout()` re-opens the last
  arrangement; then a `#app=<id>` hash (if present) opens/focuses that app on top.
- **Named Spaces.** Snapshots stored as an array under
  `localStorage["chili-os-spaces"]` (an array preserves user ordering). Switching
  a Space tears down all current windows immediately (`closeAllNow()`) then
  `applyLayout`s the snapshot.

### The `ChiliOS` API

Exposed on `window` for other UI (dashboard quick-actions, the palette, the
Spaces menu) to drive the OS:

```js
window.ChiliOS = {
  // open(app) opens/focuses the app's default window.
  // open(app, srcOverride) opens it pointed at a deep-link URL
  //   (re-navigating the iframe if the window is already open).
  // Returns true if it opened as a window, false otherwise (e.g. no such
  //   dock entry — caller can fall back to navigation).
  open: function (app, srcOverride) { /* … */ },

  // Named Spaces — snapshot / restore window arrangements by name.
  spaces: {
    list:    function ()              { /* → [{ name, count }] */ },
    save:    function (name)          { /* snapshot current layout; → bool */ },
    open:    function (name)          { /* close-all then replay; → bool */ },
    remove:  function (name)          { /* delete the space */ },
    rename:  function (oldName, newName) { /* → bool (rejects dup names) */ },
    reorder: function (names)         { /* persist a new ordering; → true */ }
  }
};
```

`ChiliOS.open` resolves the app from its dock button
(`.ws-rb[data-app="…"][data-src]`); it returns `false` for `dashboard` (no
`data-src`), which is why the palette/quick-actions fall back to navigating to
the desktop home for that one.

---

## 5. Command palette (⌘K)

### Frontend (`workspace.js`)

`⌘K` / `Ctrl+K` (or the topbar trigger) opens the scrim. Typing debounces
(~130ms) and fetches `/api/workspace/search?q=…`. A monotonic `reqSeq` guard
drops stale responses. Each request is **prepended client-side** with:

- **Recents** (`localStorage["chili-os-recents"]`, capped at 8) — shown **only on
  the empty query** as a "jump back in" list.
- **Spaces** (from `ChiliOS.spaces.list()`) — always included, filtered by query.

Results are de-duplicated by a composite key. Arrow keys move the selection,
Enter / click opens. `openResult` records the item into recents, then routes:

- a **Space** result → `ChiliOS.spaces.open(name)`;
- an **app** result whose `url` carries query params (e.g. `/trading?ticker=NVDA`)
  → `ChiliOS.open(app, url)` as a **deep-link** (re-points the window's iframe);
- an app result without params → `ChiliOS.open(app)` (default surface);
- otherwise `window.open(url, '_blank')` (if `blank`) or `location.href = url`.

### Backend (`app/services/workspace_search.py`, `GET /api/workspace/search`)

Read-only and defensive; every group is wrapped so a failure yields `[]`. Returns
a flat ranked list of `{type, label, sub, icon, app?, url, blank?}`. An empty
query returns just the static destinations.

| Result `type` | Source | Deep-link `url` |
|---------------|--------|-----------------|
| `app` | static `_DESTINATIONS` (Dashboard, Chat, Trading Desk, Brain, Research, Planner) | the surface route |
| `action` | static `_ACTIONS` (daily trading brief — opens in a new tab; research digest) | endpoint |
| `ticker` | the user's distinct `Trade.ticker` | `/trading?ticker=<t>` |
| `pattern` | `ScanPattern.name` (ranked by `trade_count`) | `/brain?pattern=<id>` |
| `research` | the user's non-stale `ReasoningResearch.topic` | `/api/brain/reasoning/research/report` |
| `project` / `task` | the user's `PlanProject` / `PlanTask` | project: `/planner` (no deep-link — the page requires `project_id`+`task_id` together); task: `/planner?project_id=<p>&task_id=<t>` |

Ticker/research/planner groups require a signed-in user (`user_id`); pattern
search does not. The route wraps results as `{"ok": true, "results": [...]}`.

---

## 6. Live cockpit + notifications

### The cockpit (`desktop.js` + `desktop_live.py` + `GET /api/workspace/desktop`)

The dashboard home renders a cockpit bar (`#ws-cockpit`) with an Eastern-time
clock (ticked client-side every second), market / kill-switch / breaker status
pills, a "Last trade · …" relative-time indicator, KPI tiles, and live
open-positions / recent-closes widgets. `desktop.js` bails if `#ws-cockpit` is
absent, so the same scripts are harmless on non-dashboard surfaces.

**Poll pattern:** `poll()` fetches `/api/workspace/desktop`, **returns early when
`document.hidden`** (hidden-pause), and uses a monotonic `seq` guard so a slow
response can't overwrite a newer one. It runs on load, every **20s**
(`setInterval`), and again on `visibilitychange` when the tab regains focus.

The endpoint returns the view-model built by `desktop_live.build_live(db,
user_id)` — read-only, with each section wrapped so a failure degrades it to a
neutral state. JSON shape (keys copied from `build_live`):

```jsonc
{
  "ok": true,

  // _numbers() — today's headline figures (from dashboard_summary)
  "net_pnl_fmt": "$0.00",
  "net_pnl_up": true,
  "win_rate_fmt": "—",
  "open_positions": 0,    // count
  "closes_today": 0,      // count (drives the "trades closed" notification)
  "top_patterns": 0,      // count

  // _lists() — compact glance widgets
  "positions": [ { "ticker": "…", "side": "…" } ],            // ≤ 6
  "closes":    [ { "ticker": "…", "pattern": "…",
                   "pnl_fmt": "…", "pnl_up": true } ],         // ≤ 5

  // trading-safety + market state (each has its own "ok" health flag)
  "kill_switch": { "ok": true, "active": false, "reason": null },
  "breaker":     { "ok": true, "tripped": false, "reason": null },
  "market":      { "ok": true, "equities_open": false, "crypto_open": true },

  // ISO-8601 UTC ("…Z") of the latest trade open/close, or null (guest / none)
  "last_trade_iso": null
}
```

`desktop.js` `apply(d)` maps these onto the UI: KPIs `net_pnl` / `win_rate` /
`open` / `patterns` (flashing on change), the positions/closes lists, the
topbar today-P/L pill, and the three pills (`unknown` when a section's `ok` is
false; `active`/`tripped`/`equities_open` drive ok/warn/bad). The
"Last trade · …" phrase is recomputed every clock tick from `last_trade_iso` so
it advances between polls.

### Notifications (`notifications.js` + `os-notify.css`)

Frontend-only, **no new backend** — it reuses the same cockpit endpoint and the
same 20s + hidden-pause + sequence-guard poll. It **diffs successive snapshots**
(`prev` vs `cur`; the first poll just seeds the baseline) and toasts on:

- kill-switch `active` flip,
- drawdown breaker `tripped` flip,
- market `equities_open` flip,
- `closes_today` increasing (newly-closed trades; uses `closes[0]` for the body).

Toasts (`#ws-toasts`, stack capped at 4, auto-dismiss ~6.5s) also feed a
**notification center**: the topbar bell (`#ws-notif-btn`) with an unread badge
(`#ws-notif-badge`) and a history panel (capped at 40). Opening the panel clears
the unread count. Reduced motion shortens dismiss animations.

---

## 7. Reference tables

### Keyboard shortcuts

| Shortcut | Action |
|----------|--------|
| `⌘K` / `Ctrl+K` | Open the command palette |
| `Esc` | Close the palette / Spaces menu / notification panel |
| `⌘`` / `Ctrl+`` (backtick) | Cycle focus to the next window |
| `⌘/Ctrl+Alt+←` | Tile focused window to the left half |
| `⌘/Ctrl+Alt+→` | Tile right half |
| `⌘/Ctrl+Alt+↑` | Maximize |
| `⌘/Ctrl+Alt+1` / `2` / `3` / `4` | Tile top-left / top-right / bottom-left / bottom-right quarter |
| `⌘/Ctrl+Alt+↓` | Minimize the focused window |
| `⌘/Ctrl+Alt+W` | Close the focused window |

(Palette navigation: `↑`/`↓` move the selection, `Enter` opens.)

### localStorage keys

| Key | Written by | Holds |
|-----|------------|-------|
| `chili-theme` | `workspace.js` | `"dark"` / `"light"` (dark-first; shared so iframed apps inherit the theme) |
| `chili-os-layout` | `os.js` | the current window layout (`{ apps:[…], order:[…] }`) for session restore |
| `chili-os-spaces` | `os.js` | named Spaces (`{ spaces:[{ name, apps, order }] }`) |
| `chili-os-recents` | `workspace.js` | up to 8 recently-opened palette items |

### Routes / endpoints

| Path | Handler | Purpose |
|------|---------|---------|
| `/`, `/workspace` | `pages.workspace` | OS desktop home (`dashboard.html`) |
| `/home` | `pages.home` | classic household home |
| `GET /api/workspace/search?q=` | `pages.workspace_search` | ⌘K palette backend |
| `GET /api/workspace/desktop` | `pages.workspace_desktop` | live cockpit poll |

### Key files

| File | Role |
|------|------|
| `app/templates/_workspace.html` | the OS shell every surface extends |
| `app/templates/dashboard.html` | the desktop home content (cockpit + KPIs + widgets) |
| `app/static/js/os.js` | window manager (lifecycle, tiling, taskbar, Spaces, `window.ChiliOS`) |
| `app/static/js/workspace.js` | theme, ⌘K palette, Spaces menu |
| `app/static/js/desktop.js` | live cockpit poller |
| `app/static/js/notifications.js` | toasts + notification center |
| `app/static/css/workspace.css` | `--ws-*` design tokens (dark-first, namespaced) |
| `app/static/css/os.css` | desktop + window visuals |
| `app/static/css/os-notify.css` | toast / notification-center styles |
| `app/services/desktop_live.py` | cockpit view-model (`build_live`) |
| `app/services/workspace_search.py` | palette search backend |
| `app/routers/pages.py` | the routes above |

---

## 8. Extending the OS

### Add a new app to the dock

1. **Add a rail button** in `_workspace.html`'s `.ws-rail`, mirroring an existing
   one:

   ```html
   <a class="ws-rb {% if _a=='myapp' %}active{% endif %}"
      href="/myapp" data-app="myapp" data-src="/myapp"
      data-title="My App" data-icon="✨">
     <span class="ws-tip">My App</span>
     <svg …></svg><span class="os-dot"></span>
   </a>
   ```

   - `href` is the no-JS fallback (keep it real).
   - `data-app` is the unique id used everywhere (`#app=` hash, `ChiliOS.open`,
     dock wiring). `data-src` is what the iframe loads — **its presence is what
     makes the item open as a window** (the Dashboard button omits it on
     purpose). `data-title` / `data-icon` populate the window chrome.

2. **Make the surface embeddable.** `os.js` loads `data-src` with `?embed=1`. The
   target template must hide its own header/nav under that flag, e.g.:

   ```jinja
   {% if request.query_params.get('embed') != '1' %}{% include "myapp/_header.html" %}{% endif %}
   ```

   (Chat, Trading, Brain, Planner all do this.) Nothing else is required —
   `embed=1` only needs to suppress the chrome so the page sits cleanly inside a
   window.

3. **(Optional) Surface it in ⌘K** by adding an entry to `_DESTINATIONS` (or
   `_ACTIONS`) in `workspace_search.py`, or by emitting result rows with the same
   `app` id from a new search group. A result whose `url` carries query params is
   treated as a deep-link and re-points the window's iframe.

### Deep-linking

`ChiliOS.open(app, srcOverride)` opens (or re-points) a window at a specific URL.
The palette uses this for ticker/pattern/task results. Externally, loading the
desktop with `#app=<id>` opens/focuses that app on top after session restore.

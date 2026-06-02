# CC_REPORT: f-workspace-os-polish (WS-4)

**Type:** operator-directed ("go", 2026-06-02). Fourth PR of the CHILI OS
initiative. Branched from latest `origin/main`. `NEXT_TASK.md` (phase-5i soak)
untouched.

## What shipped — OS polish

- **Theme propagation** (`workspace.js`) — on first visit the OS now **persists**
  the dark default via the shared `chili-theme` key, so windowed apps (same-origin
  iframes) inherit the dark theme and **windows match the OS** instead of
  rendering light. Toggling still flips + persists as before.
- **Dashboard opens windows** (`os.js` + `dashboard.html`) — a generic
  `[data-os-open="app"]` hook opens that app as an OS window instead of navigating
  full-page. Wired the hero buttons (New chat → Chat, Open desk → Trading Desk)
  and the quick-actions (Ask Chili, Trading desk, Brain). `href` stays as a no-JS
  fallback.
- **Taskbar for minimized windows** (`os.js` + `os.css`) — minimizing a window now
  drops a chip (icon + title) into a bottom taskbar; clicking it (or the dock app)
  restores the window. Chips are removed on restore/close; the taskbar hides when
  empty.

## Verification (rendered)

- Re-rendered `/` (now the OS front door) into the preview with the updated
  assets; confirmed the dashboard carries `data-os-open` links and the OS layer.
- Exercised in the preview: opened apps from the dashboard quick-actions,
  minimized a window → the taskbar chip appears, clicked it → restored. (Iframe
  bodies 404 only because the static preview has no CHILI routes.)
- `WS4_RENDER_OK` smoke.

## Surprises / deviations

- Persisting the dark default is a (small, intended) global theme change — the
  classic `/home` and direct page visits now also default dark on first OS visit.
  Aligned with "CHILI OS is dark-first"; users can still toggle light.

## Deferred

- Keyboard window management (move/resize/cycle/snap via keys), saved per-session
  window layouts, multi-monitor-style virtual desktops.
- Cmd-K real search across chats/tickers/patterns (currently filters the static
  jump list).

## Open questions for Cowork

1. Good with dark as the new global default (set on first OS visit)?
2. WS-5: keyboard window management + saved layouts, or pivot to Cmd-K real search?

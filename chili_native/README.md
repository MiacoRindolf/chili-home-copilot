# CHILI Native (Windows desktop)

A native Windows desktop version of the CHILI workspace, built with **C# (.NET 8)
+ Avalonia UI**. This is a **separate** client from the Flutter app
(`chili_mobile/`) — the Flutter app is untouched and keeps working. See
[`docs/NATIVE_DESKTOP_STACK.md`](../docs/NATIVE_DESKTOP_STACK.md) for the why.

**Goal:** powerful (native Win32 window control via P/Invoke — overlays,
multi-window, framing external game windows) **and** beautiful (Skia-rendered,
animated UI — the strengths we liked in Flutter).

## Stack

- .NET 8 (`net8.0`), Avalonia 11.2, CommunityToolkit.Mvvm, Fluent theme.
- Custom window chrome (frameless, draggable title bar, custom window buttons).
- CHILI brand palette in `App.axaml` (dark surfaces + green accent + chili-red mark).

## Run

```powershell
dotnet run --project chili_native -c Debug
# or build then launch:
dotnet build chili_native -c Debug
.\chili_native\bin\Debug\net8.0\Chili.exe
```

## Layout

- `Program.cs` / `App.axaml` — entry + theme/palette.
- `Views/` — XAML views (`MainWindow` = the shell: title bar, workspace, dock).
- `ViewModels/` — MVVM view models.
- (coming) `Services/` — RS price engine port, HTTP. `Interop/` — Win32 P/Invoke.

## Backend connection

The native client talks to the CHILI FastAPI backend (`https://localhost:8000`)
exactly like the Flutter app: `Authorization: Bearer <device_token>`, JSON bodies,
SSE for streaming chat. Settings live in `%APPDATA%/CHILI/settings.json`
(`BaseUrl` + `DeviceToken`); the local dev self-signed cert is trusted for
localhost only. See `Services/ChiliApiClient.cs`.

## Migration status

Incremental port of the Flutter capabilities. The Flutter app stays the source
of truth until this reaches parity.

- [x] **NATIVE-1** — scaffold + CHILI dark shell (custom chrome, dock).
- [x] **NATIVE-2** — RuneScape price engine (C# port: WeirdGloop GE price + RS
      Wiki search/info/thumbnail) + animated price card hosted in the shell.
      Verified live (image + GE price + wiki blurb, fuzzy search).
- [x] **NATIVE-3** — app-switching shell: the dock launches apps (Prices, Chat,
      Trading, Games, Research) into the workspace; selected-tile highlight.
- [x] **NATIVE-4** — Win32 interop (`Interop/NativeWindows.cs`): read-only window
      enumeration (title/process/geometry) + the safe SetWindowPos MoveResize
      primitive. Games app lists live windows (the frame's picker). **Safety
      boundary documented: read geometry + SetWindowPos only; never SetParent /
      inject.**
- [x] **NATIVE-5** — game frame (`Views/GameFrameWindow`): a transparent, always-
      on-top, **hollow** (SetWindowRgn) CHILI frame placed around a *picked*
      window — titlebar + thin border, center clicks pass through. Drag the
      titlebar → the frame DRIVES the target (MoveResize). Closes when the target
      closes. Picked from the Games app's "Frame" button or `--frame <title>`.
      Verified attaching around Notepad.
- [x] **NATIVE-6** — resize-by-frame: a bottom-right corner grip (kept solid in
      the hollow region) resizes the frame and the target follows live
      (BeginResizeDrag + a unified move/resize sync). The frame now both moves and
      resizes the picked window. Verified grip + borders around Notepad.
- [x] **NATIVE-7** — on-game price overlay (`Views/PriceOverlayWindow`): a small
      frameless, always-on-top RS price card that floats over the framed game,
      opened from the frame titlebar's "◈ Prices" button (anchored at the game's
      top-left), draggable by its handle. The real-time gaming awareness — and the
      exact thing Flutter multi-window couldn't position/frameless on Windows; a
      native Avalonia window just works. Verified over a framed Notepad.
- [x] **NATIVE-8** — window grouping: the frame is made an OWNED window of the game
      (`SetWindowLongPtr` GWLP_HWNDPARENT) so they group in alt-tab / the taskbar
      and the frame floats above the game — easy to return to after app-switching.
      Safe: sets an attribute on the frame only; never reparents/injects/touches
      the game.
- [x] **NATIVE-9** — 90-day price sparkline: the price card fetches the WeirdGloop
      `last90d` history and draws a StreamGeometry trend line + a color-coded
      % change (green up / chili-red down). Verified live (abyssal whip +10.9%).
- [x] **NATIVE-10** — recent searches: looked-up items appear as clickable chips
      below the search box (most-recent-first, deduped, capped at 6); click to
      re-search. Verified (Rune platebody · Dragon claw · Abyssal whip).
- [x] **NATIVE-11** — Settings/About app (6th dock tile ⚙): version + stack,
      tagline, a green-checkmark capabilities list, and the anti-cheat safety
      note. Verified rendering.
- [x] **NATIVE-12** — keyboard shortcuts + double-click-maximize: Ctrl+1..6 switch
      dock apps, Ctrl+W closes; double-clicking the title bar toggles maximize.
      Verified (Ctrl+3 → Trading).
- [x] **NATIVE-13** — frame edge-resize: resize the framed window from the left,
      right, and bottom edges + both bottom corners (not just BR). Edge handles
      live in the solid 6px border; both bottom corners are kept solid in the
      hollow region. Verified frame renders intact around Notepad.
- [x] **NATIVE-14** — 90-day high/low range line in the price card (computed from
      the history series, shown under the sparkline). Verified (abyssal whip
      76.5k – 88.7k gp).
- [x] **NATIVE-15** — copy-price button: a copy icon next to the price copies the
      raw value to the clipboard (TopLevel.Clipboard) with a brief "Copied!"
      feedback. Verified end-to-end (clipboard = 85139).
- [x] **NATIVE-16** — the main window remembers its last position + size (saved to
      `%APPDATA%/CHILI/window.json` on close, restored on launch; only when not
      maximized). Verified end-to-end (reopens at the saved bounds).
- [x] **NATIVE-17** — refresh button on the price card: a ↻ button beside the price
      re-fetches the live GE price + trend for the current item (RefreshCommand).
      Verified rendering beside the copy button.
- [x] **NATIVE-18** — price freshness line: shows "GE update · <date>" (from the
      WeirdGloop timestamp) under the volume. Verified (abyssal whip → Jun 6, 2026).
- [x] **NATIVE-19** — the title bar shows the active app name (e.g. "CHILI native ·
      Games"), bound to Current.Name and updating on app switch. Verified
      (Ctrl+4 → Games).
- [x] **NATIVE-20** — "Clear" button for recent searches (ClearRecentCommand →
      Recent.Clear; the chips row hides when empty). Verified end-to-end (chips
      appear, click Clear, chips gone).
- [x] **NATIVE-21** — welcome/empty-state polish: the idle price card shows
      clickable example-item chips (Abyssal whip, Dragon claws, Twisted bow, …)
      that run a search. Verified rendering.
- [x] **NATIVE-22** — "pop out" button in the Prices app: opens the price card as a
      standalone floating, always-on-top overlay window (reuses PriceOverlayWindow)
      near the main window — a price widget anywhere, not just from the game frame.
      Verified (floating overlay appears).
- [x] **NATIVE-23** — **Chat app** (first backend-connected port): `AppSettings`
      (base URL + device token in %APPDATA%), `ChiliApiClient` (Bearer auth, SSE),
      and a streaming chat UI (bubbles + composer) hitting `/api/mobile/chat/stream`.
      **Verified live** against the running backend — authenticated as the paired
      user, streamed a real reply.
- [x] **NATIVE-24** — **Trading cockpit** (backend): live total equity / buying
      power / cash, per-broker breakdown, kill-switch + risk-heat strip, and the
      open-positions list with color-coded P/L, from `/api/trading/broker/portfolio`,
      `/brain/governance`, `/risk/budget`, `/broker/positions` (+ Refresh).
      **Verified live** ($13,157.77 equity, 35 positions).
- [x] **NATIVE-25** — **Research** (backend): shows the stored research digest
      (`/research/report?format=json`) and runs on-demand research
      (`POST /research/run` {topic}) — web + LLM — rendering the summary + sources.
      **Verified live** ("latest RuneScape 3 game updates" → full summary + 4 sources).
      **All three backend apps (Chat · Trading · Research) are now live.**
- [x] **NATIVE-26** — Settings → **editable backend connection**: base URL +
      device token fields (token masked), Save, and a "Test connection" button
      (`ChiliApiClient.TestConnectionAsync` — health + authed probe). Makes the
      client configurable on any machine. Verified ("✓ Connected — token accepted").

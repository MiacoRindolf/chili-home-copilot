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
      (Port Chat·Trading·Research with the backend: next.)

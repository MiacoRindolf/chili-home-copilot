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

# CHILI Desktop — native UI stack recommendation

_Decision draft — drafted 2026-06-06 ~02:40. Review when rested; nothing is
migrated yet._

## Why this came up

CHILI's desktop client is a Flutter "OS workspace." Flutter is **excellent for
in-app UI** (~95% of CHILI: windowing, dock, cockpit, chat, research, agents,
games library, the rich price card). The pain is a narrow but real niche:
**OS-boundary features** — overlaying / framing / compositing *other* apps'
windows (the in-game RuneScape price overlay, multi-window). Flutter desktop is
single-window + GPU-rendered by design and fights this; it's hard for *any* app
framework, but especially Flutter.

Goal: **powerful** (native window control) **and** **beautiful** (rich,
animated UI).

## Recommendation

**Foundation: C# (.NET 8).** Native Win32 via P/Invoke is trivial — overlays,
multi-window, transparency, click-through, DWM/compositing, owned windows. These
are exactly the things we fought in Flutter.

**UI: Avalonia UI** (primary). Skia-rendered like Flutter, so fully custom +
animated + a distinct OS aesthetic; mature; easy theming; still .NET so native
interop is one P/Invoke away. Closest to the Flutter strengths we like, with
native power.

Alternatives:
- **WinUI 3 (Windows App SDK)** — most "native Fluent" Windows look,
  Microsoft-backed; some maturity rough edges; Windows-only.
- **C++ / Qt + QML** — gorgeous animated declarative UI, max control; heavier
  dev, C++.

| Option | Overlay / multi-window | UI richness | Cost |
|---|---|---|---|
| Flutter (today) | weak (single-window) | excellent | native plugin per niche feature |
| **C# + Avalonia** | strong (P/Invoke) | excellent (Skia, animated) | full rewrite |
| C# + WinUI 3 | strong | high (Fluent) | full rewrite |
| C++ / Qt + QML | strongest | high | full rewrite, slower dev |

## Plan — spike first, then incremental (NOT big-bang)

1. **Spike (1–2 days):** a small C# + Avalonia app that does the *hardest* thing
   — a beautiful, animated overlay that frames a game window + shows the RS
   price card (search → image + GE price + wiki blurb). Prove "powerful +
   beautiful" before committing.
2. **If the spike shines → incremental migration:** build the CHILI shell
   (windowing + dock) in Avalonia, then port apps one at a time. Keep the
   Flutter app running until the new stack reaches parity.
3. **Reuse:** the price/wiki engine logic and the API knowledge (WeirdGloop GE
   prices, RuneScape Wiki opensearch + extracts/thumbnails) port directly to C#.

## Honest cost

Full migration is weeks→months. Do the spike first; do **not** abandon the
working Flutter app until the new stack has proven it delivers both goals.

## Status of the current Flutter overlay work (so nothing is lost)

- Native game **frame** (Win32 in the Flutter runner): drag / 4-corner resize /
  hollow picture-frame / owned-by-game grouping — works well.
- RS price **engine** (Dart): WeirdGloop GE price + RS Wiki search/info — works,
  tested. Logic + endpoints port to C# easily.
- **In-CHILI** rich Flutter price card — works, animated, with image.
- **Native GDI** on-game quick-price overlay — works, on the game.
- **Multi-window** Flutter overlay — renders, but can't be cleanly
  positioned/frameless on Windows (the limitation that motivated this doc).

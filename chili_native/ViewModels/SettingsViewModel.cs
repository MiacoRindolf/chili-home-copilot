using System;
using System.Collections.Generic;

namespace Chili.ViewModels;

/// <summary>The Settings / About app — shows what this native client is and
/// what it can do. Self-contained (no backend).</summary>
public class SettingsViewModel : ViewModelBase
{
    public string Version => "CHILI Native · v0.11";
    public string Stack => $".NET {Environment.Version}  ·  Avalonia 11.2  ·  Skia";

    public string Tagline =>
        "A native Windows client — powerful (Win32) and beautiful (Skia). " +
        "Separate from the Flutter app, which stays untouched.";

    public IReadOnlyList<string> Capabilities { get; } = new List<string>
    {
        "OS shell — custom chrome, dock, app switching",
        "RuneScape prices — GE price, image, wiki blurb, 90-day trend, recent searches",
        "Window awareness — read-only Win32 enumeration",
        "Game frame — move + resize a picked window (hollow, safe)",
        "On-game price overlay — floats over the framed game",
        "Window grouping — frame owned by the game",
    };

    public string Safety =>
        "Reads window geometry and moves/resizes via SetWindowPos only " +
        "(FancyZones-class). Never reparents, injects, or reads another " +
        "process's memory.";
}

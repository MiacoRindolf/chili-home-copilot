using System;
using System.IO;
using System.Text.Json;

namespace Chili.Services;

/// <summary>The persisted bounds of the main window (physical X/Y, logical W/H).</summary>
public sealed record WindowBounds(int X, int Y, double W, double H);

/// <summary>Loads/saves the main window's last position + size to a small JSON in
/// %APPDATA%/CHILI, so the shell reopens where you left it.</summary>
public static class WindowStateStore
{
    private static string Dir =>
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "CHILI");

    private static string FilePath => Path.Combine(Dir, "window.json");

    public static WindowBounds? Load()
    {
        try
        {
            if (File.Exists(FilePath))
                return JsonSerializer.Deserialize<WindowBounds>(File.ReadAllText(FilePath));
        }
        catch { /* corrupt/missing — fall back to defaults */ }
        return null;
    }

    public static void Save(WindowBounds b)
    {
        try
        {
            Directory.CreateDirectory(Dir);
            File.WriteAllText(FilePath, JsonSerializer.Serialize(b));
        }
        catch { /* best-effort */ }
    }
}

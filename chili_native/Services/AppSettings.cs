using System;
using System.IO;
using System.Text.Json;

namespace Chili.Services;

/// <summary>Backend connection settings (base URL + device token), persisted to
/// %APPDATA%/CHILI/settings.json. The token authenticates the native client as a
/// paired device (Authorization: Bearer …) — same as the Flutter app.</summary>
public sealed class AppSettings
{
    public string BaseUrl { get; set; } = "https://localhost:8000";
    public string DeviceToken { get; set; } = "";

    private static string Dir =>
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "CHILI");

    private static string FilePath => Path.Combine(Dir, "settings.json");

    public static AppSettings Load()
    {
        try
        {
            if (File.Exists(FilePath))
                return JsonSerializer.Deserialize<AppSettings>(File.ReadAllText(FilePath)) ?? new AppSettings();
        }
        catch { /* corrupt/missing — defaults */ }
        return new AppSettings();
    }

    public void Save()
    {
        try
        {
            Directory.CreateDirectory(Dir);
            File.WriteAllText(FilePath,
                JsonSerializer.Serialize(this, new JsonSerializerOptions { WriteIndented = true }));
        }
        catch { /* best-effort */ }
    }
}

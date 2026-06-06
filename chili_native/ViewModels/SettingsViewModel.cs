using System;
using System.Collections.Generic;
using System.Threading.Tasks;
using Chili.Services;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace Chili.ViewModels;

/// <summary>The Settings / About app — backend connection (editable) plus what
/// this native client is and what it can do.</summary>
public partial class SettingsViewModel : ViewModelBase
{
    private readonly AppSettings _settings;

    public SettingsViewModel()
    {
        _settings = AppSettings.Load();
        _baseUrl = _settings.BaseUrl;
        _deviceToken = _settings.DeviceToken;
    }

    // ---- editable backend connection ----
    [ObservableProperty] private string _baseUrl;
    [ObservableProperty] private string _deviceToken;
    [ObservableProperty] private string _connectionStatus = "";

    [RelayCommand]
    private void Save()
    {
        _settings.BaseUrl = (BaseUrl ?? "").Trim();
        _settings.DeviceToken = (DeviceToken ?? "").Trim();
        _settings.Save();
        ConnectionStatus = "Saved. Restart CHILI to apply to all apps.";
    }

    [RelayCommand]
    private async Task Test()
    {
        ConnectionStatus = "Testing…";
        var client = new ChiliApiClient(new AppSettings
        {
            BaseUrl = (BaseUrl ?? "").Trim(),
            DeviceToken = (DeviceToken ?? "").Trim(),
        });
        ConnectionStatus = await client.TestConnectionAsync();
    }

    // ---- about ----
    public string Version => "CHILI Native · v0.26";
    public string Stack => $".NET {Environment.Version}  ·  Avalonia 11.2  ·  Skia";

    public string Tagline =>
        "A native Windows client — powerful (Win32) and beautiful (Skia). " +
        "Separate from the Flutter app, which stays untouched.";

    public IReadOnlyList<string> Capabilities { get; } = new List<string>
    {
        "OS shell — custom chrome, dock, app switching, keyboard shortcuts",
        "RuneScape prices — GE price, image, wiki, 90-day trend, recents, copy",
        "Chat — live streaming assistant (backend)",
        "Trading — live P/L cockpit, positions, governance (backend)",
        "Research — on-demand web + LLM research (backend)",
        "Games — window awareness + the CHILI game frame (move/resize/overlay)",
    };

    public string Safety =>
        "Reads window geometry and moves/resizes via SetWindowPos only " +
        "(FancyZones-class). Never reparents, injects, or reads another " +
        "process's memory.";
}

using System;
using System.Collections.ObjectModel;
using Avalonia.Media;
using Avalonia.Threading;
using Chili.Services;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace Chili.ViewModels;

public partial class MainWindowViewModel : ViewModelBase
{
    private readonly ChiliApiClient _api = new(AppSettings.Load());

    public ObservableCollection<DockApp> Apps { get; }

    [ObservableProperty] private DockApp _current;

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(OnlineBrush), nameof(OnlineText))]
    private bool _online;

    public IBrush OnlineBrush => new SolidColorBrush(Color.Parse(Online ? "#35D08A" : "#FF6B5B"));
    public string OnlineText => Online ? "online" : "offline";

    public MainWindowViewModel()
    {
        Apps = new ObservableCollection<DockApp>
        {
            new("Home", "⌂", new HomeViewModel()),
            new("Prices", "◈", new RsPriceCardViewModel()),
            new("Chat", "✉", new ChatViewModel()),
            new("Trading", "$", new TradingViewModel()),
            new("Games", "◆", new GamesViewModel()),
            new("Research", "⎰", new ResearchViewModel()),
            new("Brain", "✦", new BrainViewModel()),
            new("Settings", "⚙", new SettingsViewModel()),
        };
        _current = Apps[0];
        _current.IsSelected = true;

        // Periodic backend health → connection dot in the title bar.
        _ = CheckHealthAsync();
        var timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(20) };
        timer.Tick += async (_, _) => await CheckHealthAsync();
        timer.Start();
    }

    private async System.Threading.Tasks.Task CheckHealthAsync() => Online = await _api.HealthAsync();

    [RelayCommand]
    private void Select(DockApp? app)
    {
        if (app is null) return;
        foreach (var a in Apps) a.IsSelected = a == app;
        Current = app;
    }

    /// <summary>Switch to the app at <paramref name="index"/> (Ctrl+1..N shortcuts).</summary>
    public void SelectByIndex(int index)
    {
        if (index >= 0 && index < Apps.Count) Select(Apps[index]);
    }
}

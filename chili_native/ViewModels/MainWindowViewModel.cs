using System.Collections.ObjectModel;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace Chili.ViewModels;

public partial class MainWindowViewModel : ViewModelBase
{
    public ObservableCollection<DockApp> Apps { get; }

    [ObservableProperty] private DockApp _current;

    public MainWindowViewModel()
    {
        Apps = new ObservableCollection<DockApp>
        {
            new("Prices", "◈", new RsPriceCardViewModel()),
            new("Chat", "✉", new ChatViewModel()),
            new("Trading", "$", new TradingViewModel()),
            new("Games", "◆", new GamesViewModel()),
            new("Research", "⎰", new ResearchViewModel()),
            new("Settings", "⚙", new SettingsViewModel()),
        };
        _current = Apps[0];
        _current.IsSelected = true;
    }

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

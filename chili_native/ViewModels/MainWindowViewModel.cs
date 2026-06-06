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
            new("Chat", "✉", new PlaceholderViewModel(
                "Chat", "Talk to CHILI — local-first assistant. Coming soon.", "✉")),
            new("Trading", "$", new PlaceholderViewModel(
                "Trading", "Live P/L cockpit and the autonomous trading brain. Coming soon.", "$")),
            new("Games", "◆", new GamesViewModel()),
            new("Research", "⎰", new PlaceholderViewModel(
                "Research", "Multi-source research with visual reports. Coming soon.", "⎰")),
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
}

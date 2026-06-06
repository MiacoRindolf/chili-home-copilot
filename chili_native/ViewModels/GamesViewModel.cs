using System.Collections.ObjectModel;
using Chili.Interop;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace Chili.ViewModels;

/// <summary>
/// Games / window-awareness app — lists the real top-level windows on the desktop
/// (read-only Win32 enumeration). This is the picker the CHILI frame will drive
/// later; you choose the window (e.g. the actual game, never the anti-cheat
/// launcher) rather than CHILI auto-grabbing one.
/// </summary>
public partial class GamesViewModel : ViewModelBase
{
    public ObservableCollection<DesktopWindow> Windows { get; } = new();

    [ObservableProperty] private string _status = "";

    public GamesViewModel() => Refresh();

    [RelayCommand]
    private void Refresh()
    {
        Windows.Clear();
        foreach (var w in NativeWindows.ListTopLevelWindows())
            Windows.Add(w);
        Status = Windows.Count == 1 ? "1 open window" : $"{Windows.Count} open windows";
    }
}

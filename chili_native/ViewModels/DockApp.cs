using CommunityToolkit.Mvvm.ComponentModel;

namespace Chili.ViewModels;

/// <summary>One launchable app shown in the dock; <see cref="Content"/> is the
/// app's view model, resolved to a view by the workspace's data templates.</summary>
public partial class DockApp : ViewModelBase
{
    public string Name { get; }
    public string Glyph { get; }
    public object Content { get; }

    [ObservableProperty] private bool _isSelected;

    public DockApp(string name, string glyph, object content)
    {
        Name = name;
        Glyph = glyph;
        Content = content;
    }
}

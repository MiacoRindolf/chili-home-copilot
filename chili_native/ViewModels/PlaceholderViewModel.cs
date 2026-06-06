namespace Chili.ViewModels;

/// <summary>A simple "coming soon" placeholder for apps not yet ported.</summary>
public class PlaceholderViewModel : ViewModelBase
{
    public string Title { get; }
    public string Subtitle { get; }
    public string Glyph { get; }

    public PlaceholderViewModel(string title, string subtitle, string glyph)
    {
        Title = title;
        Subtitle = subtitle;
        Glyph = glyph;
    }
}

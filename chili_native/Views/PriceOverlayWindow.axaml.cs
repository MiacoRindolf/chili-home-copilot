using Avalonia;
using Avalonia.Controls;
using Avalonia.Input;
using Chili.ViewModels;

namespace Chili.Views;

/// <summary>
/// A small frameless, always-on-top price card that floats over the framed game —
/// the "real-time gaming awareness" overlay. Search an item, get its GE price +
/// wiki info without leaving the game.
///
/// This is the piece the Flutter multi-window build couldn't position/frameless
/// cleanly on Windows; as a native Avalonia window it just works.
/// </summary>
public partial class PriceOverlayWindow : Window
{
    public PriceOverlayWindow()
    {
        InitializeComponent();
        var handle = this.FindControl<Border>("Handle");
        if (handle != null)
            handle.PointerPressed += (_, e) =>
            {
                if (e.GetCurrentPoint(this).Properties.IsLeftButtonPressed)
                    BeginMoveDrag(e);
            };
        var close = this.FindControl<Button>("CloseBtn");
        if (close != null) close.Click += (_, _) => Close();
    }

    /// <summary>Open the price overlay anchored at the given screen point.</summary>
    public static void Open(PixelPoint at)
    {
        var win = new PriceOverlayWindow
        {
            DataContext = new RsPriceCardViewModel(),
            Position = at,
        };
        win.Show();
    }
}

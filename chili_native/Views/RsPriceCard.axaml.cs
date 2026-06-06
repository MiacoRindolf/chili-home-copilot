using Avalonia.Controls;
using Avalonia.VisualTree;

namespace Chili.Views;

public partial class RsPriceCard : UserControl
{
    public RsPriceCard()
    {
        InitializeComponent();
        AttachedToVisualTree += (_, _) =>
        {
            // Autofocus the search field once the card is on screen.
            this.FindControl<TextBox>("SearchBox")?.Focus();
        };
    }
}

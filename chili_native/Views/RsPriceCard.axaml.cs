using System;
using Avalonia.Controls;
using Avalonia.Threading;
using Chili.ViewModels;

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

        var copy = this.FindControl<Button>("CopyBtn");
        if (copy != null) copy.Click += OnCopy;
    }

    private async void OnCopy(object? sender, Avalonia.Interactivity.RoutedEventArgs e)
    {
        if (DataContext is not RsPriceCardViewModel vm) return;
        var top = TopLevel.GetTopLevel(this);
        if (top?.Clipboard != null)
            await top.Clipboard.SetTextAsync(vm.PriceValue.ToString());

        // brief "Copied!" feedback on the button
        if (sender is Button b)
        {
            var original = b.Content;
            b.Content = "Copied!";
            DispatcherTimer.RunOnce(() => b.Content = original, TimeSpan.FromMilliseconds(1100));
        }
    }
}

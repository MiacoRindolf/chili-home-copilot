using System.Linq;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.VisualTree;
using Chili.Services;
using Chili.ViewModels;

namespace Chili.Views;

public partial class MainWindow : Window
{
    public MainWindow()
    {
        InitializeComponent();

        RestoreBounds();
        Closing += (_, _) =>
        {
            if (WindowState == WindowState.Normal)
                WindowStateStore.Save(new WindowBounds(Position.X, Position.Y, Width, Height));
        };

        var titleBar = this.FindControl<Grid>("TitleBar");
        if (titleBar != null)
        {
            titleBar.PointerPressed += OnTitleBarPressed;
            titleBar.DoubleTapped += (_, _) => ToggleMaximize();
        }

        this.FindControl<Button>("MinBtn")!.Click += (_, _) => WindowState = WindowState.Minimized;
        this.FindControl<Button>("MaxBtn")!.Click += (_, _) => ToggleMaximize();
        this.FindControl<Button>("CloseBtn")!.Click += (_, _) => Close();

        KeyDown += OnKeyDown;
    }

    private void RestoreBounds()
    {
        var b = WindowStateStore.Load();
        if (b == null) return;
        if (b.W >= 600 && b.H >= 400) { Width = b.W; Height = b.H; }
        WindowStartupLocation = WindowStartupLocation.Manual;
        Opened += (_, _) => Position = new PixelPoint(b.X, b.Y);
    }

    private void ToggleMaximize() =>
        WindowState = WindowState == WindowState.Maximized
            ? WindowState.Normal
            : WindowState.Maximized;

    // Ctrl+1..6 switch dock apps; Ctrl+W closes.
    private void OnKeyDown(object? sender, KeyEventArgs e)
    {
        if (!e.KeyModifiers.HasFlag(KeyModifiers.Control)) return;
        if (e.Key is >= Key.D1 and <= Key.D9)
        {
            (DataContext as MainWindowViewModel)?.SelectByIndex(e.Key - Key.D1);
            e.Handled = true;
        }
        else if (e.Key == Key.W)
        {
            Close();
            e.Handled = true;
        }
    }

    private void OnTitleBarPressed(object? sender, PointerPressedEventArgs e)
    {
        if (!e.GetCurrentPoint(this).Properties.IsLeftButtonPressed) return;
        // Don't start a window drag when the press lands on a window control.
        if (e.Source is Visual v && v.GetSelfAndVisualAncestors().OfType<Button>().Any()) return;
        BeginMoveDrag(e);
    }
}

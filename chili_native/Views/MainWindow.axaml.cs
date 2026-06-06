using System.Linq;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.VisualTree;

namespace Chili.Views;

public partial class MainWindow : Window
{
    public MainWindow()
    {
        InitializeComponent();

        var titleBar = this.FindControl<Grid>("TitleBar");
        if (titleBar != null)
            titleBar.PointerPressed += OnTitleBarPressed;

        this.FindControl<Button>("MinBtn")!.Click += (_, _) => WindowState = WindowState.Minimized;
        this.FindControl<Button>("MaxBtn")!.Click += (_, _) =>
            WindowState = WindowState == WindowState.Maximized
                ? WindowState.Normal
                : WindowState.Maximized;
        this.FindControl<Button>("CloseBtn")!.Click += (_, _) => Close();
    }

    private void OnTitleBarPressed(object? sender, PointerPressedEventArgs e)
    {
        if (!e.GetCurrentPoint(this).Properties.IsLeftButtonPressed) return;
        // Don't start a window drag when the press lands on a window control.
        if (e.Source is Visual v && v.GetSelfAndVisualAncestors().OfType<Button>().Any()) return;
        BeginMoveDrag(e);
    }
}

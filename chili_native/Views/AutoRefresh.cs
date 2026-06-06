using System;
using System.Windows.Input;
using Avalonia.Controls;
using Avalonia.Threading;

namespace Chili.Views;

/// <summary>Drives a view's refresh command on an interval, but only while the
/// view is attached (visible) — so backgrounded cockpits don't keep polling.</summary>
public static class AutoRefresh
{
    public static void Attach(Control view, Func<ICommand?> command, int seconds = 15)
    {
        var timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(seconds) };
        timer.Tick += (_, _) =>
        {
            var cmd = command();
            if (cmd != null && cmd.CanExecute(null)) cmd.Execute(null);
        };
        view.AttachedToVisualTree += (_, _) => timer.Start();
        view.DetachedFromVisualTree += (_, _) => timer.Stop();
    }
}

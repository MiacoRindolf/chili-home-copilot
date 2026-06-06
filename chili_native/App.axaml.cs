using Avalonia;
using Avalonia.Controls.ApplicationLifetimes;
using Avalonia.Markup.Xaml;
using Avalonia.Threading;
using Chili.Interop;
using Chili.ViewModels;
using Chili.Views;

namespace Chili;

public partial class App : Application
{
    public override void Initialize()
    {
        AvaloniaXamlLoader.Load(this);
    }

    public override void OnFrameworkInitializationCompleted()
    {
        if (ApplicationLifetime is IClassicDesktopStyleApplicationLifetime desktop)
        {
            desktop.MainWindow = new MainWindow
            {
                DataContext = new MainWindowViewModel(),
            };

            // Dev/CLI hook: `--frame <title-substring>` wraps a matching window in
            // a CHILI frame on startup (used for verification; also a real feature).
            var args = desktop.Args;
            if (args != null)
            {
                for (int i = 0; i < args.Length - 1; i++)
                {
                    if (args[i] != "--frame") continue;
                    var match = args[i + 1];
                    Dispatcher.UIThread.Post(() =>
                    {
                        var target = NativeWindows.FindByTitle(match);
                        if (target != null) GameFrameWindow.Attach(target);
                    }, DispatcherPriority.Background);
                    break;
                }
            }
        }

        base.OnFrameworkInitializationCompleted();
    }
}
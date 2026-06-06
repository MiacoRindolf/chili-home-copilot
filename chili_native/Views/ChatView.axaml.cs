using System.Collections.Specialized;
using Avalonia.Controls;
using Avalonia.Threading;
using Chili.ViewModels;

namespace Chili.Views;

public partial class ChatView : UserControl
{
    private ChatViewModel? _vm;

    public ChatView()
    {
        InitializeComponent();
        DataContextChanged += (_, _) => Hook();
        AttachedToVisualTree += (_, _) => this.FindControl<TextBox>("Composer")?.Focus();
    }

    private void Hook()
    {
        if (_vm != null) _vm.Messages.CollectionChanged -= OnMessages;
        _vm = DataContext as ChatViewModel;
        if (_vm != null) _vm.Messages.CollectionChanged += OnMessages;
    }

    private void OnMessages(object? sender, NotifyCollectionChangedEventArgs e)
    {
        if (e.NewItems != null)
            foreach (var it in e.NewItems)
                if (it is ChatMessage m)
                    m.PropertyChanged += (_, _) => ScrollEnd(); // follow streamed tokens
        ScrollEnd();
    }

    private void ScrollEnd() =>
        Dispatcher.UIThread.Post(() => this.FindControl<ScrollViewer>("Scroller")?.ScrollToEnd());
}

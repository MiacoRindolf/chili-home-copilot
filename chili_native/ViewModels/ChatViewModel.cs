using System;
using System.Collections.ObjectModel;
using System.Threading.Tasks;
using Avalonia.Threading;
using Chili.Services;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace Chili.ViewModels;

/// <summary>One chat bubble (user or assistant). Text is observable so streamed
/// tokens append live.</summary>
public partial class ChatMessage : ViewModelBase
{
    public bool IsUser { get; init; }
    [ObservableProperty] private string _text = "";
}

/// <summary>The Chat app — talks to the CHILI backend (/api/mobile/chat/stream)
/// with live token streaming, threaded by conversation id.</summary>
public partial class ChatViewModel : ViewModelBase
{
    private readonly ChiliApiClient _api;
    private int? _conversationId;

    public ObservableCollection<ChatMessage> Messages { get; } = new();

    [ObservableProperty] private string _input = "";

    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(SendCommand))]
    private bool _busy;

    public ChatViewModel(ChiliApiClient? api = null)
    {
        _api = api ?? new ChiliApiClient(AppSettings.Load());
        Messages.Add(new ChatMessage
        {
            IsUser = false,
            Text = "Hi! I'm CHILI — your local-first assistant. Ask me anything.",
        });
    }

    private bool CanSend => !Busy;

    [RelayCommand(CanExecute = nameof(CanSend))]
    private async Task Send()
    {
        var text = (Input ?? "").Trim();
        if (text.Length == 0 || Busy) return;

        Input = "";
        Busy = true;
        Messages.Add(new ChatMessage { IsUser = true, Text = text });
        var reply = new ChatMessage { IsUser = false, Text = "" };
        Messages.Add(reply);

        try
        {
            _conversationId = await _api.StreamChatAsync(text, _conversationId,
                tok => Dispatcher.UIThread.Post(() => reply.Text += tok));
            if (string.IsNullOrEmpty(reply.Text))
                reply.Text = "(no reply)";
        }
        catch (Exception ex)
        {
            reply.Text = "⚠  " + ex.Message;
        }
        finally
        {
            Busy = false;
        }
    }
}

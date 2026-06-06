using System;
using System.Collections.ObjectModel;
using System.Text.Json;
using System.Threading.Tasks;
using Chili.Services;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace Chili.ViewModels;

/// <summary>A research source (title + url).</summary>
public sealed class SourceRow : ViewModelBase
{
    public string Title { get; init; } = "";
    public string Url { get; init; } = "";
}

/// <summary>A stored digest topic (name + summary).</summary>
public sealed class TopicRow : ViewModelBase
{
    public string Name { get; init; } = "";
    public string Summary { get; init; } = "";
}

/// <summary>The Research app — shows the stored research digest and runs
/// on-demand research (web + LLM) via the CHILI backend.</summary>
public partial class ResearchViewModel : ViewModelBase
{
    private readonly ChiliApiClient _api;

    public ObservableCollection<TopicRow> Topics { get; } = new();
    public ObservableCollection<SourceRow> Sources { get; } = new();

    [ObservableProperty] private string _topicInput = "";

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(HasResult))]
    private string _resultTitle = "";

    [ObservableProperty] private string _resultSummary = "";

    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(RunCommand))]
    private bool _busy;

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(HasTopics))]
    private string _status = "";

    public bool HasResult => !string.IsNullOrEmpty(ResultTitle);
    public bool HasTopics => Topics.Count > 0;

    public ResearchViewModel(ChiliApiClient? api = null)
    {
        _api = api ?? new ChiliApiClient(AppSettings.Load());
        Topics.CollectionChanged += (_, _) => OnPropertyChanged(nameof(HasTopics));
        _ = LoadDigestAsync();
    }

    private async Task LoadDigestAsync()
    {
        var digest = await _api.GetJsonAsync("/api/brain/reasoning/research/report?format=json");
        Topics.Clear();
        if (digest is { } d && d.TryGetProperty("topics", out var topics) &&
            topics.ValueKind == JsonValueKind.Array)
        {
            foreach (var t in topics.EnumerateArray())
                Topics.Add(new TopicRow { Name = Str(t, "name"), Summary = Str(t, "summary") });
        }
    }

    private bool CanRun => !Busy;

    [RelayCommand(CanExecute = nameof(CanRun))]
    private async Task Run()
    {
        var topic = (TopicInput ?? "").Trim();
        if (topic.Length == 0 || Busy) return;

        Busy = true;
        Status = $"Researching “{topic}” — this can take a moment…";
        ResultTitle = "";
        ResultSummary = "";
        Sources.Clear();

        try
        {
            var res = await _api.PostJsonAsync("/api/brain/reasoning/research/run", new { topic });
            if (res is { } r && r.TryGetProperty("ok", out var ok) && ok.ValueKind == JsonValueKind.True)
            {
                ResultSummary = Str(r, "summary");
                ResultTitle = Str(r, "topic") is { Length: > 0 } tp ? tp : topic;
                if (r.TryGetProperty("sources", out var src) && src.ValueKind == JsonValueKind.Array)
                    foreach (var s in src.EnumerateArray())
                        Sources.Add(new SourceRow
                        {
                            Title = Str(s, "title") is { Length: > 0 } ti ? ti : Str(s, "url"),
                            Url = Str(s, "url"),
                        });
                Status = $"Done · {Sources.Count} sources";
                await LoadDigestAsync();
            }
            else
            {
                Status = "Research failed (paired user only, or backend busy).";
            }
        }
        catch (Exception ex)
        {
            Status = "⚠  " + ex.Message;
        }
        finally
        {
            Busy = false;
        }
    }

    private static string Str(JsonElement e, string key) =>
        e.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.String
            ? v.GetString() ?? "" : "";
}

using System;
using System.Collections.ObjectModel;
using System.Globalization;
using System.Text.Json;
using System.Threading.Tasks;
using Chili.Services;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace Chili.ViewModels;

/// <summary>One household/system activity event.</summary>
public sealed class ActivityRow : ViewModelBase
{
    public string Glyph { get; init; } = "•";
    public string Description { get; init; } = "";
    public string Meta { get; init; } = "";
}

/// <summary>The Home app — the household/system activity feed (recent events)
/// from the CHILI backend.</summary>
public partial class HomeViewModel : ViewModelBase
{
    private readonly ChiliApiClient _api;

    public ObservableCollection<ActivityRow> Events { get; } = new();

    [ObservableProperty] private string _status = "";

    public HomeViewModel(ChiliApiClient? api = null)
    {
        _api = api ?? new ChiliApiClient(AppSettings.Load());
        _ = RefreshAsync();
    }

    [RelayCommand]
    private Task Refresh() => RefreshAsync();

    private async Task RefreshAsync()
    {
        Status = "Loading…";
        var data = await _api.GetJsonAsync("/api/activity?limit=25");
        Events.Clear();
        if (data is { } d && d.TryGetProperty("events", out var arr) &&
            arr.ValueKind == JsonValueKind.Array)
        {
            foreach (var e in arr.EnumerateArray())
            {
                var who = Str(e, "user_name");
                var when = Ago(Str(e, "created_at"));
                Events.Add(new ActivityRow
                {
                    Glyph = GlyphFor(Str(e, "icon")),
                    Description = Str(e, "description"),
                    Meta = string.IsNullOrEmpty(who) ? when : $"{who}  ·  {when}",
                });
            }
        }
        Status = Events.Count == 0 ? "No recent activity" : $"{Events.Count} recent events · updated";
    }

    private static string GlyphFor(string icon) => icon switch
    {
        "project" => "◰",
        "task" => "✓",
        "chore" => "✦",
        "birthday" => "🎂",
        _ => "•",
    };

    private static string Ago(string isoUtc)
    {
        if (!DateTime.TryParse(isoUtc, CultureInfo.InvariantCulture,
                DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal, out var t))
            return "";
        var span = DateTime.UtcNow - t;
        if (span.TotalMinutes < 1) return "just now";
        if (span.TotalMinutes < 60) return $"{(int)span.TotalMinutes}m ago";
        if (span.TotalHours < 24) return $"{(int)span.TotalHours}h ago";
        if (span.TotalDays < 30) return $"{(int)span.TotalDays}d ago";
        return t.ToLocalTime().ToString("MMM d, yyyy", CultureInfo.InvariantCulture);
    }

    private static string Str(JsonElement e, string key) =>
        e.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() ?? "" : "";
}

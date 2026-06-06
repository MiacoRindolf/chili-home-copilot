using System;
using System.Collections.ObjectModel;
using System.Globalization;
using System.Text.Json;
using System.Threading.Tasks;
using Avalonia.Media;
using Chili.Services;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace Chili.ViewModels;

/// <summary>An LLM-spend row (per provider, last 24h).</summary>
public sealed class SpendRow : ViewModelBase
{
    public string Provider { get; init; } = "";
    public string Detail { get; init; } = "";
}

/// <summary>A recent dispatch (autonomous-coding) run.</summary>
public sealed class RunRow : ViewModelBase
{
    public string Title { get; init; } = "";
    public string Decision { get; init; } = "";
    public bool Ok { get; init; }
    public IBrush DecisionBrush =>
        new SolidColorBrush(Color.Parse(Ok ? "#35D08A" : "#FF6B5B"));
}

/// <summary>The Brain app — the autonomous-coding/dispatch cockpit: LLM spend
/// (24h, per provider), context-brain mode, and recent dispatch runs.</summary>
public partial class BrainViewModel : ViewModelBase
{
    private readonly ChiliApiClient _api;

    public ObservableCollection<SpendRow> Spend { get; } = new();
    public ObservableCollection<RunRow> Runs { get; } = new();

    [ObservableProperty] private string _spendTotal = "—";
    [ObservableProperty] private string _callsTotal = "—";
    [ObservableProperty] private string _tokensTotal = "—";
    [ObservableProperty] private string _mode = "—";
    [ObservableProperty] private bool _learningOn;

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(KillBrush))]
    private bool _killActive;

    [ObservableProperty] private string _killText = "—";
    [ObservableProperty] private string _status = "";

    public IBrush KillBrush =>
        new SolidColorBrush(Color.Parse(KillActive ? "#FF6B5B" : "#35D08A"));

    public BrainViewModel(ChiliApiClient? api = null)
    {
        _api = api ?? new ChiliApiClient(AppSettings.Load());
        _ = RefreshAsync();
    }

    [RelayCommand]
    private Task Refresh() => RefreshAsync();

    private async Task RefreshAsync()
    {
        Status = "Loading…";

        var d = await _api.GetJsonAsync("/api/brain/dispatch/status");
        if (d is { } ds)
        {
            KillActive = ds.TryGetProperty("kill_switch", out var ks) &&
                         ks.TryGetProperty("active", out var a) && a.ValueKind is JsonValueKind.True;
            KillText = KillActive ? "Dispatch killed" : "Dispatch · live";

            double total = 0; long calls = 0, tokens = 0;
            Spend.Clear();
            if (ds.TryGetProperty("spend_24h", out var sp) && sp.ValueKind == JsonValueKind.Array)
            {
                foreach (var p in sp.EnumerateArray())
                {
                    var usd = Num(p, "spend_usd") ?? 0;
                    var c = (long)(Num(p, "calls") ?? 0);
                    var tk = (long)(Num(p, "tokens") ?? 0);
                    total += usd; calls += c; tokens += tk;
                    Spend.Add(new SpendRow
                    {
                        Provider = Str(p, "provider"),
                        Detail = $"{c:N0} calls · {Tokens(tk)} tok · ${usd:0.00}",
                    });
                }
            }
            SpendTotal = "$" + total.ToString("0.00", CultureInfo.InvariantCulture);
            CallsTotal = calls.ToString("N0", CultureInfo.InvariantCulture);
            TokensTotal = Tokens(tokens);

            Runs.Clear();
            if (ds.TryGetProperty("recent_runs", out var rr) && rr.ValueKind == JsonValueKind.Array)
            {
                foreach (var r in rr.EnumerateArray())
                {
                    var dec = Str(r, "decision");
                    var step = Str(r, "cycle_step");
                    var id = (long)(Num(r, "id") ?? 0);
                    Runs.Add(new RunRow
                    {
                        Title = $"#{id} · {step}",
                        Decision = dec,
                        Ok = !(dec.Contains("fail") || dec.Contains("error") || dec.Contains("reject")),
                    });
                }
            }
        }

        var c2 = await _api.GetJsonAsync("/api/brain/context/status");
        if (c2 is { } cs && cs.TryGetProperty("runtime_state", out var rs))
        {
            Mode = Str(rs, "mode");
            LearningOn = rs.TryGetProperty("learning_enabled", out var le) && le.ValueKind is JsonValueKind.True;
        }

        Status = $"{Runs.Count} recent runs · auto-refresh · {DateTime.Now:HH:mm:ss}";
    }

    private static string Tokens(long n) =>
        n >= 1000 ? (n / 1000.0).ToString("0.#", CultureInfo.InvariantCulture) + "k"
                  : n.ToString(CultureInfo.InvariantCulture);

    private static double? Num(JsonElement e, string key) =>
        e.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetDouble() : null;

    private static string Str(JsonElement e, string key) =>
        e.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() ?? "" : "";
}

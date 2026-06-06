using System.Collections.ObjectModel;
using System.Globalization;
using System.Text.Json;
using System.Threading.Tasks;
using Avalonia.Media;
using Chili.Services;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace Chili.ViewModels;

/// <summary>One open position row in the trading cockpit.</summary>
public sealed class PositionRow : ViewModelBase
{
    public string Ticker { get; init; } = "";
    public string Name { get; init; } = "";
    public string EquityText { get; init; } = "";
    public string ChangeText { get; init; } = "";
    public string Broker { get; init; } = "";
    public bool Up { get; init; }
    public IBrush ChangeBrush => new SolidColorBrush(Color.Parse(Up ? "#35D08A" : "#FF6B5B"));
}

/// <summary>The Trading cockpit — live P/L, broker breakdown, governance/kill-switch,
/// risk heat, and open positions (read-only) from the CHILI backend.</summary>
public partial class TradingViewModel : ViewModelBase
{
    private readonly ChiliApiClient _api;

    public ObservableCollection<PositionRow> Positions { get; } = new();

    [ObservableProperty] private string _totalEquity = "—";
    [ObservableProperty] private string _buyingPower = "—";
    [ObservableProperty] private string _cash = "—";
    [ObservableProperty] private string _brokers = "";

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(KillBrush))]
    private bool _killActive;

    [ObservableProperty] private string _killText = "—";
    [ObservableProperty] private string _heatText = "—";
    [ObservableProperty] private string _openText = "—";
    [ObservableProperty] private bool _breakerTripped;
    [ObservableProperty] private string _status = "";

    public IBrush KillBrush =>
        new SolidColorBrush(Color.Parse(KillActive ? "#FF6B5B" : "#35D08A"));

    public TradingViewModel(ChiliApiClient? api = null)
    {
        _api = api ?? new ChiliApiClient(AppSettings.Load());
        _ = RefreshAsync();
    }

    [RelayCommand]
    private Task Refresh() => RefreshAsync();

    private async Task RefreshAsync()
    {
        Status = "Loading…";

        var portfolio = await _api.GetJsonAsync("/api/trading/broker/portfolio");
        if (portfolio is { } pf && pf.TryGetProperty("portfolio", out var p))
        {
            TotalEquity = Money(Num(p, "total_equity"));
            BuyingPower = Money(Num(p, "total_buying_power"));
            Cash = Money(Num(p, "total_cash"));
            if (p.TryGetProperty("brokers", out var b))
            {
                var rh = b.TryGetProperty("robinhood", out var r) ? Num(r, "equity") : null;
                var cb = b.TryGetProperty("coinbase", out var c) ? Num(c, "equity") : null;
                Brokers = $"Robinhood {Money(rh)}   ·   Coinbase {Money(cb)}";
            }
        }

        var gov = await _api.GetJsonAsync("/api/trading/brain/governance");
        if (gov is { } g && g.TryGetProperty("kill_switch", out var ks))
        {
            KillActive = ks.TryGetProperty("active", out var a) &&
                         a.ValueKind is JsonValueKind.True;
            KillText = KillActive ? "KILL SWITCH ACTIVE" : "Kill switch · safe";
        }

        var risk = await _api.GetJsonAsync("/api/trading/risk/budget");
        if (risk is { } rk)
        {
            HeatText = $"{Num(rk, "total_heat_pct") ?? 0:0.#}% heat";
            OpenText = $"{(int)(Num(rk, "open_positions") ?? 0)} open";
            BreakerTripped = rk.TryGetProperty("circuit_breaker", out var cbk) &&
                             cbk.TryGetProperty("tripped", out var t) &&
                             t.ValueKind is JsonValueKind.True;
        }

        var pos = await _api.GetJsonAsync("/api/trading/broker/positions");
        Positions.Clear();
        if (pos is { } px && px.TryGetProperty("positions", out var arr) &&
            arr.ValueKind == JsonValueKind.Array)
        {
            foreach (var it in arr.EnumerateArray())
            {
                var pct = Num(it, "percent_change") ?? 0;
                Positions.Add(new PositionRow
                {
                    Ticker = Str(it, "ticker"),
                    Name = Str(it, "name"),
                    EquityText = Money(Num(it, "equity")),
                    ChangeText = $"{(pct >= 0 ? "+" : "")}{pct:0.##}%",
                    Up = pct >= 0,
                    Broker = Str(it, "broker_source"),
                });
            }
        }

        Status = $"{Positions.Count} positions · updated";
    }

    private static double? Num(JsonElement e, string key) =>
        e.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.Number
            ? v.GetDouble() : null;

    private static string Str(JsonElement e, string key) =>
        e.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.String
            ? v.GetString() ?? "" : "";

    private static string Money(double? v) =>
        v is null ? "—" : "$" + v.Value.ToString("N2", CultureInfo.InvariantCulture);
}

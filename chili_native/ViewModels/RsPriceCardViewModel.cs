using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.IO;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using Chili.Services;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace Chili.ViewModels;

public enum CardPhase { Idle, Loading, Ok, Empty, Error }

/// <summary>
/// View model for the RuneScape price card — C# port of the Flutter
/// <c>RsItemOverlay</c> state machine (idle → loading → ok / empty / error),
/// with an out-of-order guard and best-effort wiki blurb + thumbnail.
/// </summary>
public partial class RsPriceCardViewModel : ViewModelBase
{
    private readonly RuneScapePrices _prices;
    private int _seq;

    public RsPriceCardViewModel(RuneScapePrices? prices = null)
    {
        _prices = prices ?? new RuneScapePrices();
        Recent.CollectionChanged += (_, _) => OnPropertyChanged(nameof(HasRecent));
    }

    /// <summary>Recently looked-up item names (most recent first), clickable to re-search.</summary>
    public ObservableCollection<string> Recent { get; } = new();
    public bool HasRecent => Recent.Count > 0;

    [ObservableProperty] private string _searchText = "";

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(IsIdle), nameof(IsLoading), nameof(IsResult), nameof(IsMessage))]
    private CardPhase _phase = CardPhase.Idle;

    public bool IsIdle => Phase == CardPhase.Idle;
    public bool IsLoading => Phase == CardPhase.Loading;
    public bool IsResult => Phase == CardPhase.Ok;
    public bool IsMessage => Phase is CardPhase.Empty or CardPhase.Error;

    [ObservableProperty] private string _messageText = "";
    [ObservableProperty] private string _itemName = "";
    [ObservableProperty] private string _priceText = "";
    [ObservableProperty] private string _volumeText = "";
    [ObservableProperty] private long _priceValue;

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(HasAsOf))]
    private string _asOfText = "";

    public bool HasAsOf => !string.IsNullOrEmpty(AsOfText);

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(HasBlurb))]
    private string _blurb = "";

    public bool HasBlurb => !string.IsNullOrWhiteSpace(Blurb);

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(HasThumb))]
    private Bitmap? _thumb;

    public bool HasThumb => Thumb != null;

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(HasSpark))]
    private Geometry? _spark;

    public bool HasSpark => Spark != null;

    [ObservableProperty] private string _changeText = "";

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(ChangeBrush))]
    private bool _changeUp;

    public IBrush ChangeBrush => new SolidColorBrush(Color.Parse(ChangeUp ? "#35D08A" : "#FF6B5B"));

    [ObservableProperty] private string _rangeText = "";

    [RelayCommand]
    private async Task SearchAsync()
    {
        var q = (SearchText ?? "").Trim();
        if (q.Length == 0) return;
        var seq = ++_seq;
        Phase = CardPhase.Loading;
        Thumb = null;
        Spark = null;
        ChangeText = "";
        RangeText = "";
        AsOfText = "";

        try
        {
            var p = await _prices.Lookup(q);
            if (seq != _seq) return;
            if (p == null)
            {
                MessageText = $"No GE price for “{q}”";
                Phase = CardPhase.Empty;
                return;
            }

            ItemName = p.Name;
            PriceValue = p.Price;
            PriceText = $"{RuneScapePrices.FormatGpFull(p.Price)} gp";
            VolumeText = $"Vol {RuneScapePrices.FormatGpFull(p.Volume)}/day";
            AsOfText = p.TimestampMs > 0
                ? $"GE update · {DateTimeOffset.FromUnixTimeMilliseconds(p.TimestampMs).ToLocalTime():MMM d, yyyy}"
                : "";
            Blurb = "";
            Phase = CardPhase.Ok;
            Remember(p.Name);

            // Enrich with the wiki blurb + thumbnail (best-effort).
            try
            {
                var info = await _prices.WikiInfo(p.Name);
                if (seq != _seq) return;
                Blurb = RuneScapePrices.BriefBlurb(info.Extract);
                if (!string.IsNullOrEmpty(info.ThumbUrl))
                {
                    var bytes = await _prices.FetchBytes(info.ThumbUrl!);
                    if (seq != _seq) return;
                    using var ms = new MemoryStream(bytes);
                    Thumb = new Bitmap(ms);
                }
            }
            catch
            {
                // price still shows without the blurb/image
            }

            // Enrich with a 90-day price sparkline + change (best-effort).
            try
            {
                var hist = await _prices.PriceHistory(p.Name);
                if (seq != _seq) return;
                if (hist.Count > 1)
                {
                    Spark = BuildSpark(hist, 312, 40);
                    long first = hist[0], last = hist[hist.Count - 1];
                    double pct = first == 0 ? 0 : (last - first) * 100.0 / first;
                    ChangeUp = last >= first;
                    ChangeText = $"{(pct >= 0 ? "+" : "")}{pct:0.#}%  ·  90d";

                    long lo = hist[0], hi = hist[0];
                    foreach (var v in hist) { if (v < lo) lo = v; if (v > hi) hi = v; }
                    RangeText = $"90d range  ·  {RuneScapePrices.FormatGp(lo)} – {RuneScapePrices.FormatGp(hi)} gp";
                }
            }
            catch
            {
                // price still shows without the trend
            }
        }
        catch
        {
            if (seq != _seq) return;
            MessageText = "Lookup failed — check your connection";
            Phase = CardPhase.Error;
        }
    }

    /// <summary>Re-run a search for a recent item name (chip click).</summary>
    [RelayCommand]
    private async Task SearchRecent(string? name)
    {
        if (string.IsNullOrWhiteSpace(name)) return;
        SearchText = name!;
        await SearchAsync();
    }

    /// <summary>Re-fetch the live price for the current item (refresh button).</summary>
    [RelayCommand]
    private async Task Refresh()
    {
        if (string.IsNullOrWhiteSpace(ItemName)) return;
        SearchText = ItemName;
        await SearchAsync();
    }

    /// <summary>Clear the recent-searches list.</summary>
    [RelayCommand]
    private void ClearRecent() => Recent.Clear();

    /// <summary>Record an item name at the front of the recent list (deduped, capped).</summary>
    private void Remember(string name)
    {
        Recent.Remove(name);
        Recent.Insert(0, name);
        while (Recent.Count > 6) Recent.RemoveAt(Recent.Count - 1);
    }

    /// <summary>Scale a price series into a polyline geometry within a w×h box.</summary>
    private static Geometry BuildSpark(IReadOnlyList<long> prices, double w, double h)
    {
        long min = prices[0], max = prices[0];
        foreach (var v in prices) { if (v < min) min = v; if (v > max) max = v; }
        double range = Math.Max(1, max - min);

        var geo = new StreamGeometry();
        using var ctx = geo.Open();
        for (int i = 0; i < prices.Count; i++)
        {
            double x = (double)i / (prices.Count - 1) * w;
            double y = h - (prices[i] - min) / (double)range * h;
            var pt = new Point(x, y);
            if (i == 0) ctx.BeginFigure(pt, false);
            else ctx.LineTo(pt);
        }
        ctx.EndFigure(false);
        return geo;
    }
}

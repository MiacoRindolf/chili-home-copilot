using System.IO;
using System.Threading.Tasks;
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
        => _prices = prices ?? new RuneScapePrices();

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

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(HasBlurb))]
    private string _blurb = "";

    public bool HasBlurb => !string.IsNullOrWhiteSpace(Blurb);

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(HasThumb))]
    private Bitmap? _thumb;

    public bool HasThumb => Thumb != null;

    [RelayCommand]
    private async Task SearchAsync()
    {
        var q = (SearchText ?? "").Trim();
        if (q.Length == 0) return;
        var seq = ++_seq;
        Phase = CardPhase.Loading;
        Thumb = null;

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
            PriceText = $"{RuneScapePrices.FormatGpFull(p.Price)} gp";
            VolumeText = $"Vol {RuneScapePrices.FormatGpFull(p.Volume)}/day";
            Blurb = "";
            Phase = CardPhase.Ok;

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
        }
        catch
        {
            if (seq != _seq) return;
            MessageText = "Lookup failed — check your connection";
            Phase = CardPhase.Error;
        }
    }
}

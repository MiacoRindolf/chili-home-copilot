namespace Chili.ViewModels;

public partial class MainWindowViewModel : ViewModelBase
{
    /// <summary>The RuneScape price card hosted in the workspace (NATIVE-2).</summary>
    public RsPriceCardViewModel PriceCard { get; } = new();
}

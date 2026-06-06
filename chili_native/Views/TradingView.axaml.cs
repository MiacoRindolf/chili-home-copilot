using Avalonia.Controls;
using Chili.ViewModels;

namespace Chili.Views;

public partial class TradingView : UserControl
{
    public TradingView()
    {
        InitializeComponent();
        AutoRefresh.Attach(this, () => (DataContext as TradingViewModel)?.RefreshCommand, 15);
    }
}

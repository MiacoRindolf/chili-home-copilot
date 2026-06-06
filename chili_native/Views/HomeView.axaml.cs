using Avalonia.Controls;
using Chili.ViewModels;

namespace Chili.Views;

public partial class HomeView : UserControl
{
    public HomeView()
    {
        InitializeComponent();
        AutoRefresh.Attach(this, () => (DataContext as HomeViewModel)?.RefreshCommand, 30);
    }
}

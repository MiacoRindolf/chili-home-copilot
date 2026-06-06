using Avalonia.Controls;
using Chili.ViewModels;

namespace Chili.Views;

public partial class BrainView : UserControl
{
    public BrainView()
    {
        InitializeComponent();
        AutoRefresh.Attach(this, () => (DataContext as BrainViewModel)?.RefreshCommand, 20);
    }
}

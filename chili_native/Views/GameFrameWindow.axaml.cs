using System;
using System.Runtime.InteropServices;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Threading;
using Chili.Interop;

namespace Chili.Views;

/// <summary>
/// The CHILI game frame — a transparent, always-on-top, hollow picture-frame
/// window placed around a *picked* target window: a CHILI titlebar on top and a
/// thin border around the sides. The center is physically hollow (SetWindowRgn),
/// so clicks pass straight through to the game underneath.
///
/// The frame DRIVES the target: drag the titlebar to move the frame and the
/// target is repositioned to match (via NativeWindows.MoveResize / SetWindowPos).
/// It NEVER reparents or injects — it only reads the target's bounds and moves it.
/// </summary>
public partial class GameFrameWindow : Window
{
    private const int TitleH = 34;  // DIP — matches the XAML title row
    private const int Border = 6;   // DIP — matches the XAML border thickness

    private IntPtr _target;
    private int _gw, _gh;           // target size in physical px (move-only; size fixed)
    private double _scale = 1.0;
    private int _bpx, _tpx, _wpx, _hpx;
    private DispatcherTimer? _timer;
    private bool _closing;

    [DllImport("gdi32.dll")] private static extern IntPtr CreateRectRgn(int l, int t, int r, int b);
    [DllImport("gdi32.dll")] private static extern int CombineRgn(IntPtr dst, IntPtr a, IntPtr b, int mode);
    [DllImport("gdi32.dll")] private static extern bool DeleteObject(IntPtr o);
    [DllImport("user32.dll")] private static extern int SetWindowRgn(IntPtr hWnd, IntPtr hRgn, bool redraw);
    private const int RGN_DIFF = 4;

    public GameFrameWindow()
    {
        InitializeComponent();
        var close = this.FindControl<Button>("CloseBtn");
        if (close != null) close.Click += (_, _) => CloseFrame();
        var bar = this.FindControl<Border>("TitleBar");
        if (bar != null)
            bar.PointerPressed += (_, e) =>
            {
                if (e.GetCurrentPoint(this).Properties.IsLeftButtonPressed)
                    BeginMoveDrag(e);
            };
    }

    /// <summary>Wrap the given (user-picked) window in a CHILI frame.</summary>
    public static void Attach(DesktopWindow target)
    {
        if (!NativeWindows.TryGetBounds(target.Handle, out var l, out var t, out var w, out var h))
            return;

        NativeWindows.BringToFront(target.Handle);

        var win = new GameFrameWindow { _target = target.Handle, _gw = w, _gh = h };
        var tt = win.FindControl<TextBlock>("TitleText");
        if (tt != null) tt.Text = "CHILI  ·  " + target.Title;

        win.Position = new PixelPoint(l - Border, t - TitleH);
        win.Opened += (_, _) => win.OnOpened(l, t, w, h);
        win.Show();
    }

    private void OnOpened(int gx, int gy, int gw, int gh)
    {
        _scale = RenderScaling <= 0 ? 1.0 : RenderScaling;
        _bpx = (int)Math.Round(Border * _scale);
        _tpx = (int)Math.Round(TitleH * _scale);
        _wpx = gw + 2 * _bpx;
        _hpx = gh + _tpx + _bpx;

        Width = _wpx / _scale;
        Height = _hpx / _scale;
        Position = new PixelPoint(gx - _bpx, gy - _tpx);

        ApplyHollowRegion();
        PositionChanged += (_, _) => DriveTarget();

        _timer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(200) };
        _timer.Tick += Tick;
        _timer.Start();
    }

    /// <summary>Carve out the center so clicks pass through to the game.</summary>
    private void ApplyHollowRegion()
    {
        var hwnd = TryGetPlatformHandle()?.Handle ?? IntPtr.Zero;
        if (hwnd == IntPtr.Zero) return;

        IntPtr outer = CreateRectRgn(0, 0, _wpx, _hpx);
        IntPtr hole = CreateRectRgn(_bpx, _tpx, _bpx + _gw, _tpx + _gh);
        CombineRgn(outer, outer, hole, RGN_DIFF);
        SetWindowRgn(hwnd, outer, true); // window owns `outer` now
        DeleteObject(hole);
    }

    /// <summary>Reposition the target to sit inside the frame's hole.</summary>
    private void DriveTarget()
    {
        if (_closing) return;
        int gx = Position.X + _bpx;
        int gy = Position.Y + _tpx;
        NativeWindows.MoveResize(_target, gx, gy, _gw, _gh);
    }

    private void Tick(object? sender, EventArgs e)
    {
        if (_closing) return;
        if (!NativeWindows.TryGetBounds(_target, out _, out _, out _, out _))
        {
            CloseFrame(); // the framed window is gone
            return;
        }
        ApplyHollowRegion(); // re-assert in case the compositor reset it
    }

    private void CloseFrame()
    {
        if (_closing) return;
        _closing = true;
        _timer?.Stop();
        Close();
    }
}
